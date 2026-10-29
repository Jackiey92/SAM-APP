#!/usr/bin/env python3
"""Install the exact manifest runtime once; never start OpenClaw/Gateway."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.managed_runtime_installer import InstallError, install
from app.runtime_contract import RuntimeContractError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", type=Path, help="offline directory containing all four official artifacts")
    parser.add_argument("--node-release-key", type=Path, help="exported pinned Node signing public key (no key retrieval)")
    parser.add_argument("--lock-timeout", type=float, default=60, help="seconds to wait for the install lock (default: 60)")
    args = parser.parse_args(argv)
    try:
        root = install(ROOT, staging_dir=args.staging_dir, node_release_key=args.node_release_key,
                       lock_timeout=args.lock_timeout)
    except (InstallError, RuntimeContractError, OSError, ValueError, tarfile.TarError,
            subprocess.SubprocessError) as exc:
        print(f"managed runtime install failed: {exc}", file=sys.stderr)
        return 1
    print(f"managed_runtime_install_ok install_root={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
