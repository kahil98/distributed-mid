#!/usr/bin/env bash
set -euo pipefail

# Run from Azure Cloud Shell after activating Azure for Students.
: "${ACR_NAME:?Set ACR_NAME to a globally unique 5-50 character registry name}"
: "${CONFIRM_STUDENT_CREDIT:?Set CONFIRM_STUDENT_CREDIT=YES after reviewing the credit usage}"
if [[ "$CONFIRM_STUDENT_CREDIT" != "YES" ]]; then
  echo "Refusing to create resources: set CONFIRM_STUDENT_CREDIT=YES only after reviewing the Azure credit notice." >&2
  exit 2
fi

LOCATION="${LOCATION:-uaenorth}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-distributed-mid-demo}"
AKS_NAME="${AKS_NAME:-aks-distributed-mid-demo}"
IMAGE_TAG="${IMAGE_TAG:-task8}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MANIFEST="${REPO_ROOT}/deploy/kubernetes/task8-cloud.yaml"
SECRET_FILE="$(mktemp)"
chmod 600 "$SECRET_FILE"
cleanup() { rm -f "$SECRET_FILE"; }
trap cleanup EXIT

for tool in az kubectl openssl sed; do
  command -v "$tool" >/dev/null || { echo "Required tool not found: $tool" >&2; exit 1; }
done
az account show --query id --output tsv >/dev/null || {
  echo "Run az login in Cloud Shell and select the Azure for Students subscription first." >&2
  exit 1
}

printf 'Azure region: %s\nResource group: %s\nAKS: %s (one Standard_D2as_v5 node)\nACR: %s (Standard)\n' \
  "$LOCATION" "$RESOURCE_GROUP" "$AKS_NAME" "$ACR_NAME"
echo "These resources consume Azure for Students credit (the VM, disks, network and registry). Keep the deployment only as long as needed."

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" \
  --location "$LOCATION" --sku Standard --admin-enabled false --output none
ACR_SERVER="$(az acr show --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --query loginServer --output tsv)"

az aks create --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --location "$LOCATION" \
  --tier free --node-count 1 --node-vm-size Standard_D2as_v5 \
  --enable-managed-identity --attach-acr "$ACR_NAME" --generate-ssh-keys --output none
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --overwrite-existing

# Build all images remotely in ACR from the checked-out public repo; local Docker is not used.
cd "$REPO_ROOT"
az acr build --registry "$ACR_NAME" --image "distributed-storage:${IMAGE_TAG}" \
  --file storage_node/Dockerfile .
az acr build --registry "$ACR_NAME" --image "distributed-metadata:${IMAGE_TAG}" \
  --file metadata_service/Dockerfile .
az acr build --registry "$ACR_NAME" --image "distributed-gateway:${IMAGE_TAG}" \
  --file gateway_service/Dockerfile .
az acr build --registry "$ACR_NAME" --image "distributed-worker:${IMAGE_TAG}" \
  --file scripts/Dockerfile.worker .
(cd frontend && az acr build --registry "$ACR_NAME" --image "distributed-ui:${IMAGE_TAG}" --file Dockerfile .)

# Create strong demo credentials without printing them or adding them to shell history.
POSTGRES_PASSWORD="$(openssl rand -hex 24)"
JWT_SECRET="$(openssl rand -hex 32)"
GATEWAY_PASSWORD="$(openssl rand -hex 18)"
RABBITMQ_PASSWORD="$(openssl rand -hex 24)"
{
  printf 'database-url=postgresql://distmid:%s@postgres:5432/distributed\n' "$POSTGRES_PASSWORD"
  printf 'postgres-user=distmid\npostgres-password=%s\npostgres-db=distributed\n' "$POSTGRES_PASSWORD"
  printf 'jwt-secret=%s\ngateway-password=%s\n' "$JWT_SECRET" "$GATEWAY_PASSWORD"
  printf 'rabbitmq-user=distmid\nrabbitmq-password=%s\n' "$RABBITMQ_PASSWORD"
} > "$SECRET_FILE"
kubectl create namespace distributed-mid --dry-run=client -o yaml | kubectl apply -f -
kubectl -n distributed-mid create secret generic app-secrets --from-env-file="$SECRET_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -

sed -i "s|YOUR_REGISTRY|${ACR_SERVER}|g; s|VERSION|${IMAGE_TAG}|g" "$MANIFEST"
kubectl apply -f "$MANIFEST"
kubectl -n distributed-mid rollout status statefulset/postgres --timeout=10m
kubectl -n distributed-mid rollout status statefulset/rabbitmq --timeout=10m
kubectl -n distributed-mid rollout status statefulset/storage-node --timeout=10m
for deployment in elasticsearch metadata gateway repair-worker ui; do
  kubectl -n distributed-mid rollout status "deployment/${deployment}" --timeout=10m
done
kubectl -n distributed-mid get pods,pvc,svc,hpa -o wide
printf '\nUI service (wait for EXTERNAL-IP):\n'
kubectl -n distributed-mid get service ui
printf '\nGateway login name: exam-user (password is stored in Kubernetes Secret app-secrets).\n'
printf 'To verify node-add rebalance: kubectl -n distributed-mid scale statefulset/storage-node --replicas=5\n'
printf 'To stop credit usage after the exam: az group delete --name %s --yes --no-wait\n' "$RESOURCE_GROUP"
