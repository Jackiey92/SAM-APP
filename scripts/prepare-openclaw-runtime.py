#!/usr/bin/env python3
"""Prepare OpenClaw state discovery for a clean SAM startup.

This is intentionally side-effect limited: it writes the generated config
and mounts first-party skills/plugins, but does not launch a daemon.  Gateway
is owned by the long-lived Flask process and is started once on its first
agent call when gateway mode is enabled.

The read-only pinned-runtime preflight precedes every preparation side effect.
Use --check to verify the installation without preparing state or ports.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.harness.runtime.openclaw_adapter import OpenClawAdapter
from app.harness.staging import StagingStore
from app.runtime_config import RuntimeConfig
from app.runtime_contract import RuntimeContractError, preflight_runtime


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the pinned OpenClaw runtime")
    parser.add_argument("--check", action="store_true", help="run read-only preflight and exit")
    args = parser.parse_args(argv)
    config = RuntimeConfig.from_env(project_root=ROOT)
    try:
        proof = preflight_runtime(config)
    except RuntimeContractError as exc:
        raise SystemExit(f"OpenClaw runtime preflight failed: {exc}") from exc
    if config.cloud_readonly:
        if args.check:
            print("openclaw_runtime_preflight_ok engine=disabled profile=cloud")
            return 0
        raise SystemExit("cloud read-only profile must not prepare or start the OpenClaw engine")
    if args.check:
        print(
            "openclaw_runtime_preflight_ok "
            f"node={proof.node_version} openclaw={proof.openclaw_version} "
            f"commit={proof.openclaw_commit} install_root={proof.install_root}"
        )
        return 0
    adapter = OpenClawAdapter(
        StagingStore(db_path=config.main_db), config=config,
        db_path=config.main_db, objects_dir=config.objects_dir,
    )
    try:
        adapter.reclaim_gateway_port()
    except (RuntimeError, OSError) as exc:
        raise SystemExit(
            f"OpenClaw Gateway startup blocked on port {config.gateway_port}: {exc}"
        ) from exc
    adapter.prepare_runtime()
    print(f"openclaw_runtime_prepared state={config.state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
