# Distributed systems development environment

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

