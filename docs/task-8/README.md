# Task 8: container and cloud deployment

## Service images

Dedicated Dockerfiles are provided for the storage node, metadata API, gateway API, RabbitMQ job worker, and React UI. Build/push images to a registry you control, then replace `YOUR_REGISTRY` and `VERSION` in `deploy/kubernetes/task8-cloud.yaml`.

```powershell
docker build -f storage_node/Dockerfile -t YOUR_REGISTRY/distributed-storage:VERSION .
docker build -f metadata_service/Dockerfile -t YOUR_REGISTRY/distributed-metadata:VERSION .
docker build -f gateway_service/Dockerfile -t YOUR_REGISTRY/distributed-gateway:VERSION .
docker build -f scripts/Dockerfile.worker -t YOUR_REGISTRY/distributed-worker:VERSION .
docker build -f frontend/Dockerfile -t YOUR_REGISTRY/distributed-ui:VERSION frontend
```

Push each image with `docker push`. Create the `app-secrets` Kubernetes Secret for `database-url` and `jwt-secret`; use a managed PostgreSQL endpoint and configure metadata/RabbitMQ services and secrets for the selected cloud.

## Local Compose verification

Start the dependencies, then the app stack:

```powershell
docker compose up -d
docker compose -f compose.storage.yaml up -d --build --scale gateway=2 --scale job-worker=2
docker compose -f compose.storage.yaml ps
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-WebRequest http://127.0.0.1:5173
```

Expected: two gateway containers, two workers, gateway health `ok`, and UI HTTP status 200. The metadata heartbeat handler queues a `rebalance` job when a storage node first registers or returns from a failed state. Verify this by adding a node, checking RabbitMQ's `rebalancing_jobs` queue at `http://127.0.0.1:15672`, then reviewing worker logs and metadata placements:

```powershell
docker compose -f compose.storage.yaml --profile scale-test up -d node-8
docker compose -f compose.storage.yaml logs --tail 50 job-worker
Invoke-RestMethod http://127.0.0.1:8100/nodes
```

For a before/after placement movement percentage, use `POST /placement/measure` with a set of chunk hashes and the before/after node membership. The rebalance worker relocates misplaced fragments based on the current consistent-hash ring.

## Kubernetes on AWS, GCP, or Azure

`deploy/kubernetes/task8-cloud.yaml` is provider-neutral Kubernetes configuration: a four-node storage StatefulSet with PVCs, gateway Deployment (2 replicas), cloud `LoadBalancer` Service, and CPU HPA (2–10 replicas at 65%). Kubernetes requires a working cluster, StorageClass, Metrics Server, registry access, image pull credentials if private, and cloud credentials configured through the provider CLI. Those prerequisites are provider-owned; this repository does not create billable cloud resources.

After configuring `kubectl`, registry image names, secrets, and the metadata/RabbitMQ endpoints:

```powershell
kubectl apply -f deploy/kubernetes/task8-cloud.yaml
kubectl -n distributed-mid get pods,pvc,svc,hpa
kubectl -n distributed-mid describe hpa gateway
```

Cloud live deployment and HPA scale-out require an actual provider cluster and load; they cannot be verified from local config alone.

## Validation and limits

Verified: all 54 tests pass and `docker compose -f compose.storage.yaml config --quiet` passes. The Docker build was attempted but the local Docker engine failed at the base-image RUN step with `exec format error`, so images and the live Compose stack are not verified. Cloud resources were not provisioned. The cloud manifest is a template: it expects managed PostgreSQL and in-cluster metadata/RabbitMQ endpoints, but does not create or configure those dependencies. The single-node PostgreSQL in local Compose is not HA. Test cloud HPA and storage-node rebalance on a configured cluster before presenting it as a completed live cloud deployment.
