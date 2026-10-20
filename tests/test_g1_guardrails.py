"""G1 regressions: migrations/configuration/isolation, no customer writes."""
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest
from app.db.database import connect, init_db, validate_schema, SCHEMA_VERSION
from tests.isolation_guard import self_check, data_keys

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)


def test_no_data_configuration_rejects_connect(monkeypatch):
    monkeypatch.delenv('SAM_DATA_ROOT', raising=False)
    with pytest.raises(RuntimeError, match='SAM_DATA_ROOT'):
        connect()


def test_unconfigured_standard_startup_is_rejected():
    env = dict(os.environ)
    env.pop('SAM_DATA_ROOT', None)
    result = subprocess.run(['bash', 'scripts/run.sh'], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert 'Missing SAM_DATA_ROOT' in result.stderr


def test_empty_profile_rejects_unconfigured_runtime(tmp_path):
    from app.runtime_config import RuntimeConfig
    with pytest.raises(RuntimeError, match='SAM_DATA_ROOT'):
        RuntimeConfig.from_env(tmp_path, env={'SAM_CLOUD_READONLY': '1'})


def test_migrate_requires_explicit_db():
    result = run('-m', 'app.db.migrate')
    assert result.returncode == 2
    assert '--db' in result.stderr


def test_migration_cli_twice_is_identical(tmp_path):
    db = tmp_path / 'explicit.db'
    assert run('-m', 'app.db.migrate', '--db', str(db)).returncode == 0
    before = db.read_bytes()
    mtime = db.stat().st_mtime_ns
    with sqlite3.connect(db) as conn:
        dump = list(conn.iterdump())
    assert run('-m', 'app.db.migrate', '--db', str(db)).returncode == 0
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == dump
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_missing_or_old_schema_validation_never_creates_or_alters(tmp_path):
    db = tmp_path / 'missing.db'
    with pytest.raises(RuntimeError, match='app.db.migrate --db'):
        validate_schema(db)
    assert not db.exists()
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE legacy(value TEXT)')
    before = db.read_bytes(), db.stat().st_mtime_ns
    with pytest.raises(RuntimeError, match='Schema version'):
        validate_schema(db)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_application_import_with_legacy_db_does_not_mutate(tmp_path):
    db = tmp_path / 'legacy.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE legacy(value TEXT)')
    before = db.read_bytes(), db.stat().st_mtime_ns
    env = dict(os.environ, SAM_MAIN_DB=str(db), SAM_DATA_ROOT=str(tmp_path))
    result = run('-c', 'import webapp.app', env=env)
    assert result.returncode != 0
    assert 'app.db.migrate --db' in result.stderr
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_application_import_with_current_schema_is_readonly(tmp_path):
    db = tmp_path / 'current.db'
    assert run('-m', 'app.db.migrate', '--db', str(db)).returncode == 0
    before = db.read_bytes(), db.stat().st_mtime_ns
    env = dict(os.environ, SAM_MAIN_DB=str(db), SAM_DATA_ROOT=str(tmp_path))
    result = run('-c', 'import webapp.app', env=env)
    assert result.returncode == 0, result.stderr
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_isolation_environment_self_check_and_inheritance():
    self_check(os.environ, os.environ['SAM_TEST_ROOT'])
    script = 'import os,json; print(json.dumps({k:os.environ[k] for k in ' + repr(sorted(data_keys(os.environ))) + '}))'
    import json
    result = run('-c', script)
    assert result.returncode == 0, result.stderr
    self_check(json.loads(result.stdout), os.environ['SAM_TEST_ROOT'])


@pytest.mark.parametrize('key', ['SAM_MAIN_DB', 'SAM_2A_ROOT', 'SAM_OTHER_DB'])
def test_self_check_rejects_unisolated_data_key(key):
    env = dict(os.environ, **{key: '/tmp/sam-m5/main.db'})
    with pytest.raises(RuntimeError, match=key):
        self_check(env, os.environ['SAM_TEST_ROOT'])


@pytest.mark.parametrize('path', [str(ROOT / 'data/app.db'), '/tmp/sam-m5/main.db', '/home/jakki/g1-never-create'])
def test_parent_write_audit_denies_protected_and_non_temporary(path):
    with pytest.raises(PermissionError, match='pytest refused'):
        open(path, 'ab')


@pytest.mark.parametrize('path', [str(ROOT / 'data/app.db'), '/tmp/sam-m5/main.db'])
def test_native_and_python_children_cannot_open_protected_db(path):
    # No shell redirection truncation: O_RDWR open only, even if a guard regresses.
    script = 'import os; os.close(os.open(' + repr(path) + ',os.O_RDWR))'
    result = run('-c', script)
    assert result.returncode != 0
    assert 'PermissionError' in result.stderr
    # Isolated Python ignores sitecustomize; kernel read-only mount still denies.
    result = run('-I', '-c', script)
    assert result.returncode != 0
    assert 'Read-only file system' in result.stderr


def test_production_gateway_connection_denied_parent_and_child():
    with socket.socket() as client, pytest.raises(PermissionError, match='18790'):
        client.connect(('127.0.0.1', 18790))
    result = run('-c', "import socket,sys; sys.audit('socket.connect',socket.socket(),('127.0.0.1',18790))")
    assert result.returncode != 0
    assert 'pytest refused production gateway' in result.stderr


def test_bootstrap_port_is_private_random():
    port = int(os.environ['SAM_OPENCLAW_GATEWAY_PORT'])
    assert port != 18790 and port > 0
    assert os.environ['OPENCLAW_GATEWAY_PORT'] == str(port)


def test_baseline_script_reads_without_changes(tmp_path):
    db = tmp_path / 'probe.db'
    init_db(db)
    before = db.read_bytes(), db.stat().st_mtime_ns
    result = run('scripts/db_baseline.py', str(db))
    assert result.returncode == 0, result.stderr
    import json
    baseline = json.loads(result.stdout)[0]
    assert baseline['sha256'] == hashlib.sha256(before[0]).hexdigest()
    assert baseline['rows']['source_files'] == 0
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_explicit_evidence_root_required(monkeypatch):
    from app.storage.archive_paths import evidence_root
    monkeypatch.delenv('SAM_2A_ROOT', raising=False)
    with pytest.raises(RuntimeError, match='SAM_2A_ROOT'):
        evidence_root()


def test_explicit_initializer_has_no_default_target():
    with pytest.raises(TypeError):
        init_db()


def test_migration_refuses_newer_version_without_changes(tmp_path):
    db = tmp_path / 'newer.db'
    with sqlite3.connect(db) as conn:
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION + 1}')
    before = db.read_bytes(), db.stat().st_mtime_ns
    with pytest.raises(RuntimeError, match='downgrade'):
        init_db(db)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_original_db_environment_path_is_protected_before_redirect(tmp_path):
    original = tmp_path / 'original.db'
    with sqlite3.connect(original) as conn:
        conn.execute('CREATE TABLE untouched(value TEXT)')
    before = original.read_bytes(), original.stat().st_mtime_ns
    env = dict(os.environ, SAM_CUSTOM_DB=str(original))
    script = '''
import os
import tests.conftest
from pathlib import Path
assert Path(os.environ['SAM_CUSTOM_DB']).is_relative_to(os.environ['SAM_TEST_ROOT'])
try:
    os.close(os.open(ORIGINAL, os.O_RDWR))
except PermissionError:
    pass
else:
    raise AssertionError('original environment DB was not protected')
'''.replace('ORIGINAL', repr(str(original)))
    result = run('-c', script, env=env)
    assert result.returncode == 0, result.stderr
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before


def test_readonly_baseline_refuses_live_wal(tmp_path):
    db = tmp_path / 'wal.db'
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE item(value TEXT)')
        conn.commit()
        result = run('scripts/db_baseline.py', str(db))
        assert result.returncode != 0
        assert 'live WAL' in result.stderr


def test_snapshot_migration_and_constructor_are_readonly_on_reopen(tmp_path):
    from app.cloud_snapshot import SnapshotStore
    db = tmp_path / 'snapshot.db'
    with pytest.raises(RuntimeError, match='--snapshot'):
        SnapshotStore(db)
    assert not db.exists()
    assert run('-m', 'app.db.migrate', '--db', str(db), '--snapshot').returncode == 0
    before = db.read_bytes(), db.stat().st_mtime_ns
    SnapshotStore(db)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert run('-m', 'app.db.migrate', '--db', str(db), '--snapshot').returncode == 0
    assert db.read_bytes() == before[0]


def test_explicit_material_root_hashes_each_file_without_writes(tmp_path):
    material = tmp_path / 'material'
    material.mkdir()
    (material / 'original.txt').write_bytes(b'original')
    before = (material / 'original.txt').stat().st_mtime_ns
    result = run('scripts/db_baseline.py', '--evidence-root', str(material))
    assert result.returncode == 0, result.stderr
    import json
    entries = json.loads(result.stdout)
    assert entries[0]['sha256'] == hashlib.sha256(b'original').hexdigest()
    assert entries[0]['mtime_ns'] == before
    assert (material / 'original.txt').stat().st_mtime_ns == before


def test_protected_descriptor_and_directory_relative_audit_are_denied():
    # Emit audit events only: never mutate even if the guard regresses.
    with (ROOT / 'data/app.db').open('rb') as stream:
        with pytest.raises(PermissionError, match='pytest refused'):
            sys.audit('os.chmod', stream.fileno(), 0o600, -1)
    fd = os.open('/tmp/sam-m5', os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError, match='pytest refused'):
            sys.audit('os.remove', 'main.db', fd)
    finally:
        os.close(fd)


def test_startup_rejects_missing_schema_before_gateway_preparation(tmp_path):
    node = tmp_path / 'node'
    node.write_text('#!/bin/sh\necho v24.21.0\n')
    node.chmod(0o755)
    (tmp_path / 'python3').symlink_to(sys.executable)
    state = tmp_path / 'never-prepare'
    env = dict(os.environ, SAM_DATA_ROOT=str(tmp_path / 'data'),
               SAM_MAIN_DB=str(tmp_path / 'missing.db'),
               SAM_NODE_BIN=str(node), SAM_LEADER_MODEL_API_KEY='synthetic',
               SAM_OPENCLAW_STATE_DIR=str(state),
               PATH=str(tmp_path) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['bash', 'scripts/run.sh'], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Database is missing' in result.stderr and 'app.db.migrate --db' in result.stderr
    assert not state.exists()
    assert not (tmp_path / 'missing.db').exists()
