#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python || ! -f dist/index.html ]]; then
  echo 'Run ./setup.sh first to install dependencies and build the interface.' >&2
  exit 1
fi
exec .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port "${RELIEFLINK_PORT:-8000}"
