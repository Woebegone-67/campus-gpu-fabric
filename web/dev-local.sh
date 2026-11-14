#!/usr/bin/env bash
# Two terminals: bash web/dev-local.sh api / bash web/dev-local.sh web
set -euo pipefail
FABRIC_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FABRIC_REPO_DIR"
export PATH="$FABRIC_REPO_DIR/runtime/b-tools/node/node_modules/.bin:$FABRIC_REPO_DIR/runtime/b-tools/python/bin:$PATH"
case "${1:-api}" in
  api)
    FABRIC_PYTHON="${FABRIC_PYTHON:-$FABRIC_REPO_DIR/.venv/bin/python}"
    if [[ ! -x "$FABRIC_PYTHON" ]]; then
      echo 'Run make setup with Python 3.12 and Node 22 first.' >&2
      exit 1
    fi
    export FABRIC_INSTANCE="${FABRIC_INSTANCE:-owner-b-v1}"
    export FABRIC_DATA_DIR="${FABRIC_DATA_DIR:-$FABRIC_REPO_DIR/runtime/$FABRIC_INSTANCE}"
    export FABRIC_AUTH_FILE="${FABRIC_AUTH_FILE:-$FABRIC_DATA_DIR/auth.json}"
    export FABRIC_INVENTORY_FILE="${FABRIC_INVENTORY_FILE:-$FABRIC_REPO_DIR/configs/cp1_inventory.json}"
    if [[ ! -f "$FABRIC_AUTH_FILE" ]]; then
      "$FABRIC_PYTHON" scripts/init_cp1_demo.py "$FABRIC_DATA_DIR"
    fi
    exec "$FABRIC_PYTHON" -m uvicorn campus_fabric.workflows.devhost:create_app --factory \
      --host 127.0.0.1 --port "${PORT:-8081}"
    ;;
  web)
    export FABRIC_API_TARGET="${FABRIC_API_TARGET:-http://127.0.0.1:${PORT:-8081}}"
    exec npm --prefix web run dev -- --port "${WEB_PORT:-5174}"
    ;;
  *) echo 'Usage: bash web/dev-local.sh api|web' >&2; exit 2 ;;
esac
