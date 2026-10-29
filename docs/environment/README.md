# Task: environment setup

## Completed steps

1. Inspected installed tools: Python 3.11.8, Git 2.55.0, Docker CLI 29.8.2,
   and kubectl 1.36.1 were already installed.
2. Started Docker Desktop and verified that its Linux engine responds.
3. Created `.venv` with `python -m venv .venv`.
4. Installed the client libraries from `requirements.txt` and recorded their
   versions in `requirements.lock.txt`.
5. Initialized a local Git repository with branch `main` and committed setup files.
6. Added `.gitignore` entries for the virtual environment, Python caches, and `.env`.
7. Added `compose.yaml` for Prometheus, Grafana, Kafka, and RabbitMQ with persistent
   named volumes and ports restricted to the local machine.
8. Generated separate random Grafana and RabbitMQ passwords in the ignored `.env`.
9. Provisioned Grafana's Prometheus data source using `infra/grafana`.
10. Verified the existing Docker Desktop Kubernetes node is Ready.

## Start and verify

```powershell
docker desktop start
docker compose config --quiet
docker compose up -d
docker compose ps
.\.venv\Scripts\python.exe -m pip check
kubectl --context docker-desktop get nodes
docker compose exec -T rabbitmq rabbitmq-diagnostics -q ping
docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
Invoke-RestMethod http://127.0.0.1:3000/api/health
Invoke-RestMethod http://127.0.0.1:9090/-/ready
```

## Recorded verification

During setup, all four containers started successfully. Host-side Python checks
connected to Kafka at `127.0.0.1:9092`, authenticated to RabbitMQ, received Grafana's
healthy database response, and received Prometheus's readiness response. An
authenticated Grafana API request confirmed its default data source points to
`http://prometheus:9090`. `pip check` found no broken requirements. Kubernetes
reported `desktop-control-plane` Ready at version 1.36.1.

The original localhost Kafka advertisement was changed to `127.0.0.1` after
host-side testing exposed a connection failure on Windows; the IPv4 check passed.

## Version control status

Git is initialized locally and setup is committed. No GitHub/GitLab remote was
created or attached because a destination account/repository has not been supplied.
Once an empty remote exists:

```powershell
git remote add origin <repository-url>
git push -u origin main
```

The four-node storage task is documented separately in `docs/storage-node/README.md`.
