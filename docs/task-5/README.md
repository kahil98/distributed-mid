# Task 5: React console and Elasticsearch metadata search

Task 5 adds a Vite + React console under `frontend/` and indexes object metadata in Elasticsearch through the authenticated FastAPI gateway. The UI supports sign-in, bucket creation, streamed file upload/download/delete, prefix/size/date/tag search, node health and fragment distribution, degraded-object alerts, and repair/scrub progress.

## Start the services

At the repository root in PowerShell:

```powershell
$env:DOCKER_BUILDKIT = '0'
docker build -f Dockerfile.offline -t distributed-mid-storage-task:local .
Remove-Item Env:DOCKER_BUILDKIT
docker compose -f compose.storage.yaml up -d --no-build --wait
docker compose -f compose.storage.yaml ps
```

This starts the gateway at `http://127.0.0.1:8080`, Elasticsearch at `http://127.0.0.1:9200`, PostgreSQL, metadata service, and storage nodes. Elasticsearch uses one local node with security disabled for the exam stack; do not expose this configuration to an untrusted network.

Start the React development server:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

Visit the URL Vite prints (normally `http://localhost:5173`) and sign in with the configured gateway credentials. Local defaults are `exam-user` / `change-me`.

## Search indexing

The gateway creates the `distributed-object-metadata` index with keyword bucket/key/owner fields, numeric size/version, a date field, and flattened user tags. Existing PostgreSQL rows are backfilled when the gateway starts; successful uploads update the indexed document, and deletes remove it. The object search endpoint uses Elasticsearch filters for bucket/owner, key prefix, size/date ranges, and exact tags.

## Verification

```powershell
cd frontend
npm run lint
npm run build
cd ..
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_gateway.py
```

The gateway verification covers JWT, bucket/object workflows, Elasticsearch indexing/search/delete, ranges, and cluster node/repair status. Results are saved as `reports/task-5-verification.json`. Test the React console in a browser by signing in, creating a bucket, uploading a file with tags, searching by prefix/tag/size/date, and trying download/delete. Production UI compilation is checked with `npm run build`.

## Limits

This is a single-node Elasticsearch development setup and the gateway stores its rate-limit counters in process memory. Metadata indexing runs synchronously after object writes; if indexing fails, the upload endpoint reports an error even though object bytes and PostgreSQL metadata have already been committed. Restarting the gateway performs a metadata backfill. Authentication uses the single configured exam account, not a multi-user identity provider.
