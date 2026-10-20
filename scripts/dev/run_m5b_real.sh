#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
exec "$ROOT/.venv/bin/python" -B "$ROOT/scripts/dev/run_m5b_real.py" "$@"
