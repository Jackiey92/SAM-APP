"""H slice disaster drills: five real SIGKILL points and one real ENOSPC.

Every child is a fresh sacrificial interpreter (``-c`` worker driven only by
a config JSON), killed with SIGKILL at one exact point of
``rebuild_readonly_projection``:

  point 1  mid standard record_canonical multirow DML, inside the txn
  point 2  after the standard file is durable, before build_projection_wide
  point 3  after wide, before build_projection_fts
  point 4  after FTS, before the joint pointer os.replace
  point 5  after the pointer os.replace, before process exit

A sixth drill starves a real 1 MiB tmpfs (user+mount namespace) and measures
BOTH sqlite SQLITE_FULL (code 13) in a builder frame and POSIX ENOSPC (28)
from an independent raw write probe. Nothing is simulated: the kill is a real
signal to the worker PID, the disk is a real tmpfs, and every restart runs in
a brand-new interpreter.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import pickle
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from app.facts_tree import git_format, projection_bundle as bundle_mod
from app.facts_tree import projection_router as router, recovery_projection as rp
from tests.test_facts_tree_recovery_projection import (
    INST, STAMP, bundle_content, make_case, physical_inventory, rebuild,
)

PROJECT = Path(__file__).resolve().parents[2]
WORKER = "from tests.disaster_drills.test_recovery_projection_h_drills import _worker; _worker()"
STAGES = ("point1-standard-dml", "point2-before-wide", "point3-before-fts",
          "point4-before-pointer", "point5-after-pointer")


# ---------------------------------------------------------------------------
# Scene + serializable H rebuild config
# ---------------------------------------------------------------------------


def _pointer(root, scope):
    return Path(root) / git_format.encode_id(scope["repo_id"]) / git_format.encode_id(
        scope["company_id"]) / "current.json"


@pytest.fixture
def kill_scene(tmp_path):
    ctx = make_case(tmp_path, "kill", "sha1")
    receipt = rebuild(ctx, projection_root=tmp_path / "oracle-root",
                      generations_dir=tmp_path / "oracle-gens",
                      report_dir=tmp_path / "oracle-reports")
    oracle = bundle_content(tmp_path / "oracle-root", ctx.scope)
    with (tmp_path / "oracle.pickle").open("wb") as stream:
        pickle.dump(oracle, stream)
    files = {
        "standard": ctx.gens / ("gen-" + receipt.standard.generation_id + ".sqlite"),
        "derived": ctx.gens / ("derived-" + receipt.standard.generation_id + ".sqlite"),
    }
    # The success oracle must exist in its own directory; the drill case starts empty.
    assert not ctx.root.exists() and not ctx.gens.exists() and not ctx.reports.exists()
    config = {
        "isolated": str(ctx.isolated), "head": ctx.head,
        "repo_id": ctx.scope["repo_id"], "company_id": ctx.scope["company_id"],
        "package": str(ctx.package), "root": str(ctx.root),
        "gens": str(ctx.gens), "reports": str(ctx.reports),
        "evidence": str(ctx.evidence), "case_id": "case-h-kill",
        "stamp": STAMP, "generation_id": receipt.standard.generation_id,
        "record_count": receipt.standard.record_count,
        "fts_record_count": len(oracle[2]["fts_rows"]),
        "oracle_pickle": str(tmp_path / "oracle.pickle"),
        "oracle_gens": str(tmp_path / "oracle-gens"),
        **{name: str(tmp_path / name) for name in
           ("gate", "evidence-file", "finally-marker", "result")},
    }
    return ctx, config, files


def _rebuild_kwargs(config):
    return dict(
        isolated_repository=Path(config["isolated"]), head_oid=config["head"],
        repo_id=config["repo_id"], installation_id=INST,
        company_id=config["company_id"], package_dir=Path(config["package"]),
        projection_root=Path(config["root"]), generations_dir=Path(config["gens"]),
        report_dir=Path(config["reports"]), version_evidence=Path(config["evidence"]),
        case_id=config["case_id"], now=lambda: config["stamp"])


# ---------------------------------------------------------------------------
# Sacrificial-interpreter hooks (test process only; no production file changes)
# ---------------------------------------------------------------------------


def _pause(config, **evidence):
    evidence.update(stage=config["stage"], builder_pid=os.getpid())
    Path(config["evidence-file"]).write_text(json.dumps(evidence), encoding="utf-8")
    Path(config["gate"]).touch()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        time.sleep(0.05)
    raise AssertionError("parent did not SIGKILL the paused recovery process")


def _install_kill_hooks(config):
    stage = config["stage"]
    if stage == "point1-standard-dml":
        original_connect = sqlite3.connect

        class PartialConnection(sqlite3.Connection):
            def executemany(self, sql, parameters):
                if not " ".join(sql.split()).startswith("INSERT INTO record_canonical "):
                    return super().executemany(sql, parameters)
                batch = list(parameters)
                assert len(batch) > 2, "kill point 1 needs a genuinely multirow batch"
                sent = len(batch) // 2
                assert 0 < sent < len(batch) and self.in_transaction
                for row in batch[:sent]:
                    self.execute(sql, row)
                visible = self.execute("SELECT count(*) FROM record_canonical").fetchone()[0]
                assert self.in_transaction
                assert self.execute(
                    "SELECT value FROM generation_meta WHERE key='status'").fetchone() is None
                _pause(config, temp=self.drill_path, batch_size=len(batch), sent=sent,
                       unissued=len(batch) - sent, writer_count=visible,
                       writer_in_transaction=self.in_transaction)
                raise AssertionError("unreachable after kill gate")

        def connect(database, *args, **kwargs):
            target = Path(database)
            if target.name.startswith(".gen-") and ".sqlite.tmp-" in target.name:
                kwargs["factory"] = PartialConnection
                connection = original_connect(database, *args, **kwargs)
                connection.drill_path = str(target)
                return connection
            return original_connect(database, *args, **kwargs)

        sqlite3.connect = connect
    elif stage == "point2-before-wide":
        original = rp.wide.build_projection_wide

        def before_wide(standard_db_path, *args, **kwargs):
            _pause(config, hook="build_projection_wide")
            return original(standard_db_path, *args, **kwargs)

        rp.wide.build_projection_wide = before_wide
    elif stage == "point3-before-fts":
        original = rp.fts.build_projection_fts

        def before_fts(standard_db_path, *args, **kwargs):
            _pause(config, hook="build_projection_fts")
            return original(standard_db_path, *args, **kwargs)

        rp.fts.build_projection_fts = before_fts
    else:
        assert stage in ("point4-before-pointer", "point5-after-pointer")
        original_pointer = rp.service.router._atomic_pointer

        def publish(path, pointer):
            assert Path(path) == _pointer(config["root"], config["scope_id"])
            assert pointer["generation_id"] == config["generation_id"]
            if stage == "point4-before-pointer":
                _pause(config, pointer_replaced=False)
            original_pointer(path, pointer)
            _pause(config, pointer_replaced=True)

        config["scope_id"] = {"repo_id": config["repo_id"], "company_id": config["company_id"]}
        rp.service.router._atomic_pointer = publish


# ---------------------------------------------------------------------------
# Worker: killed build, or fresh-interpreter restart
# ---------------------------------------------------------------------------


def _worker():
    mode, config_path = sys.argv[1:]
    config = json.loads(Path(config_path).read_text())
    if mode == "build":
        _install_kill_hooks(config)
        try:
            rp.rebuild_readonly_projection(**_rebuild_kwargs(config))
        finally:
            Path(config["finally-marker"]).touch()
        raise AssertionError("recovery completed without reaching its kill gate")

    assert mode == "restart"
    with open(config["oracle_pickle"], "rb") as stream:
        oracle = pickle.load(stream)
    pointer_path = _pointer(config["root"], {"repo_id": config["repo_id"],
                                             "company_id": config["company_id"]})
    if config["stage"] == "point5-after-pointer":
        initial = json.loads(pointer_path.read_text())
        assert {key: value for key, value in initial.items()
                if key in router._POINTER_KEYS and key != "db_path"} == {
            key: value for key, value in oracle[0].items() if key != "db_basename"}
        assert Path(initial["db_path"]).name == oracle[0]["db_basename"]
    else:
        assert not pointer_path.exists()

    receipt = rp.rebuild_readonly_projection(**_rebuild_kwargs(config))
    scope = {"repo_id": config["repo_id"], "company_id": config["company_id"]}
    content = bundle_content(Path(config["root"]), scope)
    assert content == oracle, "restart content differs from the independent success oracle"
    finals = physical_inventory(Path(config["gens"]))
    for name, detail in physical_inventory(Path(config["oracle_gens"])).items():
        assert finals[name] == detail
    for orphan in config.get("orphans", ()):  # killed-process temp remnants are inert
        assert finals.get(orphan) == config["orphans"][orphan]
    report_path = Path(config["reports"]) / rp._REPORT_NAME
    lines = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1]
    assert receipt.standard.generation_id == config["generation_id"]
    Path(config["result"]).write_text(json.dumps({
        "stage": config["stage"], "restart_pid": os.getpid(),
        "generation_id": receipt.standard.generation_id,
        "record_count": receipt.standard.record_count,
        "report_seq": [line["seq"] for line in lines],
        "files": sorted(finals), "integrity": "oracle-equal"}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Parent-side pause observers
# ---------------------------------------------------------------------------


def _final_ro_view(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("PRAGMA integrity_check").fetchone() == ("ok",), {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}


def _observe_pause(ctx, config, files, evidence):
    stage = config["stage"]
    pointer = _pointer(ctx.root, ctx.scope)
    if stage == "point1-standard-dml":
        temp = Path(evidence["temp"])
        assert temp.exists() and evidence["writer_in_transaction"] is True
        assert 0 < evidence["sent"] < evidence["batch_size"] and evidence["unissued"] > 0
        with closing(sqlite3.connect(temp.as_uri() + "?mode=ro", uri=True, timeout=1)) as reader:
            tables = {row[0] for row in reader.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            count = (reader.execute("SELECT count(*) FROM record_canonical").fetchone()[0]
                     if "record_canonical" in tables else 0)
        assert count < evidence["writer_count"] < config["record_count"]
        assert not files["standard"].exists() and not files["derived"].exists()
        assert not pointer.exists()
        return {"independent_reader_count": count, "temp_bytes": temp.stat().st_size}

    if stage == "point5-after-pointer":
        assert evidence["pointer_replaced"] is True and pointer.exists()
        with bundle_mod.open_bundle(ctx.root, repo_id=ctx.scope["repo_id"],
                                    company_id=ctx.scope["company_id"]) as opened:
            assert opened.serving.generation_id == config["generation_id"]
            assert opened.serving.projected_commit == ctx.head
            assert opened.standard.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert opened.derived.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return {"pointer": "durable", "fts_rows": config["fts_record_count"]}

    # Points 2-4: no pointer may exist before the joint gate completes.
    assert not pointer.exists()
    standard_ok, standard_tables = _final_ro_view(files["standard"])
    assert standard_ok and "record_canonical" in standard_tables
    if stage == "point2-before-wide":
        assert not files["derived"].exists()
        return {"hook": evidence["hook"]}

    derived_ok, derived_tables = _final_ro_view(files["derived"])
    assert derived_ok and "dw_fact_current" in derived_tables and "wide_meta" in derived_tables
    if stage == "point3-before-fts":
        assert "fts_records" not in derived_tables and "fts_meta" not in derived_tables
        return {"hook": evidence["hook"]}

    assert stage == "point4-before-pointer"
    assert evidence["pointer_replaced"] is False
    assert "fts_records" in derived_tables and "fts_meta" in derived_tables
    with closing(sqlite3.connect(files["derived"].as_uri() + "?mode=ro", uri=True)) as reader:
        assert dict(reader.execute("SELECT key,value FROM fts_meta"))["status"] == "ready"
        fts_count = reader.execute("SELECT count(*) FROM fts_records").fetchone()[0]
    assert fts_count == config["fts_record_count"]
    return {"fts_rows": fts_count}


# ---------------------------------------------------------------------------
# The five SIGKILL drills
# ---------------------------------------------------------------------------


def _run_sigkill(kill_scene, stage, tmp_path):
    ctx, config, files = kill_scene
    anchor_before = ctx.anchor.read_bytes()
    config = {**config, "stage": stage}
    config_path = tmp_path / ("config-" + stage + ".json")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    log = tmp_path / ("builder-" + stage + ".log")
    with log.open("w") as stream:
        child = subprocess.Popen(
            [sys.executable, "-c", WORKER, "build", str(config_path)],
            cwd=PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 40
            while not Path(config["gate"]).exists():
                assert child.poll() is None, log.read_text()
                assert time.monotonic() < deadline, "handshake timed out: " + log.read_text()
                time.sleep(0.02)
            evidence = json.loads(Path(config["evidence-file"]).read_text())
            assert child.poll() is None
            observation = _observe_pause(ctx, config, files, evidence)
            # conftest wraps Popen in bwrap: signal the real Python PID from the
            # handshake instead of mistaking the bwrap supervisor for the worker.
            if evidence["builder_pid"] == child.pid:
                child.kill()
            else:
                os.kill(evidence["builder_pid"], signal.SIGKILL)
            returncode = child.wait(timeout=10)
            assert returncode in (-signal.SIGKILL, 128 + signal.SIGKILL), log.read_text()
            assert not Path(config["finally-marker"]).exists(), "SIGKILL leaves no finally chance"
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)

    # Report append is after the joint gate + readback for every kill point.
    assert not (ctx.reports / rp._REPORT_NAME).exists()
    after_kill = physical_inventory(ctx.gens) if ctx.gens.exists() else {}
    orphans = {name: detail for name, detail in after_kill.items() if name.startswith(".")}
    if stage == "point1-standard-dml":
        assert Path(evidence["temp"]).name in orphans
    config["orphans"] = orphans
    config_path.write_text(json.dumps(config), encoding="utf-8")

    restarted = subprocess.run(
        [sys.executable, "-c", WORKER, "restart", str(config_path)],
        cwd=PROJECT, capture_output=True, text=True, timeout=90)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    result = json.loads(Path(config["result"]).read_text())
    assert result["restart_pid"] != evidence["builder_pid"] and result["restart_pid"] != os.getpid()
    assert ctx.anchor.read_bytes() == anchor_before
    surviving = [name for name in physical_inventory(ctx.gens) if name.startswith(".")]
    assert set(surviving) == set(orphans)
    print(json.dumps({"stage": stage, "kill_returncode": returncode,
                      "pause_observation": observation, "restart": result},
                     ensure_ascii=False), flush=True)


@pytest.mark.parametrize("stage", STAGES)
def test_h_recovery_five_real_sigkill_points(kill_scene, tmp_path, stage):
    _run_sigkill(kill_scene, stage, tmp_path)


# ---------------------------------------------------------------------------
# Real ENOSPC: 1 MiB tmpfs, SQLITE_FULL (13) and POSIX ENOSPC (28) measured
# ---------------------------------------------------------------------------


ENOSPC_WORKER = (
    "from tests.disaster_drills.test_recovery_projection_h_drills import _enospc_worker; "
    "_enospc_worker()")


def _enospc_worker():
    _, config_path = sys.argv[1], sys.argv[2]
    config = json.loads(Path(config_path).read_text())
    volume = Path(config["volume"])
    capacity = os.statvfs(volume)
    assert capacity.f_blocks * capacity.f_frsize == 1048576

    kwargs = _rebuild_kwargs(config)
    # Independent success content is produced outside the starved volume.
    oracle_kwargs = dict(kwargs, projection_root=Path(config["oracle_root"]),
                         generations_dir=Path(config["oracle_gens"]),
                         report_dir=Path(config["oracle_reports"]))
    rebuild_receipt = rp.rebuild_readonly_projection(**oracle_kwargs)
    scope = {"repo_id": config["repo_id"], "company_id": config["company_id"]}
    oracle = bundle_content(Path(config["oracle_root"]), scope)

    gens = Path(config["gens"])
    reserve = 8192
    remaining = os.statvfs(volume).f_bavail * capacity.f_frsize - reserve
    assert remaining > 0
    filler = volume / "filler"
    with filler.open("wb", buffering=0) as stream:
        while remaining:
            written = stream.write(b"F" * min(65536, remaining))
            assert written > 0
            remaining -= written
        os.fsync(stream.fileno())
    assert os.statvfs(volume).f_bavail * capacity.f_frsize == reserve
    anchor_before = Path(config["anchor"]).read_bytes()

    builder_frames = {"projection_generation.py", "projection_advance.py",
                      "projection_wide.py", "projection_fts.py"}
    seen = []

    def trace(frame, event, arg):
        if event == "exception" and Path(frame.f_code.co_filename).name in builder_frames:
            error = arg[1]
            if isinstance(error, sqlite3.OperationalError) and error.sqlite_errorcode == 13:
                seen.append({"function": frame.f_code.co_name,
                             "file": Path(frame.f_code.co_filename).name,
                             "line": frame.f_lineno, "sqlite_errorname": error.sqlite_errorname})
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)  # Observe only: nothing is injected into the builder.
    try:
        try:
            rp.rebuild_readonly_projection(**kwargs)
        except Exception as exc:  # surface the chain to the parent test
            chain = []
            cursor = exc
            while cursor is not None:
                chain.append({"type": type(cursor).__name__,
                              "sqlite_code": getattr(cursor, "sqlite_errorcode", None)})
                cursor = cursor.__cause__ or cursor.__context__
            failure = {"type": type(exc).__name__, "chain": chain}
        else:
            raise AssertionError("rebuild unexpectedly succeeded on a full tmpfs")
    finally:
        sys.settrace(previous)

    files = {p.name for p in gens.iterdir()} if gens.exists() else set()
    assert files == set(), "starved rebuild left a product or temp: " + repr(sorted(files))
    assert not _pointer(Path(config["root"]), scope).exists()
    assert not (Path(config["reports"]) / rp._REPORT_NAME).exists()
    assert Path(config["anchor"]).read_bytes() == anchor_before

    import errno
    probe = volume / "independent-enospc-probe"
    try:
        with probe.open("wb", buffering=0) as stream:
            try:
                while True:
                    assert stream.write(b"X" * 65536) > 0
            except OSError as os_error:
                measured_errno = os_error.errno
        assert measured_errno == errno.ENOSPC == 28
    finally:
        probe.unlink(missing_ok=True)

    filler.unlink()
    receipt = rp.rebuild_readonly_projection(**kwargs)
    assert bundle_content(Path(config["root"]), scope) == oracle
    finals = physical_inventory(gens)
    oracle_inventory = physical_inventory(Path(config["oracle_gens"]))
    assert finals == oracle_inventory
    result = {"capacity": 1048576, "reserve": reserve,
              "sqlite_full_events": seen, "failure": failure,
              "independent_os_errno": measured_errno,
              "generation_id": receipt.standard.generation_id,
              "zero_products_while_starved": True,
              "anchor_bytes_unchanged": True, "retry_oracle_equal": True}
    Path(config["result"]).write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps(result), flush=True)


def test_h_recovery_real_tmpfs_enosppc_then_retry(tmp_path):
    ctx = make_case(tmp_path, "enospc", "sha1")
    volume = tmp_path / "volume"
    volume.mkdir()
    # Mount capability is exercised for real in a disposable user/mount namespace.
    probe = subprocess.run(["unshare", "-Urm", "sh", "-c",
        'mount -t tmpfs -o size=1m tmpfs "$1" && stat -f -c "%S %b" "$1"',
        "probe", str(volume)], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=15)
    assert probe.returncode == 0, probe.stdout
    block, count = map(int, probe.stdout.split())
    assert block * count == 1048576

    sqlite_temp = tmp_path / "sqlite-temp"
    sqlite_temp.mkdir()
    config = {
        "isolated": str(ctx.isolated), "head": ctx.head,
        "repo_id": ctx.scope["repo_id"], "company_id": ctx.scope["company_id"],
        "package": str(ctx.package), "volume": str(volume),
        "root": str(volume / "root"), "gens": str(volume / "generations"),
        "reports": str(volume / "reports"), "evidence": str(ctx.evidence),
        "case_id": "case-h-enospc", "stamp": STAMP, "anchor": str(ctx.anchor),
        "oracle_root": str(tmp_path / "oracle-root"),
        "oracle_gens": str(tmp_path / "oracle-gens"),
        "oracle_reports": str(tmp_path / "oracle-reports"),
        "result": str(tmp_path / "enospc-result.json")}
    config_path = tmp_path / "enospc-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    wrapper = ["/usr/bin/bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/",
               "--bind", str(tmp_path), str(tmp_path), "--proc", "/proc", "--dev", "/dev",
               "--setenv", "TMPDIR", str(sqlite_temp),
               "--setenv", "SQLITE_TMPDIR", str(sqlite_temp),
               "--size", "1048576", "--tmpfs", str(volume),
               "--chdir", str(PROJECT), "--"]
    completed = subprocess.run(
        [*wrapper, sys.executable, "-c", ENOSPC_WORKER, "enospc", str(config_path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    assert completed.returncode == 0, completed.stdout
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps({"enospc": result}), flush=True)
    assert result["independent_os_errno"] == 28
    assert result["retry_oracle_equal"] and result["zero_products_while_starved"]
    assert result["anchor_bytes_unchanged"]
    assert result["failure"]["chain"]
    sqlite_full = [event for event in result["sqlite_full_events"]]
    assert sqlite_full and all(event["sqlite_errorname"] == "SQLITE_FULL" for event in sqlite_full)
