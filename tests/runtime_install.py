"""Synthetic managed installs and Node metadata runners; never execute Node."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app import runtime_contract as contract


ROOT = Path(__file__).resolve().parents[1]
NODE_BYTES = b"synthetic node; never execute"


def managed_runner(engine_runner=None, *, calls=None):
    """Answer only synthetic Node probes, then delegate engine commands."""
    def run(argv, **kwargs):
        if argv[1:] in (["--version"], ["-p", "process.platform + '/' + process.arch"]):
            assert Path(argv[0]).read_bytes() == NODE_BYTES
            assert kwargs == {"capture_output": True, "text": True, "timeout": 10}
            if calls is not None:
                calls.append(argv)
            output = "v24.22.1\n" if argv[1] == "--version" else "linux/x64\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if engine_runner is None:
            pytest.fail(f"unexpected engine command: {argv}")
        return engine_runner(argv, **kwargs)
    return run


def make_managed_install(tmp_path, *, project_root=None):
    if project_root is None:
        project_root = tmp_path
        (project_root / "sam-manifest.yaml").write_bytes((ROOT / "sam-manifest.yaml").read_bytes())
    pins = contract.load_contract(project_root, env={})
    root = tmp_path / "managed"
    entry = root / "node_modules/openclaw/openclaw.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"synthetic entry\n")
    package = entry.with_name("package.json")
    package.write_text(json.dumps({"version": pins.openclaw_version}), encoding="utf-8")
    node = root / "bin/node"
    node.parent.mkdir()
    node.write_bytes(NODE_BYTES)
    node.chmod(0o700)
    ledger = {
        "schema": 1,
        "contract": {"node": pins.node_version, "node_platform": pins.node_platform,
                     "node_arch": pins.node_arch, "openclaw": pins.openclaw_version,
                     "commit": pins.openclaw_commit, "tarball": pins.openclaw_tarball,
                     "integrity": pins.openclaw_integrity},
        "files": [{"path": str(p.relative_to(root)),
                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (entry, package)],
        "openclaw": {"commit": pins.openclaw_commit, "commit_provenance": "synthetic evidence",
                     "tarball_sha512": pins.tarball_sha512_b64},
        "node": {"binary": "bin/node", "sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
                 "official_sha256": "a" * 64, "official_signature": "synthetic signature evidence"},
    }
    ledger_path = root / contract.LEDGER_NAME
    calls = []

    def save():
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        ledger_path.chmod(0o600)

    config = SimpleNamespace(project_root=project_root, openclaw_entry=entry, node_bin="", cloud_readonly=False)
    save()
    return SimpleNamespace(root=root, entry=entry, package=package, node=node, ledger=ledger,
                           ledger_path=ledger_path, save=save, runner=managed_runner(calls=calls),
                           calls=calls, config=config)


def managed_config(config, tmp_path):
    install = make_managed_install(tmp_path, project_root=config.project_root)
    return replace(config, openclaw_entry=install.entry, node_bin=str(install.node))


@pytest.fixture
def managed_install(tmp_path):
    return make_managed_install(tmp_path)
