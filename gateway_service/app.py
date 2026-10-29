"""JWT-authenticated, rate-limited user gateway for the object metadata service."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_app(*, initialize_db=True, secret=None, username=None, password=None, rate_limit=None):
    secret = (secret or os.getenv("JWT_SECRET", "local-exam-only-change-this-secret")).encode()
    username = username or os.getenv("GATEWAY_USERNAME", "exam-user")
    password = password or os.getenv("GATEWAY_PASSWORD", "change-me")
    rate_limit = int(rate_limit or os.getenv("GATEWAY_RATE_LIMIT", "120"))
    metadata_url = os.getenv("METADATA_URL", "http://127.0.0.1:8100").rstrip("/")
    database_url = os.getenv("DATABASE_URL", "postgresql://distributed:distributed@127.0.0.1:5433/distributed")
    elasticsearch_url = os.getenv("ELASTICSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")
    search_index = "distributed-object-metadata"
    requests_by_client = defaultdict(deque)
    lock = asyncio.Lock()
    metric_counts = defaultdict(int)
    metric_latency_sum = 0.0
    metric_latency_count = 0

    def connect():
        return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5)

    def initialize():
        with connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS gateway_buckets ("
                         "bucket TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.execute("ALTER TABLE objects ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{}'::jsonb")
            conn.execute("ALTER TABLE objects ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'anonymous'")

    @asynccontextmanager
    async def lifespan(app):
        if initialize_db:
            await asyncio.to_thread(initialize)
            await initialize_search()
        yield

    app = FastAPI(title="Distributed Object Storage Gateway", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def collect_request_metrics(request: Request, call_next):
        nonlocal metric_latency_sum, metric_latency_count
        started = time.perf_counter()
        try:
            response = await call_next(request)
            return response
        finally:
            metric_counts[(request.method, request.url.path)] += 1
            metric_latency_sum += time.perf_counter() - started
            metric_latency_count += 1

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        lines = ["# TYPE gateway_requests_total counter"]
        for (method, path), count in sorted(metric_counts.items()):
            safe_path = path.replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'gateway_requests_total{{method="{method}",path="{safe_path}"}} {count}')
        lines.extend(["# TYPE gateway_request_duration_seconds summary",
                      f"gateway_request_duration_seconds_count {metric_latency_count}",
                      f"gateway_request_duration_seconds_sum {metric_latency_sum}"])
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-Object-Tags", "Range"])

    def document_id(owner, bucket, key):
        return hashlib.sha256(f"{owner}\x1f{bucket}\x1f{key}".encode()).hexdigest()

    async def initialize_search():
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{elasticsearch_url}/{search_index}")
            if response.status_code == 404:
                created = await client.put(f"{elasticsearch_url}/{search_index}", json={"mappings": {"properties": {
                    "bucket": {"type": "keyword"}, "key": {"type": "keyword"},
                    "owner": {"type": "keyword"}, "size": {"type": "long"},
                    "version": {"type": "long"}, "created_at": {"type": "date"},
                    "tags": {"type": "flattened"}}}})
                created.raise_for_status()
            else:
                response.raise_for_status()
        offset = 0
        while True:
            with connect() as conn:
                rows = conn.execute("SELECT bucket,object_key,size,version,created_at,tags,owner FROM objects "
                                    "ORDER BY bucket,object_key OFFSET %s LIMIT 500", (offset,)).fetchall()
            if not rows:
                break
            lines = []
            for row in rows:
                doc = {**row, "key": row.pop("object_key"), "created_at": row["created_at"].isoformat()}
                lines.extend([json.dumps({"index": {"_id": document_id(doc["owner"], doc["bucket"], doc["key"])}}, separators=(",", ":")),
                              json.dumps(doc, separators=(",", ":"))])
            async with httpx.AsyncClient(timeout=60) as client:
                result = await client.post(f"{elasticsearch_url}/{search_index}/_bulk?refresh=true",
                                           content="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"})
                result.raise_for_status()
                if result.json().get("errors"):
                    raise RuntimeError("Elasticsearch bulk indexing failed during metadata backfill")
            offset += len(rows)

    async def index_object(doc):
        identifier = document_id(doc["owner"], doc["bucket"], doc["key"])
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.put(f"{elasticsearch_url}/{search_index}/_doc/{identifier}?refresh=wait_for", json=doc)
            response.raise_for_status()

    async def delete_search_doc(owner, bucket, key):
        identifier = document_id(owner, bucket, key)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f"{elasticsearch_url}/{search_index}/_doc/{identifier}?refresh=wait_for")
            if response.status_code not in (200, 404):
                response.raise_for_status()

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path == "/health" or request.url.path.startswith("/docs") or request.url.path == "/openapi.json":
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        async with lock:
            hits = requests_by_client[client]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= rate_limit:
                return Response(status_code=429, content='{"detail":"Rate limit exceeded"}',
                                media_type="application/json", headers={"Retry-After": "60"})
            hits.append(now)
        return await call_next(request)

    def issue_token(subject: str):
        now = datetime.now(timezone.utc)
        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64(json.dumps({"sub": subject, "iat": int(now.timestamp()),
                                   "exp": int((now + timedelta(hours=1)).timestamp())}, separators=(",", ":")).encode())
        signing = f"{header}.{payload}"
        signature = _b64(hmac.new(secret, signing.encode(), hashlib.sha256).digest())
        return f"{signing}.{signature}"

    def current_user(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization[7:]
        try:
            head, payload, signature = token.split(".")
            expected = _b64(hmac.new(secret, f"{head}.{payload}".encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            claims = json.loads(_unb64(payload))
            if claims.get("exp", 0) <= time.time() or not claims.get("sub"):
                raise ValueError("expired")
            if json.loads(_unb64(head)).get("alg") != "HS256":
                raise ValueError("algorithm")
            return str(claims["sub"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(401, "Invalid or expired bearer token", headers={"WWW-Authenticate": "Bearer"}) from exc

    async def ensure_bucket(bucket: str, owner: str):
        exists = await asyncio.to_thread(_bucket_owner, bucket)
        if exists != owner:
            raise HTTPException(404, "Bucket not found")

    def _bucket_owner(bucket):
        with connect() as conn:
            row = conn.execute("SELECT owner FROM gateway_buckets WHERE bucket=%s", (bucket,)).fetchone()
        return row["owner"] if row else None

    async def upstream(method, path, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                response = await client.request(method, metadata_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Object metadata service is unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "Upstream request failed")
            except (ValueError, AttributeError):
                detail = "Upstream request failed"
            raise HTTPException(response.status_code, detail)
        return response

    @app.get("/health")
    async def health():
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(metadata_url + "/health")
            status = "ok" if response.status_code == 200 else "degraded"
        except httpx.HTTPError:
            status = "degraded"
        return {"status": status, "service": "gateway"}

    @app.post("/auth/token")
    async def token(payload: dict):
        if not hmac.compare_digest(str(payload.get("username", "")), username) or not hmac.compare_digest(str(payload.get("password", "")), password):
            raise HTTPException(401, "Invalid username or password")
        return {"access_token": issue_token(username), "token_type": "bearer", "expires_in": 3600}

    @app.get("/v1")
    async def api_index(user=Depends(current_user)):
        return {"service": "Distributed Object Storage Gateway", "user": user,
                "endpoints": ["/v1/buckets", "/v1/buckets/{bucket}/objects", "/v1/cluster/status"]}

    @app.post("/v1/buckets", status_code=201)
    async def create_bucket(payload: dict, user=Depends(current_user)):
        bucket = payload.get("bucket")
        if not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", bucket):
            raise HTTPException(422, "bucket must be 1-63 safe characters")
        try:
            with connect() as conn:
                conn.execute("INSERT INTO gateway_buckets(bucket,owner) VALUES(%s,%s)", (bucket, user))
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, "Bucket already exists") from exc
        return {"bucket": bucket, "owner": user}

    @app.get("/v1/buckets")
    async def list_buckets(user=Depends(current_user)):
        with connect() as conn:
            rows = conn.execute("SELECT bucket,created_at FROM gateway_buckets WHERE owner=%s ORDER BY bucket", (user,)).fetchall()
        return [{"bucket": r["bucket"], "created_at": r["created_at"].isoformat()} for r in rows]

    @app.put("/v1/buckets/{bucket}/objects/{key:path}", openapi_extra={
        "requestBody": {"required": True, "content": {
            "text/plain": {"schema": {"type": "string", "example": "Hello from Swagger UI"}},
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
        }}
    })
    async def upload_object(bucket: str, key: str, request: Request, mode: str = "fixed",
                            x_object_tags: str = Header(default="{}"), user=Depends(current_user)):
        await ensure_bucket(bucket, user)
        try:
            tags = json.loads(x_object_tags)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "X-Object-Tags must be a JSON string map") from exc
        if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tags.items()):
            raise HTTPException(422, "X-Object-Tags must be a JSON string map")
        headers = {"content-type": request.headers.get("content-type", "application/octet-stream"),
                   "x-object-tags": json.dumps(tags), "x-object-owner": user}
        response = await upstream("PUT", f"/objects/{bucket}/{key}", params={"mode": mode}, headers=headers, content=request.stream())
        result = response.json()
        try:
            await index_object({"bucket": bucket, "key": key, "owner": user, "size": result["size"],
                                "version": result["version"], "created_at": result["created_at"], "tags": tags})
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logging.getLogger(__name__).exception("Elasticsearch indexing failed after object upload")
            raise HTTPException(503, "Object stored, but metadata indexing failed; retry the upload") from exc
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    @app.get("/v1/buckets/{bucket}/objects")
    async def search_objects(bucket: str, prefix: str = "", min_size: int | None = Query(default=None, ge=0),
                             max_size: int | None = Query(default=None, ge=0),
                             created_after: datetime | None = None, created_before: datetime | None = None,
                             tags: str | None = None, limit: int = Query(default=100, ge=1, le=1000),
                             offset: int = Query(default=0, ge=0),
                             user=Depends(current_user)):
        await ensure_bucket(bucket, user)
        filters = [{"term": {"bucket": bucket}}, {"term": {"owner": user}}]
        if prefix: filters.append({"prefix": {"key": prefix}})
        size_range = {}
        if min_size is not None: size_range["gte"] = min_size
        if max_size is not None: size_range["lte"] = max_size
        if size_range: filters.append({"range": {"size": size_range}})
        date_range = {}
        if created_after is not None: date_range["gte"] = created_after.isoformat()
        if created_before is not None: date_range["lte"] = created_before.isoformat()
        if date_range: filters.append({"range": {"created_at": date_range}})
        if tags is not None:
            try: tag_filter = json.loads(tags)
            except json.JSONDecodeError as exc: raise HTTPException(422, "tags must be a JSON object") from exc
            if not isinstance(tag_filter, dict): raise HTTPException(422, "tags must be a JSON object")
            for tag, value in tag_filter.items():
                if not isinstance(tag, str) or not isinstance(value, str):
                    raise HTTPException(422, "tags must be a JSON string map")
                filters.append({"term": {f"tags.{tag}": value}})
        body = {"query": {"bool": {"filter": filters}}, "from": offset, "size": limit,
                "sort": [{"created_at": {"order": "desc"}}, {"key": {"order": "asc"}}]}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{elasticsearch_url}/{search_index}/_search", json=body)
        if response.status_code >= 400:
            raise HTTPException(503, "Elasticsearch metadata search is unavailable")
        hits = response.json()["hits"]
        objects = [hit["_source"] for hit in hits["hits"]]
        return {"objects": objects, "count": len(objects), "total": hits["total"]["value"], "offset": offset}

    @app.get("/v1/buckets/{bucket}/objects/{key:path}")
    async def object_metadata(bucket: str, key: str, user=Depends(current_user)):
        await ensure_bucket(bucket, user)
        with connect() as conn:
            owned = conn.execute("SELECT 1 FROM objects WHERE bucket=%s AND object_key=%s AND owner=%s", (bucket, key, user)).fetchone()
        if not owned: raise HTTPException(404, "Object not found")
        response = await upstream("GET", f"/objects/{bucket}/{key}")
        return response.json()

    @app.delete("/v1/buckets/{bucket}/objects/{key:path}", status_code=204)
    async def delete_object(bucket: str, key: str, user=Depends(current_user)):
        await ensure_bucket(bucket, user)
        with connect() as conn:
            owned = conn.execute("SELECT 1 FROM objects WHERE bucket=%s AND object_key=%s AND owner=%s", (bucket, key, user)).fetchone()
        if not owned: raise HTTPException(404, "Object not found")
        await upstream("DELETE", f"/objects/{bucket}/{key}")
        await delete_search_doc(user, bucket, key)
        return Response(status_code=204)

    @app.api_route("/v1/download/{bucket}/{key:path}", methods=["GET", "HEAD"])
    async def download_object(bucket: str, key: str, request: Request,
                              range_header: str | None = Header(default=None, alias="Range"),
                              user=Depends(current_user)):
        await ensure_bucket(bucket, user)
        with connect() as conn:
            obj = conn.execute("SELECT size FROM objects WHERE bucket=%s AND object_key=%s AND owner=%s", (bucket, key, user)).fetchone()
        if not obj: raise HTTPException(404, "Object not found")
        size = obj["size"]
        byte_range = range_header
        start, end, status = 0, max(0, size - 1), 200
        headers = {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range.strip())
            if not match or size == 0: raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start = int(first); end = int(last) if last else size - 1
            elif last:
                length = int(last); start = max(0, size - length); end = size - 1
            if start >= size or end < start: raise HTTPException(416, "Unsatisfiable byte range", headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1); status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        if request.method == "HEAD": return Response(status_code=status, headers=headers)

        async def body():
            remaining_skip, remaining = start, length
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                async with client.stream("GET", f"{metadata_url}/download/{bucket}/{key}") as response:
                    if response.status_code != 200: return
                    async for chunk in response.aiter_bytes():
                        if remaining_skip:
                            skipped = min(remaining_skip, len(chunk)); remaining_skip -= skipped; chunk = chunk[skipped:]
                        if chunk and remaining:
                            out = chunk[:remaining]; remaining -= len(out); yield out
                        if remaining <= 0: break
        return StreamingResponse(body(), status_code=status, headers=headers)

    @app.get("/v1/cluster/status")
    async def cluster_status(user=Depends(current_user)):
        with connect() as conn:
            node_rows = conn.execute(
                "SELECT n.node_id,n.address,n.free_space,n.chunk_count,n.last_seen,"
                "CASE WHEN n.last_seen < now()-interval '12 seconds' THEN 'failed' ELSE n.state END AS health,"
                "COALESCE(p.fragment_count,0) AS managed_fragment_count,"
                "COALESCE(p.physical_bytes,0) AS managed_fragment_bytes FROM storage_nodes n "
                "LEFT JOIN (SELECT node_id,count(*) AS fragment_count,sum(size) AS physical_bytes "
                "FROM chunk_placements WHERE state='healthy' GROUP BY node_id) p USING(node_id) ORDER BY n.node_id"
            ).fetchall()
            rows = conn.execute(
                "SELECT o.bucket,o.object_key,o.size,o.chunks FROM objects o "
                "WHERE o.owner=%s AND EXISTS (SELECT 1 FROM jsonb_array_elements(o.chunks) c "
                "WHERE (SELECT count(*) FROM chunk_placements p WHERE p.hash=c->>'hash' "
                "AND p.policy_id=c->>'policy_id' AND p.state='healthy') < "
                "COALESCE((c->'policy'->>'n')::int,3))", (user,)
            ).fetchall()
        for node in node_rows: node["last_seen"] = node["last_seen"].isoformat()
        degraded = [{"bucket": r["bucket"], "key": r["object_key"], "size": r["size"]} for r in rows]
        stats = (await upstream("GET", "/reliability/stats")).json()
        return {"nodes": node_rows, "node_count": len(node_rows), "degraded_objects": degraded,
                "degraded_count": len(degraded), "repair_progress": stats["repair_jobs"],
                "repair_traffic_bytes": stats["repair_traffic_bytes"], "scrub_read_bytes": stats["scrub_read_bytes"]}

    return app


app = create_app()
