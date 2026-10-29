# Distributed systems development environment

See [System architecture and repository structure](docs/system-architecture.md) for the component diagram, data flows, deployment model, and project layout.

## Exam tasks

Every task includes implementation, executable verification, and a task-specific README.
See [Environment setup](docs/environment/README.md) for the setup steps and checks.
See [Storage node task](docs/storage-node/README.md) for the four-node FastAPI service,
API contract, setup steps, tests, and workload results.
See [Task 2: chunking, metadata, and placement](docs/task-2/README.md) for fixed/CDC
chunking, PostgreSQL object manifests, deduplication, and consistent hashing.
See [Task 3: availability and coding](docs/task-3/README.md) for quorum replication,
Reed-Solomon, failure repair, scrubbing, and storage/traffic comparisons.
See [Task 4: authenticated FastAPI gateway](docs/task-4/README.md) for JWT auth,
streaming uploads, range downloads, object search, cluster status, and Postman checks.
See [Task 5: React console and Elasticsearch search](docs/task-5/README.md) for the
Vite UI, metadata indexing, cluster monitoring, and frontend verification steps.
See [Task 6: scaling, job queues, Kubernetes, and monitoring](docs/task-6/README.md) for
the Nginx gateway pair, RabbitMQ worker, storage StatefulSet, and Prometheus/Grafana setup.
See [Task 8: cloud deployment](docs/task-8/README.md) for per-service Dockerfiles, local
Compose checks, Kubernetes gateway autoscaling, and storage-node rebalance steps.

## Python

Python 3.11 or newer, Git, Docker Desktop (Linux containers), and kubectl are required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use `.\.venv\Scripts\python.exe` directly, or activate with `.\.venv\Scripts\Activate.ps1`.
Installed dependency versions are recorded in `requirements.lock.txt`.

## Local services

The initial setup generates `.env` with local passwords. For a fresh clone, copy
`.env.example` to `.env` and replace both passwords before starting.

```powershell
docker desktop start
docker compose up -d
docker compose ps
```

| Service | Local address | Login |
| --- | --- | --- |
| Prometheus | http://localhost:9090 | None |
| Grafana | http://localhost:3000 | admin / GRAFANA_PASSWORD in .env |
| Kafka | 127.0.0.1:9092 | None |
| RabbitMQ AMQP | localhost:5672 | dev / RABBITMQ_PASSWORD in .env |
| RabbitMQ management | http://localhost:15672 | Same as AMQP |

Grafana has Prometheus configured as its default data source. Prometheus initially
scrapes its own metrics. Kafka clients in Compose use `kafka:29092`; host clients
use `127.0.0.1:9092`. This is a single-node local development stack.

`docker compose down` stops the services and preserves data in named volumes.

## Kubernetes

Docker Desktop provides the local Kubernetes cluster. Enable Kubernetes in Docker
Desktop if needed, then verify it:

```powershell
kubectl --context docker-desktop get nodes
```

The four services run through Compose; Kubernetes is available separately for
application deployments.

## Version control

This folder is initialized with Git on branch `main`. Create an empty repository
in your GitHub or GitLab account, then connect it:

```powershell
git remote add origin <your-repository-url>
git push -u origin main
```

`.env` and `.venv` are excluded from version control.

