# Task 6: gateway scaling, jobs, Kubernetes, and monitoring

## What is included

- `compose.storage.yaml` runs two FastAPI gateway replicas behind Nginx at `http://127.0.0.1:8080`.
- RabbitMQ management/AMQP is used for three durable queues (`repair_jobs`, `rebalancing_jobs`, `garbage_collection_jobs`); workers process repair, rebalance, and garbage-collection requests. The metadata reliability loop queues repair work when failed chunks need recovery.
- `deploy/kubernetes/storage-nodes.yaml` defines a four-replica StatefulSet, headless service, per-pod 10 GiB PVCs, and HTTP liveness/readiness probes. Apply it after building/loading `distributed-mid-storage-task:local` into the Kubernetes cluster and ensuring a `metadata` service exists.
- Gateway request count/latency, per-node free disk/chunk count, degraded-object count, cumulative repair count, and repair bytes are exposed as Prometheus metrics. Prometheus scrapes host-published gateway, metadata, and storage ports. Grafana provisions a dashboard at startup.
- The current metadata database remains the single PostgreSQL container in Compose with a persistent volume. It is not replicated/HA; PostgreSQL HA needs a separate replication/consensus deployment and is an explicit remaining item.

## Start and check

From the repository root in PowerShell:

```powershell
docker compose -f compose.storage.yaml up -d --build --scale gateway=2 --scale job-worker=2
docker compose -f compose.storage.yaml ps
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-RestMethod http://127.0.0.1:8100/nodes
```

Open Prometheus at `http://127.0.0.1:9090` and Grafana at `http://127.0.0.1:3000` (credentials are in `.env`). Prometheus queries include `gateway_requests_total`, `gateway_request_duration_seconds_sum`, `storage_node_free_bytes`, and `storage_node_chunks`.

Queue a job through the metadata API with `POST http://127.0.0.1:8100/admin/jobs/repair`, `/admin/jobs/rebalance`, or `/admin/jobs/gc`. The broker credentials for the management UI at `http://127.0.0.1:15672` are `dev` and `RABBITMQ_PASSWORD` from `.env`.

Kubernetes (Docker Desktop context):

```powershell
kubectl apply -f deploy/kubernetes/storage-nodes.yaml
kubectl get pods,pvc -l app=storage-node
```

## Verification / limits

Verified: `pytest -q` passes (54 tests), the Compose model passes `docker compose -f compose.storage.yaml config --quiet`, and Python modules compile. Runtime validation could not finish because Docker Desktop returned an Engine `/ _ping` HTTP 500 during rollout and kubectl could not complete its API TLS handshake. Re-run the Compose start and Kubernetes dry-run commands when Docker Desktop is healthy.

The local metadata database is still a single PostgreSQL instance, so metadata availability is not HA yet. For a real HA exam deployment, use a PostgreSQL failover cluster (for example Patroni with three DCS members) and point `DATABASE_URL` at its stable primary endpoint. The local exam stack does not claim to satisfy this availability requirement until that cluster is deployed and failover-tested. RabbitMQ's management API worker acknowledges on dequeue, so a worker crash after dequeue can lose that job; use an AMQP client with manual acknowledgements for production-grade delivery guarantees.
