"""D14-3 pinned paging against real jointly published read-only bundles."""
import ast
import base64
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from app.facts_tree import git_format, projection_bundle as bundles
from app.facts_tree import projection_cursor as cursor, projection_query as d4
from app.facts_tree import projection_derived_query as query, projection_fts as fts
from app.facts_tree.record_schema import SCHEMA_COLUMNS
from tests.facts_tree_full_fixture import SCOPE
from tests.test_projection_advance_service import _run
from tests.test_projection_incremental import _append, chain  # noqa: F401

KEY = b"test-only-D4-host-signing-key"
AUTHZ = "authorized"


@pytest.fixture(autouse=True)
def configured_key():
    d4.set_cursor_key(KEY)


@pytest.fixture
def published(chain, tmp_path):
    repo, commits = chain
    root, directory = tmp_path / "root", tmp_path / "generations"
    _run(repo, root, directory, target=commits[1])
    with bundles.open_bundle(root, **SCOPE) as handle:
        yield handle, root, directory, repo, commits


def unpack(token):
    raw = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def word_for(handle, family="fact_revision"):
    rows = handle.derived.execute("SELECT record_text FROM fts_records WHERE family=?", (family,)).fetchall()
    words = sorted({word for row in rows for word in fts.tokenize_text(row[0]).split()
                    if len(word) >= 2 and any("\u4e00" <= c <= "\u9fff" for c in word)})
    assert words, family
    for word in words:
        expression = '"' + word.replace('"', '""') + '"'
        count = handle.derived.execute(
            "SELECT count(*) FROM fts_records WHERE fts_records MATCH ? AND family=?",
            (expression, family)).fetchone()[0]
        if count >= 2:
            return word
    pytest.fail("fixture must provide at least two searchable revisions")


def wide(handle, **kwargs):
    return query.list_family_rows(handle, "fact_revision", **{"authz_digest": AUTHZ, "limit": 1, **kwargs})


def search(handle, word, **kwargs):
    return query.search(handle, word, **{"authz_digest": AUTHZ, "limit": 1, **kwargs})


def search_oracle(handle, word, family=None, *, current=False, status=None, leaf_code=None):
    # Independent SQL, with SQLite (rather than Python float formatting) defining score text.
    expression = '"' + word.replace('"', '""') + '"'
    sql = "SELECT f.path,f.family,CAST(bm25(fts_records) AS TEXT)"
    sql += (",d.fact_uid,d.revision," + ("w.status" if current else "NULL")
            if family == "fact_revision" else ",NULL,NULL,NULL")
    sql += " FROM fts_records f "
    if family:
        sql += "JOIN dw_" + family + " d ON d.path=f.path "
    if current:
        sql += "JOIN dw_fact_current w ON (w.fact_uid,w.revision)=(d.fact_uid,d.revision) "
    sql += "WHERE fts_records MATCH ?"
    params = [expression]
    if family:
        sql += " AND f.family=?"
        params.append(family)
    if status is not None:
        sql += " AND " + ("w.status" if current else "d.status") + "=?"
        params.append(status)
    if leaf_code is not None:
        sql += " AND w.leaf_code=?"
        params.append(leaf_code)
    sql += " ORDER BY CAST(bm25(fts_records) AS TEXT),f.path LIMIT 1000"
    return [query.SearchHitRow(*row) for row in handle.derived.execute(sql, params)]


def collect_search(handle, word, **options):
    found, token = [], None
    for _ in range(100):
        page = search(handle, word, cursor=token, **options)
        assert page == search(handle, word, cursor=token, **options)
        found.extend(page.hits)
        if page.next_cursor is None:
            return found
        token = page.next_cursor
    pytest.fail("pagination did not terminate")


def test_all_families_page_without_loss_and_keep_system_columns(published):
    handle = published[0]
    for family in SCHEMA_COLUMNS:
        oracle = handle.derived.execute(
            "SELECT path,payload_json,canonical_sha256,byte_length FROM dw_" + family + " ORDER BY path").fetchall()
        rows, token = [], None
        for _ in range(100):
            page = query.list_family_rows(handle, family, authz_digest=AUTHZ, limit=1, cursor=token)
            assert page == query.list_family_rows(handle, family, authz_digest=AUTHZ, limit=1, cursor=token)
            assert (page.generation_id, page.projected_commit, page.family) == (
                handle.serving.generation_id, handle.serving.projected_commit, family)
            rows.extend(page.rows)
            token = page.next_cursor
            if token is None:
                break
        else:
            pytest.fail("wide paging did not terminate")
        assert rows == [query.WideRow(path, family, payload, digest, size)
                        for path, payload, digest, size in oracle]
        assert len({row.path for row in rows}) == len(rows)
        assert query.list_family_rows(handle, family, authz_digest=AUTHZ).rows == tuple(rows)


def test_empty_families_and_empty_search(chain, tmp_path):
    repo, commits = chain
    root = tmp_path / "empty-root"
    _run(repo, root, tmp_path / "empty-generations", target=commits[0])
    with bundles.open_bundle(root, **SCOPE) as handle:
        for family in SCHEMA_COLUMNS:
            page = query.list_family_rows(handle, family, authz_digest=AUTHZ)
            assert page.rows == () and page.next_cursor is None
        page = search(handle, "不会命中")
        assert page.hits == () and page.next_cursor is None and page.record_count == 0


@pytest.mark.parametrize("kind", ["wide", "fts"])
def test_unsigned_tampering_and_signed_bad_shapes(published, kind):
    handle = published[0]
    word = word_for(handle)
    call = (lambda **kw: wide(handle, **kw)) if kind == "wide" else (lambda **kw: search(handle, word, **kw))
    token = call().next_cursor
    assert token
    original = unpack(token)
    digest_key = "family_digest" if kind == "wide" else "search_digest"
    for field, value in [("last_path", "tampered"), ("limit", 2), (digest_key, "tampered"), ("kind", "other")]:
        forged = cursor.encode_cursor({**original, field: value}).split(".")[0] + "." + token.split(".")[1]
        with pytest.raises(d4.InvalidPageCursor, match="^invalid page cursor$"):
            call(cursor=forged)
    for field, value in [("v", True), ("v", "1"), ("kind", "wrong"), ("limit", True),
                         ("limit", 0), ("limit", 1001), (digest_key, "changed"),
                         ("last_path", ""), ("authz_digest", "x\x00y")]:
        with pytest.raises(d4.InvalidPageCursor, match="^invalid page cursor$"):
            call(cursor=cursor.encode_cursor({**original, field: value}))
    if kind == "fts":
        for value in ("", None, 1, "\x00"):
            with pytest.raises(d4.InvalidPageCursor, match="^invalid page cursor$"):
                call(cursor=cursor.encode_cursor({**original, "last_score": value}))
    for bad in [False, b"bytes", "", token + "=", "bad.token", token + ".extra",
                token.split(".")[0] + "." + "A" * 43,
                cursor.encode_cursor({key: value for key, value in original.items() if key != digest_key}),
                cursor.encode_cursor({**original, "extra": 1})]:
        with pytest.raises(d4.InvalidPageCursor, match="^invalid page cursor$"):
            call(cursor=bad)


def test_cross_kind_and_d4_tokens_are_rejected_in_all_directions(published):
    handle = published[0]
    word = word_for(handle)
    tokens = [wide(handle).next_cursor, search(handle, word).next_cursor,
              d4.list_records(handle.standard, authz_digest=AUTHZ, limit=1).next_cursor]
    calls = [lambda token: wide(handle, cursor=token), lambda token: search(handle, word, cursor=token),
             lambda token: d4.list_records(handle.standard, authz_digest=AUTHZ, limit=1, cursor=token)]
    for i, token in enumerate(tokens):
        assert token
        for j, call in enumerate(calls):
            if i != j:
                with pytest.raises(d4.InvalidPageCursor):
                    call(token)


@pytest.mark.parametrize("kind", ["wide", "fts"])
def test_context_changes_and_expiration_order(published, kind):
    handle, root, directory, repo, commits = published
    word = word_for(handle)

    def call(target, **kwargs):
        return wide(target, **kwargs) if kind == "wide" else search(target, word, **kwargs)

    token = call(handle).next_cursor
    assert token
    with pytest.raises(d4.InvalidPageCursor, match="^authz_changed$") as caught:
        call(handle, cursor=token, authz_digest="revoked")
    assert caught.value.reason == "authz_changed"
    with pytest.raises(d4.InvalidPageCursor, match="^invalid page cursor$"):
        call(handle, cursor=token, limit=2, authz_digest="revoked")
    defensive = replace(handle, serving=replace(handle.serving, projected_commit="f" * 40))
    with pytest.raises(d4.ProjectionAdvanced, match="^PROJECTION_ADVANCED$"):
        call(defensive, cursor=token, limit=2, authz_digest="revoked")
    _run(repo, root, directory, target=commits[2])
    with bundles.open_bundle(root, **SCOPE) as fresh:
        with pytest.raises(d4.SnapshotExpired, match="^SNAPSHOT_EXPIRED$"):
            call(fresh, cursor=token, limit=2, authz_digest="revoked")
    assert call(handle, cursor=token).generation_id == handle.serving.generation_id


@pytest.mark.parametrize("family", [None, "fact_revision", "entity_revision"])
def test_fts_history_paging_equals_sqlite_text_score_oracle(published, family):
    handle = published[0]
    word = word_for(handle, family or "fact_revision")
    expected = search_oracle(handle, word, family)
    actual = collect_search(handle, word, family=family)
    assert len(expected) >= 2
    assert actual == expected
    assert [(hit.score, hit.path) for hit in actual] == sorted((hit.score, hit.path) for hit in actual)
    assert all(type(hit.score) is str for hit in actual)
    assert len({hit.path for hit in actual}) == len(actual)
    page = query.search(handle, word, family=family, authz_digest=AUTHZ)
    assert page.hits == tuple(actual) and page.next_cursor is None
    assert page.record_count == int(handle.derived.execute(
        "SELECT value FROM fts_meta WHERE key='record_count'").fetchone()[0])


def test_current_view_filters_and_historical_revision_boundary(published):
    handle = published[0]
    word = word_for(handle)
    history = search_oracle(handle, word, "fact_revision")
    current = search_oracle(handle, word, "fact_revision", current=True)
    assert len(history) == 2 and len(current) == 1
    assert current[0].revision == 2 and any(hit.revision == 1 for hit in history)
    leaf, status = handle.derived.execute("SELECT leaf_code,status FROM dw_fact_current").fetchone()
    for options in [{"current_only": True}, {"status": status}, {"leaf_code": leaf},
                    {"status": status, "leaf_code": leaf, "current_only": True}]:
        actual = collect_search(handle, word, family="fact_revision", **options)
        expected = search_oracle(handle, word, "fact_revision", current=True,
                                 status=options.get("status"), leaf_code=options.get("leaf_code"))
        assert actual == expected == current
    for options in [{"status": "absent"}, {"leaf_code": "absent"}]:
        assert collect_search(handle, word, family="fact_revision", **options) == []
    for options in [{"status": status}, {"leaf_code": leaf}]:
        with pytest.raises(d4.ProjectionQueryError):
            search(handle, word, family="fact_revision", current_only=False, **options)
    entity_word = word_for(handle, "entity_revision")
    actual = collect_search(handle, entity_word, family="entity_revision", status="active")
    assert actual == search_oracle(handle, entity_word, "entity_revision", status="active")
    assert all(hit.fact_uid is hit.revision is hit.status is None for hit in actual)


def test_real_equal_score_ties_use_path_successor(published):
    handle, root, directory, repo, commits = published
    payloads = {record.path: git_format.loads_record(record.content)
                for record in repo.read_snapshot_at(commits[-1]).records}
    previous = payloads[git_format.record_path("fact_revision", fact_uid="fact-1", revision=2)]
    for revision in (3, 4):
        value = deepcopy(previous)
        value.update(revision=revision, statement_raw="共同测试中文陈述", reason="共同测试理由",
                     predecessor_ref={"record_type": "fact_revision", "stable_id": "fact-1", "revision": revision - 1})
        payloads[git_format.record_path("fact_revision", fact_uid="fact-1", revision=revision)] = value
        previous = value
    payloads[git_format.record_path("fact_current", fact_uid="fact-1")]["revision"] = 4
    placement = deepcopy(payloads[git_format.record_path("placement_revision", fact_uid="fact-1", revision=2)])
    placement.update(revision=3, fact_ref={"record_type": "fact_revision", "stable_id": "fact-1", "revision": 4},
                     predecessor_ref={"record_type": "placement_revision", "stable_id": "fact-1", "revision": 2})
    payloads[git_format.record_path("placement_revision", fact_uid="fact-1", revision=3)] = placement
    payloads[git_format.record_path("placement_current", fact_uid="fact-1")]["revision"] = 3
    commit = _append(repo, payloads)
    _run(repo, root, directory, target=commit)
    with bundles.open_bundle(root, **SCOPE) as fresh:
        actual = collect_search(fresh, "共同", family="fact_revision")
        assert len(actual) == 2 and actual[0].score == actual[1].score
        assert actual == search_oracle(fresh, "共同", "fact_revision")
        assert actual[0].path < actual[1].path


def test_request_parameters_are_bound_into_digests(published):
    handle = published[0]
    word = word_for(handle)
    token = search(handle, word, family="fact_revision").next_cursor
    assert token
    for options in [{"query": "其他"}, {"family": None}, {"current_only": True}, {"limit": 2}]:
        with pytest.raises(d4.InvalidPageCursor):
            query.search(handle, **{"query": word, "family": "fact_revision", "authz_digest": AUTHZ,
                                    "limit": 1, "cursor": token, **options})
    with pytest.raises(d4.InvalidPageCursor):
        query.list_family_rows(handle, "entity_revision", authz_digest=AUTHZ, limit=1,
                               cursor=wide(handle).next_cursor)


def test_parameter_guards_and_public_errors(published):
    handle = published[0]
    for limit in (0, -1, 1001, True, 1.0, "1", None):
        for call in (lambda: wide(handle, limit=limit), lambda: search(handle, "示例", limit=limit)):
            with pytest.raises(d4.ProjectionQueryError):
                call()
    for authz in (None, "", "a\x00b", 1):
        with pytest.raises(d4.ProjectionQueryError):
            wide(handle, authz_digest=authz)
        with pytest.raises(d4.ProjectionQueryError):
            search(handle, "示例", authz_digest=authz)
    for family in ("unknown", "fact_revision; DROP TABLE x", [], 1, None):
        with pytest.raises(d4.ProjectionQueryError, match="unknown family"):
            query.list_family_rows(handle, family, authz_digest=AUTHZ)
    for bad_query in (None, 1, "", "  ", "x\x00y"):
        with pytest.raises(d4.ProjectionQueryError) as caught:
            search(handle, bad_query)
        assert not isinstance(caught.value, (cursor.CursorError, fts.FTSBuildError))
    for options, message in [({"family": "bad"}, "unknown family"),
                             ({"family": "fact_current"}, "fact_current has no searchable text"),
                             ({"leaf_code": "x"}, "leaf_code requires a fact family"),
                             ({"family": "evidence", "status": "active"}, "status requires a family with a status column"),
                             ({"current_only": True}, "current_only requires fact_revision"),
                             ({"current_only": 1}, "current_only must be bool or None")]:
        with pytest.raises(d4.ProjectionQueryError, match="^" + message + "$"):
            search(handle, "示例", **options)
    for bad in (None, object()):
        with pytest.raises(d4.ProjectionQueryError):
            wide(bad)
        with pytest.raises(d4.ProjectionQueryError):
            search(bad, "示例")


def test_cursor_and_fts_error_conversion(published, monkeypatch):
    handle = published[0]

    def no_key(*args, **kwargs):
        raise cursor.CursorError("cursor key not configured")

    with monkeypatch.context() as patch:
        patch.setattr(query, "require_cursor_key", no_key)
        for call in (lambda: wide(handle), lambda: search(handle, "示例")):
            with pytest.raises(d4.ProjectionQueryError, match="^cursor key not configured$") as caught:
                call()
            assert type(caught.value) is d4.ProjectionQueryError
    with monkeypatch.context() as patch:
        patch.setattr(query, "encode_cursor", no_key)
        with pytest.raises(d4.ProjectionQueryError, match="^cursor key not configured$"):
            wide(handle)
    with monkeypatch.context() as patch:
        patch.setattr(query.fts, "build_match_expression", Mock(side_effect=fts.FTSBuildError("private-detail")))
        with pytest.raises(d4.ProjectionQueryError, match="^invalid query$"):
            search(handle, "示例")


@pytest.mark.parametrize("row", [None, ("",), ("bad",), ("1.0",), ("-1",), (None,), (1,)])
def test_record_count_guard(published, row):
    handle = published[0]
    proxy = Mock(wraps=handle.derived)
    response = Mock()
    response.fetchone.return_value = row
    proxy.execute = Mock(return_value=response)
    test_handle = replace(handle, derived=proxy)
    with pytest.raises(d4.ProjectionQueryError, match="^invalid fts record_count$"):
        search(test_handle, "示例")


def test_readonly_determinism_dataclasses_and_opaque_tokens(published):
    handle = published[0]
    word = word_for(handle)
    statements = []
    handle.derived.set_trace_callback(statements.append)
    before = handle.derived.total_changes
    first, second = search(handle, word), search(handle, word)
    wide_first, wide_second = wide(handle), wide(handle)
    assert first.next_cursor == second.next_cursor and first.next_cursor
    assert wide_first.next_cursor == wide_second.next_cursor and wide_first.next_cursor
    # FTS5 traces internal reads with '-- ', including this read-only version probe.
    unexpected = [sql for sql in statements if not sql.removeprefix("-- ").startswith("SELECT ")
                  and sql != "-- PRAGMA 'main'.data_version"]
    assert not unexpected, unexpected
    assert handle.derived.total_changes == before
    handle.derived.set_trace_callback(None)
    for sql in ("CREATE TABLE forbidden(x)", "INSERT INTO dw_evidence(path) VALUES ('forbidden')"):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            handle.derived.execute(sql)
    for token, digest_key in [(first.next_cursor, "search_digest"), (wide_first.next_cursor, "family_digest")]:
        data = unpack(token)
        assert word not in json.dumps(data, ensure_ascii=False)
        assert len(data[digest_key]) == 64
        assert "query" not in data and "family" not in data
    for obj in (first, first.hits[0], wide_first, wide_first.rows[0]):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, fields(obj)[0].name, None)
    assert [field.name for field in fields(query.SearchHitRow)] == ["path", "family", "score", "fact_uid", "revision", "status"]


def test_import_and_readonly_sql_guard():
    source = Path(query.__file__).read_text()
    tree = ast.parse(source)
    allowed = {"dataclasses", "hashlib", "sqlite3", "typing", "app.facts_tree.projection_bundle",
               "app.facts_tree.projection_cursor", "app.facts_tree.projection_query", "app.facts_tree.record_schema"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            if node.module == "app.facts_tree":
                assert [(alias.name, alias.asname) for alias in node.names] == [("projection_fts", "fts")]
            else:
                assert node.module in allowed
                if node.module == "app.facts_tree.projection_bundle":
                    assert [alias.name for alias in node.names] == ["ProjectionBundle"]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"connect", "open_bundle", "open_current", "commit", "rollback", "executemany", "executescript"}
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "fts":
                assert node.func.attr == "build_match_expression"
        elif isinstance(node, ast.Name) and node.id == "ProjectionBundle" and isinstance(node.ctx, ast.Load):
            assert any(isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                       and parent.func.id == "isinstance" and node in parent.args for parent in ast.walk(tree))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.strip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ", "ATTACH ", "PRAGMA "))
