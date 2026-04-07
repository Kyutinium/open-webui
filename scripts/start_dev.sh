#!/usr/bin/env bash
# Full dev environment: frontend (Vite) + backend (uvicorn) + pipelines
# Usage: ./scripts/dev.sh
#
# Frontend: http://localhost:5173 (hot reload)
# Backend:  http://localhost:8080 (auto reload on .py changes)
# Pipelines: existing Docker container (restart only)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

cleanup() {
    echo ""
    echo "Shutting down..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    wait $BACKEND_PID $FRONTEND_PID 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

# 1. Restart pipelines container
if docker ps --format '{{.Names}}' | grep -q pipelines-dev; then
    echo "=== Restarting pipelines-dev ==="
    docker restart pipelines-dev
fi

# 2. Start backend
echo "=== Starting backend (port 8080) ==="
cd "$PROJECT_DIR/backend"
export CORS_ALLOW_ORIGIN="http://localhost:5173;http://localhost:8080"
PORT="${PORT:-8080}"
uvicorn open_webui.main:app --port "$PORT" --host 0.0.0.0 --forwarded-allow-ips '*' --reload &
BACKEND_PID=$!
cd "$PROJECT_DIR"

# 3. Start frontend
echo "=== Starting frontend (port 5173) ==="
npm run dev &
FRONTEND_PID=$!

echo ""
echo "========================================="
echo "  Frontend: http://localhost:5173"
echo "  Backend:  http://localhost:${PORT}"
echo "========================================="
echo "  Press Ctrl+C to stop all"
echo ""

wait
