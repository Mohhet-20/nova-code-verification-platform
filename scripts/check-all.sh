#!/usr/bin/env bash
# macOS/Linux equivalent of check-all.ps1. Assumes backend/.venv and frontend/node_modules exist.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
"$root/backend/.venv/bin/python" "$root/scripts/scan_secrets.py"
cd "$root/backend" && .venv/bin/python -m ruff check . && .venv/bin/python -m app.contracts && .venv/bin/python -m pytest
cd "$root/frontend" && npm run typecheck && npm run lint && npm test && npm run build
