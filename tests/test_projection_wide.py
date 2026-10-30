"""D8 full-generation black-box invariants and fault injection."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree import git_format, projection_wide as wide
from app.facts_tree.projection_generation import build_projection_generation
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.record_schema import SCHEMA_COLUMNS
from app.facts_tree.record_tree import build_record_tree
from tests.facts_tree_full_fixture import SCOPE, full_tree


EXPECTED = {"expected_repo_id": SCOPE["repo_id"], "expected_company_id": SCOPE["company_id"]}
META_KEYS = {"generation_id", "projected_commit", "render_sha256", "repo_id", "company_id",
             "wide_schema_version", "status", "source_table_count", "wide_table_count"}
NO_SCHEMA = {"repository_meta", "taxonomy_manifest", "taxonomy_leaf", "bindings_current"}
OWNERSHIP = ("trunk_code", "leaf_code", "pack_version", "template_pack", "binding_revision")


def build_standard(directory, *, decimal=None, inactive=False, commit="c" * 40):
    payloads = full_tree()
    payloads[git_format.record_path("repository_meta")] = dict(SCOPE)
    payloads[git_format.record_path("taxonomy_manifest", pack_version="pack")] = {"version": 1}
    payloads[git_format.record_path("taxonomy_leaf", pack_version="pack", leaf_id="leaf")] = {"code": "leaf"}
    payloads[git_format.record_path("bindings_current")] = {"record_type": "bindings_current", **SCOPE}
    if decimal is not None:
        for payload in payloads.values():
            if payload.get("record_type") == "fact_revision":
                payload["canonical_value"] = decimal
    if inactive:
        payloads[git_format.record_path("fact_current", fact_uid="fact-1")]["status"] = "retracted"
    records = [(p, git_format.dumps_record(v)) for p, v in payloads.items()]
    tree = build_record_tree(records, **SCOPE, commit_oid=commit, head_oid=commit)
    active = frozenset(p["fact_uid"] for p in payloads.values()
                       if p.get("record_type") == "fact_current" and p["status"] == "active")
    return build_projection_generation(build_projection_plan(tree), records, active, directory, **EXPECTED)


def build(source, **kwargs):
    return wide.build_projection_wide(source.db_path, directory=Path(source.db_path).parent,
                                      **{**EXPECTED, **kwargs})


def connect(path):
    return closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True))


def inventory(directory):
    return {p.name: (p.stat().st_ino, p.read_bytes()) for p in directory.iterdir()}


def rewrite_source(source, transform):
    """Inject validly hashed malformed structure, so D8 must check beyond hashes."""
    with closing(sqlite3.connect(source.db_path)) as conn:
        rows = conn.execute("SELECT path, family, canonical_bytes FROM record_canonical ORDER BY path").fetchall()
        payloads = {p: git_format.loads_record(raw) for p, _, raw in rows}
        transform(payloads)
        records = [(p, git_format.canonical_record_type(p), git_format.dumps_record(v))
                   for p, v in sorted(payloads.items())]
        render = b"\x1e".join(f.encode() + b"\x1f" + p.encode() + b"\x1f" + raw for p, f, raw in records)
        with conn:
            conn.execute("DELETE FROM record_canonical")
            conn.executemany("INSERT INTO record_canonical VALUES (?,?,?,?,?)",
                             [(p, f, raw, len(raw), sha256(raw).hexdigest()) for p, f, raw in records])
            conn.execute("DELETE FROM family_counts")
            conn.execute("INSERT INTO family_counts SELECT family, count(*) FROM record_canonical GROUP BY family")
            conn.execute("UPDATE generation_meta SET value=? WHERE key='record_count'", (str(len(records)),))
            conn.execute("UPDATE generation_meta SET value=? WHERE key='render_sha256'", (sha256(render).hexdigest(),))


def test_full_family_independent_oracle_and_where(tmp_path):
    source = build_standard(tmp_path)
    before = inventory(tmp_path)
    result = build(source)
    assert result.ready and result.generation_id == source.generation_id
    assert result.record_count == 18
    with pytest.raises(FrozenInstanceError):
        result.ready = False
    with connect(source.db_path) as src, connect(result.db_path) as dest:
        tables = {r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"wide_meta", *("dw_" + family for family in SCHEMA_COLUMNS)}
        assert not tables.intersection("dw_" + f for f in NO_SCHEMA)
        meta = dict(dest.execute("SELECT * FROM wide_meta"))
        assert set(meta) == META_KEYS
        assert meta["status"] == "ready" and meta["wide_schema_version"] == "1"
        assert meta["source_table_count"] == "5" and meta["wide_table_count"] == "16"
        standard_meta = dict(src.execute("SELECT * FROM generation_meta"))
        for key in META_KEYS & standard_meta.keys() - {"status"}:
            assert meta[key] == standard_meta[key]
        assert src.execute("SELECT sum(record_count) FROM family_counts WHERE family IN (?,?,?,?)",
                           sorted(NO_SCHEMA)).fetchone() == (4,)
        total = 0
        for family, columns in SCHEMA_COLUMNS.items():
            source_rows = src.execute("SELECT path, sha256, byte_length, canonical_bytes FROM record_canonical "
                                      "WHERE family=? ORDER BY path", (family,)).fetchall()
            actual = dest.execute("SELECT path, canonical_sha256, byte_length FROM dw_" + family
                                  + " ORDER BY path").fetchall()
            assert actual == [r[:3] for r in source_rows]
            total += len(actual)
            for path, digest, length, raw in source_rows:
                payload = git_format.loads_record(raw)
                encoded, = dest.execute("SELECT payload_json FROM dw_" + family + " WHERE path=?", (path,)).fetchone()
                assert git_format.dumps_record(git_format.loads_record(encoded)) == raw
                assert len(raw) == length and sha256(raw).hexdigest() == digest
                scalar = dest.execute("SELECT " + ",".join(c.name for c in columns)
                                      + " FROM dw_" + family + " WHERE path=?", (path,)).fetchone()
                assert scalar == tuple(payload[c.name] for c in columns)
        assert total == result.record_count
        assert dest.execute("SELECT fact_uid, revision FROM dw_fact_current WHERE leaf_code=? "
                            "AND record_type=? AND status=?", ("leaf-test", "fact_current", "active")).fetchall() == [("fact-1", 2)]
        assert dest.execute("SELECT revision FROM dw_fact_revision WHERE event_time=? AND record_type=?",
                            (1793318400, "fact_revision")).fetchall() == [(1,), (2,)]
        assert dest.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert inventory(tmp_path)[Path(source.db_path).name] == before[Path(source.db_path).name]
    assert set(inventory(tmp_path)) == set(before) | {Path(result.db_path).name}


@pytest.mark.parametrize("inactive", [False, True])
def test_ownership_uses_current_pointer_not_matching_revision_numbers(tmp_path, inactive):
    result = build(build_standard(tmp_path, inactive=inactive))
    with connect(result.db_path) as conn:
        assert conn.execute("SELECT " + ",".join(OWNERSHIP) + " FROM dw_fact_current").fetchall() == [
            ("trunk-test", "leaf-test", 1, "pack-test", 1)]
        assert conn.execute("SELECT revision FROM dw_fact_current").fetchone() == (2,)
        assert conn.execute("SELECT revision FROM dw_placement_current").fetchone() == (1,)


@pytest.mark.parametrize("number", ["1.20", "0.1", "0.12345678901234567890123456789"])
def test_decimal_payload_bare_number_and_exact_roundtrip(tmp_path, number):
    result = build(build_standard(tmp_path, decimal=Decimal(number)))
    with connect(result.db_path) as conn:
        for text, in conn.execute("SELECT payload_json FROM dw_fact_revision"):
            assert '"canonical_value":' + number + ',' in text
            assert json.loads(text, parse_float=Decimal)["canonical_value"] == Decimal(number)
            assert "示例" in text


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                                  float("nan"), float("inf"), object()])
def test_compact_json_rejects_invalid_values(bad):
    with pytest.raises((ValueError, TypeError)):
        wide._compact_json({"nested": [bad]})


def test_compact_json_standard_values_and_order():
    value = {"中": (True, False, None, 2, 1.5), "a": {"b": "\n\""}}
    assert wide._compact_json(value) == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with pytest.raises(TypeError):
        wide._compact_json({1: "bad"})


def test_idempotence_concurrency_and_byte_rebuild(tmp_path):
    source = build_standard(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: build(source), range(4)))
    assert all(result == results[0] for result in results)
    before = inventory(tmp_path)
    assert build(source) == results[0]
    assert inventory(tmp_path) == before
    derived = Path(results[0].db_path)
    expected = derived.read_bytes()
    derived.unlink()
    assert Path(build(source).db_path).read_bytes() == expected
    assert len(list(tmp_path.iterdir())) == 2


def test_different_generations_are_independent(tmp_path):
    a = build_standard(tmp_path)
    b = build_standard(tmp_path, commit="e" * 40)
    first = build(a)
    before = Path(first.db_path).read_bytes()
    second = build(b)
    assert first.generation_id != second.generation_id
    assert first.db_path != second.db_path
    assert Path(first.db_path).read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 4


@pytest.mark.parametrize("mutation", ["no-pointer", "no-placement", "wrong-pointer", "old-fact", "other-fact"])
def test_missing_or_wrong_current_ownership_fails_without_output(tmp_path, mutation):
    source = build_standard(tmp_path)
    def change(payloads):
        pointer_path = git_format.record_path("placement_current", fact_uid="fact-1")
        placement_path = git_format.record_path("placement_revision", fact_uid="fact-1", revision=1)
        if mutation == "no-pointer":
            del payloads[pointer_path]
        elif mutation == "no-placement":
            del payloads[placement_path]
        elif mutation == "wrong-pointer":
            payloads[pointer_path]["revision"] = 9
        else:
            ref = payloads[placement_path]["fact_ref"]
            ref["revision" if mutation == "old-fact" else "stable_id"] = 1 if mutation == "old-fact" else "other"
    rewrite_source(source, change)
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError, match="placement"):
        build(source)
    assert inventory(tmp_path) == before


def test_duplicate_current_ownership_fails_without_output(tmp_path):
    source = build_standard(tmp_path)
    with closing(sqlite3.connect(source.db_path)) as conn:
        with conn:
            conn.execute("ALTER TABLE record_canonical RENAME TO original_records")
            conn.execute("CREATE TABLE record_canonical AS SELECT * FROM original_records")
            conn.execute("INSERT INTO record_canonical SELECT * FROM original_records WHERE family='placement_current'")
            conn.execute("DROP TABLE original_records")
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError, match="duplicate"):
        build(source)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("key,value", [("status", "staging"), ("repo_id", "other"),
                                        ("company_id", "other"), ("generation_id", "../escape"),
                                        ("render_sha256", "f" * 64)])
def test_bad_source_metadata(tmp_path, key, value):
    source = build_standard(tmp_path)
    with closing(sqlite3.connect(source.db_path)) as conn:
        with conn:
            conn.execute("UPDATE generation_meta SET value=? WHERE key=?", (value, key))
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError):
        build(source)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("scope", [{"expected_repo_id": "wrong"}, {"expected_company_id": "wrong"}])
def test_wrong_expected_identity(tmp_path, scope):
    source = build_standard(tmp_path)
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError):
        build(source, **scope)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("mutation", ["bytes", "length", "hash", "scalar", "family-count", "extra-table"])
def test_tampered_source_fails_without_output(tmp_path, mutation):
    source = build_standard(tmp_path)
    if mutation == "scalar":
        rewrite_source(source, lambda payloads: payloads[git_format.record_path(
            "fact_current", fact_uid="fact-1")].update(status={"bad": "compound"}))
    else:
        sql = {
            "bytes": "UPDATE record_canonical SET canonical_bytes=x'00' WHERE family='fact_current'",
            "length": "UPDATE record_canonical SET byte_length=0 WHERE family='fact_current'",
            "hash": "UPDATE record_canonical SET sha256='bad' WHERE family='fact_current'",
            "family-count": "UPDATE family_counts SET record_count=999 WHERE family='fact_current'",
            "extra-table": "CREATE TABLE extra(x)",
        }[mutation]
        with closing(sqlite3.connect(source.db_path)) as conn:
            with conn:
                conn.execute(sql)
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError):
        build(source)
    assert inventory(tmp_path) == before


def test_outside_directory_and_missing_source_create_nothing(tmp_path):
    directory = tmp_path / "source"
    source = build_standard(directory)
    before = inventory(directory)
    with pytest.raises(wide.WideBuildError, match="share directory"):
        wide.build_projection_wide(source.db_path, directory=tmp_path, **EXPECTED)
    with pytest.raises(wide.WideBuildError):
        wide.build_projection_wide(tmp_path / "missing.sqlite", directory=tmp_path, **EXPECTED)
    assert inventory(directory) == before
    assert {p.name for p in tmp_path.iterdir()} == {"source"}


@pytest.mark.parametrize("phase", ["write", "verify-temp", "verify-final", "replace", "fsync"])
def test_injected_io_failure_cleans_only_owned_files(tmp_path, monkeypatch, phase):
    source = build_standard(tmp_path)
    before = inventory(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("injected " + phase)
    if phase == "write":
        original = wide._write
        def partial(*args):
            original(*args)
            fail()
        monkeypatch.setattr(wide, "_write", partial)
    elif phase.startswith("verify"):
        original = wide._verify
        def verify(path, *args):
            original(path, *args)
            if (phase == "verify-temp") == path.name.startswith("."):
                fail()
        monkeypatch.setattr(wide, "_verify", verify)
    elif phase == "replace":
        monkeypatch.setattr(wide.os, "replace", fail)
    else:
        monkeypatch.setattr(wide, "_sync_file", fail)
    with pytest.raises(wide.WideBuildError, match="injected"):
        build(source)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("sql", ["UPDATE dw_fact_current SET leaf_code='wrong'",
                                 "UPDATE dw_fact_revision SET payload_json='{}'",
                                 "DELETE FROM dw_evidence",
                                 "INSERT INTO wide_meta VALUES ('extra','x')",
                                 "CREATE TABLE extra(x)"])
def test_existing_invalid_companion_is_never_overwritten(tmp_path, sql):
    source = build_standard(tmp_path)
    result = build(source)
    with closing(sqlite3.connect(result.db_path)) as conn:
        with conn:
            conn.execute(sql)
    before = inventory(tmp_path)
    with pytest.raises(wide.WideBuildError):
        build(source)
    assert inventory(tmp_path) == before


def test_empty_schema_families_still_have_all_tables(tmp_path):
    from tests.test_projection_generation import _build, _input, META_PATH
    source = _build(tmp_path, _input({META_PATH: dict(SCOPE)}))
    result = build(source)
    assert result.record_count == 0
    with connect(result.db_path) as conn:
        assert len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == 17


def test_source_boundary_no_network_model_or_git_writer():
    tree = ast.parse(Path(wide.__file__).read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imports |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {"collections", "contextlib", "dataclasses", "decimal", "fcntl", "hashlib", "json",
                       "math", "os", "pathlib", "re", "sqlite3", "tempfile", "app.facts_tree",
                       "app.facts_tree.projection_decode", "app.facts_tree.record_schema"}


@pytest.mark.parametrize("selected", [1, 2])
def test_pointer_selects_one_of_multiple_historical_placements(tmp_path, selected):
    source = build_standard(tmp_path)
    def change(payloads):
        old_path = git_format.record_path("placement_revision", fact_uid="fact-1", revision=1)
        new = dict(payloads[old_path])
        new.update(revision=2, leaf_code="second-leaf", action="reassign",
                   predecessor_ref={"record_type": "placement_revision", "stable_id": "fact-1", "revision": 1})
        payloads[git_format.record_path("placement_revision", fact_uid="fact-1", revision=2)] = new
        payloads[git_format.record_path("placement_current", fact_uid="fact-1")]["revision"] = selected
    rewrite_source(source, change)
    result = build(source)
    with connect(result.db_path) as conn:
        assert conn.execute("SELECT leaf_code FROM dw_fact_current").fetchall() == [
            ("leaf-test" if selected == 1 else "second-leaf",)]


def test_ready_is_written_only_after_staging_verification(tmp_path, monkeypatch):
    source = build_standard(tmp_path)
    original = wide._verify
    seen = []
    def verify(path, *args):
        with connect(path) as conn:
            seen.append((path.name.startswith("."), dict(conn.execute("SELECT * FROM wide_meta")).get("status")))
        return original(path, *args)
    monkeypatch.setattr(wide, "_verify", verify)
    result = build(source)
    assert seen == [(True, None), (True, "ready"), (False, "ready")]
    with connect(result.db_path) as conn:
        for family in SCHEMA_COLUMNS:
            names = [r[1] for r in conn.execute("PRAGMA table_info(dw_" + family + ")")]
            assert names[-4:] == ["path", "payload_json", "canonical_sha256", "byte_length"]
