#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo 'Python 3.10 or newer is required.' >&2; exit 1; }
command -v npm >/dev/null || { echo 'Node.js and npm are required.' >&2; exit 1; }
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-dev.txt
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
npm run build
.venv/bin/python -m backend.seed
echo
echo 'ReliefLink is ready. Run ./run.sh, then open http://localhost:8000'
