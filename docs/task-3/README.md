# Task 3: Replication, erasure coding, repair, and scrubbing

This task extends the object store with per-bucket durability policies, quorum checks, replica/fragment placement, failure repair, and scrub repair. Metadata and placements are stored in PostgreSQL; fragment bytes stay on storage-node disks.

## Start the stack

From the repository root in PowerShell:

```powershell
$env:DOCKER_BUILDKIT = '0'
docker build -f Dockerfile.offline -t distributed-mid-storage-task:local .
Remove-Item Env:DOCKER_BUILDKIT
docker compose -f compose.storage.yaml up -d --no-build --wait
```

The storage stack exposes metadata at `http://127.0.0.1:8100`, nodes on ports 8201–8207 (six are sufficient for 4+2 plus one repair spare), and PostgreSQL on port 5433. Check status with `docker compose -f compose.storage.yaml ps`.

## Configure bucket durability

Default policy is replication `N=3, W=2, R=2`. `W + R > N` is required. Set policy before uploading:

```powershell
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8100/buckets/demo/policy `
  -ContentType 'application/json' -Body '{"mode":"replication","n":3,"w":2,"r":2}'
```

For Reed-Solomon 4+2, six live nodes are required:

```powershell
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8100/buckets/archive/policy `
  -ContentType 'application/json' -Body '{"mode":"erasure","k":4,"m":2,"w":4,"r":4}'
```

Upload an object with `PUT /objects/{bucket}/{key}` and retrieve bytes with `GET /download/{bucket}/{key}`. `GET /objects/{bucket}/{key}` returns object metadata. The API docs are at `/docs`.

## Failure and bitrot checks

Mark a node failed, run queued placement repairs, then recover it:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8100/admin/nodes/node-1/fail
Invoke-RestMethod -Method Post http://127.0.0.1:8100/admin/repair/run
Invoke-RestMethod -Method Post http://127.0.0.1:8100/admin/nodes/node-1/recover
```

Nodes missing heartbeats are marked failed by the metadata service. Scrubbing can be triggered with `POST /admin/scrub/run`; it checks fragment hashes and queues repairs for missing or corrupted fragments. Repairs read healthy copies/shares and write to a replacement node. `GET /reliability/stats` reports scrub and repair traffic.

## Run verification

The short live verifier checks quorum rejection, replication repair, 4+2 recovery after a missing shard, and compares bounded samples (12 small files and two 64–96 KiB large-file stand-ins per policy):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_reliability.py
```

The JSON result is written to `reports/task-3-verification.json`. Samples are deliberately small because the pure-Python Reed-Solomon encoder is CPU intensive; this is a placement/overhead check, not the full 5,000-file and 10 × 100 MiB–1 GiB benchmark. Repair traffic figures in the report are estimates derived from the coding layout, while live recovery/scrub byte counters are measured by the service.

Latest bounded sample results:

| Sample | Replication 3x overhead | Erasure 4+2 overhead | Estimated repair traffic (replication / erasure) |
| --- | ---: | ---: | ---: |
| 12 small files, about 49 KB total | 3.00x | 1.50x | 33,266 / 62,375 bytes |
| 2 large-file stand-ins, 160 KB total | 3.00x | 1.50x | 109,226 / 204,800 bytes |

Erasure coding halves stored bytes in these samples, but a single-fragment repair reads four data shares and writes a replacement, so its estimated repair traffic is higher than replica repair. These measurements are from the small quick-check sample and do not predict full-size throughput.

## Implementation note

Erasure coding is a systematic Reed-Solomon implementation over GF(2^8), supporting up to 32 fragments. The service uses this in-repository implementation rather than zfec/pyeclib because a compatible Linux wheel was not available in the offline build cache. The 4+2 mode is exercised against every possible four-of-six fragment subset by unit tests.
