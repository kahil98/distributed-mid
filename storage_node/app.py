import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import errno
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

LOG = logging.getLogger(__name__)
HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Settings:
    node_id: str = "node-1"
    data_dir: Path = Path("data")
    metadata_url: str = "http://127.0.0.1:8100"
    advertised_url: str = "http://127.0.0.1:8201"
    heartbeat_interval: float = 3.0
    max_chunk_bytes: int = 8 * 1024 * 1024

    @classmethod
    def from_env(cls):
        return cls(
            node_id=os.getenv("NODE_ID", "node-1"),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            metadata_url=os.getenv("METADATA_URL", "http://127.0.0.1:8100"),
            advertised_url=os.getenv("ADVERTISED_URL", "http://127.0.0.1:8201"),
            heartbeat_interval=float(os.getenv("HEARTBEAT_INTERVAL", "3")),
            max_chunk_bytes=int(os.getenv("MAX_CHUNK_BYTES", str(8 * 1024 * 1024))),
        )


def chunk_path(root: Path, digest: str) -> Path:
    if not HASH.fullmatch(digest):
        raise HTTPException(400, "Chunk ID must be a lowercase SHA-256 hex digest")
    return root / digest[:2] / digest


def snapshot(path: Path, digest: str):
    """Verify a private disk snapshot before any response bytes are sent."""
    copied = tempfile.TemporaryFile()
    try:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                hasher.update(block)
                copied.write(block)
                size += len(block)
        if hasher.hexdigest() != digest:
            raise HTTPException(409, "Stored chunk is corrupted: SHA-256 mismatch")
        copied.seek(0)
        return copied, size
    except BaseException:
        copied.close()
        raise


def disk_error(exc: OSError):
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, "Chunk not found")
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return HTTPException(507, "Insufficient storage")
    LOG.error("Storage operation failed: %s", exc)
    return HTTPException(503, "Storage unavailable")


def create_app(settings=None, heartbeat_transport=None):
    settings = settings or Settings.from_env()
    if settings.heartbeat_interval <= 0 or settings.max_chunk_bytes <= 0:
        raise ValueError("Heartbeat interval and maximum chunk size must be positive")
    root = settings.data_dir / "chunks"
    staging = settings.data_dir / "staging"

    def stats():
        count = sum(1 for p in root.glob("*/*") if p.is_file() and HASH.fullmatch(p.name))
        return {
            "node_id": settings.node_id,
            "address": settings.advertised_url,
            "free_space": shutil.disk_usage(settings.data_dir).free,
            "chunk_count": count,
        }

    async def heartbeat():
        async with httpx.AsyncClient(timeout=2, transport=heartbeat_transport) as client:
            while True:
                try:
                    payload = await asyncio.to_thread(stats)
                    response = await client.post(
                        settings.metadata_url.rstrip("/") + "/heartbeats", json=payload
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, OSError) as exc:
                    LOG.warning("Heartbeat failed for %s; will retry: %s", settings.node_id, exc)
                await asyncio.sleep(settings.heartbeat_interval)

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        app.state.locks = [asyncio.Lock() for _ in range(256)]
        task = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Content-addressed storage node", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/health")
    async def health():
        return {"status": "ok", **await asyncio.to_thread(stats)}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        current = await asyncio.to_thread(stats)
        return Response(
            "# TYPE storage_node_free_bytes gauge\n"
            f"storage_node_free_bytes{{node_id=\"{settings.node_id}\"}} {current['free_space']}\n"
            "# TYPE storage_node_chunks gauge\n"
            f"storage_node_chunks{{node_id=\"{settings.node_id}\"}} {current['chunk_count']}\n",
            media_type="text/plain; version=0.0.4",
        )

    @app.put("/chunks/{digest}")
    async def put(digest: str, request: Request):
        path = chunk_path(root, digest)
        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length")
            if declared > settings.max_chunk_bytes:
                raise HTTPException(413, "Chunk exceeds MAX_CHUNK_BYTES")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=staging, delete=False) as output:
                temporary = Path(output.name)
                hasher = hashlib.sha256()
                size = 0
                async for block in request.stream():
                    size += len(block)
                    if size > settings.max_chunk_bytes:
                        raise HTTPException(413, "Chunk exceeds MAX_CHUNK_BYTES")
                    hasher.update(block)
                    await asyncio.to_thread(output.write, block)
                await asyncio.to_thread(output.flush)
                await asyncio.to_thread(os.fsync, output.fileno())
            if hasher.hexdigest() != digest:
                raise HTTPException(422, "Uploaded content does not match chunk SHA-256")
            async with app.state.locks[int(digest[:2], 16)]:
                existed = path.exists()
                path.parent.mkdir(parents=True, exist_ok=True)
                valid_existing = False
                if existed:
                    try:
                        def existing_digest():
                            with path.open("rb") as source:
                                return hashlib.file_digest(source, "sha256").hexdigest()
                        existing_hash = await asyncio.to_thread(existing_digest)
                        valid_existing = existing_hash == digest
                    except OSError:
                        valid_existing = False
                if not valid_existing:
                    await asyncio.to_thread(os.replace, temporary, path)
            return Response(status_code=200 if existed else 201, headers={"ETag": f'"{digest}"'})
        except OSError as exc:
            raise disk_error(exc) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @app.api_route("/chunks/{digest}", methods=["GET", "HEAD"])
    async def read(digest: str, request: Request):
        path = chunk_path(root, digest)
        try:
            verified, size = await asyncio.to_thread(snapshot, path, digest)
        except OSError as exc:
            raise disk_error(exc) from exc
        headers = {"ETag": f'"{digest}"', "Content-Length": str(size)}
        if request.method == "HEAD":
            verified.close()
            return Response(headers=headers, media_type="application/octet-stream")

        def blocks():
            try:
                while block := verified.read(1024 * 1024):
                    yield block
            finally:
                verified.close()

        return StreamingResponse(blocks(), headers=headers, media_type="application/octet-stream")

    @app.delete("/chunks/{digest}", status_code=204)
    async def delete(digest: str):
        path = chunk_path(root, digest)
        try:
            async with app.state.locks[int(digest[:2], 16)]:
                await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise disk_error(exc) from exc
        return Response(status_code=204)

    return app


app = create_app()
