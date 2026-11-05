# Task 8: Docker and Kubernetes deployment

## Scope and status

Dedicated images are configured for the storage node, metadata service, gateway, RabbitMQ repair worker, and React UI. Docker Compose is the local deployment. The Kubernetes manifest defines the application workloads, four PVC-backed storage pods, PVC-backed demo PostgreSQL and RabbitMQ, Elasticsearch, two gateways behind a CPU HPA, and a `LoadBalancer` UI service.

The source checks pass, but this is **not yet a verified live deployment**. A previous image build failed inside Docker Desktop with `exec /bin/sh: exec format error`; Docker's API still times out even though the desktop app reports it is running. C: reached 0 free bytes during verification and later recovered to about 13.6 GB free. The updated Dockerfiles have not been built. The Azure Portal is signed in, but it showed no active subscription. Azure for Students activation is pending; cloud provisioning, HPA scale-out, and live chunk movement remain unverified.

## Files

- `storage_node/Dockerfile`, `metadata_service/Dockerfile`, `gateway_service/Dockerfile`, `scripts/Dockerfile.worker`, and `frontend/Dockerfile` build the five services.
- `compose.storage.yaml` wires the dedicated images, Postgres, Elasticsearch, Nginx, gateway replicas, worker replicas, and storage nodes; node 8 is in the `scale-test` profile.
- `frontend/nginx.conf` serves the React app and proxies API/docs routes to the gateway. The cloud/Compose production bundle uses a relative API URL.
- `infra/nginx.conf` load-balances the Compose gateway replicas.
- `deploy/kubernetes/task8-cloud.yaml` provides Kubernetes resources and a gateway HPA with a 2–10 replica range at 65% CPU utilization.
- `deploy/azure/deploy-student.sh` builds images remotely in Azure Container Registry Tasks and deploys the manifest from Azure Cloud Shell after subscription activation. The script passed Bash syntax validation; it has not been run against Azure.
- `tests/test_task8_deployment.py` verifies that a new node heartbeat queues one rebalance job and repeat heartbeats do not queue duplicates.

## Local setup and checks

Start Docker Desktop with Linux containers. On a fresh checkout, copy `.env.example` to `.env` and set local credentials. Start the shared infrastructure (RabbitMQ, Prometheus, Grafana, Kafka):

```powershell
Copy-Item .env.example .env
# Edit .env and set the local passwords.
docker compose up -d
docker compose ps
```

Start the application dependencies and services, with two gateways and two workers:

```powershell
docker compose -f compose.storage.yaml up -d --build --scale gateway=2 --scale job-worker=2
docker compose -f compose.storage.yaml ps
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-WebRequest http://127.0.0.1:5173
Invoke-RestMethod http://127.0.0.1:8100/nodes
```

Open the UI at `http://127.0.0.1:5173`, gateway docs at `http://127.0.0.1:8080/docs`, RabbitMQ management at `http://127.0.0.1:15672`, and Grafana at `http://127.0.0.1:3000`. Postgres and Elasticsearch are started by `compose.storage.yaml`; the shared Compose file starts RabbitMQ.

Upload a few objects in the UI, then capture node counts and worker logs before/after adding a node:

```powershell
$before = Invoke-RestMethod http://127.0.0.1:8100/nodes
$before | ConvertTo-Json -Depth 5
docker compose -f compose.storage.yaml --profile scale-test up -d node-8
Start-Sleep -Seconds 8
$after = Invoke-RestMethod http://127.0.0.1:8100/nodes
$after | ConvertTo-Json -Depth 5
docker compose -f compose.storage.yaml logs --tail 80 job-worker
```

The new node's first heartbeat queues a rebalance message. A worker calls the metadata rebalance handler; it checks fragment placements against the current consistent-hash ring and repairs misplaced fragments. Observe the `rebalancing_jobs` queue in RabbitMQ management and the per-node fragment counts in the UI Cluster view. Only stored chunks whose target changes should move. Keep the before/after output, queue state, and worker logs as exam evidence.

## Build images and prepare cloud deployment

Replace `YOUR_REGISTRY` and `VERSION` with your registry and image tag, then authenticate to that registry and run from the repository root:

```powershell
docker build -f storage_node/Dockerfile -t YOUR_REGISTRY/distributed-storage:VERSION .
docker build -f metadata_service/Dockerfile -t YOUR_REGISTRY/distributed-metadata:VERSION .
docker build -f gateway_service/Dockerfile -t YOUR_REGISTRY/distributed-gateway:VERSION .
docker build -f scripts/Dockerfile.worker -t YOUR_REGISTRY/distributed-worker:VERSION .
docker build -f frontend/Dockerfile -t YOUR_REGISTRY/distributed-ui:VERSION frontend
```

Push these five images, then replace all image placeholders in `deploy/kubernetes/task8-cloud.yaml`. Provide managed PostgreSQL and create the secret in the target cluster; do not commit real credentials:

```powershell
kubectl create namespace distributed-mid
kubectl -n distributed-mid create secret generic app-secrets `
  --from-literal=database-url='postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require' `
  --from-literal=jwt-secret='REPLACE_WITH_A_LONG_RANDOM_SECRET' `
  --from-literal=gateway-password='REPLACE_WITH_A_STRONG_PASSWORD' `
  --from-literal=rabbitmq-user='distmid' `
  --from-literal=rabbitmq-password='REPLACE_WITH_A_STRONG_PASSWORD'
```

The UI/API login name defaults to `exam-user`; its password is the `gateway-password` secret. Configure a cloud StorageClass supporting `ReadWriteOnce`, registry pull credentials if needed, and the Kubernetes Metrics Server for HPA. Then configure AWS/GCP/Azure CLI authentication and the cluster context:

```powershell
kubectl config current-context
kubectl apply -f deploy/kubernetes/task8-cloud.yaml
kubectl -n distributed-mid rollout status deployment/metadata
kubectl -n distributed-mid rollout status deployment/gateway
kubectl -n distributed-mid get pods,pvc,svc,hpa -o wide
kubectl -n distributed-mid describe hpa gateway
```

## Azure for Students deployment

Your Azure session is already signed in, but the portal currently has no active subscription. In the Azure Portal, open **Azure for Students** and select **Start**. The account holder must verify academic status and confirm they meet the offer requirements (18+ and full-time at an accredited two- or four-year institution). Microsoft currently lists $100 of credit for 12 months without a credit card; AKS control-plane management is free, while node compute, disks, network, and other resources consume credit. See [Azure for Students offer terms](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p) and [Azure AKS student offer details](https://azure.microsoft.com/en-us/free/students/). Do not upgrade the subscription to pay-as-you-go for this exam deployment.

After the subscription appears as active, open Azure Cloud Shell in **Bash**. Cloud Shell supplies `az` and `kubectl`, so the app images are built in ACR without the local Docker engine. From Cloud Shell, run:

```bash
git clone https://github.com/kahil98/distributed-mid.git
cd distributed-mid
export ACR_NAME="distmid${RANDOM}${RANDOM}"
export LOCATION="uaenorth"
export CONFIRM_STUDENT_CREDIT="YES"
bash deploy/azure/deploy-student.sh
```

The script creates resource group `rg-distributed-mid-demo`, a Standard ACR, and a one-node AKS cluster using a `Standard_D2as_v5` VM; it builds all five images through ACR Tasks, generates demo credentials into a short-lived local secret file, and applies the Kubernetes manifest. The cluster, registry, disks, public IP, and network use Azure student credit. Resource quotas and credit availability can vary by account and region. The script stops if the active Azure CLI session or explicit credit acknowledgment is missing.

When rollout completes, run `kubectl -n distributed-mid get service ui` until `EXTERNAL-IP` is assigned, then open `http://<EXTERNAL-IP>`. The username is `exam-user`; read the generated password with `kubectl -n distributed-mid get secret app-secrets -o jsonpath='{.data.gateway-password}' | base64 -d; echo`. Demonstrate node addition with `kubectl -n distributed-mid scale statefulset/storage-node --replicas=5`, then inspect `kubectl -n distributed-mid logs deployment/repair-worker --all-pods=true --tail=100` and the cluster page.

After the exam, delete the demo's resource group to stop ongoing credit use and remove the cluster, registry, and all stored data:

```bash
az group delete --name rg-distributed-mid-demo --yes --no-wait
```

This cleanup is deliberately manual because it permanently deletes the deployment and its stored object data. The deployment script has not been run yet; it is waiting for the active student subscription.

## GitHub Codespaces demo

`.devcontainer/devcontainer.json` defines a Python 3.12 environment with Docker in Docker
and forwards the UI, API, monitoring, and RabbitMQ management ports. No cloud keys are
stored in the repository. The startup script creates an ignored `.env` with random demo
secrets, starts the shared RabbitMQ/monitoring Compose services, and builds/starts the
application services:

```bash
bash scripts/codespaces-up.sh
```

Open forwarded port 5173 for the React UI or port 8080 plus `/docs` for Swagger. Codespaces
ports are private by default. This is not a persistent Kubernetes cluster; volumes belong
to the Codespace. GitHub Free personal accounts currently include 120 core-hours and
15 GB-month of Codespaces usage. Stop the Codespace when finished and check billing before
creating it; paid usage can apply if limits are exceeded and billing is enabled.

The script generates random credentials in `.env` and never prints them. In the Codespace
terminal, run `grep -E '^(GATEWAY_PASSWORD|GRAFANA_PASSWORD|RABBITMQ_PASSWORD)=' .env` to
read the demo login values. Gateway username is `exam-user`; Grafana username is `admin`.

Use the external address on service `ui`. To demonstrate HPA scale-out, generate sustained authenticated API load and check HPA targets and gateway replica count. To add a cloud storage node, scale the StatefulSet and observe the queue and node distribution:

```powershell
kubectl -n distributed-mid scale statefulset/storage-node --replicas=5
kubectl -n distributed-mid get pods -w
kubectl -n distributed-mid logs deployment/repair-worker --all-pods=true --tail=100 -f
```

## Verification results

- `python -m pytest -q -p no:cacheprovider`: **55 passed**, one third-party Starlette/AnyIO deprecation warning.
- `npm run build` with `VITE_GATEWAY_URL` empty: **passed**; verified no `http://127.0.0.1:8080` was embedded in the generated bundle.
- `docker compose -f compose.storage.yaml config --quiet`: **passed**.
- `node` with `js-yaml`: **passed**, all 18 Kubernetes YAML documents parsed.
- `bash -n deploy/azure/deploy-student.sh`: **passed**.
- `kubectl` client dry-run on the updated manifest: **not available**; the local Kubernetes API timed out during discovery. YAML syntax was independently parsed, but API schema validation needs a live cluster.
- Automated heartbeat test: **passed**; a first-time node registration queues one rebalance job, and subsequent heartbeats do not enqueue duplicates. Consistent-hash movement tests also pass.
- Updated Docker image build and live Compose/rebalance: **not verified** due to the Docker failure and full disk noted above.
- Cloud deployment, HPA scale-out, and live node-rebalance measurement: **not run**; no provider/authenticated cluster is available yet.

## Limitations

The demo manifest runs a single PostgreSQL StatefulSet with a PVC; it is not an HA database. RabbitMQ and Elasticsearch are also single-replica demo services, and Elasticsearch has no PVC. The HPA needs Metrics Server and available cluster capacity. Use managed/HA dependencies, TLS, cloud secret management, persistent Elasticsearch storage, and provider-specific policies before treating this as a production deployment.
