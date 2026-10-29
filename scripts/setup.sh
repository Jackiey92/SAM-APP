#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

INSTALL_RUNTIME=0
RUNTIME_ARGS=()
while (($#)); do
  case "$1" in
    --install-managed-runtime) INSTALL_RUNTIME=1; shift ;;
    --staging-dir|--node-release-key|--lock-timeout)
      if (($# < 2)); then echo "Missing value for $1" >&2; exit 2; fi
      RUNTIME_ARGS+=("$1" "$2"); shift 2 ;;
    *) echo "Unknown setup argument: $1" >&2; exit 2 ;;
  esac
done
if ((INSTALL_RUNTIME == 0 && ${#RUNTIME_ARGS[@]} > 0)); then
  echo "Runtime options require --install-managed-runtime" >&2
  exit 2
fi

python3 -m pip install --no-cache-dir -r requirements.txt

if ((INSTALL_RUNTIME)); then
  python3 "$SCRIPT_DIR/install-managed-runtime.py" "${RUNTIME_ARGS[@]}"
fi
