#!/usr/bin/env bash
# Flask only: no Node, Gateway, parser or memory daemon.
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ -z "${SAM_DATA_ROOT:-}" ]]; then
  echo "Missing SAM_DATA_ROOT; configure an explicit snapshot data root" >&2
  exit 2
fi
export SAM_CLOUD_READONLY=1
export PORT="${PORT:-5000}"
# Select explicitly supplied Python, otherwise the local venv or platform Python.
PYTHON="${SAM_CLOUD_PYTHON:-python3}"
if [[ -z "${SAM_CLOUD_PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi
# Initialize only the dedicated FactsTree snapshot store (idempotent); the
# local app ledger app.db is never created or migrated on the cloud host.
"$PYTHON" -m app.db.migrate --db "$SAM_DATA_ROOT/cloud-facts-tree-snapshot.db" --snapshot
exec "$PYTHON" webapp/app.py
