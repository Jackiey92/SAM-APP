#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
.venv/bin/python -m pip install -r poc/memory_m0/requirements-m0.txt
if [[ ! -d poc/memory_m0/model-cache ]]; then
    .venv/bin/python poc/memory_m0/download.py > poc/memory_m0/download.log 2>&1
fi
sha256sum -c poc/memory_m0/fixture-before.sha256
.venv/bin/python poc/memory_m0/run.py > poc/memory_m0/online.log 2>&1
unshare -Urn env HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 M0_OUTPUT=offline .venv/bin/python poc/memory_m0/offline.py > poc/memory_m0/offline.log 2>&1
