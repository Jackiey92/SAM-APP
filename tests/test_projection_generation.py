"""Appendix D-2 v1: canonical generations and independent read-only probes."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree import git_format, projection_generation as generation
from app.facts_tree.git_repository import SnapshotRecord
from app.facts_tree.projection_generation import (
    BuiltProjectionGeneration, build_projection_generation,
)
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.record_tree import build_record_tree
from tests.facts_tree_full_fixture import SCOPE, full_tree


META_PATH = git_format.record_path("repository_meta")
TABLES = {"generation_meta", "record_canonical", "family_counts",
          "fact_current_view", "placement_current_view"}
META_KEYS = {
    "generation_id", "repo_id", "company_id", "projected_commit", "head_oid",
    "object_format", "record_count", "active_fact_count", "render_sha256",
    "projection_schema_version", "builder_version", "renderer_version",
    "required_indexes", "status",
}


def _input(payloads=None, **identity):
    if payloads is None:
        payloads = full_tree()
        payloads[META_PATH] = {
            "schema_version": 1, "format_version": 1, "installation_id": "install-test", **SCOPE}
    records = [(path, git_format.dumps_record(payload)) for path, payload in payloads.items()]
    tree = build_record_tree(records, **{
        **SCOPE, "commit_oid": "c" * 40, "head_oid": "d" * 40, **identity})
    active = frozenset(payload["fact_uid"] for payload in tree.ordered.values()
                       if payload.get("record_type") == "fact_current"
                       and payload["status"] == "active")
    return build_projection_plan(tree), records, active


def _build(directory, inputs=None, **expected):
    plan, records, active = _input() if inputs is None else inputs
    return build_projection_generation(plan, records, active, directory, **{
        "expected_repo_id": SCOPE["repo_id"], "expected_company_id": SCOPE["company_id"],
        **expected})


def _read(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
        return {table: connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                for table in TABLES}


def _reference(plan, records, active):
    """Independent oracle: no private generation helpers/constants are used."""
    parts = [plan.repo_id, plan.company_id, plan.commit_oid, plan.render_sha256, "1", "1"]
    identifier = sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]
    rows, family_counts, heads, placements, segments = [], {}, [], [], []
    for path, content in sorted(records):
        family = git_format.canonical_record_type(path)
        payload = git_format.loads_record(content)
        rows.append((path, family, content, len(content), sha256(content).hexdigest()))
        family_counts[family] = family_counts.get(family, 0) + 1
        if family == "fact_current":
            heads.append((payload["fact_uid"], payload["revision"], int(payload["fact_uid"] in active)))
        elif family == "placement_current":
            placements.append((payload["fact_uid"], payload["revision"]))
        segments.append(b"\x1f".join((family.encode(), path.encode(), content)))
    render = b"\x1e".join(segments)
    assert render == plan.render_bytes
    assert sha256(render).hexdigest() == plan.render_sha256
    meta = {
        "generation_id": identifier, "repo_id": plan.repo_id, "company_id": plan.company_id,
        "projected_commit": plan.commit_oid, "head_oid": plan.head_oid,
        "object_format": plan.object_format, "record_count": str(len(records)),
        "active_fact_count": str(len(active)), "render_sha256": sha256(render).hexdigest(),
        "projection_schema_version": "1", "builder_version": "1", "renderer_version": "1",
        "required_indexes": '["canonical_records"]', "status": "ready",
    }
    return identifier, {
        "record_canonical": rows, "family_counts": sorted(family_counts.items()),
        "fact_current_view": sorted(heads), "placement_current_view": sorted(placements),
        "generation_meta": sorted(meta.items()),
    }


def test_full_tree_independent_probe(tmp_path):
    inputs = _input()
    result = _build(tmp_path, inputs)
    identifier, expected = _reference(*inputs)
    assert type(result) is BuiltProjectionGeneration
    assert result == BuiltProjectionGeneration(
        str(tmp_path / ("gen-" + identifier + ".sqlite")), identifier,
        inputs[0].commit_oid, inputs[0].render_sha256, 19, True)
    assert _read(result.db_path) == expected
    meta = dict(expected["generation_meta"])
    assert set(meta) == META_KEYS
    assert json.loads(meta["required_indexes"]) == ["canonical_records"]
    with closing(sqlite3.connect(Path(result.db_path).as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == TABLES
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM generation_meta")
    with pytest.raises(FrozenInstanceError):
        result.ready = False
    assert {path.name for path in tmp_path.iterdir()} == {Path(result.db_path).name}
    print("\nPROBE generation_id=" + identifier)
    print("PROBE tables=5 records=19 meta_keys=14 independent_all_rows=equal")
    print("PROBE render_sha256=" + inputs[0].render_sha256)
    print("PROBE mode=ro opened; writes=readonly; integrity_check=[('ok',)]; foreign_key_check=[]")


def test_versions_fields_and_frozen_ddl(tmp_path):
    assert (generation.PROJECTION_SCHEMA_VERSION, generation.PROJECTION_BUILDER_VERSION,
            generation.PROJECTION_RENDERER_VERSION) == (1, 1, 1)
    assert [field.name for field in fields(BuiltProjectionGeneration)] == [
        "db_path", "generation_id", "projected_commit", "render_sha256", "record_count", "ready"]
    result = _build(tmp_path)
    columns = {
        "generation_meta": [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)],
        "record_canonical": [("path", "TEXT", 0, 1), ("family", "TEXT", 1, 0),
                             ("canonical_bytes", "BLOB", 1, 0), ("byte_length", "INTEGER", 1, 0),
                             ("sha256", "TEXT", 1, 0)],
        "family_counts": [("family", "TEXT", 0, 1), ("record_count", "INTEGER", 1, 0)],
        "fact_current_view": [("fact_uid", "TEXT", 0, 1), ("revision", "INTEGER", 1, 0),
                              ("is_active", "INTEGER", 1, 0)],
        "placement_current_view": [("fact_uid", "TEXT", 0, 1), ("revision", "INTEGER", 1, 0)],
    }
    with closing(sqlite3.connect(result.db_path)) as connection:
        for table, expected in columns.items():
            assert [(row[1], row[2], row[3], row[5]) for row in connection.execute(
                "PRAGMA table_info(" + table + ")")] == expected
        assert connection.execute("PRAGMA index_info(idx_record_canonical_family)").fetchall() == [(0, 1, "family")]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE fact_current_view SET is_active=2")


def test_idempotent_rerun_never_opens_writer_or_replaces(tmp_path, monkeypatch):
    inputs = _input()
    first = _build(tmp_path, inputs)
    path = Path(first.db_path)
    before = (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
    connect = sqlite3.connect

    def ro_only(database, *args, **kwargs):
        assert kwargs.get("uri") is True and str(database).endswith("?mode=ro")
        return connect(database, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("idempotent rerun must never replace")

    monkeypatch.setattr(generation.sqlite3, "connect", ro_only)
    monkeypatch.setattr(generation.os, "replace", forbidden)
    assert _build(tmp_path, inputs) == first
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) == before


def test_separate_directories_and_reversed_records_are_equivalent(tmp_path):
    inputs = _input()
    first = _build(tmp_path / "a", inputs)
    second = _build(tmp_path / "b", (inputs[0], iter(reversed(inputs[1])), inputs[2]))
    assert first.generation_id == second.generation_id
    assert _read(first.db_path) == _read(second.db_path)


@pytest.mark.parametrize("column,value", [
    ("expected_repo_id", "other-repo"), ("expected_company_id", "other-company"),
])
def test_expected_scope_rejected_before_io(tmp_path, column, value):
    with pytest.raises(ValueError, match="repo/company"):
        _build(tmp_path, **{column: value})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", ["byte", "missing", "extra", "duplicate", "unknown_path"])
def test_records_cannot_differ_from_plan(tmp_path, damage):
    plan, records, active = _input()
    if damage == "byte":
        records[0] = (records[0][0], records[0][1] + b" ")
    elif damage == "missing":
        records.pop()
    elif damage == "extra":
        records.append((git_format.record_path("taxonomy_leaf", pack_version="p", leaf_id="extra"), b"{}\n"))
    elif damage == "duplicate":
        records.append(records[0])
    else:
        records.append(("unknown.json", b"{}\n"))
    with pytest.raises(ValueError):
        _build(tmp_path, (plan, records, active))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("active", [{"outside"}, set(), {"fact-1", "outside"}])
def test_active_subset_and_count_enforced(tmp_path, active):
    plan, records, _ = _input()
    with pytest.raises(ValueError, match="active"):
        _build(tmp_path, (plan, records, active))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payloads", [{}, {META_PATH: SCOPE}])
def test_empty_and_repository_meta_only(tmp_path, payloads):
    inputs = _input(payloads)
    result = _build(tmp_path, inputs)
    identifier, expected = _reference(*inputs)
    assert result.record_count == len(payloads)
    assert result.generation_id == identifier
    assert _read(result.db_path) == expected


@pytest.mark.parametrize("status", ["active", "superseded", "retracted"])
def test_all_current_statuses_preserve_heads_and_placement(tmp_path, status):
    payloads = full_tree()
    path = git_format.record_path("fact_current", fact_uid="fact-1")
    payloads[path]["status"] = status
    inputs = _input(payloads)
    result = _build(tmp_path, inputs)
    assert _read(result.db_path) == _reference(*inputs)[1]
    assert _read(result.db_path)["fact_current_view"] == [("fact-1", 2, int(status == "active"))]


def test_snapshot_records_are_supported_without_mutating_inputs(tmp_path):
    plan, records, active = _input()
    snapshots = [SnapshotRecord(path=path, content=content) for path, content in records]
    before = deepcopy(snapshots)
    result = _build(tmp_path, (plan, snapshots, active))
    assert snapshots == before
    assert _read(result.db_path) == _reference(plan, records, active)[1]


def test_decimal_controls_and_all_unregistered_families_preserved(tmp_path):
    from tests.test_facts_tree_record_schema_closure import binding_tree

    payloads, _, _ = binding_tree()
    payloads[META_PATH] = dict(SCOPE)
    payloads[git_format.record_path("taxonomy_manifest", pack_version="pack")] = {"version": 7}
    path = git_format.record_path("taxonomy_leaf", pack_version="pack", leaf_id="a\x1fb\x1ec")
    payloads[path] = {"code": "a\x1fb\x1ec", "decimal": Decimal("123456789.00100"), "text": "原文\n\x1f\x1e",
                      "nested": [None, False, {"raw": "字节"}]}
    before = deepcopy(payloads)
    inputs = _input(payloads)
    result = _build(tmp_path, inputs)
    assert payloads == before
    actual = _read(result.db_path)
    assert actual == _reference(*inputs)[1]
    assert {"repository_meta", "taxonomy_manifest", "taxonomy_leaf", "bindings_current"} <= {
        row[0] for row in actual["family_counts"]}
    assert b"123456789.00100" in next(row[2] for row in actual["record_canonical"] if row[0] == path)


@pytest.mark.parametrize("sql", [
    "UPDATE generation_meta SET value='other' WHERE key='repo_id'",
    "DELETE FROM generation_meta WHERE key='status'",
    "INSERT INTO generation_meta VALUES ('extra','value')",
    "UPDATE record_canonical SET byte_length=0",
    "UPDATE record_canonical SET sha256='bad'",
    "UPDATE record_canonical SET canonical_bytes=X'00'",
    "UPDATE record_canonical SET family='other'",
    "DELETE FROM record_canonical",
    "UPDATE family_counts SET record_count=100",
    "UPDATE fact_current_view SET revision=100",
    "UPDATE fact_current_view SET is_active=0",
    "UPDATE placement_current_view SET revision=100",
    "CREATE TABLE extra(value TEXT)",
    "DROP INDEX idx_record_canonical_family",
])
def test_conflicting_existing_generation_is_rejected_and_preserved(tmp_path, sql):
    inputs = _input()
    result = _build(tmp_path, inputs)
    path = Path(result.db_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        _build(tmp_path, inputs)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_existing_file_is_not_overwritten(tmp_path):
    result = _build(tmp_path)
    path = Path(result.db_path)
    path.write_bytes(b"corrupt generation")
    with pytest.raises(ValueError):
        _build(tmp_path)
    assert path.read_bytes() == b"corrupt generation"


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_readonly_validation_failure_removes_only_own_files(tmp_path, monkeypatch, stage):
    original = generation._verify
    calls = []
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    def verify(path, plan, rows, active, meta):
        original(path, plan, rows, active, meta)
        calls.append((path.name, dict(_read(path)["generation_meta"])))
        if len(calls) == stage:
            raise ValueError("injected validation failure")

    monkeypatch.setattr(generation, "_verify", verify)
    with pytest.raises(ValueError, match="injected"):
        _build(tmp_path)
    assert len(calls) == stage
    assert "status" not in calls[0][1]
    if stage >= 2:
        assert "status" not in calls[1][1]
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("operation,fail_on", [("fsync", 1), ("fsync", 2), ("fsync", 3),
                                               ("fsync", 4), ("replace", 1)])
def test_io_failure_is_value_error_and_no_product_remains(tmp_path, monkeypatch, operation, fail_on):
    original = getattr(generation.os, operation)
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise OSError("injected " + operation)
        return original(*args, **kwargs)

    monkeypatch.setattr(generation.os, operation, fail)
    with pytest.raises(ValueError, match="injected") as caught:
        _build(tmp_path)
    assert type(caught.value) is ValueError
    assert list(tmp_path.iterdir()) == []


def test_single_write_transaction_and_foreign_keys_and_atomic_filename(tmp_path, monkeypatch):
    connect = sqlite3.connect
    statements, modes, replaced = [], [], []
    original_replace = os.replace

    def tracked(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        modes.append((str(database), kwargs.get("uri", False)))
        connection.set_trace_callback(statements.append)
        return connection

    def atomic(source, target):
        assert Path(source).name == "." + Path(target).name + ".tmp-" + str(os.getpid())
        assert "status" not in dict(_read(source)["generation_meta"])
        replaced.append((source, target))
        original_replace(source, target)

    monkeypatch.setattr(generation.sqlite3, "connect", tracked)
    monkeypatch.setattr(generation.os, "replace", atomic)
    result = _build(tmp_path)
    assert len(replaced) == 1 and str(replaced[0][1]) == result.db_path
    assert statements.count("PRAGMA foreign_keys=ON") == 2
    start = statements.index("BEGIN IMMEDIATE")
    finish = statements.index("COMMIT", start)
    assert sum(statement.startswith("CREATE TABLE") for statement in statements[start:finish]) == 5
    assert sum(statement.startswith("INSERT INTO record_canonical") for statement in statements[start:finish]) == 19
    assert not any("'ready'" in statement for statement in statements[start:finish])
    assert sum(uri and path.endswith("?mode=ro") for path, uri in modes) >= 3


def test_concurrent_builders_are_idempotent(tmp_path):
    inputs = _input()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _build(tmp_path, inputs), range(4)))
    assert all(result == results[0] for result in results)
    assert len(list(tmp_path.iterdir())) == 1
    assert _read(results[0].db_path) == _reference(*inputs)[1]


def test_preexisting_temp_file_is_preserved(tmp_path):
    inputs = _input()
    identifier = _reference(*inputs)[0]
    temp = tmp_path / (".gen-" + identifier + ".sqlite.tmp-" + str(os.getpid()))
    temp.write_bytes(b"not owned by this call")
    with pytest.raises(ValueError):
        _build(tmp_path, inputs)
    assert list(tmp_path.iterdir()) == [temp]
    assert temp.read_bytes() == b"not owned by this call"


@pytest.mark.parametrize("pragma", ["integrity_check", "foreign_key_check"])
def test_bad_pragma_results_cannot_be_ready(tmp_path, monkeypatch, pragma):
    connect = sqlite3.connect

    class BadCheck(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA " + pragma:
                return super().execute("SELECT 'injected failure'")
            return super().execute(sql, *args, **kwargs)

    def checked(database, *args, **kwargs):
        if kwargs.get("uri"):
            kwargs["factory"] = BadCheck
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(generation.sqlite3, "connect", checked)
    with pytest.raises(ValueError, match=pragma):
        _build(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sql", [
    "UPDATE record_canonical SET sha256='bad'",
    "UPDATE family_counts SET record_count=100",
    "UPDATE fact_current_view SET is_active=0",
    "UPDATE placement_current_view SET revision=100",
    "INSERT INTO generation_meta VALUES ('extra','value')",
    "CREATE TABLE extra(value TEXT)",
])
def test_written_content_must_pass_real_readonly_checks_before_ready(tmp_path, monkeypatch, sql):
    write = generation._write_records

    def corrupt(path, *args):
        write(path, *args)
        with closing(sqlite3.connect(path)) as connection, connection:
            assert connection.execute("SELECT value FROM generation_meta WHERE key='status'").fetchall() == []
            connection.execute(sql)

    monkeypatch.setattr(generation, "_write_records", corrupt)
    with pytest.raises(ValueError):
        _build(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", [None, {}, []])
def test_wrong_plan_type(tmp_path, bad):
    with pytest.raises(TypeError):
        _build(tmp_path, (bad, [], set()))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("records", [[(1, b"{}")], [("meta/repository.json", "text")],
                                      [("meta/repository.json", bytearray())], [None], [("path",)]])
def test_malformed_records_raise_type_error(tmp_path, records):
    plan, _, active = _input()
    with pytest.raises(TypeError):
        _build(tmp_path, (plan, records, active))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("active", [None, [], ["fact-1"], "fact-1", {1}])
def test_malformed_active_set_rejected(tmp_path, active):
    plan, records, _ = _input()
    with pytest.raises(TypeError):
        _build(tmp_path, (plan, records, active))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", [
    {"render_sha256": "0" * 64}, {"render_bytes": b"other"}, {"total_records": 20},
    {"family_counts": {"repository_meta": 19}}, {"active_fact_count": 0},
])
def test_inconsistent_plan_rejected_before_io(tmp_path, change):
    plan, records, active = _input()
    with pytest.raises(ValueError):
        _build(tmp_path, (replace(plan, **change), records, active))
    assert list(tmp_path.iterdir()) == []


def test_production_imports_and_source_do_not_cross_scope():
    source = Path(generation.__file__).read_text()
    parsed = ast.parse(source)
    imports = [node.module for node in ast.walk(parsed) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(parsed) if isinstance(node, ast.Import) for alias in node.names]
    forbidden = {"tests", "subprocess", "socket", "requests", "urllib", "time", "datetime", "random", "secrets"}
    assert not any(name and name.split(".")[0] in forbidden for name in imports)
    assert not any(name and any(word in name for word in ("broker", "leaf_", "backup")) for name in imports)
