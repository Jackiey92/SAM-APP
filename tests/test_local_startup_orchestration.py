from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.harness.runtime.openclaw_adapter import OpenClawAdapter
from app.harness.staging import StagingStore
from app.runtime_config import RuntimeConfig
from tests.runtime_install import managed_config, managed_runner


@pytest.fixture(autouse=True)
def synthetic_node_probes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", managed_runner(subprocess.run))

ROOT = Path(__file__).resolve().parents[1]






def test_readonly_standard_entry_skips_node_key_and_gateway(tmp_path):
    fake = tmp_path / "python3"
    calls = tmp_path / "calls"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    env = {"PATH": str(tmp_path) + ":/usr/bin:/bin", "HOME": str(tmp_path), "SAM_DATA_ROOT": str(tmp_path / "data"),
           "SAM_CLOUD_READONLY": "1", "CALLS": str(calls)}
    result = subprocess.run(["bash", "scripts/run.sh"], cwd=ROOT, env=env, capture_output=True)
    assert result.returncode == 0
    assert calls.read_text().splitlines() == ["webapp/app.py"]


def test_full_entry_orders_prepare_then_flask(tmp_path):
    fake = tmp_path / "python3"
    calls = tmp_path / "calls"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    node = tmp_path / "node"
    node.write_text('#!/bin/sh\necho v24.21.0\n')
    node.chmod(0o755)
    env = {"PATH": str(tmp_path) + ":/usr/bin:/bin", "HOME": str(tmp_path), "SAM_DATA_ROOT": str(tmp_path / "data"),
           "SAM_MEMORY_ROOT_URI": "sam://test", "SAM_NODE_BIN": str(node),
           "SAM_LEADER_MODEL_API_KEY": "test", "CALLS": str(calls)}
    result = subprocess.run(["bash", "scripts/run.sh"], cwd=ROOT, env=env, capture_output=True)
    assert result.returncode == 0
    lines = calls.read_text().splitlines()
    assert lines[0].startswith('-c from app.db.database import validate_schema;')
    assert lines[1:] == ["scripts/prepare-openclaw-runtime.py", "webapp/app.py"]












def adapter(tmp_path):
    config = RuntimeConfig.from_env(project_root=ROOT)
    config = replace(config, state_dir=tmp_path / "state", config_path=tmp_path / "state/openclaw.json")
    return OpenClawAdapter(StagingStore(tmp_path / "missing.db"), config=managed_config(config, tmp_path))


@pytest.mark.parametrize("cmdline,expected", [
    ("openclaw-gateway" + "\0" * 4096, True),
    ("openclaw-gateway\0other", False),
    ("not-openclaw-gateway\0", False),
])
def test_gateway_title_nul_padding(tmp_path, cmdline, expected):
    assert adapter(tmp_path)._gateway_cmdline_is_ours(cmdline) is expected


def test_gateway_full_command_requires_exact_entry_and_arguments(tmp_path):
    a = adapter(tmp_path)
    cmd = f"node\0{a.config.openclaw_entry}\0gateway\0run\0"
    assert a._gateway_cmdline_is_ours(cmd)
    assert not a._gateway_cmdline_is_ours(cmd.replace("openclaw.mjs", "openclaw.mjs-forged"))


def test_owner_file_private_atomic_and_rejects_symlink_or_writable_state(tmp_path):
    a = adapter(tmp_path)
    a._write_gateway_owner(SimpleNamespace(pid=os.getpid()), {})
    assert a._gateway_owner_path.stat().st_mode & 0o777 == 0o600
    assert a._read_gateway_owner()["start_ticks"] == a._process_start_ticks(os.getpid())
    a._gateway_owner_path.chmod(0o644)
    assert a._read_gateway_owner() is None
    a._gateway_owner_path.chmod(0o600)
    a.config.state_dir.chmod(0o777)
    assert a._read_gateway_owner() is None
    a.config.state_dir.chmod(0o700)
    target = tmp_path / "record"
    a._gateway_owner_path.rename(target)
    a._gateway_owner_path.symlink_to(target)
    assert a._read_gateway_owner() is None


def test_owner_binds_process_start_uid_executable_cwd_and_listener(tmp_path, monkeypatch):
    import app.harness.runtime.openclaw_adapter as module
    a = adapter(tmp_path)
    a.root = Path.cwd()
    a.config = replace(a.config, gateway_port=18890)
    # Keep real PID/start/UID proofs; fixture the executable and network tables.
    tcp, tcp6, fds = tmp_path / "tcp", tmp_path / "tcp6", tmp_path / "fd"
    tcp.write_text("header\n0: 0100007F:49CA 00000000:0000 0A 0 0 0 0 0 12345\n")
    tcp6.write_text("header\n")
    fds.mkdir()
    fd = fds / "7"
    fd.symlink_to("socket:[12345]")
    proc = tmp_path / "process"
    proc.mkdir()
    (proc / "exe").symlink_to(a.config.node_bin)
    (proc / "cwd").symlink_to(a.root, target_is_directory=True)
    (proc / "cmdline").write_bytes(b"openclaw-gateway\0")
    real_path = Path
    redirects = {"/proc/net/tcp": tcp, "/proc/net/tcp6": tcp6, f"/proc/{os.getpid()}/fd": fds,
                 f"/proc/{os.getpid()}": proc}
    monkeypatch.setattr(module, "Path", lambda value: redirects.get(str(value), real_path(value)))
    a._write_gateway_owner(SimpleNamespace(pid=os.getpid()), {})
    owner = a._read_gateway_owner()
    monkeypatch.setattr(a, "_gateway_cmdline_is_ours", lambda cmd: True)
    assert a._owner_is_sam_gateway(owner)
    assert not a._owner_is_sam_gateway({**owner, "start_ticks": "stale"})
    assert not a._owner_is_sam_gateway({**owner, "uid": -1})
    assert not a._owner_is_sam_gateway({**owner, "state_dir": "/other"})
    (proc / "exe").unlink()
    (proc / "exe").symlink_to(sys.executable)
    assert not a._owner_is_sam_gateway(owner)
    (proc / "exe").unlink()
    (proc / "exe").symlink_to(a.config.node_bin)
    fd.unlink()
    assert not a._owner_is_sam_gateway(owner)


@pytest.mark.parametrize("healthy", [True, False])
def test_resident_gateway_never_respawned_or_killed(tmp_path, monkeypatch, healthy):
    a = adapter(tmp_path)
    monkeypatch.setattr(a, "_gateway_port_open", lambda: True)
    monkeypatch.setattr(a, "_read_gateway_owner", lambda: {"fingerprint": a._gateway_fingerprint({})})
    monkeypatch.setattr(a, "_owner_is_sam_gateway", lambda owner: True)
    monkeypatch.setattr(a, "_gateway_rpc_healthy", lambda *args, **kw: healthy)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kw: pytest.fail("respawned resident gateway"))
    monkeypatch.setattr(a, "_stop_owned_orphan", lambda *args: pytest.fail("killed resident gateway"))
    if healthy:
        a._ensure_gateway({}, timeout=1)
    else:
        with pytest.raises(RuntimeError, match="refusing to restart"):
            a._ensure_gateway({}, timeout=1)


def test_config_mismatch_rejected_without_restart(tmp_path, monkeypatch):
    a = adapter(tmp_path)
    monkeypatch.setattr(a, "_gateway_port_open", lambda: True)
    monkeypatch.setattr(a, "_read_gateway_owner", lambda: {"fingerprint": "old"})
    monkeypatch.setattr(a, "_owner_is_sam_gateway", lambda owner: True)
    with pytest.raises(RuntimeError, match="configuration differs"):
        a._reclaim_or_reject_gateway_port({})


@pytest.mark.parametrize('mismatch', [None, 'full_launch', 'uid', 'cwd', 'environ', 'environ_missing', 'environ_permission', 'cmdline', 'listener', 'unsafe_owner'])
def test_missing_owner_reclaimed_only_with_all_process_proofs(tmp_path, monkeypatch, mismatch):
    import app.harness.runtime.openclaw_adapter as module
    a = adapter(tmp_path)
    # Validate the synthetic install before process-identity-only /proc and UID mocks.
    a._require_engine_proof()
    proc_root = tmp_path / 'proc'
    proc = proc_root / '45678'
    proc.mkdir(parents=True)
    (proc / 'cwd').symlink_to(a.root if mismatch != 'cwd' else tmp_path, target_is_directory=True)
    (proc / 'cmdline').write_bytes(b'openclaw-gateway\0\0' if mismatch != 'cmdline' else b'foreign-gateway\0')
    if mismatch == 'full_launch':
        (proc / 'cmdline').write_bytes(f'node\0{a.config.openclaw_entry}\0gateway\0run\0'.encode())
    state = a.config.state_dir if mismatch != 'environ' else tmp_path / 'other-state'
    (proc / 'environ').write_bytes(b'OTHER=value\0SAM_OPENCLAW_STATE_DIR=' + os.fsencode(state) + b'\0')
    if mismatch == 'environ_missing':
        (proc / 'environ').unlink()
    if mismatch == 'unsafe_owner':
        a.config.state_dir.mkdir()
        a._gateway_owner_path.write_text('not a valid owner')
    real_path = Path
    if mismatch == 'environ_permission':
        read_bytes = real_path.read_bytes
        def restricted_read(path):
            if path == proc / 'environ':
                raise PermissionError('environ access denied')
            return read_bytes(path)
        monkeypatch.setattr(real_path, 'read_bytes', restricted_read)
    def path(value):
        value = str(value)
        if value == '/proc' or value.startswith('/proc/'):
            return real_path(str(proc_root) + value.removeprefix('/proc'))
        return real_path(value)
    monkeypatch.setattr(module, 'Path', path)
    monkeypatch.setattr(a, '_process_start_ticks', lambda pid: '123')
    monkeypatch.setattr(a, '_pid_listens_on_gateway_port', lambda pid: mismatch != 'listener')
    if mismatch == 'uid':
        uid = os.getuid()
        monkeypatch.setattr(module.os, 'getuid', lambda: uid + 1)
    listening, signals = {45678}, []
    monkeypatch.setattr(a, '_gateway_port_open', lambda: bool(listening))
    def kill(pid, sig):
        signals.append((pid, sig))
        listening.discard(pid)
    monkeypatch.setattr(module.os, 'kill', kill)
    if mismatch in (None, 'full_launch'):
        assert a._reclaim_or_reject_gateway_port() is False
        assert not listening
        assert signals == [(45678, module.signal.SIGTERM)]
    else:
        with pytest.raises(RuntimeError, match='unverified process'):
            a._reclaim_or_reject_gateway_port()
        assert listening and not signals


@pytest.mark.parametrize('pid_reused', [False, True])
def test_orphan_escalation_revalidates_pid_before_kill(tmp_path, monkeypatch, pid_reused):
    import app.harness.runtime.openclaw_adapter as module
    a = adapter(tmp_path)
    listening, signals = [True], []
    monkeypatch.setattr(a, '_gateway_port_open', lambda: listening[0])
    monkeypatch.setattr(a, '_orphan_pid_is_sam_gateway', lambda pid: True)
    monkeypatch.setattr(a, '_process_start_ticks', lambda pid: 'new' if pid_reused and signals else '123')
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    def kill(pid, sig):
        signals.append(sig)
        if sig == module.signal.SIGKILL:
            listening[0] = False
    monkeypatch.setattr(module.os, 'kill', kill)
    if pid_reused:
        with pytest.raises(RuntimeError, match='unverified process'):
            a._stop_owned_orphan({'pid': 45678, 'start_ticks': '123'})
        assert signals == [module.signal.SIGTERM]
    else:
        a._stop_owned_orphan({'pid': 45678, 'start_ticks': '123'})
        assert signals == [module.signal.SIGTERM, module.signal.SIGKILL]
        assert not listening[0]


def test_retired_backend_fails_before_preparing_or_loading_secrets(tmp_path):
    calls = tmp_path / 'calls'
    fake = tmp_path / 'python3'
    fake.write_text('#!/bin/sh\necho invoked >> "$CALLS"\n')
    fake.chmod(0o755)
    env = {'PATH': str(tmp_path) + ':/usr/bin:/bin', 'HOME': str(tmp_path), 'SAM_DATA_ROOT': str(tmp_path / 'data'),
           'SAM_MEMORY_BACKEND': 'ov', 'CALLS': str(calls)}
    result = subprocess.run(['bash', 'scripts/run.sh'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert 'OV 已摘除' in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize('gate', ['0', '1'])
def test_real_readonly_script_entry_bootstraps_without_project_pythonpath(tmp_path, gate):
    """Execute the shipped file entry, not an import that already finds app/."""
    from app.cloud_snapshot import initialize_snapshot
    initialize_snapshot(tmp_path / 'cloud-snapshot.db')
    env = dict(PATH=str(Path(sys.executable).parent)+':/usr/bin:/bin', HOME=str(tmp_path),
        SAM_CLOUD_READONLY='1', SAM_PROFILE='cloud', SAM_FACTS_TREE=gate,
        SAM_DATA_ROOT=str(tmp_path), SAM_MAIN_DB=str(tmp_path/'app.db'),
        SAM_APP_DB=str(tmp_path/'app.db'), SAM_2A_ROOT=str(tmp_path/'2a'),
        HOST='127.0.0.1', PORT='0')
    # No inherited credentials, project PYTHONPATH or gateway configuration.
    log = tmp_path / 'readonly-startup.log'
    import time
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, 'webapp/app.py'], cwd=ROOT,
            env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+15
            while process.poll() is None and time.monotonic()<deadline:
                if 'Running on http://127.0.0.1:' in log.read_text(): break
                time.sleep(.05)
            text = log.read_text()
            assert process.poll() is None and 'Running on http://127.0.0.1:' in text, text
            assert not (tmp_path/'app.db').exists()
        finally:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
