"""D14-1 pure cursor checks and D4 compatibility at the read-only boundary."""
import ast
import base64
from hashlib import sha256
import hmac
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from app.facts_tree import projection_cursor as cursor, projection_query as query

KEY = b"test-only-D4-host-signing-key"
D4_PAYLOAD = dict(v=1, generation_id="generation", projected_commit="c" * 40,
                  query_digest="query", authz_digest="authorized", last_path="事实/路径", limit=1)
D4_KEYS = frozenset(D4_PAYLOAD)


def old_b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_raw(raw):
    return old_b64(raw) + "." + old_b64(hmac.digest(KEY, raw, "sha256"))


def old_token(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sign_raw(raw)


@pytest.fixture
def isolated_cursor():
    name = "_isolated_projection_cursor_test"
    spec = importlib.util.spec_from_file_location(name, cursor.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[name]


@pytest.fixture
def configured_cursor(isolated_cursor):
    isolated_cursor.set_cursor_key(KEY)
    return isolated_cursor


def test_key_lifecycle_without_mutating_live_module(isolated_cursor):
    module = isolated_cursor
    assert module.has_cursor_key() is False
    for operation in (module.require_cursor_key,
                      lambda: module.encode_cursor({"value": 1}),
                      lambda: module.decode_cursor(old_token({"value": 1}))):
        with pytest.raises(module.CursorError, match="^cursor key not configured$"):
            operation()
    module.set_cursor_key(KEY)
    module.set_cursor_key(KEY)
    assert module.has_cursor_key() is True
    assert module.require_cursor_key() is None
    with pytest.raises(ValueError, match="^cursor key already configured$"):
        module.set_cursor_key(b"different")
    token = module.encode_cursor(D4_PAYLOAD)
    assert token == old_token(D4_PAYLOAD)
    assert module.decode_cursor(token) == D4_PAYLOAD


@pytest.mark.parametrize("key", [None, "secret", b"", bytearray(b"secret"), 1, True])
def test_bad_key_does_not_configure_or_replace(isolated_cursor, key):
    module = isolated_cursor
    with pytest.raises(ValueError, match="^cursor key must be nonempty bytes$"):
        module.set_cursor_key(key)
    assert module.has_cursor_key() is False
    module.set_cursor_key(KEY)
    before = module.encode_cursor(D4_PAYLOAD)
    with pytest.raises(ValueError, match="^cursor key must be nonempty bytes$"):
        module.set_cursor_key(key)
    assert module.encode_cursor(D4_PAYLOAD) == before


@pytest.mark.parametrize("keys", [None, D4_KEYS, set(D4_KEYS), tuple(sorted(D4_KEYS))])
def test_old_formula_roundtrip_and_determinism(configured_cursor, keys):
    module = configured_cursor
    token = module.encode_cursor(D4_PAYLOAD)
    assert token == module.encode_cursor(dict(reversed(list(D4_PAYLOAD.items()))))
    assert token == old_token(D4_PAYLOAD)
    assert module.decode_cursor(token, expected_keys=keys) == D4_PAYLOAD


def test_scalar_values_keep_json_types(configured_cursor):
    payload = {"text": "中文\x00", "empty": "", "number": -123, "zero": 0,
               "yes": True, "no": False, "nothing": None}
    decoded = configured_cursor.decode_cursor(configured_cursor.encode_cursor(payload))
    assert decoded == payload
    assert {key: type(value) for key, value in decoded.items()} == {
        key: type(value) for key, value in payload.items()}


@pytest.mark.parametrize("payload", [None, [], (), {}, {"": 1}, {1: "value"},
                                     {"x": []}, {"x": {}}, {"x": ()}, {"x": b"bytes"},
                                     {"x": 1.0}, {"x": float("nan")}, {"x": object()}])
def test_encode_rejects_non_scalar_or_invalid_payload(configured_cursor, payload):
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.encode_cursor(payload)


@pytest.mark.parametrize("damage", ["payload_byte", "signature", "swap", "extra_segment",
                                    "missing_segment", "padding", "signature_padding",
                                    "non_url_payload", "non_url_signature", "pad_bits",
                                    "empty_payload", "empty_signature", "non_ascii"])
def test_tampered_or_noncanonical_tokens(configured_cursor, damage):
    token = old_token(D4_PAYLOAD)
    payload, signature = token.split(".")
    tokens = {
        "payload_byte": ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature,
        "signature": payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:],
        "swap": signature + "." + payload,
        "extra_segment": token + ".extra",
        "missing_segment": payload,
        "padding": payload + "=." + signature,
        "signature_padding": token + "=",
        "non_url_payload": sign_raw(b"\xfb").replace("-", "+", 1),
        "non_url_signature": payload + "." + "/" * 43,
        # A SHA-256 MAC has two unused bits in its last base64 character.
        "pad_bits": payload + "." + signature[:-1] +
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(signature[-1]) + 1],
        "empty_payload": "." + signature,
        "empty_signature": payload + ".",
        "non_ascii": "中文." + signature,
    }
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.decode_cursor(tokens[damage])


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}',
                                 b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
                                 b'{"x":[NaN]}', b"{", b"[]", b"null", b"1", b'"text"',
                                 b"\xff"])
def test_signed_bad_json_or_non_object_is_uniformly_rejected(configured_cursor, raw):
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.decode_cursor(sign_raw(raw))


def test_json_recursion_error_is_uniformly_rejected(configured_cursor, monkeypatch):
    def recursion_error(*args, **kwargs):
        raise RecursionError("parser recursion")

    monkeypatch.setattr(configured_cursor.json, "loads", recursion_error)
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.decode_cursor(old_token(D4_PAYLOAD))


@pytest.mark.parametrize("token", [None, b"a.b", 1, True, [], {}, "", ".", "a.b.c", "%%.@@"])
def test_invalid_token_types_and_shapes(configured_cursor, token):
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.decode_cursor(token)


@pytest.mark.parametrize("keys", [D4_KEYS | {"extra"}, D4_KEYS - {"v"}, frozenset(), set(), (),
                                  ["v"], "v", 1, ("",), (1,), ([],)])
def test_expected_keys_fail_closed(configured_cursor, keys):
    with pytest.raises(configured_cursor.CursorError, match="^invalid page cursor$"):
        configured_cursor.decode_cursor(old_token(D4_PAYLOAD), expected_keys=keys)


def test_decode_leaves_field_semantics_to_the_caller(configured_cursor):
    payload = {**D4_PAYLOAD, "v": "1"}
    assert configured_cursor.decode_cursor(old_token(payload), expected_keys=D4_KEYS) == payload


def test_module_imports_and_calls_are_pure_standard_library():
    tree = ast.parse(Path(cursor.__file__).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add(node.module)
    assert imports <= {"base64", "binascii", "hashlib", "hmac", "json", "typing"}
    local_functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            assert node.func.id in local_functions | {
                "CursorError", "ValueError", "type", "any", "set", "len"}
        else:
            assert isinstance(node.func, ast.Attribute)
            assert node.func.attr in {
                "compare_digest", "digest", "urlsafe_b64encode", "b64decode",
                "rstrip", "decode", "encode", "dumps", "loads", "values", "split"}


@pytest.fixture
def readonly_connection(tmp_path):
    from app.facts_tree import projection_router as router
    from tests.test_projection_router import _build, _publish, SCOPE

    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    _, connection = router.open_current(root, **SCOPE)
    try:
        yield connection
    finally:
        connection.close()


def test_query_wrapper_converts_unconfigured_key_error(readonly_connection, monkeypatch):
    def missing_key():
        raise cursor.CursorError("cursor key not configured")

    monkeypatch.setattr(query, "require_cursor_key", missing_key)
    with pytest.raises(query.ProjectionQueryError, match="^cursor key not configured$") as error:
        query.list_records(readonly_connection, authz_digest="authorized")
    assert type(error.value) is query.ProjectionQueryError
    assert not isinstance(error.value, cursor.CursorError)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        readonly_connection.execute("CREATE TABLE forbidden(x)")


def test_d4_pages_and_tokens_match_old_formula_in_both_directions(readonly_connection):
    assert query.set_cursor_key is cursor.set_cursor_key
    query.set_cursor_key(KEY)
    connection = readonly_connection
    first = query.list_records(connection, authz_digest="authorized", limit=1)
    digest = sha256("\n".join([repr(None), repr(None), str(False), str(1)]).encode()).hexdigest()
    payload = dict(v=1, generation_id=first.generation_id, projected_commit=first.projected_commit,
                   query_digest=digest, authz_digest="authorized", last_path=first.records[0].path, limit=1)
    assert first.next_cursor == old_token(payload) == cursor.encode_cursor(payload)
    assert cursor.decode_cursor(first.next_cursor, expected_keys=D4_KEYS) == payload
    legacy_page = query.list_records(connection, authz_digest="authorized", limit=1,
                                     cursor=old_token(payload))
    shared_page = query.list_records(connection, authz_digest="authorized", limit=1,
                                     cursor=cursor.encode_cursor(payload))
    assert legacy_page == shared_page
    expected = connection.execute("SELECT path FROM record_canonical ORDER BY path").fetchall()
    assert [first.records[0].path, shared_page.records[0].path] == [row[0] for row in expected[:2]]


@pytest.mark.parametrize("field,value", [("v", "1"), ("v", True), ("v", 2), ("limit", True),
                                         ("limit", 0), ("limit", 1001), ("limit", "1"),
                                         ("generation_id", ""), ("projected_commit", None),
                                         ("query_digest", 1), ("authz_digest", "a\x00b"),
                                         ("last_path", False)])
def test_d4_keeps_its_field_validation(readonly_connection, field, value):
    query.set_cursor_key(KEY)
    payload = {**D4_PAYLOAD, field: value}
    with pytest.raises(query.InvalidPageCursor, match="^invalid page cursor$") as error:
        query.list_records(readonly_connection, authz_digest="authorized", limit=1,
                           cursor=cursor.encode_cursor(payload))
    assert type(error.value) is query.InvalidPageCursor
    assert query.InvalidPageCursor.__bases__ == (query.ProjectionQueryError,)
    assert not isinstance(error.value, cursor.CursorError)
