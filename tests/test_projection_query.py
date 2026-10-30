"""D4 public API checks against real D2 builds and D3 read-only publication."""
import base64
from contextlib import closing
from hashlib import sha256
import hmac
import importlib.util
import json
import sqlite3
import sys

import pytest

from app.facts_tree import projection_query as query, projection_router as router
from tests.test_projection_router import _build, _publish, _mutate_meta, SCOPE

KEY = b"test-only-D4-host-signing-key"


@pytest.fixture(autouse=True)
def configured_key():
    query.set_cursor_key(KEY)


@pytest.fixture
def generation(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    serving, connection = router.open_current(root, **SCOPE)
    try:
        yield built, serving, connection, root, directory
    finally:
        connection.close()


def page(connection, **kwargs):
    return query.list_records(connection, **{"authz_digest": "authorized", **kwargs})


def unpack(token):
    return json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))


def signed(data):
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    enc = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    return enc(raw) + "." + enc(hmac.digest(KEY, raw, "sha256"))


@pytest.mark.parametrize("limit", [1, 2, 7, 200, 1000])
def test_pages_equal_independent_oracle(generation, limit):
    _, serving, conn, root, _ = generation
    expected = conn.execute("SELECT path FROM record_canonical ORDER BY path").fetchall()
    paths, cursor = [], None
    while True:
        # Every page uses a fresh D3 read-only connection.
        _, fresh = router.open_current(root, **SCOPE)
        with closing(fresh):
            result = page(fresh, limit=limit, cursor=cursor)
            assert result == page(fresh, limit=limit, cursor=cursor)
        assert result.record_count == serving.record_count
        assert len(result.records) <= limit
        paths.extend((row.path,) for row in result.records)
        cursor = result.next_cursor
        if cursor is None:
            break
        assert len(result.records) == limit
        assert set(unpack(cursor)) == {"v", "generation_id", "projected_commit", "query_digest",
                                      "authz_digest", "last_path", "limit"}
    assert paths == expected
    assert len(set(paths)) == len(paths)


@pytest.mark.parametrize("family", [None, "repository_meta", "fact_current", "absent"])
@pytest.mark.parametrize("prefix", [None, "meta/", "facts/", "%", "_", "\\", "meta/%_\\", "META/"])
def test_filters(generation, family, prefix):
    conn = generation[2]
    oracle = [row[0] for row in conn.execute("SELECT path, family FROM record_canonical ORDER BY path")
              if (family is None or row[1] == family) and (prefix is None or row[0].startswith(prefix))]
    result = page(conn, family=family, path_prefix=prefix)
    assert [row.path for row in result.records] == oracle
    assert result.next_cursor is None


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "2", None])
def test_bad_limit(generation, limit):
    with pytest.raises(ValueError):
        page(generation[2], limit=limit)


@pytest.mark.parametrize("options", [{"authz_digest": ""}, {"authz_digest": None},
                                    {"family": ""}, {"family": 2}, {"path_prefix": ""},
                                    {"path_prefix": False}, {"include_canonical": True}])
def test_input_validation(generation, options):
    with pytest.raises(query.ProjectionQueryError):
        page(generation[2], **options)


def test_connection_type():
    with pytest.raises(query.ProjectionQueryError):
        page(object())


@pytest.mark.parametrize("field,value", [("last_path", "changed"), ("query_digest", "changed"),
                                         ("limit", 2), ("authz_digest", "changed")])
def test_unsigned_payload_tampering(generation, field, value):
    token = page(generation[2], limit=1).next_cursor
    data = unpack(token)
    data[field] = value
    forged = signed(data).split(".")[0] + "." + token.split(".")[1]
    with pytest.raises(query.InvalidPageCursor, match="^invalid page cursor$"):
        page(generation[2], limit=1, cursor=forged)


@pytest.mark.parametrize("options", [{"family": "repository_meta"}, {"path_prefix": "meta/"},
                                    {"limit": 2}, {"authz_digest": "revoked"}])
def test_changed_context(generation, options):
    conn = generation[2]
    token = page(conn, limit=1).next_cursor
    with pytest.raises(query.InvalidPageCursor) as error:
        page(conn, **{"limit": 1, "cursor": token, **options})
    if "authz_digest" in options:
        assert error.value.reason == "authz_changed"


@pytest.mark.parametrize("damage", ["payload_byte", "hmac", "v", "missing", "extra", "bool", "base64", "json", "shape"])
def test_bad_tokens(generation, damage):
    conn = generation[2]
    token = page(conn, limit=1).next_cursor
    data = unpack(token)
    if damage == "payload_byte":
        token = ("A" if token[0] != "A" else "B") + token[1:]
    elif damage == "hmac":
        token = token.split(".")[0] + "." + "A" * 43
    elif damage == "v":
        data["v"] = 2
        token = signed(data)
    elif damage == "missing":
        del data["last_path"]
        token = signed(data)
    elif damage == "extra":
        data["extra"] = 1
        token = signed(data)
    elif damage == "bool":
        data["v"] = True
        token = signed(data)
    elif damage == "base64":
        token = "%%.@@"
    elif damage == "json":
        token = signed([])
    else:
        token = "bad"
    with pytest.raises(query.InvalidPageCursor):
        page(conn, limit=1, cursor=token)


def test_generation_identity_and_order(generation):
    built, _, conn, root, directory = generation
    token = page(conn, limit=1).next_cursor
    other = _build(directory, "e")
    _publish(other, root, directory)
    _, fresh = router.open_current(root, **SCOPE)
    with closing(fresh), pytest.raises(query.SnapshotExpired):
        page(fresh, limit=1, cursor=token, authz_digest="revoked", family="absent")
    # Defensive impossible-in-production same-id commit mutation, after D3 validation.
    conn.rollback()
    _mutate_meta(built, "projected_commit", "f" * 40)
    with pytest.raises(query.ProjectionAdvanced):
        page(conn, limit=1, cursor=token)


@pytest.mark.parametrize("key,value", [("status", "staging"), ("status", None),
                                     ("generation_id", None), ("projected_commit", None),
                                     ("record_count", None), ("record_count", "1.5"),
                                     ("record_count", "-1")])
def test_invalid_meta(generation, key, value):
    built, _, conn, _, _ = generation
    conn.rollback()
    _mutate_meta(built, key, value)
    with pytest.raises(query.ProjectionQueryError):
        page(conn)


def test_bytes_readonly_and_schema(generation):
    conn = generation[2]
    before = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    changes, transaction = conn.total_changes, conn.in_transaction
    statements = []
    conn.set_trace_callback(statements.append)
    for row in page(conn).records:
        content, family, digest, length = query.get_record_canonical(conn, row.path, authz_digest="authorized")
        assert type(content) is bytes
        assert (family, digest, length) == (row.family, row.sha256, row.byte_length)
        assert len(content) == length and sha256(content).hexdigest() == digest
    with pytest.raises(query.ProjectionQueryError, match="record_not_found"):
        query.get_record_canonical(conn, "missing", authz_digest="authorized")
    conn.set_trace_callback(None)
    assert all(sql.startswith("SELECT ") for sql in statements)
    assert (conn.total_changes, conn.in_transaction) == (changes, transaction)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE forbidden(x)")
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_empty_generation(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory, empty=True)
    _publish(built, root, directory)
    _, conn = router.open_current(root, **SCOPE)
    with closing(conn):
        result = page(conn)
        assert result.records == () and result.next_cursor is None and result.record_count == 0


def test_key_lifecycle_without_mutating_live_module(generation):
    name = "_isolated_projection_query_test"
    spec = importlib.util.spec_from_file_location(name, query.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        with pytest.raises(module.ProjectionQueryError, match="cursor key not configured"):
            module.list_records(generation[2], authz_digest="authorized")
        module.set_cursor_key(KEY)
        module.set_cursor_key(KEY)
        with pytest.raises(ValueError):
            module.set_cursor_key(b"different")
        assert module.list_records(generation[2], authz_digest="authorized") == module.list_records(
            generation[2], authz_digest="authorized")
    finally:
        del sys.modules[name]
