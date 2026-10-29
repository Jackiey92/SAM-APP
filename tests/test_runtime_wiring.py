"""CHG-070 entry point gates, using synthetic installations and runners only."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import subprocess

import pytest

from app.harness.runtime import openclaw_adapter as adapter_module
from app.harness.staging import StagingStore
from app.runtime_config import RuntimeConfig
from app.runtime_contract import RuntimeContractError
from tests.runtime_install import ROOT, managed_install, managed_runner


ENTRIES = {
    "parse": lambda a: a.run_parse("unused", "pdf"),
    "chat": lambda a: a.run_agent_message("unused"),
    "ready": lambda a: a.ensure_runtime_ready(),
    "agent": lambda a: a._invoke_agent("unused", timeout=1),
    "env": lambda a: a._base_env(),
    "gateway": lambda a: a._ensure_gateway({}, timeout=1),
    "reclaim_internal": lambda a: a._reclaim_or_reject_gateway_port(),
    "reclaim": lambda a: a.reclaim_gateway_port(),
    "health": lambda a: a._gateway_rpc_healthy({}, timeout=1),
    "prepare": lambda a: a.prepare_runtime(),
}


def config_for(install):
    config = RuntimeConfig.from_env(ROOT, env={"SAM_PROFILE": "local"})
    base = install.root.parent
    return replace(config, project_root=install.config.project_root,
                   openclaw_entry=install.config.openclaw_entry, node_bin=install.config.node_bin,
                   harness_root=base / "harness", state_dir=base / "state",
                   config_path=base / "state/openclaw.json")


def adapter_for(install, runner):
    return adapter_module.OpenClawAdapter(
        StagingStore(install.root.parent / "missing.db"), config=config_for(install), runner=runner)


def forbidden(*args, **kwargs):
    pytest.fail("gate allowed filesystem installation probing, writes or engine side effects")


@pytest.mark.parametrize("entry", ENTRIES)
def test_cloud_rejects_before_any_install_probe_or_side_effect(managed_install, monkeypatch, entry):
    a = adapter_for(managed_install, forbidden)
    a.config = replace(a.config, cloud_readonly=True)
    with monkeypatch.context() as patch:
        patch.setattr(adapter_module, "preflight_runtime", forbidden)
        for name in ("resolve", "stat", "read_text", "read_bytes", "mkdir", "write_text"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(subprocess, "run", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(adapter_module.os, "kill", forbidden)
        patch.setattr(adapter_module.socket, "create_connection", forbidden)
        with pytest.raises(RuntimeContractError, match="cloud read-only"):
            ENTRIES[entry](a)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("damage", ["missing_ledger", "bad_json", "digest", "entry_escape", "contract", "node_override", "node_symlink"])
def test_invalid_install_blocks_injected_runner_before_any_command(managed_install, monkeypatch, entry, damage):
    i = managed_install
    if damage == "missing_ledger":
        i.ledger_path.unlink()
    elif damage == "bad_json":
        i.ledger_path.write_text("{")
    elif damage == "digest":
        i.entry.write_bytes(b"tampered")
    elif damage == "entry_escape":
        outside = i.root.parent / "outside.mjs"
        outside.write_bytes(i.entry.read_bytes())
        i.entry.unlink()
        i.entry.symlink_to(outside)
    elif damage == "contract":
        i.ledger["contract"]["node"] = "22.0.0"
        i.save()
    else:
        foreign = i.root.parent / "foreign-node"
        foreign.write_bytes(i.node.read_bytes())
        foreign.chmod(0o700)
        if damage == "node_symlink":
            link = i.root.parent / "node-link"
            link.symlink_to(foreign)
            foreign = link
        i.config.node_bin = str(foreign)
    a = adapter_for(i, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter_module.os, "kill", forbidden)
    monkeypatch.setattr(adapter_module.socket, "create_connection", forbidden)
    with pytest.raises(RuntimeContractError):
        ENTRIES[entry](a)
    assert a._engine_proof is None
    assert not a.config.state_dir.exists()
    assert not a.root.exists()


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("bad", ["v22.23.3", "v24.22.2", "darwin/arm64"])
def test_wrong_node_metadata_only_runs_preflight_probes(managed_install, monkeypatch, entry, bad):
    calls = []
    def runner(argv, **kwargs):
        assert argv[0] == str(managed_install.node)
        assert argv[1:] in (["--version"], ["-p", "process.platform + '/' + process.arch"])
        calls.append(argv)
        output = (bad if "/" not in bad else "v24.22.1") if argv[1] == "--version" else bad
        return subprocess.CompletedProcess(argv, 0, output, "")
    a = adapter_for(managed_install, runner)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter_module.os, "kill", forbidden)
    monkeypatch.setattr(adapter_module.socket, "create_connection", forbidden)
    with pytest.raises(RuntimeContractError, match="managed Node must"):
        ENTRIES[entry](a)
    assert len(calls) == (2 if "/" in bad else 1)
    assert not a.config.state_dir.exists()
    assert not a.root.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_environment_node_override_rejected_even_with_correct_probe_answers(managed_install, symlink):
    i = managed_install
    foreign = i.root.parent / "foreign-node"
    foreign.write_bytes(i.node.read_bytes())
    foreign.chmod(0o700)
    configured = foreign
    if symlink:
        configured = i.root.parent / "node-link"
        configured.symlink_to(foreign)
    config = RuntimeConfig.from_env(ROOT, env={
        "SAM_PROFILE": "local", "SAM_NODE_BIN": str(configured), "SAM_OPENCLAW_ENTRY": str(i.entry),
    })
    probes = []
    a = adapter_module.OpenClawAdapter(StagingStore(i.root.parent / "missing.db"), config=config,
                                      runner=managed_runner(forbidden, calls=probes))
    with pytest.raises(RuntimeContractError, match="SAM_NODE_BIN"):
        a.run_agent_message("unused")
    assert probes == []


def test_proof_cached_and_verified_node_used_for_agent_and_health(managed_install, monkeypatch):
    i = managed_install
    link = i.root.parent / "node-alias"
    link.symlink_to(i.node)
    i.config.node_bin = str(link)
    commands, preflights = [], []
    def engine(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")
    a = adapter_for(i, managed_runner(engine, calls=i.calls))
    original = adapter_module.preflight_runtime
    def preflight(config, **kwargs):
        preflights.append(config)
        assert kwargs["runner"] is a._runner
        return original(config, **kwargs)
    monkeypatch.setattr(adapter_module, "preflight_runtime", preflight)
    first = a._require_engine_proof()
    assert a._require_engine_proof() is first
    a.run_agent_message("hello")
    a.ensure_runtime_ready()
    assert a._gateway_rpc_healthy({}, timeout=1)
    assert len(preflights) == 1 and len(i.calls) == 2
    assert len(commands) == 2
    assert all(cmd[0] == str(i.node) for cmd in commands)
    # Cloud rejection precedes even a previously cached local proof.
    a.config = replace(a.config, cloud_readonly=True)
    with pytest.raises(RuntimeContractError, match="cloud read-only"):
        a._require_engine_proof()


def prepare_module(config, monkeypatch):
    spec = importlib.util.spec_from_file_location("prepare_wiring", ROOT / "scripts/prepare-openclaw-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.RuntimeConfig, "from_env", classmethod(lambda cls, **kw: config))
    return module


def test_prepare_check_local_fields_and_no_writes(managed_install, monkeypatch, capsys):
    i = managed_install
    config = config_for(i)
    module = prepare_module(config, monkeypatch)
    monkeypatch.setattr(subprocess, "run", i.runner)
    monkeypatch.setattr(module, "OpenClawAdapter", forbidden)
    assert module.main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == (
        "openclaw_runtime_preflight_ok node=24.22.1 openclaw=2026.10.7 "
        f"commit={i.ledger['openclaw']['commit']} install_root={i.root}")
    assert len(i.calls) == 2
    assert not config.state_dir.exists()


@pytest.mark.parametrize("check", [True, False])
def test_prepare_cloud_never_probes_install_or_constructs_adapter(managed_install, monkeypatch, capsys, check):
    from app import runtime_contract
    config = replace(config_for(managed_install), cloud_readonly=True)
    module = prepare_module(config, monkeypatch)
    monkeypatch.setattr(runtime_contract, "_check_owned_directory", forbidden)
    monkeypatch.setattr(runtime_contract, "_read_ledger", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(module, "OpenClawAdapter", forbidden)
    if check:
        assert module.main(["--check"]) == 0
        assert capsys.readouterr().out.strip() == "openclaw_runtime_preflight_ok engine=disabled profile=cloud"
    else:
        with pytest.raises(SystemExit, match="cloud read-only profile must not prepare or start"):
            module.main([])
    assert not config.state_dir.exists()


@pytest.mark.parametrize("check", [True, False])
def test_prepare_bad_ledger_fails_before_assembly(managed_install, monkeypatch, check):
    config = config_for(managed_install)
    managed_install.ledger_path.unlink()
    module = prepare_module(config, monkeypatch)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(module, "OpenClawAdapter", forbidden)
    with pytest.raises(SystemExit, match="OpenClaw runtime preflight failed: .*ledger is missing") as exc:
        module.main(["--check"] if check else [])
    assert exc.value.code != 0
    assert not config.state_dir.exists()


def test_verified_node_used_to_spawn_gateway(managed_install, monkeypatch):
    i = managed_install
    link = i.root.parent / "node-alias"
    link.symlink_to(i.node)
    i.config.node_bin = str(link)
    commands = []
    class Process:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0
    def popen(argv, **kwargs):
        commands.append(argv)
        return Process()
    def engine(argv, **kwargs):
        assert argv[0] == str(i.node)
        assert argv[2:4] == ["gateway", "health"]
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")
    run = managed_runner(engine, calls=i.calls)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    a = adapter_for(i, run)
    monkeypatch.setattr(a, "_reclaim_or_reject_gateway_port", lambda env: False)
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(adapter_module.socket, "create_connection", lambda *a, **kw: Socket())
    try:
        a.ensure_runtime_ready()
        assert len(commands) == 1
        assert commands[0][:4] == [str(i.node), str(i.entry), "gateway", "run"]
        assert len(i.calls) == 2
    finally:
        adapter_module._stop_gateways()
