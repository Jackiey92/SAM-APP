"""Acceptance lifecycle tests use fake process groups, never models/OV."""
from contextlib import nullcontext
from dataclasses import replace
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from app.runtime_config import RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]


def acceptance_module():
    spec = importlib.util.spec_from_file_location('check_2b_cleanup_test', ROOT / 'scripts/check_2b_e2e.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('phase', ['prepare', 'prepare_timeout', 'post_assertion', 'termination', 'unowned_listener'])
def test_main_finally_stops_own_groups_before_removing_private_state(tmp_path, monkeypatch, phase):
    module = acceptance_module()
    fixture = tmp_path / 'fixture.xlsx'
    fixture.write_bytes(b'acceptance fixture')
    config = RuntimeConfig.from_env(project_root=ROOT)
    config = replace(config, project_root=tmp_path, main_db=tmp_path / 'data/app.db',
                     objects_dir=tmp_path / 'data/2a/bin', harness_root=tmp_path / 'harness',
                     config_path=tmp_path / 'missing-config', gateway_port=18890)
    config.main_db.parent.mkdir(parents=True)
    # parse_staging is lazily created on the first real parser write.
    with sqlite3.connect(config.main_db) as conn:
        conn.execute('CREATE TABLE parse_staging(file_hash TEXT)')
    monkeypatch.setattr(RuntimeConfig, 'from_env', classmethod(lambda cls, **kwargs: config))
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'FIXTURE', fixture)
    report_path = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['check', '--report', str(report_path)])
    if phase == 'unowned_listener':
        monkeypatch.setattr('app.runtime_config.PROCESS_STOP_GRACE_SECONDS', 0)
    env = dict(os.environ, SAM_MEMORY_ROOT_URI='sam://test', SAM_LEADER_MODEL_API_KEY='fake-key')
    env.pop('SAM_CLOUD_READONLY', None)
    monkeypatch.setattr(module.os, 'environ', env)
    monkeypatch.setattr(module.socket, 'socket', lambda *args: nullcontext())
    listeners = {18789: set(), 18890: set(), 1933: {99999}}
    alive, processes, signals, events = set(), [], [], []
    class Process:
        def __init__(self, command, **kwargs):
            assert kwargs['start_new_session'] is True
            self.pid = 45000 + len(processes)
            self.command = command
            self.first_wait = True
            processes.append(self)
            if (phase.startswith('prepare') and len(processes) == 1) or command[-1] == 'webapp/app.py':
                alive.add(self.pid)
                listeners[18789].add(self.pid)
                listeners[18890].add(self.pid)
                if phase == 'unowned_listener':
                    listeners[18890].add(99998)
        def poll(self):
            # The Flask leader has ALREADY exited, but its gateway/worker
            # remains in the same group. Cleanup must not skip killpg.
            return 0
        def wait(self, timeout):
            if self.first_wait:
                self.first_wait = False
                if phase == 'prepare' and len(processes) == 1:
                    return 1
                if phase == 'prepare_timeout' and len(processes) == 1:
                    raise subprocess.TimeoutExpired(self.command, timeout)
            return 0
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    monkeypatch.setattr(module, 'process_group_alive', lambda pgid: pgid in alive)
    def listener_pids(port):
        events.append(('check', port))
        return set(listeners[port])
    monkeypatch.setattr(module, 'listener_pids', listener_pids)
    def killpg(pgid, sig):
        assert pgid in {process.pid for process in processes}
        signals.append((pgid, sig))
        events.append(('signal', sig))
        if pgid not in alive:
            raise ProcessLookupError()
        alive.remove(pgid)
        for port in (18789, 18890):
            listeners[port].discard(pgid)
    monkeypatch.setattr(module.os, 'killpg', killpg)
    class Memory:
        def delete(self, *args, **kwargs):
            assert not alive
        def query(self, **kwargs):
            return []
    monkeypatch.setattr('app.providers.memory_provider', lambda config: Memory())
    def http_json(base, path, payload=None, **kwargs):
        if phase == 'termination' and path == '/api/entity-roster':
            module.terminate_acceptance(signal.SIGTERM, None)
        if path == '/api/upload':
            return {'file_hash': hashlib.sha256(fixture.read_bytes()).hexdigest(), 'job_id': 'test-job'}
        if path.startswith('/api/parse-jobs/'):
            return {'job': {'status': 'done'}}
        return {}
    monkeypatch.setattr(module, 'http_json', http_json)
    real_rmtree = module.shutil.rmtree
    def rmtree(path, *args, **kwargs):
        assert not alive and not listeners[18789] and not listeners[18890]
        assert listeners[1933] == {99999}, 'resident OV must not be stopped'
        events.append(('rmtree', str(path)))
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(module.shutil, 'rmtree', rmtree)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    if phase == 'termination':
        with pytest.raises(SystemExit):
            module.main()
    else:
        assert module.main() == 1
    report = json.loads(report_path.read_text())
    assert report['status'] == 'failed'
    if phase == 'unowned_listener':
        assert 'private state retained' in report['cleanup_errors'][0]
        assert list((tmp_path / 'data').glob('acc-runtime-*'))
        assert listeners[18890] == {99998}
        assert not any(event[0] == 'rmtree' for event in events)
        assert not alive and listeners[1933] == {99999}
        assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
        return
    assert 'cleanup_errors' not in report
    assert report['private_runtime_clean']
    assert not list((tmp_path / 'data').glob('acc-runtime-*'))
    assert not alive and not listeners[18789] and not listeners[18890]
    assert listeners[1933] == {99999}
    assert any(sig == signal.SIGTERM for _, sig in signals)
    assert events[-1][0] == 'rmtree'
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
    if phase == 'post_assertion':
        assert report['error'].startswith('AssertionError:')
        assert report['after'] == {table: 0 for table in module.BUSINESS_TABLES}


def test_group_stop_escalates_after_exited_leader(tmp_path, monkeypatch):
    module = acceptance_module()
    alive, clock, signals = [True], [0.0], []
    class Process:
        pid = 45000
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(module, 'process_group_alive', lambda pgid: alive[0])
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    def killpg(pgid, sig):
        assert pgid == 45000
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive[0] = False
    monkeypatch.setattr(module.os, 'killpg', killpg)
    module.stop_process_groups([Process()])
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert not alive[0]


def test_busy_external_flask_is_not_killed(monkeypatch):
    module = acceptance_module()
    monkeypatch.setattr(module, 'listener_pids', lambda port: {99999})
    monkeypatch.setattr(module.os, 'kill', lambda *args: pytest.fail('foreign process killed'))
    with pytest.raises(RuntimeError, match='not started by this run'):
        module.check_flask_port_available()


def test_port_verification_never_kills_an_unowned_listener(monkeypatch):
    module = acceptance_module()
    monkeypatch.setattr(module, 'listener_pids', lambda port: {99999} if port == 18890 else set())
    monkeypatch.setattr(module.os, 'killpg', lambda *args: pytest.fail('unowned group killed'))
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(RuntimeError, match='private state retained'):
        module.wait_acceptance_ports_closed()


def test_process_group_alive_checks_workers_not_zombies(tmp_path, monkeypatch):
    module = acceptance_module()
    proc = tmp_path / 'proc'
    proc.mkdir()
    for pid, state, group in [(1, 'Z', 45000), (2, 'S', 45001), (3, 'S', 45000)]:
        directory = proc / str(pid)
        directory.mkdir()
        (directory / 'stat').write_text(f'{pid} (worker (name)) {state} 0 {group} 0')
    real_path = Path
    monkeypatch.setattr(module, 'Path', lambda value: proc if str(value) == '/proc' else real_path(value))
    assert module.process_group_alive(45000)
    (proc / '3/stat').write_text('3 (worker (name)) Z 0 45000 0')
    assert not module.process_group_alive(45000)
