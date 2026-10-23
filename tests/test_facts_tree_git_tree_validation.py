"""Synthetic checks for the facts Git tree entry validator.

All paths, OIDs and record payloads below are synthetic fixtures built
in this file; no real company data, repositories, Git object stores,
files, databases, models or services are touched. The contract under
test is F-001 P01 sections 10.2.3 and 14.2 (approved design baseline
36f9035555c7a016d0f51eb2a266611a36ab5e3f): a commit tree may only carry
canonical relative paths from the caller whitelist and regular
non-executable files whose content is canonical serialized JSON, and no
entry path may be a proper slash-delimited directory prefix of another
(a file and its own subtree cannot coexist in one tree). The
validator proves only tree physical format and whitelist conformance; it
never proves an object exists, that its Git type is blob, or any
business schema, identity, authorization, reference closure, unique-leaf
or commit-history invariant.
"""
from decimal import Decimal

import pytest

from app.facts_tree import git_format
from app.facts_tree.git_tree_validation import validate_tree_entries

SHA1_OID = "a" * 40
SHA256_OID = "b" * 64


def _content(payload):
    return git_format.dumps_record(payload)


_DEFAULT_CONTENT = object()


def _entry(path, *, mode="100644", oid=SHA1_OID, content=_DEFAULT_CONTENT):
    if content is _DEFAULT_CONTENT:
        content = _content({"record": path})
    return (mode, path, oid, content)


def _validate(entries, paths, object_format="sha1"):
    return validate_tree_entries(
        entries, expected_paths=paths, object_format=object_format)


# -- valid trees -----------------------------------------------------------


def test_valid_tree_with_multiple_entries_returns_none():
    paths = [
        "meta/repository.json",
        "facts/f1/current.json",
        "facts/f1/revisions/1.json",
    ]
    entries = [_entry(path) for path in paths]
    assert _validate(entries, paths) is None


def test_valid_sha256_tree():
    paths = ["meta/repository.json", "bindings/current.json"]
    entries = [_entry(path, oid=SHA256_OID) for path in paths]
    assert _validate(entries, paths, object_format="sha256") is None


def test_empty_tree_and_empty_whitelist_accepted():
    assert _validate([], []) is None


def test_empty_object_payload_accepted():
    entry = _entry("meta/repository.json", content=_content({}))
    assert _validate([entry], ["meta/repository.json"]) is None


def test_decimal_payload_round_trips_byte_exact():
    payload = {"amount": Decimal("1.10"), "scale": Decimal("0.0001")}
    entry = _entry("facts/f1/current.json", content=_content(payload))
    assert _validate([entry], ["facts/f1/current.json"]) is None


def test_entries_and_whitelist_may_be_generators():
    paths = ["meta/repository.json", "events/e1.json"]
    entries = (_entry(path) for path in paths)
    whitelist = (path for path in paths)
    assert _validate(entries, whitelist) is None


def test_inputs_are_not_mutated():
    paths = ["meta/repository.json", "events/e1.json"]
    entries = [_entry(path) for path in paths]
    snapshot = list(entries)
    whitelist = list(paths)
    _validate(entries, whitelist)
    assert entries == snapshot
    assert whitelist == paths


# -- object_format ---------------------------------------------------------


@pytest.mark.parametrize("object_format", ["sha512", "SHA1", "Sha256", ""])
def test_unsupported_object_format_rejected(object_format):
    entry = _entry("a.json")
    with pytest.raises(ValueError, match="object_format"):
        _validate([entry], ["a.json"], object_format=object_format)


@pytest.mark.parametrize("object_format", [None, 1, b"sha1"])
def test_object_format_must_be_str(object_format):
    entry = _entry("a.json")
    with pytest.raises(TypeError, match="object_format"):
        _validate([entry], ["a.json"], object_format=object_format)


def test_object_format_is_checked_before_entries_and_whitelist():
    bad_entry = ("bad-mode", "../bad.json", "z", b"no")
    with pytest.raises(ValueError, match="object_format"):
        _validate([bad_entry], ["../bad.json"], object_format="sha512")


# -- entry structure -------------------------------------------------------


@pytest.mark.parametrize("entry", [
    ["100644", "a.json", SHA1_OID, _content({})],
    "100644",
    None,
])
def test_entry_must_be_a_tuple(entry):
    with pytest.raises(TypeError, match="tuple"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("entry", [
    ("100644", "a.json", SHA1_OID),
    ("100644", "a.json", SHA1_OID, _content({}), "extra"),
    (),
])
def test_entry_must_have_exactly_four_items(entry):
    with pytest.raises(ValueError, match="exactly 4"):
        _validate([entry], ["a.json"])


def test_entries_must_not_be_a_bare_str_or_bytes():
    with pytest.raises(TypeError, match="iterable"):
        _validate("meta/repository.json", ["meta/repository.json"])
    with pytest.raises(TypeError, match="iterable"):
        _validate(b"bytes", ["meta/repository.json"])


def test_entries_must_be_iterable():
    with pytest.raises(TypeError, match="iterable"):
        _validate(None, ["a.json"])


# -- mode ------------------------------------------------------------------


@pytest.mark.parametrize("mode", [
    "120000",   # symlink
    "100755",   # executable file
    "160000",   # Git submodule (gitlink)
    "040000",   # directory
    "100664",   # unknown regular-file mode
    "",
    "1006440",
])
def test_forbidden_or_unknown_modes_rejected(mode):
    entry = _entry("a.json", mode=mode)
    with pytest.raises(ValueError, match="mode"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("mode", [0o100644, None, True])
def test_mode_must_be_str(mode):
    entry = _entry("a.json", mode=mode)
    with pytest.raises(TypeError, match="mode"):
        _validate([entry], ["a.json"])


# -- oid shape -------------------------------------------------------------


@pytest.mark.parametrize("oid", ["", "a", "a" * 39, "a" * 41, "a" * 64])
def test_sha1_oid_wrong_length_rejected(oid):
    entry = _entry("a.json", oid=oid)
    with pytest.raises(ValueError, match="oid"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("oid", ["b" * 40, "b" * 63, "b" * 65])
def test_sha256_oid_wrong_length_rejected(oid):
    entry = _entry("a.json", oid=oid)
    with pytest.raises(ValueError, match="oid"):
        _validate([entry], ["a.json"], object_format="sha256")


@pytest.mark.parametrize("oid", [
    "A" * 40,
    "a" * 39 + "F",
    "g" * 40,
    "a" * 20 + " " + "a" * 19,
])
def test_oid_must_be_lowercase_hex(oid):
    entry = _entry("a.json", oid=oid)
    with pytest.raises(ValueError, match="lowercase hex"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("oid", [None, 123, b"a" * 40])
def test_oid_must_be_str(oid):
    entry = _entry("a.json", oid=oid)
    with pytest.raises(TypeError, match="oid"):
        _validate([entry], ["a.json"])


# -- entry paths -----------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/absolute.json",
    "a//b.json",
    "a/",
    ".",
    "..",
    "a/../b.json",
    "a/./b.json",
    "a\\b.json",
    "a\x00b.json",
    "",
])
def test_unsafe_entry_paths_rejected(path):
    entry = _entry(path)
    with pytest.raises(ValueError):
        _validate([entry], ["facts/x.json"])


@pytest.mark.parametrize("path", [b"a.json", None, 7])
def test_entry_path_must_be_str(path):
    entry = _entry(path, content=_content({}))
    with pytest.raises(TypeError, match="path"):
        _validate([entry], ["facts/x.json"])


def test_duplicate_entry_path_rejected():
    first = _entry("a.json")
    second = _entry("a.json", content=_content({"other": 1}))
    with pytest.raises(ValueError, match="duplicated"):
        _validate([first, second], ["a.json"])


def test_extra_entry_path_not_in_whitelist_rejected():
    entry = _entry("facts/extra.json")
    with pytest.raises(ValueError, match="whitelist"):
        _validate([entry], ["facts/a.json"])


def test_missing_whitelisted_path_rejected():
    entry = _entry("facts/a.json")
    with pytest.raises(ValueError, match="missing"):
        _validate([entry], ["facts/a.json", "facts/b.json"])


# -- cross-entry file/directory prefix conflicts ----------------------------


@pytest.mark.parametrize("paths", [
    ["a", "a/child.json"],
    ["a/child.json", "a"],
])
def test_file_directory_prefix_conflict_rejected_both_orders(paths):
    entries = [_entry(path) for path in paths]
    with pytest.raises(ValueError, match="conflict"):
        _validate(entries, list(paths))


def test_lexical_gap_does_not_hide_prefix_conflict():
    # "a-b" sorts between "a" and "a/child.json" because "-" < "/", so
    # checking only adjacent sorted paths would miss the conflict.
    paths = ["a", "a-b", "a/child.json"]
    entries = [_entry(path) for path in paths]
    with pytest.raises(ValueError, match="conflict"):
        _validate(entries, paths)


@pytest.mark.parametrize("paths", [
    ["a", "a/b/c.json"],        # transitive: "a" is a depth-1 ancestor
    ["a/b", "a/b/c.json"],      # conflict at a deeper directory level
    ["sub/dir", "sub/dir/leaf.json", "other.json"],
])
def test_nested_prefix_conflict_rejected(paths):
    entries = [_entry(path) for path in paths]
    with pytest.raises(ValueError, match="conflict"):
        _validate(entries, paths)


@pytest.mark.parametrize("paths", [
    ["a-b", "a.json"],
    ["a", "a-b"],
    ["a", "ab/c.json"],
    ["a/x.json", "a-b/y.json"],
])
def test_legal_neighboring_paths_accepted(paths):
    entries = [_entry(path) for path in paths]
    assert _validate(entries, paths) is None


def test_prefix_conflict_reported_before_missing_paths():
    # Documented precedence: the cross-entry prefix-conflict check runs
    # after the per-entry checks and before the missing-paths
    # comparison.
    entries = [_entry("a"), _entry("a/child.json")]
    with pytest.raises(ValueError, match="conflict"):
        _validate(entries, ["a", "a/child.json", "missing.json"])


def test_caller_lists_unchanged_on_prefix_conflict_failure():
    paths = ["a", "a/child.json"]
    entries = [_entry(path) for path in paths]
    entries_snapshot = list(entries)
    whitelist = list(paths)
    with pytest.raises(ValueError, match="conflict"):
        _validate(entries, whitelist)
    assert entries == entries_snapshot
    assert whitelist == paths


def test_conflict_report_is_independent_of_entry_order():
    forward = [_entry("a"), _entry("a/child.json")]
    reversed_entries = [_entry("a/child.json"), _entry("a")]
    messages = []
    for entries in (forward, reversed_entries):
        with pytest.raises(ValueError) as excinfo:
            _validate(entries, ["a", "a/child.json"])
        messages.append(str(excinfo.value))
    assert messages[0] == messages[1]
    assert "'a'" in messages[0]
    assert "'a/child.json'" in messages[0]


# -- whitelist -------------------------------------------------------------


def test_whitelist_duplicate_path_rejected():
    with pytest.raises(ValueError, match="duplicated"):
        _validate([], ["a.json", "a.json"])


@pytest.mark.parametrize("path", ["../a.json", "/a.json", "a\\b.json", ""])
def test_whitelist_unsafe_path_rejected(path):
    with pytest.raises(ValueError):
        _validate([], [path])


def test_whitelist_path_must_be_str():
    with pytest.raises(TypeError, match="path"):
        _validate([], [b"a.json"])


def test_whitelist_must_not_be_a_bare_str_or_bytes():
    entry = _entry("a.json")
    with pytest.raises(TypeError, match="iterable"):
        _validate([entry], "a.json")
    with pytest.raises(TypeError, match="iterable"):
        _validate([entry], b"a.json")


def test_whitelist_must_be_iterable():
    with pytest.raises(TypeError, match="iterable"):
        _validate([], None)


# -- content bytes ---------------------------------------------------------


@pytest.mark.parametrize("content", [
    b"{not json",
    b"[1, 2]",
    b'"text"',
    b"42",
    b"null",
])
def test_illegal_json_or_non_object_payload_rejected(content):
    entry = _entry("a.json", content=content)
    with pytest.raises(ValueError, match="content"):
        _validate([entry], ["a.json"])


def test_duplicate_json_keys_rejected():
    entry = _entry("a.json", content=b'{"a": 1, "a": 2}\n')
    with pytest.raises(ValueError, match="content"):
        _validate([entry], ["a.json"])


def test_nan_payload_rejected():
    entry = _entry("a.json", content=b'{"a": NaN}\n')
    with pytest.raises(ValueError, match="content"):
        _validate([entry], ["a.json"])


def test_invalid_utf8_content_rejected():
    entry = _entry("a.json", content=b'\xff\xfe{"a": 1}\n')
    with pytest.raises(ValueError, match="UTF-8"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("content", [
    # Valid JSON, but keys are not in canonical sorted order.
    b'{\n  "b": 1,\n  "a": 2\n}\n',
    # Canonical bytes plus an extra trailing newline.
    _content({"a": 1}) + b"\n",
    # CRLF line endings instead of LF.
    b'{\r\n  "a": 1\r\n}\r\n',
    # Compact JSON without the canonical indentation.
    b'{"a": 1}\n',
])
def test_non_canonical_serialization_rejected(content):
    entry = _entry("a.json", content=content)
    with pytest.raises(ValueError, match="canonical"):
        _validate([entry], ["a.json"])


@pytest.mark.parametrize("content", ['{"a": 1}\n', bytearray(b"{}\n"), None])
def test_content_must_be_bytes(content):
    entry = _entry("a.json", content=content)
    assert entry[3] is content
    with pytest.raises(TypeError, match="content"):
        _validate([entry], ["a.json"])
