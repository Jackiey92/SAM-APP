"""D15-3: real tmpfs exhaustion, pinned readers, and portable lock simulation."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import errno
import json
import os
from pathlib import Path
import pickle
import selectors
import sqlite3
import subprocess
import sys

import pytest

from app.facts_tree import git_format, projection_advance_service as service
from app.facts_tree import projection_read_session as sessions, projection_router as router
from app.facts_tree.git_repository import FactsGitRepository
from tests.test_projection_derived_advance import _contents
from tests.test_projection_fault_drills_d15 import scoped_tree
from tests.test_projection_incremental import _append
from tests.test_projection_sigkill_d15 import _advance, _inventory, _snapshot


PROJECT = Path(__file__).resolve().parents[1]
WORKER = "from tests.test_projection_resources_d15 import _worker; _worker()"
TABLES = ("generation_meta", "record_canonical", "family_counts",
          "fact_current_view", "placement_current_view")


@pytest.fixture
def resource_scene(tmp_path, request):
    scope = {"repo_id": "repo-d15-resource-" + request.node.name,
             "company_id": "acc-d15-resource-" + request.node.name}
    repo = FactsGitRepository.initialize(tmp_path / "facts.git",
        installation_id="install-d15-resource", **scope)
    genesis = repo.head_oid()
    tree = scoped_tree(scope, genesis)
    meta = git_format.record_path("repository_meta")
    tree[meta] = git_format.loads_record(repo.read_snapshot_at(genesis).records[0].content)
    old = _append(repo, tree)
    changed = deepcopy(tree)
    changed[git_format.record_path("fact_current", fact_uid="fact-1")]["status"] = "retracted"
    target = _append(repo, changed)
    config = {"repo": str(repo.path), "scope": scope, "old": old, "target": target,
              **{key: str(tmp_path / key) for key in
                 ("root", "directory", "oracle-root", "oracle-directory", "volume",
                  "ready", "release", "old-oracle", "result")}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return repo, config, path


def _pointer(config):
    scope = config["scope"]
    return Path(config["root"]) / git_format.encode_id(scope["repo_id"]) / git_format.encode_id(
        scope["company_id"]) / "current.json"


def _publish(repo, config, *, oracle=False, old=False):
    return _advance(repo, config["oracle-root"] if oracle else config["root"],
                    config["oracle-directory"] if oracle else config["directory"],
                    config["scope"], config["old"] if old else config["target"])


def _causes(error):
    result = []
    while error is not None:
        result.append(error)
        error = error.__cause__
    return result


def _run_worker(mode, config_path, *, wrapper=()):
    command = [*wrapper, sys.executable, "-c", WORKER, mode, str(config_path)]
    completed = subprocess.run(command, cwd=PROJECT, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=90)
    assert completed.returncode == 0, completed.stdout
    result = json.loads(completed.stdout)
    print(json.dumps(result), flush=True)
    return result


def _disk_full(repo, config, stage):
    directory = Path(config["volume"]) / "generations"
    config["directory"] = str(directory)
    _publish(repo, config, old=True)
    old = _snapshot(config["root"], config["scope"])
    _publish(repo, config, oracle=True)
    oracle = _snapshot(config["oracle-root"], config["scope"])
    if stage == "derived":
        # Make advance reuse a complete, unpublished standard component so only
        # the derived COW backup competes for the remaining filesystem space.
        service.adv1.build_standard_generation_for_commit(repo, config["target"], directory,
            expected_repo_id=config["scope"]["repo_id"],
            expected_company_id=config["scope"]["company_id"])
    before, pointer = _inventory(directory), _pointer(config).read_bytes()
    volume = Path(config["volume"])
    capacity = os.statvfs(volume)
    assert capacity.f_blocks * capacity.f_frsize == 1048576
    reserve = 8192 if stage == "standard" else 32768
    remaining = capacity.f_bavail * capacity.f_frsize - reserve
    assert remaining > 0
    filler = volume / "filler"
    with filler.open("wb", buffering=0) as stream:
        while remaining:
            written = stream.write(b"F" * min(65536, remaining))
            assert written > 0
            remaining -= written
        os.fsync(stream.fileno())
    assert os.statvfs(volume).f_bavail * capacity.f_frsize == reserve
    evidence = []

    def trace(frame, event, arg):
        if (event == "exception" and frame.f_code.co_name ==
                ("_write_records" if stage == "standard" else "build_incremental_derived")):
            error = arg[1]
            if isinstance(error, sqlite3.OperationalError) and error.sqlite_errorcode == sqlite3.SQLITE_FULL:
                evidence.append({"function": frame.f_code.co_name, "line": frame.f_lineno,
                                 "files": _inventory(directory)})
        return trace

    previous_trace = sys.gettrace()
    sys.settrace(trace)  # Observe the actual exception before finally cleans temp; inject nothing.
    try:
        with pytest.raises(service.AdvanceServiceError) as raised:
            _publish(repo, config)
    finally:
        sys.settrace(previous_trace)
    causes = _causes(raised.value)
    assert len(causes) == 3 and isinstance(causes[1], ValueError)
    assert isinstance(causes[2], sqlite3.OperationalError)
    assert causes[2].sqlite_errorcode == 13 == sqlite3.SQLITE_FULL
    assert causes[2].sqlite_errorname == "SQLITE_FULL"
    assert getattr(causes[2], "errno", None) is None
    assert len(evidence) == 1
    prefix = ".gen-" if stage == "standard" else ".derived-"
    temporary = {name: info for name, info in evidence[0]["files"].items() if name.startswith(prefix)}
    assert len(temporary) == 1
    assert next(iter(temporary.values()))["bytes"] == 0
    final_prefix = "gen-" if stage == "standard" else "derived-"
    assert final_prefix + oracle[0]["generation_id"] + ".sqlite" not in evidence[0]["files"]
    assert _pointer(config).read_bytes() == pointer
    assert _snapshot(config["root"], config["scope"]) == old
    assert _inventory(directory) == before  # Old hashes unchanged; no owned temp/partial final survives.
    probe = volume / "independent-enospc-probe"
    try:
        with probe.open("wb", buffering=0) as stream:
            with pytest.raises(OSError) as os_raised:
                while True:
                    assert stream.write(b"X" * 65536) > 0
        assert os_raised.value.errno == errno.ENOSPC == 28
    finally:
        probe.unlink(missing_ok=True)
    filler.unlink()
    report = _publish(repo, config)
    assert not report.already_current and report.published_commits == (config["target"],)
    assert _snapshot(config["root"], config["scope"]) == oracle
    for name, detail in before.items():
        assert _inventory(directory)[name] == detail
    return {"stage": stage, "capacity": 1048576, "reserve": reserve,
            "sqlite_errorcode": causes[2].sqlite_errorcode, "sqlite_errorname": "SQLITE_FULL",
            "independent_os_errno": os_raised.value.errno, "failure": evidence[0],
            "pointer_unchanged": True, "old_hashes_unchanged": True,
            "owned_temp_cleaned": True, "retry_oracle_equal": True}


def _opened_snapshot(session):
    identity = asdict(session.serving)
    del identity["db_path"]
    for connection in (session.standard, session.derived):
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    rows = {table: session.standard.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for table in TABLES}
    assert len(rows["record_canonical"]) == session.serving.record_count
    return identity, rows, _contents(session.derived)


def _held_reader(config):
    with open(config["old-oracle"], "rb") as stream:
        expected = pickle.load(stream)
    with sessions.open_read_session(config["root"], **config["scope"], allow_degraded=False) as session:
        before = _opened_snapshot(session)
        assert before == expected
        with open(config["ready"], "w") as ready:
            ready.write(json.dumps({"reader_pid": os.getpid(), **before[0]}) + "\n")
        with open(config["release"], "r") as release:
            with selectors.DefaultSelector() as waiter:
                waiter.register(release, selectors.EVENT_READ)
                assert waiter.select(30), "publisher did not release held reader"
            assert release.readline() == "release\n"
        after = _opened_snapshot(session)
        assert after == before
        standard = session.serving.db_path
        derived = str(Path(standard).with_name("derived-" + session.serving.generation_id + ".sqlite"))
        handles = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                name = os.readlink(fd)
                if name in (standard + " (deleted)", derived + " (deleted)"):
                    info = fd.stat()
                    assert info.st_nlink == 0
                    handles.append({"path": name, "nlink": info.st_nlink, "inode": info.st_ino})
            except FileNotFoundError:
                pass  # The directory iterator's own descriptor may already be closed.
        assert {item["path"] for item in handles} == {standard + " (deleted)", derived + " (deleted)"}
        return {"reader_pid": os.getpid(), **after[0], "canonical_rows": len(after[1]["record_canonical"]),
                "snapshot_unchanged": True, "integrity": ["ok", "ok"], "deleted_handles": handles}


def _worker():
    mode, path = sys.argv[1:]
    config = json.loads(Path(path).read_text())
    if mode == "reader":
        result = _held_reader(config)
    elif mode == "fresh":
        snapshot = _snapshot(config["root"], config["scope"])
        result = {"reader_pid": os.getpid(), **snapshot[0], "integrity": ["ok", "ok"]}
    else:
        assert mode in ("standard", "derived")
        repo = FactsGitRepository.open(config["repo"], installation_id="install-d15-resource", **config["scope"])
        result = _disk_full(repo, config, mode)
    Path(config["result"]).write_text(json.dumps(result))
    print(json.dumps(result), flush=True)


def _run_full(scene, stage):
    _, config, path = scene
    volume = Path(config["volume"])
    volume.mkdir()
    # Mount capability is actually exercised in a disposable user/mount namespace.
    probe = subprocess.run(["unshare", "-Urm", "sh", "-c",
        'mount -t tmpfs -o size=1m tmpfs "$1" && stat -f -c "%S %b" "$1"', "probe", str(volume)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    assert probe.returncode == 0, probe.stdout
    block, count = map(int, probe.stdout.split())
    assert block * count == 1048576
    sqlite_temp = path.parent / "sqlite-temp"
    sqlite_temp.mkdir()
    # Keep SQLite's auxiliary temp files in this case's writable directory;
    # the inherited pytest TMPDIR is outside the inner namespace's bind mount.
    wrapper = ["/usr/bin/bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/",
               "--bind", str(path.parent), str(path.parent), "--proc", "/proc", "--dev", "/dev",
               "--setenv", "TMPDIR", str(sqlite_temp), "--setenv", "SQLITE_TMPDIR", str(sqlite_temp),
               "--size", "1048576", "--tmpfs", str(volume), "--chdir", str(PROJECT), "--"]
    result = _run_worker(stage, path, wrapper=wrapper)
    assert result["retry_oracle_equal"] and result["independent_os_errno"] == 28


def test_real_tmpfs_full_standard_retry(resource_scene):
    """SQLite code 13 is not POSIX errno 28; both are measured, with different meanings."""
    _run_full(resource_scene, "standard")


def test_real_tmpfs_full_derived_cow_retry(resource_scene):
    """SQLite code 13 is not POSIX errno 28; both are measured, with different meanings."""
    _run_full(resource_scene, "derived")


def test_old_reader_survives_publish_and_unlink(resource_scene):
    """FIFO barriers span publish/unlink; fixture unlink does not imply production retirement GC."""
    repo, config, path = resource_scene
    _publish(repo, config, old=True)
    old = _snapshot(config["root"], config["scope"])
    with open(config["old-oracle"], "wb") as stream:
        pickle.dump(old, stream)
    for key in ("ready", "release"):
        os.mkfifo(config[key], 0o600)
    with ExitStack() as cleanup:
        ready = os.open(config["ready"], os.O_RDWR | os.O_NONBLOCK)
        cleanup.callback(os.close, ready)
        release = os.open(config["release"], os.O_RDWR | os.O_NONBLOCK)
        cleanup.callback(os.close, release)
        child = subprocess.Popen([sys.executable, "-c", WORKER, "reader", str(path)],
            cwd=PROJECT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with selectors.DefaultSelector() as waiter:
                waiter.register(ready, selectors.EVENT_READ)
                waiter.register(child.stdout, selectors.EVENT_READ)
                readable = {key.fd for key, _ in waiter.select(30)}
            if ready not in readable and child.stdout.fileno() in readable:
                output, _ = child.communicate(timeout=10)
                if child.returncode != 0:
                    pytest.fail(output)
            assert ready in readable, "reader did not reach ready gate"
            message = os.read(ready, 4096)
            held = json.loads(message)
            assert held["generation_id"] == old[0]["generation_id"]
            old_pointer_inode = _pointer(config).stat().st_ino
            _publish(repo, config)
            assert _pointer(config).stat().st_ino != old_pointer_inode
            fresh = _run_worker("fresh", path)
            assert fresh["reader_pid"] != held["reader_pid"]
            assert fresh["generation_id"] != held["generation_id"]
            assert fresh["projected_commit"] == config["target"] and fresh["integrity"] == ["ok", "ok"]
            directory = Path(config["directory"])
            for prefix in ("gen-", "derived-"):
                (directory / (prefix + held["generation_id"] + ".sqlite")).unlink()
            os.write(release, b"release\n")
            output, _ = child.communicate(timeout=30)
            assert child.returncode == 0, output
            finished = json.loads(output)
            assert finished["reader_pid"] == held["reader_pid"]
            assert finished["generation_id"] == old[0]["generation_id"]
            assert finished["projected_commit"] == config["old"]
            assert finished["record_count"] == finished["canonical_rows"] == old[0]["record_count"]
            print(json.dumps({"held_reader": finished, "fresh_reader": fresh}), flush=True)
        finally:
            if child.poll() is None:
                os.write(release, b"release\n")
                try:
                    child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=10)


def test_simulated_windows_eacces_preserves_current(resource_scene, monkeypatch):
    """逻辑模拟，未在仓库内做 Windows 真机交互。

    2026-11-01 native Windows Python 3.13.14 / SQLite 3.50.4 experiment:
    both an open SQLite connection and external CreateFileW(dwShareMode=0)
    made os.replace raise PermissionError(errno=13, winerror=5); both succeeded
    after closing the handle. This portable test models the external exclusive lock.
    """
    repo, config, _ = resource_scene
    _publish(repo, config, old=True)
    old = _snapshot(config["root"], config["scope"])
    before, pointer = _inventory(config["directory"]), _pointer(config).read_bytes()
    original, calls = router.os.replace, []

    def locked(source, target):
        if Path(target) == _pointer(config):
            assert json.loads(Path(source).read_text())["projected_commit"] == config["target"]
            calls.append(Path(source))
            raise PermissionError(errno.EACCES, "simulated external exclusive Windows handle", str(target))
        return original(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(router.os, "replace", locked)
        with pytest.raises(service.AdvanceServiceError) as raised:
            _publish(repo, config)
    causes = _causes(raised.value)
    assert isinstance(causes[-1], PermissionError) and causes[-1].errno == 13 == errno.EACCES
    assert len(calls) == 1 and not calls[0].exists()
    assert _pointer(config).read_bytes() == pointer
    assert _snapshot(config["root"], config["scope"]) == old
    assert not list(Path(config["root"]).rglob(".current.json.tmp-*"))
    after = _inventory(config["directory"])
    assert not any(".tmp-" in name for name in after)
    assert all(after[name] == value for name, value in before.items())
    report = _publish(repo, config)
    assert not report.already_current and report.published_commits == (config["target"],)
    _publish(repo, config, oracle=True)
    assert _snapshot(config["root"], config["scope"]) == _snapshot(config["oracle-root"], config["scope"])
    print(json.dumps({"label": "simulated_external_windows_lock", "errno": causes[-1].errno,
                      "pointer_unchanged": True, "temp_cleaned": True, "retry_oracle_equal": True}), flush=True)
