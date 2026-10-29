"""PostgreSQL object manifests, reference counts, node heartbeats and chunk placement."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import base64
from pathlib import Path
import tempfile
from collections import Counter

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .chunking import split_chunks
from .ring import ConsistentHashRing
from .reliability import decode as decode_shares, encode as encode_shares, fragment_digest, validate_policy
from .reliability import ReedSolomon

LOG = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://distributed:distributed@127.0.0.1:5432/distributed")
FIXED_SIZE = int(os.getenv("FIXED_CHUNK_SIZE", str(4 * 1024 * 1024)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_nodes (
 node_id TEXT PRIMARY KEY, address TEXT NOT NULL, free_space BIGINT NOT NULL CHECK(free_space >= 0),
 chunk_count BIGINT NOT NULL CHECK(chunk_count >= 0), last_seen TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 hash CHAR(64) PRIMARY KEY, size BIGINT NOT NULL CHECK(size >= 0),
 node_id TEXT NOT NULL, reference_count BIGINT NOT NULL CHECK(reference_count > 0)
);
CREATE TABLE IF NOT EXISTS objects (
 bucket TEXT NOT NULL, object_key TEXT NOT NULL, size BIGINT NOT NULL CHECK(size >= 0),
 version BIGINT NOT NULL CHECK(version > 0), chunking_mode TEXT NOT NULL,
 chunks JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
 tags JSONB NOT NULL DEFAULT '{}'::jsonb, owner TEXT NOT NULL DEFAULT 'anonymous',
 PRIMARY KEY(bucket, object_key)
);
CREATE TABLE IF NOT EXISTS bucket_policies (
 bucket TEXT PRIMARY KEY, policy JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_placements (
 hash CHAR(64) NOT NULL, policy_id TEXT NOT NULL, fragment_index INTEGER NOT NULL,
 fragment_hash CHAR(64) NOT NULL, node_id TEXT NOT NULL, size BIGINT NOT NULL,
 config JSONB NOT NULL, state TEXT NOT NULL DEFAULT 'healthy',
 PRIMARY KEY(hash,policy_id,fragment_index)
);
CREATE INDEX IF NOT EXISTS placements_by_node ON chunk_placements(node_id,state);
CREATE TABLE IF NOT EXISTS repair_jobs (
 id BIGSERIAL PRIMARY KEY, hash CHAR(64) NOT NULL, policy_id TEXT NOT NULL,
 fragment_index INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 traffic_bytes BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS reliability_stats (
 name TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0
);
ALTER TABLE storage_nodes ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'healthy';
ALTER TABLE storage_nodes ADD COLUMN IF NOT EXISTS manual_fail BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'anonymous';
CREATE TABLE IF NOT EXISTS gateway_buckets (
 bucket TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5)


def initialize():
    with connect() as conn:
        conn.execute(SCHEMA)


def live_nodes():
    with connect() as conn:
        rows = conn.execute(
            "SELECT node_id,address,free_space,chunk_count,last_seen FROM storage_nodes "
            "WHERE last_seen > now() - interval '12 seconds' AND state='healthy' ORDER BY node_id"
        ).fetchall()
    return rows


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(initialize)
    tasks = [asyncio.create_task(reliability_loop(10)), asyncio.create_task(scrubber_loop(30))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Distributed object metadata and placement", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(503, "PostgreSQL unavailable") from exc
    return {"status": "ok", "database": "ok"}


@app.post("/heartbeats")
async def heartbeat(payload: dict):
    node_id, address = payload.get("node_id"), payload.get("address")
    try:
        free, count = int(payload["free_space"]), int(payload["chunk_count"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "Heartbeat requires free_space and chunk_count")
    if not node_id or not address or free < 0 or count < 0:
        raise HTTPException(422, "Invalid node heartbeat")
    with connect() as conn:
        previous = conn.execute("SELECT state FROM storage_nodes WHERE node_id=%s", (node_id,)).fetchone()
        should_rebalance = previous is None or previous["state"] != "healthy"
        conn.execute(
            "INSERT INTO storage_nodes(node_id,address,free_space,chunk_count,last_seen,state) "
            "VALUES (%s,%s,%s,%s,now(),'healthy') "
            "ON CONFLICT(node_id) DO UPDATE SET address=excluded.address,free_space=excluded.free_space,"
            "chunk_count=excluded.chunk_count,last_seen=now(),"
            "state=CASE WHEN storage_nodes.manual_fail THEN 'failed' ELSE 'healthy' END",
            (node_id, address, free, count),
        )
    queued = False
    if should_rebalance:
        try:
            await enqueue_job("rebalance")
            queued = True
        except Exception:
            LOG.exception("Could not queue rebalance after node %s joined", node_id)
    return {"accepted": True, "rebalance_queued": queued}


@app.get("/nodes")
def nodes():
    return live_nodes()


DEFAULT_POLICY = validate_policy({})


@app.get("/buckets/{bucket}/policy")
def get_bucket_policy(bucket: str):
    with connect() as conn:
        row = conn.execute("SELECT policy FROM bucket_policies WHERE bucket=%s", (bucket,)).fetchone()
    return row["policy"] if row else DEFAULT_POLICY


@app.put("/buckets/{bucket}/policy")
def put_bucket_policy(bucket: str, payload: dict):
    try:
        policy = validate_policy(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with connect() as conn:
        live_count = conn.execute(
            "SELECT count(*) AS n FROM storage_nodes WHERE state='healthy' "
            "AND last_seen > now()-interval '12 seconds'"
        ).fetchone()["n"]
        if policy["n"] > live_count:
            raise HTTPException(422, f"Policy needs {policy['n']} distinct live nodes; only {live_count} are available")
        conn.execute(
            "INSERT INTO bucket_policies VALUES (%s,%s,now()) ON CONFLICT(bucket) DO UPDATE "
            "SET policy=excluded.policy,updated_at=now()", (bucket, json.dumps(policy))
        )
    return {"bucket": bucket, **policy}


def policy_id(policy):
    return ":".join(str(policy[name]) for name in ("mode", "n", "w", "r", "k", "m"))


def select_nodes(ring, addresses, key, count, excluded=()):
    members = [node for node in ring.nodes if node in addresses and node not in set(excluded)]
    if len(members) < count:
        raise HTTPException(503, f"Policy needs {count} distinct nodes, only {len(members)} are available")
    start = ring.get_node(key)
    if start in members:
        offset = members.index(start)
        members = members[offset:] + members[:offset]
    return members[:count]


def ring_and_addresses():
    members = live_nodes()
    ring = ConsistentHashRing((n["node_id"] for n in members), virtual_nodes=128)
    return ring, {n["node_id"]: n["address"] for n in members}


@app.get("/placement/{digest}")
def placement(digest: str):
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise HTTPException(400, "Chunk ID must be a lowercase SHA-256 digest")
    ring, _ = ring_and_addresses()
    try:
        return {"chunk_hash": digest, "node_id": ring.get_node(digest), "virtual_nodes": ring.virtual_nodes}
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/placement/measure")
def measure_placement(payload: dict):
    nodes_before = payload.get("nodes")
    hashes = payload.get("chunk_hashes")
    add = payload.get("add_node")
    remove = payload.get("remove_node")
    if (not isinstance(nodes_before, list) or not nodes_before or
        not isinstance(hashes, list) or not hashes or (bool(add) == bool(remove))):
        raise HTTPException(400, "Provide nodes, chunk_hashes, and exactly one add_node/remove_node")
    if (any(not isinstance(n, str) or not n for n in nodes_before) or
        any(not isinstance(h, str) or not h for h in hashes) or
        (add is not None and not isinstance(add, str)) or
        (remove is not None and not isinstance(remove, str))):
        raise HTTPException(400, "Node IDs and chunk hashes must be nonempty strings")
    before_ring = ConsistentHashRing(nodes_before, 128)
    after_nodes = set(nodes_before)
    if add:
        after_nodes.add(add)
    else:
        after_nodes.discard(remove)
    if not after_nodes:
        raise HTTPException(400, "Cannot remove the last ring member")
    after_ring = ConsistentHashRing(after_nodes, 128)
    moved = sum(before_ring.get_node(h) != after_ring.get_node(h) for h in hashes)
    expected = 100 / (len(after_nodes) if add else len(set(nodes_before)))
    return {"before_nodes": sorted(set(nodes_before)), "after_nodes": sorted(after_nodes),
            "chunks": len(hashes), "moved_chunks": moved,
            "moved_percent": round(100 * moved / len(hashes), 4),
            "expected_approx_percent": round(expected, 4),
            "virtual_nodes": 128}


@app.put("/objects/{bucket}/{key:path}")
async def put_object(bucket: str, key: str, request: Request, mode: str = "fixed"):
    if not bucket or not key or len(bucket) > 255 or len(key) > 1024:
        raise HTTPException(400, "Bucket and key are required and must fit supported lengths")
    if mode not in {"fixed", "cdc"}:
        raise HTTPException(400, "mode must be fixed or cdc")
    ring, addresses = await asyncio.to_thread(ring_and_addresses)
    if not addresses:
        raise HTTPException(503, "No live storage nodes")
    policy = await asyncio.to_thread(get_bucket_policy, bucket)
    if policy["n"] > len(addresses):
        raise HTTPException(503, f"Bucket policy requires {policy['n']} live nodes")
    pid = policy_id(policy)
    temp = None
    size = 0
    manifest = []
    try:
        tags = json.loads(request.headers.get("x-object-tags", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "x-object-tags must be a JSON object") from exc
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tags.items()):
        raise HTTPException(400, "Object tags must be string key/value pairs")
    owner = request.headers.get("x-object-owner", "anonymous")[:255]
    try:
        with tempfile.NamedTemporaryFile(prefix="object-", delete=False) as stream:
            temp = Path(stream.name)
            async for block in request.stream():
                size += len(block)
                await asyncio.to_thread(stream.write, block)
        def blocks():
            with temp.open("rb") as f:
                while block := f.read(1024 * 1024):
                    yield block

        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            for data in split_chunks(blocks(), mode, fixed_size=FIXED_SIZE):
                digest = hashlib.sha256(data).hexdigest()
                fragments, padlen = encode_shares(data, policy)
                targets = select_nodes(ring, addresses, digest + pid, policy["n"])
                stored = []
                planned = []
                for index, (fragment, node_id) in enumerate(zip(fragments, targets)):
                    shard_hash = fragment_digest(fragment)
                    try:
                        response = await client.put(f"{addresses[node_id].rstrip('/')}/chunks/{shard_hash}",
                                                    content=fragment)
                        succeeded = response.status_code in (200, 201)
                    except httpx.HTTPError:
                        succeeded = False
                    planned.append({"fragment_index": index, "fragment_hash": shard_hash,
                                    "node_id": node_id, "size": len(fragment),
                                    "state": "healthy" if succeeded else "failed"})
                    if succeeded:
                        stored.append({"fragment_index": index, "fragment_hash": shard_hash,
                                       "node_id": node_id, "size": len(fragment)})
                if len(stored) < policy["w"]:
                    raise HTTPException(503, f"Write quorum not reached: stored {len(stored)}, need W={policy['w']}")
                item_node = stored[0]["node_id"]
                manifest.append({"hash": digest, "size": len(data), "policy_id": pid,
                                 "policy": policy, "padlen": padlen, "fragments": stored,
                                 "_planned_fragments": planned, "node_id": item_node})

        old_chunks = []
        doomed = []
        now = datetime.now(timezone.utc)
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (bucket + "\x1f" + key,))
            old = conn.execute("SELECT version,chunks FROM objects WHERE bucket=%s AND object_key=%s FOR UPDATE",
                               (bucket, key)).fetchone()
            version = old["version"] + 1 if old else 1
            if old:
                old_chunks = old["chunks"]
            old_counts = Counter(item["hash"] for item in old_chunks)
            new_counts = Counter(item["hash"] for item in manifest)
            manifest_nodes = {}
            item_info = {item["hash"]: item for item in manifest}
            for digest in old_counts.keys() | new_counts.keys():
                row = conn.execute(
                    "SELECT reference_count,node_id FROM chunks WHERE hash=%s FOR UPDATE", (digest,)
                ).fetchone()
                if row:
                    count = row["reference_count"] + new_counts[digest] - old_counts[digest]
                    node_id = row["node_id"]
                    if count > 0:
                        conn.execute("UPDATE chunks SET reference_count=%s WHERE hash=%s", (count, digest))
                        manifest_nodes[digest] = node_id
                    else:
                        conn.execute("DELETE FROM chunks WHERE hash=%s", (digest,))
                        old_placements = conn.execute(
                            "SELECT fragment_hash,node_id FROM chunk_placements WHERE hash=%s", (digest,)
                        ).fetchall()
                        doomed.extend((row["fragment_hash"].strip(), row["node_id"]) for row in old_placements)
                        conn.execute("DELETE FROM chunk_placements WHERE hash=%s", (digest,))
                elif new_counts[digest]:
                    item = item_info[digest]
                    saved = conn.execute(
                        "INSERT INTO chunks(hash,size,node_id,reference_count) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT(hash) DO UPDATE SET reference_count=chunks.reference_count+excluded.reference_count "
                        "RETURNING node_id",
                        (digest, item["size"], item["node_id"], new_counts[digest]),
                    ).fetchone()
                    manifest_nodes[digest] = saved["node_id"]
            for item in manifest:
                item["node_id"] = item["fragments"][0]["node_id"]
                for fragment in item["_planned_fragments"]:
                    conn.execute(
                        "INSERT INTO chunk_placements(hash,policy_id,fragment_index,fragment_hash,node_id,size,config,state) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(hash,policy_id,fragment_index) "
                        "DO UPDATE SET fragment_hash=excluded.fragment_hash,node_id=excluded.node_id,"
                        "size=excluded.size,config=excluded.config,state=excluded.state",
                        (item["hash"], item["policy_id"], fragment["fragment_index"], fragment["fragment_hash"],
                         fragment["node_id"], fragment["size"],
                         json.dumps({**policy, "padlen": item["padlen"]}), fragment["state"]),
                    )
                    if fragment["state"] == "failed":
                        conn.execute(
                            "INSERT INTO repair_jobs(hash,policy_id,fragment_index,reason) "
                            "SELECT %s,%s,%s,'write_degraded' WHERE NOT EXISTS "
                            "(SELECT 1 FROM repair_jobs WHERE hash=%s AND policy_id=%s AND fragment_index=%s "
                            "AND status IN ('pending','running'))",
                            (item["hash"], item["policy_id"], fragment["fragment_index"],
                             item["hash"], item["policy_id"], fragment["fragment_index"]),
                        )
            conn.execute(
                "INSERT INTO objects(bucket,object_key,size,version,chunking_mode,chunks,created_at,tags,owner) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(bucket,object_key) DO UPDATE SET "
                "size=excluded.size,version=excluded.version,chunking_mode=excluded.chunking_mode,"
                "chunks=excluded.chunks,created_at=excluded.created_at,tags=excluded.tags,owner=excluded.owner",
                (bucket, key, size, version, mode, json.dumps(manifest), now, json.dumps(tags), owner),
            )
        await delete_physical_chunks(doomed, addresses)
        for item in manifest:
            item.pop("_planned_fragments", None)
        return {"bucket": bucket, "key": key, "size": size, "version": version,
                "chunking_mode": mode, "policy": policy, "chunks": manifest, "created_at": now.isoformat()}
    except psycopg.Error as exc:
        raise HTTPException(503, "Metadata database operation failed") from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


async def delete_physical_chunks(doomed, addresses):
    async with httpx.AsyncClient(timeout=15) as client:
        for digest, node_id in doomed:
            address = addresses.get(node_id)
            if address:
                try:
                    response = await client.delete(f"{address.rstrip('/')}/chunks/{digest}")
                    if response.status_code not in (204, 404):
                        LOG.error("Could not delete unreferenced chunk %s: HTTP %s", digest, response.status_code)
                except httpx.HTTPError:
                    LOG.exception("Could not delete unreferenced chunk %s from %s", digest, node_id)


def _known_chunk_nodes():
    with connect() as conn:
        return {r["hash"].strip(): r["node_id"] for r in conn.execute("SELECT hash,node_id FROM chunks")}


@app.get("/objects/{bucket}/{key:path}")
def get_object(bucket: str, key: str):
    with connect() as conn:
        obj = conn.execute("SELECT * FROM objects WHERE bucket=%s AND object_key=%s", (bucket, key)).fetchone()
    if not obj:
        raise HTTPException(404, "Object not found")
    obj["created_at"] = obj["created_at"].isoformat()
    return obj


@app.delete("/objects/{bucket}/{key:path}", status_code=204)
async def delete_object(bucket: str, key: str):
    ring, addresses = await asyncio.to_thread(ring_and_addresses)
    del ring
    try:
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (bucket + "\x1f" + key,))
            obj = conn.execute("SELECT chunks FROM objects WHERE bucket=%s AND object_key=%s FOR UPDATE",
                               (bucket, key)).fetchone()
            if not obj:
                return Response(status_code=204)
            items = obj["chunks"]
            counts = Counter(item["hash"] for item in items)
            doomed = []
            for digest, decrement in counts.items():
                row = conn.execute("SELECT reference_count,node_id FROM chunks WHERE hash=%s FOR UPDATE",
                                   (digest,)).fetchone()
                if not row:
                    continue
                if row["reference_count"] <= decrement:
                    conn.execute("DELETE FROM chunks WHERE hash=%s", (digest,))
                    old_placements = conn.execute(
                        "SELECT fragment_hash,node_id FROM chunk_placements WHERE hash=%s", (digest,)
                    ).fetchall()
                    doomed.extend((item["fragment_hash"].strip(), item["node_id"]) for item in old_placements)
                    conn.execute("DELETE FROM chunk_placements WHERE hash=%s", (digest,))
                else:
                    conn.execute("UPDATE chunks SET reference_count=reference_count-%s WHERE hash=%s",
                                 (decrement, digest))
            conn.execute("DELETE FROM objects WHERE bucket=%s AND object_key=%s", (bucket, key))
        await delete_physical_chunks(doomed, addresses)
    except psycopg.Error as exc:
        raise HTTPException(503, "Metadata database operation failed") from exc
    return Response(status_code=204)


@app.get("/chunks/{digest}/references")
def chunk_references(digest: str):
    with connect() as conn:
        row = conn.execute("SELECT hash,size,node_id,reference_count FROM chunks WHERE hash=%s", (digest,)).fetchone()
    if not row:
        raise HTTPException(404, "Chunk metadata not found")
    return row


@app.get("/chunks/{digest}/placements")
def chunk_placements(digest: str):
    with connect() as conn:
        rows = conn.execute(
            "SELECT policy_id,fragment_index,fragment_hash,node_id,size,state "
            "FROM chunk_placements WHERE hash=%s ORDER BY policy_id,fragment_index", (digest,)
        ).fetchall()
    return {"hash": digest, "fragments": rows}


@app.get("/download/{bucket}/{key:path}")
async def download_object(bucket: str, key: str):
    obj = await asyncio.to_thread(get_object, bucket, key)
    ring, addresses = await asyncio.to_thread(ring_and_addresses)
    del ring

    async def content():
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            for item in obj["chunks"]:
                data = await fetch_logical_chunk(client, item, addresses)
                yield data
    return StreamingResponse(content(), media_type="application/octet-stream",
                             headers={"Content-Length": str(obj["size"])})


async def fetch_logical_chunk(client, item, addresses):
    policy = item["policy"]
    with connect() as conn:
        placements = conn.execute(
            "SELECT fragment_index,fragment_hash,node_id FROM chunk_placements "
            "WHERE hash=%s AND policy_id=%s AND state='healthy' ORDER BY fragment_index",
            (item["hash"], item["policy_id"]),
        ).fetchall()
    fragments, indices = [], []
    for placement in placements:
        address = addresses.get(placement["node_id"])
        if not address:
            continue
        try:
            response = await client.get(f"{address.rstrip('/')}/chunks/{placement['fragment_hash'].strip()}")
            if response.status_code == 200 and fragment_digest(response.content) == placement["fragment_hash"].strip():
                fragments.append(response.content)
                indices.append(placement["fragment_index"])
                if len(fragments) >= policy["r"]:
                    break
        except httpx.HTTPError:
            continue
    if len(fragments) < policy["r"]:
        raise HTTPException(503, f"Read quorum not reached: found {len(fragments)}, need R={policy['r']}")
    try:
        data = decode_shares(fragments, indices, policy, item["padlen"])
    except (ValueError, IndexError) as exc:
        raise HTTPException(503, "Not enough valid fragments to reconstruct the chunk") from exc
    if hashlib.sha256(data).hexdigest() != item["hash"]:
        raise HTTPException(502, "Reconstructed chunk failed SHA-256 verification")
    return data


def mark_expired_nodes():
    with connect() as conn:
        expired = conn.execute(
            "SELECT node_id FROM storage_nodes WHERE state='healthy' "
            "AND last_seen < now()-interval '12 seconds'"
        ).fetchall()
        for node in expired:
            node_id = node["node_id"]
            conn.execute("UPDATE storage_nodes SET state='failed' WHERE node_id=%s", (node_id,))
            losses = conn.execute(
                "UPDATE chunk_placements SET state='failed' WHERE node_id=%s AND state='healthy' "
                "RETURNING hash,policy_id,fragment_index", (node_id,)
            ).fetchall()
            for loss in losses:
                conn.execute(
                    "INSERT INTO repair_jobs(hash,policy_id,fragment_index,reason) "
                    "SELECT %s,%s,%s,'node_failed' WHERE NOT EXISTS "
                    "(SELECT 1 FROM repair_jobs WHERE hash=%s AND policy_id=%s AND fragment_index=%s "
                    "AND status IN ('pending','running'))",
                    (loss["hash"], loss["policy_id"], loss["fragment_index"],
                     loss["hash"], loss["policy_id"], loss["fragment_index"]),
                )


async def reliability_loop(interval):
    while True:
        try:
            await asyncio.to_thread(mark_expired_nodes)
            with connect() as conn:
                pending = conn.execute("SELECT count(*) AS n FROM repair_jobs WHERE status='pending'").fetchone()["n"]
            if pending:
                await enqueue_job("repair")
        except Exception:
            LOG.exception("Reliability controller iteration failed")
        await asyncio.sleep(interval)


async def scrubber_loop(interval):
    while True:
        await asyncio.sleep(interval)
        try:
            await scrub_once()
        except Exception:
            LOG.exception("Scrubber iteration failed")


async def scrub_once():
    _, addresses = await asyncio.to_thread(ring_and_addresses)
    with connect() as conn:
        rows = conn.execute(
            "SELECT hash,policy_id,fragment_index,fragment_hash,node_id,size FROM chunk_placements "
            "WHERE state='healthy' ORDER BY hash,policy_id,fragment_index"
        ).fetchall()
    corrupt = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for row in rows:
            address = addresses.get(row["node_id"])
            if not address:
                continue
            try:
                response = await client.get(f"{address.rstrip('/')}/chunks/{row['fragment_hash'].strip()}")
                valid = response.status_code == 200 and fragment_digest(response.content) == row["fragment_hash"].strip()
            except httpx.HTTPError:
                valid = False
            if not valid:
                with connect() as conn:
                    conn.execute(
                        "UPDATE chunk_placements SET state='corrupt' WHERE hash=%s AND policy_id=%s "
                        "AND fragment_index=%s",
                        (row["hash"], row["policy_id"], row["fragment_index"]),
                    )
                    conn.execute(
                        "INSERT INTO repair_jobs(hash,policy_id,fragment_index,reason) "
                        "SELECT %s,%s,%s,'scrub_corruption' WHERE NOT EXISTS "
                        "(SELECT 1 FROM repair_jobs WHERE hash=%s AND policy_id=%s AND fragment_index=%s "
                        "AND status IN ('pending','running'))",
                        (row["hash"], row["policy_id"], row["fragment_index"],
                         row["hash"], row["policy_id"], row["fragment_index"]),
                    )
                corrupt += 1
            with connect() as conn:
                conn.execute(
                    "INSERT INTO reliability_stats(name,value) VALUES ('scrub_bytes',%s) "
                    "ON CONFLICT(name) DO UPDATE SET value=reliability_stats.value+excluded.value",
                    (len(response.content) if 'response' in locals() and valid else 0,),
                )
    return {"checked": len(rows), "corrupt": corrupt}


async def repair_pending(limit=100):
    with connect() as conn:
        jobs = conn.execute(
            "SELECT id,hash,policy_id,fragment_index,reason FROM repair_jobs "
            "WHERE status='pending' ORDER BY id LIMIT %s", (limit,)
        ).fetchall()
    completed = 0
    for job in jobs:
        with connect() as conn:
            conn.execute("UPDATE repair_jobs SET status='running' WHERE id=%s", (job["id"],))
        try:
            traffic = await repair_fragment(job)
            with connect() as conn:
                conn.execute("UPDATE repair_jobs SET status='complete',traffic_bytes=%s,completed_at=now() WHERE id=%s",
                             (traffic, job["id"]))
                conn.execute(
                    "INSERT INTO reliability_stats(name,value) VALUES ('repair_bytes',%s) "
                    "ON CONFLICT(name) DO UPDATE SET value=reliability_stats.value+excluded.value",
                    (traffic,),
                )
                conn.execute(
                    "INSERT INTO reliability_stats(name,value) VALUES ('repair_count',1) "
                    "ON CONFLICT(name) DO UPDATE SET value=reliability_stats.value+1"
                )
            completed += 1
        except Exception:
            LOG.exception("Repair failed for job %s; will retry", job["id"])
            with connect() as conn:
                conn.execute("UPDATE repair_jobs SET status='pending' WHERE id=%s", (job["id"],))
    return completed


async def repair_fragment(job):
    _, addresses = await asyncio.to_thread(ring_and_addresses)
    with connect() as conn:
        target = conn.execute(
            "SELECT * FROM chunk_placements WHERE hash=%s AND policy_id=%s AND fragment_index=%s",
            (job["hash"], job["policy_id"], job["fragment_index"]),
        ).fetchone()
        siblings = conn.execute(
            "SELECT * FROM chunk_placements WHERE hash=%s AND policy_id=%s AND state='healthy' "
            "AND fragment_index<>%s ORDER BY fragment_index",
            (job["hash"], job["policy_id"], job["fragment_index"]),
        ).fetchall()
    if not target:
        return 0
    policy = target["config"]
    _, all_addresses = await asyncio.to_thread(ring_and_addresses)
    sources = [s for s in siblings if s["node_id"] in all_addresses]
    needed = policy["k"] if policy["mode"] == "erasure" else 1
    if len(sources) < needed:
        raise RuntimeError(f"Repair needs {needed} healthy source fragments; only {len(sources)} remain")
    shards, indexes, traffic = [], [], 0
    async with httpx.AsyncClient(timeout=60) as client:
        for source in sources:
            address = all_addresses[source["node_id"]]
            response = await client.get(f"{address.rstrip('/')}/chunks/{source['fragment_hash'].strip()}")
            if response.status_code == 200 and fragment_digest(response.content) == source["fragment_hash"].strip():
                shards.append(response.content)
                indexes.append(source["fragment_index"])
                traffic += len(response.content)
                if len(shards) >= needed:
                    break
        if len(shards) < needed:
            raise RuntimeError("Not enough valid fragments remain for repair")
        logical = decode_shares(shards, indexes, policy, policy.get("padlen", 0))
        if hashlib.sha256(logical).hexdigest() != job["hash"]:
            raise RuntimeError("Repair sources do not reconstruct the requested logical chunk")
        encoded, _ = encode_shares(logical, policy)
        fragment = encoded[job["fragment_index"]]
        new_hash = fragment_digest(fragment)
        ring, _ = await asyncio.to_thread(ring_and_addresses)
        used = {s["node_id"] for s in siblings if s["node_id"] in all_addresses}
        used.add(target["node_id"])
        destination = select_nodes(ring, all_addresses, job["hash"] + job["policy_id"] + str(job["fragment_index"]),
                                  1, used)[0]
        response = await client.put(f"{all_addresses[destination].rstrip('/')}/chunks/{new_hash}", content=fragment)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Replacement node rejected fragment with HTTP {response.status_code}")
    with connect() as conn:
        conn.execute(
            "UPDATE chunk_placements SET fragment_hash=%s,node_id=%s,size=%s,state='healthy' "
            "WHERE hash=%s AND policy_id=%s AND fragment_index=%s",
            (new_hash, destination, len(fragment), job["hash"], job["policy_id"], job["fragment_index"]),
        )
    return traffic + len(fragment)


@app.post("/admin/scrub/run")
async def run_scrub():
    return await scrub_once()


@app.post("/admin/repair/run")
async def run_repair():
    return {"repaired": await repair_pending()}


RABBIT_API = os.getenv("RABBITMQ_MANAGEMENT_URL", "http://host.docker.internal:15672").rstrip("/")
RABBIT_USER = os.getenv("RABBITMQ_USER", "dev")
RABBIT_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "dev")
JOB_QUEUES = {"repair": "repair_jobs", "rebalance": "rebalancing_jobs", "gc": "garbage_collection_jobs"}


async def enqueue_job(job_type):
    queue = JOB_QUEUES[job_type]
    auth = (RABBIT_USER, RABBIT_PASSWORD)
    async with httpx.AsyncClient(timeout=5, auth=auth) as client:
        await client.put(f"{RABBIT_API}/api/queues/%2F/{queue}", json={"durable": True})
        response = await client.post(f"{RABBIT_API}/api/exchanges/%2F/amq.default/publish", json={
            "properties": {"delivery_mode": 2}, "routing_key": queue,
            "payload": json.dumps({"type": job_type}), "payload_encoding": "string"})
        response.raise_for_status()
        if not response.json().get("routed"):
            raise RuntimeError(f"RabbitMQ did not route {job_type} job")
    return {"queued": True, "type": job_type, "queue": queue}


@app.post("/admin/jobs/{job_type}")
async def queue_job(job_type: str):
    if job_type not in JOB_QUEUES:
        raise HTTPException(422, "job_type must be repair, rebalance, or gc")
    try:
        return await enqueue_job(job_type)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(503, f"Could not queue job: {exc}") from exc


@app.post("/admin/rebalance/run")
async def run_rebalance():
    ring, addresses = await asyncio.to_thread(ring_and_addresses)
    queued = 0
    with connect() as conn:
        placements = conn.execute("SELECT hash,policy_id,fragment_index,node_id FROM chunk_placements "
                                  "WHERE state='healthy' ORDER BY hash,policy_id,fragment_index").fetchall()
        for row in placements:
            expected = select_nodes(ring, addresses, row["hash"] + row["policy_id"],
                                    len([p for p in placements if p["hash"] == row["hash"] and p["policy_id"] == row["policy_id"]]))
            if row["fragment_index"] < len(expected) and row["node_id"] != expected[row["fragment_index"]]:
                conn.execute("UPDATE chunk_placements SET state='failed' WHERE hash=%s AND policy_id=%s AND fragment_index=%s",
                             (row["hash"], row["policy_id"], row["fragment_index"]))
                conn.execute("INSERT INTO repair_jobs(hash,policy_id,fragment_index,reason) VALUES (%s,%s,%s,'rebalance') "
                             "ON CONFLICT DO NOTHING", (row["hash"], row["policy_id"], row["fragment_index"]))
                queued += 1
    return {"misplaced_fragments": queued, "repaired": await repair_pending()}


@app.post("/admin/gc/run")
async def run_garbage_collection():
    with connect() as conn:
        orphaned = conn.execute("DELETE FROM chunk_placements p WHERE NOT EXISTS "
                                "(SELECT 1 FROM chunks c WHERE c.hash=p.hash) RETURNING fragment_hash,node_id").fetchall()
        old_jobs = conn.execute("DELETE FROM repair_jobs WHERE status='complete' "
                                "AND completed_at < now()-interval '7 days'").rowcount
        addresses = {row["node_id"]: row["address"] for row in conn.execute("SELECT node_id,address FROM storage_nodes")}
    removed = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for item in orphaned:
            address = addresses.get(item["node_id"])
            if address:
                try:
                    response = await client.delete(f"{address.rstrip('/')}/chunks/{item['fragment_hash'].strip()}")
                    removed += response.status_code in (204, 404)
                except httpx.HTTPError:
                    pass
    return {"orphan_fragments_removed": removed, "expired_jobs_removed": old_jobs}


@app.get("/metrics", include_in_schema=False)
def metrics():
    with connect() as conn:
        node_rows = conn.execute("SELECT node_id,free_space,chunk_count FROM storage_nodes").fetchall()
        job_rows = conn.execute("SELECT status,count(*) AS n FROM repair_jobs GROUP BY status").fetchall()
        counters = {row["name"]: row["value"] for row in conn.execute(
            "SELECT name,value FROM reliability_stats WHERE name IN ('repair_bytes','repair_count')")}
        degraded = conn.execute("SELECT count(*) AS n FROM objects o WHERE EXISTS "
                                 "(SELECT 1 FROM jsonb_array_elements(o.chunks) c WHERE "
                                 "(SELECT count(*) FROM chunk_placements p WHERE p.hash=c->>'hash' "
                                 "AND p.policy_id=c->>'policy_id' AND p.state='healthy') < "
                                 "COALESCE((c->'policy'->>'n')::int,3))").fetchone()["n"]
    lines = ["# TYPE storage_degraded_objects gauge", f"storage_degraded_objects {degraded}",
             "# TYPE storage_repair_jobs gauge", "# TYPE storage_repairs_total counter",
             f"storage_repairs_total {counters.get('repair_count', 0)}",
             "# TYPE storage_repair_bytes_total counter",
             f"storage_repair_bytes_total {counters.get('repair_bytes', 0)}"]
    lines.extend(f'storage_repair_jobs{{status="{row["status"]}"}} {row["n"]}' for row in job_rows)
    lines.extend(["# TYPE storage_node_free_bytes gauge", "# TYPE storage_node_chunks gauge"])
    for row in node_rows:
        node = row["node_id"].replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'storage_node_free_bytes{{node_id="{node}"}} {row["free_space"]}')
        lines.append(f'storage_node_chunks{{node_id="{node}"}} {row["chunk_count"]}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.post("/admin/nodes/{node_id}/fail")
def simulate_node_failure(node_id: str):
    with connect() as conn:
        result = conn.execute("UPDATE storage_nodes SET state='failed',manual_fail=true WHERE node_id=%s RETURNING node_id",
                              (node_id,)).fetchone()
        if not result:
            raise HTTPException(404, "Node not found")
        losses = conn.execute(
            "UPDATE chunk_placements SET state='failed' WHERE node_id=%s AND state='healthy' "
            "RETURNING hash,policy_id,fragment_index", (node_id,)
        ).fetchall()
        for loss in losses:
            conn.execute(
                "INSERT INTO repair_jobs(hash,policy_id,fragment_index,reason) VALUES (%s,%s,%s,'simulated_failure')",
                (loss["hash"], loss["policy_id"], loss["fragment_index"]),
            )
    return {"node_id": node_id, "failed_fragments": len(losses)}


@app.post("/admin/nodes/{node_id}/recover")
def recover_node(node_id: str):
    with connect() as conn:
        conn.execute("UPDATE storage_nodes SET state='healthy',manual_fail=false,last_seen=now() WHERE node_id=%s",
                     (node_id,))
    return {"node_id": node_id, "state": "healthy"}


@app.get("/reliability/stats")
def reliability_stats():
    with connect() as conn:
        rows = conn.execute(
            "SELECT policy_id,count(*) AS fragments,sum(size) AS physical_bytes "
            "FROM chunk_placements WHERE state='healthy' GROUP BY policy_id ORDER BY policy_id"
        ).fetchall()
        counters = {row["name"]: row["value"] for row in conn.execute("SELECT name,value FROM reliability_stats")}
        jobs = conn.execute("SELECT status,count(*) AS count FROM repair_jobs GROUP BY status").fetchall()
        repair_by_policy = conn.execute(
            "SELECT policy_id,sum(traffic_bytes) AS traffic_bytes FROM repair_jobs "
            "WHERE status='complete' GROUP BY policy_id"
        ).fetchall()
    return {"fragment_storage": rows, "repair_traffic_bytes": counters.get("repair_bytes", 0),
            "repair_traffic_by_policy": repair_by_policy,
            "scrub_read_bytes": counters.get("scrub_bytes", 0), "repair_jobs": jobs}
