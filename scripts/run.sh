#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if [[ -z "${SAM_DATA_ROOT:-}" ]]; then
  echo "Missing SAM_DATA_ROOT; configure an explicit data root before startup" >&2
  exit 2
fi

# Snapshot-only cloud windows must not require Node, secrets or local daemons.
if [[ "${SAM_CLOUD_READONLY:-0}" == 1 ]]; then
  export PORT=18789
  exec python3 webapp/app.py
fi

# Reject a retired backend before Node, credentials or runtime preparation.
if [[ "${SAM_MEMORY_BACKEND:-localvector}" != localvector ]]; then
  if [[ "${SAM_MEMORY_BACKEND}" == ov ]]; then
    echo "OV 已摘除；SAM_MEMORY_BACKEND=ov 不再支持，请移除遗留配置" >&2
  else
    echo "SAM_MEMORY_BACKEND must be localvector" >&2
  fi
  exit 2
fi

# Node selection and version verification belong to the read-only preflight
# in prepare/adapter. SAM_NODE_BIN only records a path, which must resolve to
# the trusted install ledger's managed Node.

export HOST="${HOST:-0.0.0.0}"
# Single-host local deployment: default scope. Override only for multi-host setups.
export SAM_COMPANY_ID="${SAM_COMPANY_ID:-default}"
# Keep the browser-facing Flask service separate from the resident Gateway.
# This is the canonical startup entry point: Flask 18789, Gateway 18790.
export PORT=18789
export OPENCLAW_GATEWAY_PORT="${SAM_OPENCLAW_GATEWAY_PORT:-${OPENCLAW_GATEWAY_PORT:-18790}}"
export SAM_OPENCLAW_GATEWAY_PORT="$OPENCLAW_GATEWAY_PORT"
export SAM_DATA_ROOT
export SAM_PROJECT_ROOT="$PROJECT_DIR"
export SAM_HARNESS_ROOT="${SAM_HARNESS_ROOT:-$PROJECT_DIR/harness-openclaw}"
export SAM_SKILL_ROOT="${SAM_SKILL_ROOT:-$PROJECT_DIR/skills}"
export SAM_TOOL_PLUGIN_DIR="${SAM_TOOL_PLUGIN_DIR:-$PROJECT_DIR/harness-openclaw/plugins/sam-memory}"
# Token Plan routing is declarative; the API key is never given a default and
# must come from the existing secret environment/file, never from source.
if [[ -z "${SAM_LEADER_MODEL_API_KEY:-}" && -n "${SAM_GUIDE_MODEL_API_KEY:-}" ]]; then
  SAM_LEADER_MODEL_API_KEY="$SAM_GUIDE_MODEL_API_KEY"
fi
if [[ -z "${SAM_LEADER_MODEL_API_KEY:-}" && -r "${HOME}/.bl_tokenplan_key" ]]; then
  SAM_LEADER_MODEL_API_KEY="$(< "${HOME}/.bl_tokenplan_key")"
  export SAM_LEADER_MODEL_API_KEY
fi
if [[ -z "${SAM_LEADER_MODEL_API_KEY:-}" ]]; then
  echo "Missing SAM_LEADER_MODEL_API_KEY; set it in the secret environment or ${HOME}/.bl_tokenplan_key" >&2
  exit 2
fi
export SAM_LEADER_MODEL_API_KEY
export SAM_LEADER_MODEL_BASE_URL="${SAM_LEADER_MODEL_BASE_URL:-${SAM_GUIDE_MODEL_BASE_URL:-https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}}"
export SAM_LEADER_MODEL="${SAM_LEADER_MODEL:-${SAM_GUIDE_MODEL:-qwen3.8-max}}"
# Validate before preparing files or contacting/starting any Gateway.
python3 -c 'from app.db.database import validate_schema; import os; validate_schema(os.environ.get("SAM_MAIN_DB") or os.path.join(os.environ["SAM_DATA_ROOT"], "app.db"))'
python3 scripts/prepare-openclaw-runtime.py
exec python3 webapp/app.py
