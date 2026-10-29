# Task 2: Chunking, object metadata, deduplication, and placement

This extends the four storage nodes with fixed 4 MiB and rolling Buzhash content-defined chunking (16 KiB minimum, about 64 KiB average, 256 KiB maximum), PostgreSQL object manifests, chunk-level reference counts, and a SHA-256 consistent-hash ring with 128 virtual nodes per storage node.

## Run

Run PowerShell in the project folder:

~~~powershell
docker desktop start
$env:DOCKER_BUILDKIT = '0'
.\.venv\Scripts\python.exe -m pip download --only-binary=:all: --no-deps psycopg==3.2.9 -d build-wheels
.\.venv\Scripts\python.exe -m pip download --only-binary=:all: --no-deps --platform manylinux2014_x86_64 --python-version 311 --implementation cp --abi cp311 psycopg-binary==3.2.9 -d build-wheels
docker build -f Dockerfile.offline -t distributed-mid-storage-task:local .
Remove-Item Env:DOCKER_BUILDKIT
docker compose -f compose.storage.yaml up -d --no-build --wait
docker compose -f compose.storage.yaml ps
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_metadata.py
~~~

The portable build is Dockerfile. This machine's BuildKit cache is broken and its package downloads can time out; Dockerfile.offline reuses installed common dependencies from the Task 1 image. PostgreSQL listens at 127.0.0.1:5433; local credentials are distributed / distributed. The metadata API is at http://127.0.0.1:8100/docs.

## API

- PUT /objects/{bucket}/{key}?mode=fixed|cdc uploads a raw binary object.
- GET /objects/{bucket}/{key} returns its stored manifest and metadata.
- GET /download/{bucket}/{key} streams and reassembles verified chunks.
- DELETE /objects/{bucket}/{key} decrements references and deletes chunks with no remaining object references.
- GET /chunks/{sha256}/references inspects a chunk's owner and reference count.
- GET /placement/{sha256} maps a new chunk hash to a live node.
- POST /placement/measure simulates a node addition or removal and reports moved chunks and percentage.

Object metadata includes bucket, key, size, incrementing version, chunk mode, ordered chunk hashes and sizes, storage node IDs, and creation time. Upload bodies are spooled to temporary disk to support large files without buffering an entire object in memory.

Example placement request: POST /placement/measure with JSON containing nodes, chunk_hashes, and add_node (or remove_node). Adding one node to four should move about 20% of hashes; removing one should move hashes previously assigned to that node, about 25%.

## Verification and scope

tests/test_metadata_algorithms.py checks chunk reconstruction, stream-boundary independence, CDC resynchronization after a local insertion, ring determinism, and placement movement over 100,000 hashes. scripts/verify_metadata.py checks PostgreSQL heartbeats, manifests, deduplication, reference counts and physical chunk deletion, CDC upload/download, and node-add/remove movement over 50,000 hashes. It saves results to reports/task-2-verification.json.

## Recorded verification — October 28, 2026

- 39 automated tests passed; see reports/task-2-unit-tests.xml.
- 17 live metadata checks passed; see reports/task-2-verification.json.
- The existing storage-node regression suite passed 35 live checks; see reports/container-verification.json.
- Four nodes and PostgreSQL are running and healthy.
- On 50,000 synthetic chunk hashes and a 128-virtual-node ring, adding node-simulated-5 moved 10,801 chunks (21.602%). Removing node-1 moved 12,955 chunks (25.91%). Expected shares are approximately 20% and 25%.
- A shared 4 MiB chunk had reference count 2 after uploading two objects. Deleting one preserved the chunk at count 1; deleting the second removed it from disk.
- A 350,000-byte file reconstructed exactly using both chunking modes. Fixed chunking produced one chunk; CDC produced multiple chunks.

This CDC implementation uses a rolling 64-byte Buzhash; fixed chunking is the baseline comparison. Ring membership changes placement for new chunks. This task measures remapping but does not rebalance existing chunks or add replication, erasure coding, or node-failure repair. Placement changes are simulated by the measurement API without changing live membership.
