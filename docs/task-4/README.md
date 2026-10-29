# Task 4: Authenticated FastAPI gateway

The gateway gives users a JWT-protected API in front of the metadata/object service. It streams object uploads to the backend, supports one HTTP byte range for downloads, stores user tags and owner metadata in PostgreSQL, applies a per-client rate limit, and reports node/degraded-object status.

## Start and access

From PowerShell at the repository root:

```powershell
docker compose -f compose.storage.yaml up -d --build --wait
docker compose -f compose.storage.yaml ps
```

The gateway is at `http://127.0.0.1:8080`; interactive API docs are at `/docs`. Get a JWT (local Compose defaults shown):

```powershell
$token = (Invoke-RestMethod -Method Post http://127.0.0.1:8080/auth/token `
  -ContentType 'application/json' -Body '{"username":"exam-user","password":"change-me"}').access_token
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod http://127.0.0.1:8080/v1 -Headers $headers
```

Set `JWT_SECRET`, `GATEWAY_USERNAME`, and `GATEWAY_PASSWORD` in the environment before starting Compose for non-demo use. The Compose defaults are for local exam demonstrations. Rate limit defaults to 120 requests per minute per client IP and can be changed with `GATEWAY_RATE_LIMIT`. `/health`, Swagger docs, and OpenAPI JSON are excluded from the rate limit. Login attempts and all `/v1` endpoints are rate limited; `/v1` also requires a valid one-hour HS256 JWT.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/token` | Exchange configured username/password for JWT |
| GET | `/v1` | Basic authenticated API interface/index |
| POST / GET | `/v1/buckets` | Create a bucket / list buckets owned by caller |
| PUT | `/v1/buckets/{bucket}/objects/{key}` | Stream upload; optional `?mode=fixed` or `?mode=cdc`; tags in `X-Object-Tags: {"team":"exam"}` |
| GET | `/v1/buckets/{bucket}/objects` | Search by `prefix`, `min_size`, `max_size`, `created_after`, `created_before`, JSON `tags`, and `limit` |
| GET | `/v1/buckets/{bucket}/objects/{key}` | Object metadata |
| GET / HEAD | `/v1/download/{bucket}/{key}` | Stream download; supports single `Range: bytes=start-end` or suffix ranges |
| DELETE | `/v1/buckets/{bucket}/objects/{key}` | Delete an object |
| GET | `/v1/cluster/status` | Live nodes (heartbeat free-space/chunk count) and caller-owned degraded objects |

The gateway records the authenticated user as object owner; bucket and object access is scoped to that user. Uploads use an async request stream and downloads remain streamed through the gateway. Range requests return `206` and `Content-Range`; invalid ranges return `416`. Tags are JSON string-to-string maps.

## Run automated and live checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_gateway.py
```

The live check verifies missing/valid JWTs, bucket create/list, streamed upload, tags and search, metadata, range/full download, cluster status, and delete. Results are saved to `reports/task-4-verification.json`.

## Postman

Import [`postman/task-4-gateway.postman_collection.json`](../../postman/task-4-gateway.postman_collection.json) into Postman and run the collection in order. Set collection variables `baseUrl`, `username`, and `password` as needed; the login request saves the JWT into the `token` variable. Collection tests assert the HTTP responses and key response fields. The bucket creation test permits `409` on repeated runs; upload then replaces the same test key.

## Notes

Rate limiting is process-local and keyed by client IP, so multiple gateway replicas do not share counters. Authentication uses one configured credential pair for this exam service rather than a user directory/refresh-token flow. A single byte range is supported per request. Metadata still uses the existing backend implementation and PostgreSQL; large request bodies are streamed from gateway to backend, then temporarily spooled by the metadata service for chunking.
