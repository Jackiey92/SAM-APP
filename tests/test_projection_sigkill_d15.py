"""D15-2: actual SIGKILL during DML and on both sides of durable publication."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import pickle
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app.facts_tree import git_format, projection_advance_service as service
from app.facts_tree import projection_read_session as sessions, projection_router as router
from app.facts_tree.git_repository import FactsGitRepository
from tests.test_projection_derived_advance import _contents
from tests.test_projection_fault_drills_d15 import add_operation, scoped_tree
from tests.test_projection_incremental import _append


PROJECT = Path(__file__).resolve().parents[1]
STAMP = "2026-11-01T00:00:00+00:00"
WORKER = "from tests.test_projection_sigkill_d15 import _worker; _worker()"


def _advance(repo, root, directory, scope, target):
    return service.advance_projection(repo, root, directory, expected_repo_id=scope["repo_id"],
        expected_company_id=scope["company_id"], target_commit=target, now=lambda: STAMP)


def _snapshot(root, scope):
    serving = router.load_current(root, **scope)
    with sessions.open_read_session(root, **scope, allow_degraded=False) as session:
        assert session.serving == serving
        identity = asdict(serving)
        del identity["db_path"]  # The successful oracle has its own directory.
        for connection in (session.standard, session.derived):
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        canonical = {table: session.standard.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for table in ("generation_meta", "record_canonical", "family_counts",
                          "fact_current_view", "placement_current_view")}
        assert len(canonical["record_canonical"]) == serving.record_count
        return identity, canonical, _contents(session.derived)


def _inventory(directory):
    return {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p.read_bytes()).hexdigest()}
            for p in sorted(Path(directory).iterdir()) if p.is_file()}


def _pointer(config):
    scope = config["scope"]
    return Path(config["root"]) / git_format.encode_id(scope["repo_id"]) / git_format.encode_id(
        scope["company_id"]) / "current.json"


def _pause(config, **evidence):
    evidence.update(stage=config["stage"], builder_pid=os.getpid())
    Path(config["evidence"]).write_text(json.dumps(evidence), encoding="utf-8")
    Path(config["gate"]).touch()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        time.sleep(0.05)
    raise AssertionError("parent did not SIGKILL the paused builder")


def _install_child_hooks(config):
    """Called only in the sacrificial interpreter; no production file is changed."""
    stage = config["stage"]
    if stage in ("standard-dml", "derived-dml"):
        original_connect = sqlite3.connect
        table = "record_canonical" if stage == "standard-dml" else "dw_fact_revision"
        prefix = ".gen-" if stage == "standard-dml" else ".derived-"

        class PartialConnection(sqlite3.Connection):
            def executemany(self, sql, parameters):
                if not " ".join(sql.split()).startswith("INSERT INTO " + table + " "):
                    return super().executemany(sql, parameters)
                batch = list(parameters)
                assert len(batch) > 2, "DML kill point needs a genuinely multirow batch"
                sent = len(batch) // 2
                assert 0 < sent < len(batch) and self.in_transaction
                for row in batch[:sent]:
                    self.execute(sql, row)
                visible = self.execute("SELECT count(*) FROM " + table).fetchone()[0]
                assert self.in_transaction
                if stage == "standard-dml":
                    assert self.execute("SELECT value FROM generation_meta WHERE key='status'").fetchone() is None
                _pause(config, temp=self.drill_path, table=table, batch_size=len(batch),
                       sent=sent, unissued=len(batch) - sent, writer_count=visible,
                       writer_in_transaction=self.in_transaction)
                raise AssertionError("unreachable after kill gate")

        def connect(database, *args, **kwargs):
            target = Path(database)
            if target.name.startswith(prefix) and ".sqlite.tmp-" in target.name:
                kwargs["factory"] = PartialConnection
                connection = original_connect(database, *args, **kwargs)
                connection.drill_path = str(target)
                return connection
            return original_connect(database, *args, **kwargs)

        sqlite3.connect = connect
    else:
        original_pointer = service.router._atomic_pointer

        def publish(path, pointer):
            assert Path(path) == _pointer(config)
            assert pointer["generation_id"] == config["new"]["generation_id"]
            if stage == "before-pointer":
                _pause(config, pointer_replaced=False)
            assert stage == "after-pointer"
            original_pointer(path, pointer)
            _pause(config, pointer_replaced=True)

        service.router._atomic_pointer = publish


def _worker():
    """Fresh-interpreter build or restart/repair entry, driven solely by argv/files."""
    mode, config_path = sys.argv[1:]
    config = json.loads(Path(config_path).read_text())
    repo = FactsGitRepository.open(config["repo"], installation_id="install-d15-kill", **config["scope"])
    if mode == "build":
        _install_child_hooks(config)
        try:
            _advance(repo, config["root"], config["directory"], config["scope"], config["target"])
        finally:
            Path(config["finally_marker"]).touch()
        raise AssertionError("builder completed without reaching its kill gate")
    assert mode == "restart"
    expected_name = "new" if config["stage"] == "after-pointer" else "old"
    with open(config[expected_name + "_oracle"], "rb") as stream:
        expected = pickle.load(stream)
    initial = _snapshot(config["root"], config["scope"])
    assert initial == expected, "restart selected a half, orphan or wrong generation"
    pointer = json.loads(_pointer(config).read_text())
    assert pointer["projected_commit"] == expected[0]["projected_commit"]
    assert pointer["generation_id"] == expected[0]["generation_id"]
    before = _inventory(config["directory"])
    report = _advance(repo, config["root"], config["directory"], config["scope"], config["target"])
    if expected_name == "new":
        assert report.already_current is True and report.published_commits == () and report.generations == ()
        assert report.from_commit == report.to_commit == config["target"]
        assert _inventory(config["directory"]) == before
    else:
        assert report.already_current is False and report.published_commits == (config["target"],)
        assert len(report.generations) == 1 and report.to_commit == config["target"]
    with open(config["new_oracle"], "rb") as stream:
        oracle = pickle.load(stream)
    repaired = _snapshot(config["root"], config["scope"])
    assert repaired == oracle, "repaired canonical/wide/FTS differ from the successful build"
    after = _inventory(config["directory"])
    for name, detail in config["orphans"].items():
        assert after[name] == detail, "unserved orphan changed or blocked recovery"
    result = {"stage": config["stage"], "restart_pid": os.getpid(),
              "initial_generation": initial[0]["generation_id"],
              "initial_commit": initial[0]["projected_commit"],
              "final_generation": repaired[0]["generation_id"], "final_commit": repaired[0]["projected_commit"],
              "record_count": repaired[0]["record_count"], "render_sha256": repaired[0]["render_sha256"],
              "integrity": "ok", "oracle_equal": True, "already_current": report.already_current,
              "published_commits": report.published_commits, "after_rebuild": after}
    Path(config["result"]).write_text(json.dumps(result), encoding="utf-8")


@pytest.fixture
def drill(tmp_path, request):
    scope = {"repo_id": "repo-d15-kill-" + request.node.name,
             "company_id": "acc-d15-kill-" + request.node.name}
    repo = FactsGitRepository.initialize(tmp_path / "facts.git", installation_id="install-d15-kill", **scope)
    genesis = repo.head_oid()
    tree = scoped_tree(scope, genesis)
    meta = git_format.record_path("repository_meta")
    tree[meta] = git_format.loads_record(repo.read_snapshot_at(genesis).records[0].content)
    old_commit = _append(repo, tree)
    changed = deepcopy(tree)
    frame = SimpleNamespace(repo=repo, scope=scope)
    for number in range(4):
        add_operation(frame, changed, "kill-" + str(number), terminal=True, fact_uid="fact-kill-" + str(number))
    new_commit = _append(repo, changed)
    root, directory = tmp_path / "root", tmp_path / "generations"
    _advance(repo, root, directory, scope, old_commit)
    old = _snapshot(root, scope)
    _advance(repo, tmp_path / "oracle-root", tmp_path / "oracle-generations", scope, new_commit)
    new = _snapshot(tmp_path / "oracle-root", scope)
    for label, value in (("old", old), ("new", new)):
        with (tmp_path / (label + "-oracle.pickle")).open("wb") as stream:
            pickle.dump(value, stream)
    return {"repo": str(repo.path), "scope": scope, "root": str(root), "directory": str(directory),
            "target": new_commit, "old": old[0], "new": new[0],
            "old_oracle": str(tmp_path / "old-oracle.pickle"), "new_oracle": str(tmp_path / "new-oracle.pickle"),
            "new_fact_revisions": len(new[2]["wide"]["fact_revision"]),
            **{name: str(tmp_path / name) for name in ("gate", "evidence", "finally_marker", "result")}}


def _assert_partial_from_independent_reader(config, evidence):
    temp = Path(evidence["temp"])
    assert temp.exists() and evidence["writer_in_transaction"] is True
    assert 0 < evidence["sent"] < evidence["batch_size"] and evidence["unissued"] > 0
    table = evidence["table"]
    with closing(sqlite3.connect(temp.as_uri() + "?mode=ro", uri=True, timeout=1)) as reader:
        tables = {row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        count = reader.execute("SELECT count(*) FROM " + table).fetchone()[0] if table in tables else 0
        committed_generation = (reader.execute("SELECT value FROM wide_meta WHERE key='generation_id'").fetchone()[0]
                                if "wide_meta" in tables else None)
    target = config["new"]["record_count"] if config["stage"] == "standard-dml" else config["new_fact_revisions"]
    assert count < evidence["writer_count"] < target
    prefix = "gen-" if config["stage"] == "standard-dml" else "derived-"
    final = Path(config["directory"]) / (prefix + config["new"]["generation_id"] + ".sqlite")
    assert not final.exists()
    if config["stage"] == "derived-dml":
        assert committed_generation == config["old"]["generation_id"]
    return {**evidence, "independent_reader_count": count, "independent_table_exists": table in tables,
            "expected_count": target,
            "independent_generation": committed_generation, "temp_bytes": temp.stat().st_size,
            "final_exists": final.exists()}


def _run_drill(drill, stage, tmp_path):
    config = {**drill, "stage": stage}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    log = tmp_path / "builder.log"
    with log.open("w") as stream:
        child = subprocess.Popen([sys.executable, "-c", WORKER, "build", str(config_path)],
                                 cwd=PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while not Path(config["gate"]).exists():
                assert child.poll() is None, log.read_text()
                assert time.monotonic() < deadline, "builder handshake timed out: " + log.read_text()
                time.sleep(0.02)
            evidence = json.loads(Path(config["evidence"]).read_text())
            assert child.poll() is None
            if stage in ("standard-dml", "derived-dml"):
                evidence = _assert_partial_from_independent_reader(config, evidence)
            else:
                for prefix, table in (("gen-", "generation_meta"), ("derived-", "wide_meta"), ("derived-", "fts_meta")):
                    filename = Path(config["directory"]) / (prefix + config["new"]["generation_id"] + ".sqlite")
                    with closing(sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True)) as reader:
                        assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                        meta = dict(reader.execute("SELECT key,value FROM " + table))
                        assert meta["status"] == "ready" and meta["generation_id"] == config["new"]["generation_id"]
            expected = config["new"] if stage == "after-pointer" else config["old"]
            assert router.load_current(config["root"], **config["scope"]).generation_id == expected["generation_id"]
            # conftest wraps Popen in bwrap: signal the actual Python PID from
            # the handshake, rather than mistaking its supervisor for the builder.
            if evidence["builder_pid"] == child.pid:
                child.kill()
            else:
                os.kill(evidence["builder_pid"], signal.SIGKILL)
            returncode = child.wait(timeout=10)
            assert returncode in (-signal.SIGKILL, 128 + signal.SIGKILL), log.read_text()
            assert not Path(config["finally_marker"]).exists(), "SIGKILL must leave no finally opportunity"
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    after_kill = _inventory(config["directory"])
    config["orphans"] = {name: detail for name, detail in after_kill.items() if name.startswith(".")}
    if stage in ("standard-dml", "derived-dml"):
        assert Path(evidence["temp"]).name in config["orphans"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    restarted = subprocess.run([sys.executable, "-c", WORKER, "restart", str(config_path)],
        cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    result = json.loads(Path(config["result"]).read_text())
    assert result["restart_pid"] != evidence["builder_pid"] and result["restart_pid"] != os.getpid()
    assert result["initial_generation"] == expected["generation_id"]
    print(json.dumps({"stage": stage, "kill_returncode": returncode, "gate_evidence": evidence,
                      "after_kill": after_kill, "restart": result}, ensure_ascii=False), flush=True)


def test_standard_dml_mid_transaction_sigkill(drill, tmp_path):
    _run_drill(drill, "standard-dml", tmp_path)


def test_derived_cow_dml_mid_transaction_sigkill(drill, tmp_path):
    _run_drill(drill, "derived-dml", tmp_path)


def test_ready_pair_before_pointer_sigkill(drill, tmp_path):
    _run_drill(drill, "before-pointer", tmp_path)


def test_durable_pointer_after_publish_sigkill(drill, tmp_path):
    _run_drill(drill, "after-pointer", tmp_path)
