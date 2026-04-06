#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "=== 1/3  npm run build ==="
npm run build

echo "=== 2/3  docker compose build (rc.dev) ==="
docker compose -f docker-compose.rc.dev.yaml build

echo "=== 3/3  restart containers ==="
docker compose -f docker-compose.rc.dev.yaml up -d
docker restart pipelines-dev

echo "=== Done ==="
