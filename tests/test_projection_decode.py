"""D6 checks against real built, published, read-only generations."""
from collections import Counter
from contextlib import closing
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
from types import MappingProxyType

import pytest

from app.facts_tree import git_format, projection_decode as decode, projection_query as query
from app.facts_tree.record_schema import RecordSchemaClosureError, loads_schema_record
from tests.test_projection_query import configured_key
from tests.test_projection_router import _build, _publish, router, SCOPE


from app.facts_tree.projection_generation import build_projection_generation
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.record_tree import build_record_tree
from tests.facts_tree_full_fixture import full_tree


def build(directory, company_id="company-test"):
    payloads = full_tree()
    payloads[git_format.record_path("repository_meta")] = dict(SCOPE)
    payloads[git_format.record_path("taxonomy_manifest", pack_version="pack")] = {"version": 1}
    payloads[git_format.record_path("taxonomy_leaf", pack_version="pack", leaf_id="leaf")] = {"code": "leaf"}
    payloads[git_format.record_path("bindings_current")] = {"record_type": "bindings_current", **SCOPE}
    def scope(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "company_id":
                    value[key] = company_id
                else:
                    scope(child)
        elif isinstance(value, list):
            for child in value:
                scope(child)
    scope(payloads)
    # Evidence fingerprints bind the source bytes after scope replacement.
    source = next(p for p in payloads.values() if p.get("record_type") == "source_version")
    for payload in payloads.values():
        if payload.get("record_type") == "evidence":
            payload["quote_digest"] = sha256(git_format.dumps_record(source)).hexdigest()
    issue = next(v for v in payloads.values() if v.get("record_type") == "issue_event")
    for payload in payloads.values():
        if payload.get("record_type") == "event":
            payload["after"] = [sha256(git_format.dumps_record(issue)).hexdigest()]
    records = [(p, git_format.dumps_record(v)) for p, v in payloads.items()]
    tree = build_record_tree(records, repo_id=SCOPE["repo_id"], company_id=company_id,
                             commit_oid="c" * 40, head_oid="d" * 40)
    active = frozenset(v["fact_uid"] for v in payloads.values()
                       if v.get("record_type") == "fact_current" and v["status"] == "active")
    return build_projection_generation(build_projection_plan(tree), records, active, directory,
                                       expected_repo_id=SCOPE["repo_id"], expected_company_id=company_id)


@pytest.fixture
def generation(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = build(directory)
    _publish(built, root, directory)
    serving, conn = router.open_current(root, **SCOPE)
    try:
        yield built, serving, conn, root, directory
    finally:
        conn.close()


def run(conn, **kwargs):
    return decode.decode_generation_records(conn, authz_digest="authorized", **kwargs)


def test_full_roundtrip_readonly_deterministic(generation):
    built, _, conn, _, _ = generation
    tables = conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    result = run(conn)
    assert result == run(conn)
    counts = dict(conn.execute("SELECT family, count(*) FROM record_canonical GROUP BY family"))
    assert dict(result.skipped_families) == {f: counts[f] for f in decode.UNSCHEMA_FAMILIES}
    assert dict(result.decoded_families) == {f: n for f, n in counts.items() if f not in decode.UNSCHEMA_FAMILIES}
    assert len(result.records) + sum(result.skipped_families.values()) == sum(counts.values())
    assert result.generation_id == built.generation_id
    for record in result.records:
        raw, = conn.execute("SELECT canonical_bytes FROM record_canonical WHERE path=?", (record.path,)).fetchone()
        assert isinstance(record.payload, MappingProxyType)
        assert dict(record.payload) == loads_schema_record(raw, path=record.path, **SCOPE)
        assert git_format.dumps_record(dict(record.payload)) == raw
        assert git_format.canonical_record_type(record.path) == record.family
        with pytest.raises(TypeError):
            record.payload["company_id"] = "changed"
    with pytest.raises(TypeError):
        result.decoded_families["x"] = 1
    with pytest.raises(TypeError):
        result.skipped_families["x"] = 1
    with pytest.raises(FrozenInstanceError):
        result.generation_id = "changed"
    with pytest.raises(FrozenInstanceError):
        result.records[0].path = "changed"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE unwanted(x)")
    assert conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall() == tables
    assert conn.total_changes == 0


def test_filters_paths_and_limits(generation):
    conn = generation[2]
    full = run(conn)
    for families in ({full.records[0].family}, [r.family for r in full.records[:4]]):
        assert run(conn, families=families).records == tuple(r for r in full.records if r.family in families)
    paths = [r.path for r in full.records[-4:]][::-1]
    assert run(conn, paths=paths).records == full.records[-4:]
    other = next(r.family for r in full.records if r.family != full.records[0].family)
    assert run(conn, paths=[full.records[0].path], families={other}).records == ()
    for limit in (1, 3, 7):
        assert run(conn, limit=limit).records == full.records[:limit]
    with pytest.raises(decode.ProjectionDecodeError, match="record_not_found"):
        run(conn, paths=[paths[0], "missing"], limit=1)


@pytest.mark.parametrize("options", [
    {"families": value} for value in ([], (), set(), frozenset(), "event", {"absent"},
                                       {"repository_meta"}, [None], [""])
] + [{"paths": value} for value in ([], "abc", ["x", "x"], [None], [""], ["a\x00"])]
  + [{"limit": value} for value in (0, -1, True, 1.5, "2")]
  + [{"authz_digest": value} for value in ("", None, "a\x00")])
def test_bad_inputs(generation, options):
    with pytest.raises(decode.ProjectionDecodeError):
        decode.decode_generation_records(generation[2], **{"authz_digest": "ok", **options})


def test_bad_connection():
    for function, args in ((decode.decode_generation_records, ()), (decode.decode_rows, ([],))):
        with pytest.raises(decode.ProjectionDecodeError):
            function(None, *args, authz_digest="ok")


def test_rows_pagination(generation):
    conn = generation[2]
    records, skips, cursor = [], Counter(), None
    while True:
        page = query.list_records(conn, authz_digest="ok", limit=7, cursor=cursor)
        result = decode.decode_rows(conn, reversed(page.records), authz_digest="ok")
        records.extend(result.records)
        skips.update(result.skipped_families)
        tuples = [(r.path, r.family, r.byte_length, r.sha256) for r in page.records]
        assert decode.decode_rows(conn, tuples, authz_digest="ok") == result
        cursor = page.next_cursor
        if cursor is None:
            break
    assert tuple(records) == run(conn).records
    assert dict(skips) == dict(run(conn).skipped_families)
    assert decode.decode_rows(conn, [], authz_digest="ok").records == ()
    with pytest.raises(decode.ProjectionDecodeError, match="record_not_found"):
        decode.decode_rows(conn, [("absent", "event", 1, "a")], authz_digest="ok")
    row = query.list_records(conn, authz_digest="ok", limit=1).records[0]
    with pytest.raises(decode.ProjectionDecodeError):
        decode.decode_rows(conn, [row, row], authz_digest="ok")
    with pytest.raises(decode.ProjectionDecodeError, match="row_metadata_mismatch"):
        decode.decode_rows(conn, [(row.path, row.family, row.byte_length + 1, row.sha256)], authz_digest="ok")


@pytest.fixture
def copied(generation, tmp_path):
    path = tmp_path / "independent.sqlite"
    assert path.is_relative_to(Path("/tmp"))
    shutil.copyfile(generation[0].db_path, path)
    yield path
    path.unlink()


@pytest.mark.parametrize("key,value", [("status", "building"), ("repo_id", None),
                                        ("record_count", "1.5"), ("record_count", "-1"),
                                        ("object_format", "bad")])
def test_bad_meta(copied, key, value):
    with closing(sqlite3.connect(copied)) as writer, writer:
        if value is None:
            writer.execute("DELETE FROM generation_meta WHERE key=?", (key,))
        else:
            writer.execute("UPDATE generation_meta SET value=? WHERE key=?", (value, key))
    with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as conn:
        with pytest.raises(decode.ProjectionDecodeError):
            run(conn)


@pytest.mark.parametrize("kind", ["byte", "encoding", "schema", "repo_id", "company_id"])
def test_corruption_and_scope(copied, kind):
    with closing(sqlite3.connect(copied)) as writer, writer:
        path, raw = writer.execute("SELECT path, canonical_bytes FROM record_canonical WHERE family='fact_current' LIMIT 1").fetchone()
        if kind in ("repo_id", "company_id"):
            writer.execute("UPDATE generation_meta SET value='other-company' WHERE key=?", (kind,))
        else:
            changed = bytes([raw[0] ^ 1]) + raw[1:] if kind == "byte" else raw + b" "
            if kind == "schema":
                payload = git_format.loads_record(raw)
                payload["unexpected"] = 1
                changed = git_format.dumps_record(payload)
            writer.execute("UPDATE record_canonical SET canonical_bytes=? WHERE path=?", (changed, path))
            if kind != "byte":
                writer.execute("UPDATE record_canonical SET byte_length=?, sha256=? WHERE path=?",
                               (len(changed), sha256(changed).hexdigest(), path))
    with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as conn:
        error = decode.ProjectionDecodeError if kind == "byte" else RecordSchemaClosureError
        with pytest.raises(error):
            run(conn, paths=[path])


def test_other_company_generation(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = build(directory, company_id="other-company")
    _publish(built, root, directory, expected_company_id="other-company")
    _, conn = router.open_current(root, repo_id=SCOPE["repo_id"], company_id="other-company")
    with closing(conn):
        assert all(r.payload["company_id"] == "other-company" for r in run(conn).records)
