#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  command -v openssl >/dev/null || { echo "openssl is required to create demo secrets" >&2; exit 1; }
  cat > .env <<EOF
GRAFANA_PASSWORD=$(openssl rand -hex 16)
RABBITMQ_PASSWORD=$(openssl rand -hex 20)
JWT_SECRET=$(openssl rand -hex 32)
GATEWAY_PASSWORD=$(openssl rand -hex 16)
EOF
  chmod 600 .env
  echo "Created an ignored .env with random Codespaces demo credentials."
fi

docker compose -f compose.yaml up -d
docker compose -f compose.storage.yaml up -d --build --scale gateway=2 --scale job-worker=2

echo
docker compose -f compose.yaml ps
docker compose -f compose.storage.yaml ps
echo
echo "Open the forwarded UI port 5173; gateway Swagger docs are on port 8080 at /docs."
