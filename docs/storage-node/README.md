# Task: FastAPI storage nodes

## Requirements and scope

This task implements the storage-node module of the exam framework:

- PUT, GET, HEAD, and DELETE for chunks addressed by lowercase SHA-256.
- Local disk storage, with one independent persistent Docker volume per node.
- Verification of the uploaded hash and the stored hash on every GET and HEAD.
- Heartbeats every three seconds containing node ID, free bytes, chunk count,
  and the node's internal URL.
- Four nodes in four separate containers, plus a development heartbeat receiver.
- Automated tests and two synthetic file workloads against the running nodes.

Task 1 originally used `metadata_stub.py` as an in-memory heartbeat receiver.
Task 2 replaced the Compose registry with its PostgreSQL-backed metadata service;
the original stub remains for Task 1 unit tests. Placement metadata and object
manifests are implemented in Task 2. Replication, erasure coding, automatic repair,
and an S3-compatible gateway remain later framework modules. The Task 1 benchmark
performs splitting and reassembly in its client and does not test those later modules.

## Files added

| File | Purpose |
| --- | --- |
| `storage_node/app.py` | FastAPI service, disk operations, integrity verification, heartbeats |
| `metadata_stub.py` | Development heartbeat receiver and node list |
| `Dockerfile` | Python image running as an unprivileged user |
| `Dockerfile.offline` | Local fallback reusing installed Python dependencies |
| `compose.storage.yaml` | Four nodes, four volumes, registry, health checks |
| `requirements-node.txt` | Runtime dependencies |
| `requirements-dev.txt` | Test dependencies |
| `requirements-dev.lock.txt` | Exact installed development environment versions |
| `tests/test_storage_node.py` | Isolated automated service tests |
| `scripts/verify_containers.py` | Verification against the actual containers |
| `scripts/benchmark_storage.py` | Small/large workload generation and verification |
| `reports/` | Recorded verification and benchmark results |

## Setup steps

Run PowerShell in the project directory. Python, Git, Docker Desktop and Kubernetes
were verified in the environment setup task. Docker Desktop must use Linux containers.

```powershell
# Create the virtual environment only if it does not exist.
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker desktop start
docker compose -f compose.storage.yaml config --quiet
docker compose -f compose.storage.yaml build metadata
docker compose -f compose.storage.yaml up -d --wait
docker compose -f compose.storage.yaml ps
```

All five services share the same built image. This stack uses the separate Compose
project `distributed-mid-storage-task`, avoiding the pre-existing project and its
ports. Docker named volumes preserve chunks when containers restart or are recreated.

On this machine the initial BuildKit build encountered a broken cached filesystem
(`exec format error`, and a missing snapshot reported by `docker system df`). If
that occurs, the existing Docker installation also supports this build fallback:

```powershell
$env:DOCKER_BUILDKIT = '0'
docker build -t distributed-mid-storage-task:local .
Remove-Item Env:DOCKER_BUILDKIT
docker compose -f compose.storage.yaml up -d --no-build --wait
```

This uses the same Dockerfile and does not reset Docker or delete existing volumes.

The actual local build also encountered a package-download timeout. For verification
on this machine, an offline fallback copies Python dependencies (not application
code) from the pre-existing `distributed-mid-storage-node-1:latest` image into a
Python base image, then adds this task's new source code. `pip check` validates that
runtime. This fallback requires that cached image and is only for this machine;
the normal Dockerfile above remains the build path for a fresh environment.

```powershell
$env:DOCKER_BUILDKIT = '0'
docker build -f Dockerfile.offline -t distributed-mid-storage-task:local .
Remove-Item Env:DOCKER_BUILDKIT
docker compose -f compose.storage.yaml up -d --no-build --wait
```

| Service | Host URL | Internal URL |
| --- | --- | --- |
| Node 1 | http://127.0.0.1:8201 | http://node-1:8000 |
| Node 2 | http://127.0.0.1:8202 | http://node-2:8000 |
| Node 3 | http://127.0.0.1:8203 | http://node-3:8000 |
| Node 4 | http://127.0.0.1:8204 | http://node-4:8000 |
| Heartbeat registry | http://127.0.0.1:8100 | http://metadata:8000 |

Each node exposes interactive API documentation at `/docs`, and its statistics
at `/health`. View registry entries at http://127.0.0.1:8100/nodes.

## Chunk API

The request body is raw binary data, not multipart or JSON. The client calculates
the SHA-256 first, then uses `/chunks/{64-character-lowercase-sha256}`.

| Method | Success | Behavior |
| --- | --- | --- |
| PUT | 201 new, 200 replacement | Verify incoming hash; atomically install chunk |
| GET | 200 | Verify entire stored content before returning any bytes |
| HEAD | 200 | Verify content; return Content-Length and ETag with no body |
| DELETE | 204 | Remove chunk; also succeeds when already absent |

GET and HEAD include the quoted SHA-256 in `ETag`. Error responses use 400 for an
invalid ID, 404 for a missing chunk, 409 for corrupt stored content, 413 for a chunk
larger than the configured limit, 422 for an upload hash mismatch, 507 for a full
disk, and 503 for other disk failures. Corruption is rejected without sending
corrupt bytes. Re-uploading the correct content repairs a corrupt chunk.

Example using the installed Python environment:

```powershell
@'
import hashlib
import requests

data = b"distributed storage exam"
key = hashlib.sha256(data).hexdigest()
url = f"http://127.0.0.1:8201/chunks/{key}"
r = requests.put(url, data=data, timeout=10)
assert r.status_code in (200, 201)
r = requests.get(url, timeout=10)
r.raise_for_status()
assert r.content == data
assert hashlib.sha256(r.content).hexdigest() == key
r = requests.head(url, timeout=10)
assert r.status_code == 200
assert int(r.headers["Content-Length"]) == len(data)
assert requests.delete(url, timeout=10).status_code == 204
assert requests.get(url, timeout=10).status_code == 404
print("PUT / GET / HEAD / DELETE passed")
'@ | .\.venv\Scripts\python.exe -
```

## Disk layout and integrity

Chunks are stored as `/data/chunks/<first-two-hash-characters>/<full-hash>`.
Uploads stream into uniquely named files under `/data/staging`, compute SHA-256
incrementally, flush and fsync, then use an atomic rename on the same filesystem.
Invalid uploads never replace an existing chunk. Per-prefix asynchronous locks
serialize PUT/DELETE commits. Repeated uploads occupy one final chunk path.

GET/HEAD copy the file into a private temporary disk snapshot while hashing. A
mismatch produces HTTP 409 before a successful response begins. GET streams that
verified snapshot, protecting it from concurrent deletion, replacement, or later
modification of the original file. This requires temporary disk capacity and an
extra disk read/write, but bounds memory usage and avoids returning unverified data.
HEAD also reads and hashes the entire chunk, as required by the integrity rule.

The default maximum chunk size is 8 MiB. The benchmark splits large files into
4 MiB chunks. Large objects are therefore supported by splitting; sending an entire
100 MiB object as one chunk is intentionally rejected by the configured limit.

Each container runs one Uvicorn worker. Shutdown cancels the heartbeat task cleanly.
Failed heartbeats log a warning and retry; chunk operations remain available during
a metadata outage. Counts represent committed chunk files, including any corrupted
ones until repaired or deleted; counts are not a background integrity audit.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| NODE_ID | node-1 | Stable node identity, distinct for each container |
| DATA_DIR | data (Docker: /data) | Local persistent data directory |
| METADATA_URL | http://127.0.0.1:8100 | Receiver base URL; POST to `/heartbeats` |
| ADVERTISED_URL | http://127.0.0.1:8201 | Address advertised to the registry |
| HEARTBEAT_INTERVAL | 3 | Seconds between heartbeat attempts |
| MAX_CHUNK_BYTES | 8388608 | Maximum raw chunk size |

## Run verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q --junitxml=reports/unit-tests.xml
.\.venv\Scripts\python.exe scripts/verify_containers.py
.\.venv\Scripts\python.exe scripts/benchmark_storage.py
```

The container verification checks four distinct containers, all four HTTP methods,
upload mismatch rejection, disk persistence after restarting node 1, actual disk
tampering followed by GET/HEAD rejection, independent disks, repair by re-upload,
operation during a registry outage, heartbeat recovery, and heartbeat counts after
deletion. It restarts the development registry and uses its own test chunk.
Run it before workloads on this dedicated task stack; its final count assertion
expects empty node volumes.

The default workload uses 5,000 synthetic binary files of 1–50 KiB and 10 synthetic
files of 100 MiB each. Content is reproducible from fixed random seeds. Eight
workers run small files and four run large files, distributing chunks round-robin
across all four nodes. Each file is uploaded, read, reconstructed in order, and
checked for exact length and whole-file SHA-256, with every returned chunk checked
too. Test chunks are deleted afterward. No full file is held in memory.

`reports/workloads.json` records byte counts, chunk counts, elapsed time, throughput,
and median/p95 PUT and GET latency. Throughput includes data generation, uploads,
downloads, integrity checks, and cleanup; it is not a production capacity claim.

## Recorded results — October 28, 2026

- **30 automated tests passed** in 1.03 seconds; see `reports/unit-tests.xml`.
- **35 live-container checks passed**; see `reports/container-verification.json`.
- All four storage containers and the registry reported healthy after deployment.
- The running container's runtime versions were verified as FastAPI 0.115.12,
  Uvicorn 0.34.2, HTTPX 0.28.1, and Pydantic 2.14.1.
- `pip check`, Python compilation, and Compose configuration validation passed.
- Pytest emitted one dependency deprecation warning about Starlette's AnyIO portal
  alias; no test failed.

| Workload | Files | Payload bytes | Chunks | Elapsed including cleanup | Roundtrip throughput |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small, 1–50 KiB | 5,000 | 131,867,328 | 5,000 | 31.793 s | 7.911 MiB/s |
| Large, 100 MiB each | 10 | 1,048,576,000 | 250 | 36.007 s | 55.545 MiB/s |

| Workload | PUT median / p95 | GET median / p95 |
| --- | --- | --- |
| Small | 25.623 / 57.809 ms | 8.086 / 17.834 ms |
| Large (4 MiB chunks) | 342.340 / 461.401 ms | 146.050 / 251.388 ms |

Every file passed length and whole-file SHA-256 verification, and every downloaded
chunk passed SHA-256 verification. Chunks were retained until each workload finished,
then deleted. The nodes remain running for the exam demonstration. These measurements
cover the storage-node API on this local Docker Desktop machine, alongside other
running services; they do not measure the future complete distributed framework.

The implementation uses FastAPI's lifespan mechanism for background-task startup
and shutdown, and the request streaming interface for uploads. References:
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/),
[Starlette request streaming](https://www.starlette.io/requests/).

## Operate and troubleshoot

```powershell
docker compose -f compose.storage.yaml logs --tail 50 node-1 metadata
docker compose -f compose.storage.yaml restart node-1
docker compose -f compose.storage.yaml down
```

Stopping with `down` preserves the named volumes. Host ports bind only to loopback.
The chunk endpoints are unauthenticated local exam services; production deployment
would need access control, TLS, quotas, and additional durability planning. A crash
during an upload can leave a staging file, which is never counted or served as a
chunk. Named-volume persistence and atomic writes protect normal restarts; they do
not provide cross-node availability without the later replication/coding module.
