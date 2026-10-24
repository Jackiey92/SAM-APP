"""Pinned checks for the versioned facts Git record format helpers.

Covers the approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f
sections 10.2.2-10.2.3 plus DEC-030 approved appendix 97 section 2.1:
deterministic serialization, exact decimal handling, host-only path
construction from fixed record types and opaque IDs, and numeric revision
parameters whose path segments are canonical ASCII decimal strings.
"""
from array import array
from decimal import Decimal

import pytest

from app.facts_tree import git_format


def test_format_version_and_limits_documented():
    assert git_format.FORMAT_VERSION == 1
    assert git_format.MAX_DEPTH == 32
    assert git_format.MAX_RECORD_BYTES == 1024 * 1024
    doc = git_format.__doc__
    assert "FORMAT_VERSION" in doc
    assert "MAX_DEPTH" in doc
    assert "MAX_RECORD_BYTES" in doc
    assert "decimal" in doc
    assert "float is rejected" in doc.lower() or "float is never accepted" in doc


def test_key_order_is_sorted_and_stable():
    payload = {"b": 1, "a": {"d": 2, "c": 3}, "Z": 0}
    first = git_format.dumps_record(payload)
    second = git_format.dumps_record(payload)
    assert first == second
    text = first.decode("utf-8")
    assert text.index('"Z"') < text.index('"a"') < text.index('"b"')
    assert text.index('"c"') < text.index('"d"')


def test_utf8_raw_strings_and_array_order_preserved():
    payload = {
        "原文": "否定词不能规范化",
        "quote顺序": ["丙", "甲", "乙"],
        "emoji": "🦀",
    }
    data = git_format.dumps_record(payload)
    assert "否定词不能规范化".encode("utf-8") in data
    assert "🦀".encode("utf-8") in data
    text = data.decode("utf-8")
    assert text.index('"丙"') < text.index('"甲"') < text.index('"乙"')
    assert git_format.loads_record(data) == payload


def test_output_shape_lf_and_single_trailing_newline():
    data = git_format.dumps_record({"a": [1, {"b": None}], "c": True})
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")
    assert b"\r" not in data


def test_round_trip_preserves_ints_and_strings():
    payload = {
        "big": 10**40,
        "text": "line1\nline2\t\"quoted\"",
        "flag": False,
        "nothing": None,
        "nested": {"list": [1, "二", None, True]},
    }
    assert git_format.loads_record(git_format.dumps_record(payload)) == payload


def test_top_level_must_be_object():
    with pytest.raises(TypeError):
        git_format.dumps_record([1, 2])
    with pytest.raises(ValueError, match="JSON object"):
        git_format.loads_record(b"[1, 2]")


def test_duplicate_json_keys_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        git_format.loads_record(b'{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="duplicate"):
        git_format.loads_record(b'{"a": {"x": 1, "x": 2}}')


def test_nan_and_infinity_rejected():
    for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
        with pytest.raises(ValueError, match="non-finite"):
            git_format.loads_record(raw)
    with pytest.raises(ValueError):
        git_format.dumps_record({"a": Decimal("NaN")})
    with pytest.raises(ValueError):
        git_format.dumps_record({"a": Decimal("-Infinity")})
    with pytest.raises(TypeError):
        git_format.dumps_record({"a": float("inf")})


def test_invalid_utf8_rejected():
    with pytest.raises(UnicodeDecodeError):
        git_format.loads_record(b'{"a": "\xff\xfe"}')


def test_loads_rejects_escaped_lone_surrogates():
    # Python's json keeps an unpaired \uXXXX escape as a lone surrogate,
    # which can never be encoded back to UTF-8; the record must be
    # rejected before it is returned, on both keys and values.
    with pytest.raises(ValueError, match="surrogate"):
        git_format.loads_record(bytes.fromhex("7b2278223a225c7564383030227d"))
    with pytest.raises(ValueError, match="surrogate"):
        git_format.loads_record(b'{"x": "\\udc00"}')
    with pytest.raises(ValueError, match="surrogate"):
        git_format.loads_record(b'{"\\ud800": 1}')
    with pytest.raises(ValueError, match="surrogate"):
        git_format.loads_record(b'{"x": ["ok", "\\udfff"]}')
    with pytest.raises(ValueError, match="surrogate"):
        git_format.loads_record('{"x": "\\ud800"}')
    # A literal lone surrogate in a str input is also rejected before
    # return (the size precheck encodes the input and fails closed with
    # UnicodeEncodeError, a ValueError subclass).
    with pytest.raises(ValueError):
        git_format.loads_record('{"x": "\ud800"}')


def test_dumps_rejects_lone_surrogate_strings():
    # A Python str carrying a lone surrogate is not valid UTF-8 text and
    # must never be encoded into a record, as key or as value.
    for payload in ({"x": "\ud800"}, {"x": "\udc00"}, {"\ud800": 1},
                    {"x": ["ok", "\udfff"]}):
        with pytest.raises(ValueError):
            git_format.dumps_record(payload)


def test_legal_unicode_and_surrogate_pairs_round_trip():
    payload = {"emoji": "🦀", "text": "汉字 é", "music": "𝄞"}
    data = git_format.dumps_record(payload)
    assert git_format.loads_record(data) == payload
    # A properly paired \uXXXX escape is legal JSON for a non-BMP
    # character and must stay readable.
    assert git_format.loads_record(b'{"x": "\\ud83e\\udd80"}') == {"x": "🦀"}
    assert git_format.loads_record(b'{"x": "\\ud834\\udd1e"}') == {"x": "𝄞"}


def test_float_rejected_so_decimals_never_binary_rewritten():
    with pytest.raises(TypeError, match="float"):
        git_format.dumps_record({"x": 0.1})
    loaded = git_format.loads_record(b'{"x": 0.1}')
    assert type(loaded["x"]) is Decimal
    assert loaded["x"] == Decimal("0.1")
    data = git_format.dumps_record(loaded)
    assert b"0.1" in data
    assert b"0.1000000000000000" not in data
    precise = {"x": Decimal("3.141592653589793238462643383279")}
    assert git_format.loads_record(git_format.dumps_record(precise)) == precise


def test_decimal_exponent_budget():
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("1E+999999999")})
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("1E-999999999")})
    with pytest.raises(ValueError, match="exponent"):
        git_format.loads_record(b'{"x": 1E+999999999}')


def test_zero_decimal_extreme_exponent():
    # A zero value has adjusted() == 0, so the raw exponent must be
    # checked explicitly on both ends or 0E-+huge would expand into a
    # giant zero-padded fixed-point string.
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("0E-999999999")})
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("0E+999999999")})
    with pytest.raises(ValueError, match="exponent"):
        git_format.loads_record(b'{"x": 0E-999999999}')
    with pytest.raises(ValueError, match="exponent"):
        git_format.loads_record(b'{"x": 0E+999999999}')
    ordinary = {"x": Decimal("0"), "y": Decimal("0.00"), "z": Decimal("-0.0")}
    assert git_format.loads_record(git_format.dumps_record(ordinary)) == ordinary


def test_decimal_coefficient_budget():
    # An oversized coefficient always pushes the adjusted exponent out of
    # the +/- MAX_DECIMAL_EXPONENT window, so it is rejected before any
    # fixed-point expansion happens.
    limit = git_format.MAX_DECIMAL_EXPONENT
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("9" * (limit * 4))})
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("0." + "0" * (limit * 4) + "1")})
    with pytest.raises(ValueError, match="exponent"):
        git_format.loads_record(
            b'{"x": ' + b"9" * (limit * 4) + b".0}")
    edge = {
        "hi": Decimal("1E+%d" % limit),
        "lo": Decimal("1E-%d" % limit),
    }
    assert git_format.loads_record(git_format.dumps_record(edge)) == edge


def test_decimal_trailing_zero_coefficient_budget():
    # A non-zero decimal like 1.000...0 keeps adjusted() at 0 no matter
    # how long the trailing-zero tail is, so the adjusted exponent alone
    # would let a huge coefficient through fixed-point expansion; the
    # raw exponent and the coefficient length are capped separately.
    limit = git_format.MAX_DECIMAL_EXPONENT
    with pytest.raises(ValueError, match="exponent"):
        git_format.dumps_record({"x": Decimal("1." + "0" * (limit * 4))})
    with pytest.raises(ValueError, match="exponent"):
        git_format.loads_record(b'{"x": 1.' + b"0" * (limit * 4) + b"}")
    # A coefficient longer than the budget is rejected on its length
    # even when both the adjusted and the raw exponent are in range.
    fat = Decimal("9" * limit + "." + "9" * limit)
    with pytest.raises(ValueError, match="coefficient"):
        git_format.dumps_record({"x": fat})
    # Legitimate trailing zeros at the boundary still round-trip with
    # their exact decimal text preserved.
    edge = {"x": Decimal("1." + "0" * (limit - 1)), "y": Decimal("9" * limit)}
    data = git_format.dumps_record(edge)
    assert ("1." + "0" * (limit - 1)).encode("utf-8") in data
    assert git_format.loads_record(data) == edge


def test_depth_limit_enforced_on_dump_and_load():
    def nested(levels):
        value = {}
        cursor = value
        for _ in range(levels - 1):
            cursor["k"] = {}
            cursor = cursor["k"]
        return value

    git_format.dumps_record(nested(git_format.MAX_DEPTH))
    with pytest.raises(ValueError, match="MAX_DEPTH"):
        git_format.dumps_record(nested(git_format.MAX_DEPTH + 1))
    over = git_format.MAX_DEPTH + 1
    deep = b'{"k": ' * over + b"1" + b"}" * over
    with pytest.raises(ValueError, match="MAX_DEPTH"):
        git_format.loads_record(deep)


def test_record_byte_limit_enforced():
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record({"x": "a" * git_format.MAX_RECORD_BYTES})
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record(b" " * (git_format.MAX_RECORD_BYTES + 1))
    # Many individually modest pieces that exceed the budget only in
    # aggregate must also be rejected.
    pieces = {"k%02d" % i: "a" * 100000 for i in range(20)}
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record(pieces)
    # The budget aborts mid-render: "a" alone already crosses the cap, so
    # the unsupported value under "z" is never reached; an eager renderer
    # would surface its TypeError instead of the budget error.
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record(
            {"a": "a" * git_format.MAX_RECORD_BYTES, "z": object()})


def test_render_string_precheck_before_fragment(monkeypatch):
    # An over-budget string or key must be rejected from its exact
    # escaped size (or its character lower bound) before json.dumps
    # ever builds its fragment; a short legal key still encodes
    # through json.dumps normally.
    calls = []
    real_dumps = git_format.json.dumps

    def spy(value, **kwargs):
        calls.append(value)
        return real_dumps(value, **kwargs)

    monkeypatch.setattr(git_format.json, "dumps", spy)
    over = '"' * git_format.MAX_RECORD_BYTES
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record({"x": over})
    # The legal key "x" is encoded first; the over-long value is
    # rejected before it reaches json.dumps.
    assert calls == ["x"]
    calls.clear()
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record({over: 1})
    # The over-long key is rejected before json.dumps is called at all.
    assert calls == []


def test_escaped_size_boundary_still_accepted():
    # The precheck measures the exact escaped size, so a record that
    # fits the budget precisely, escapes included, is still accepted.
    room = git_format.MAX_RECORD_BYTES - 14  # fixed framing overhead
    payload = {"x": "a" * room}
    data = git_format.dumps_record(payload)
    assert len(data) == git_format.MAX_RECORD_BYTES
    assert git_format.loads_record(data) == payload
    quoted = {"x": '"' * (room // 2)}
    data = git_format.dumps_record(quoted)
    assert len(data) == git_format.MAX_RECORD_BYTES
    assert git_format.loads_record(data) == quoted
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.dumps_record({"x": "a" * (room + 1)})


def test_loads_size_precheck_before_copy_or_encode():
    limit = git_format.MAX_RECORD_BYTES
    # A memoryview is measured by byte count, not element count: this
    # 'I' array has fewer elements than the limit but more bytes.
    words = memoryview(array("I", [0] * (limit // 4 + 1)))
    assert words.nbytes > limit > len(words)
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record(words)
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record(bytearray(limit + 1))
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record(memoryview(bytearray(limit + 1)))
    # An over-long str is rejected before any UTF-8 encoding: encoding
    # these lone surrogates would fail with UnicodeEncodeError, so
    # seeing the budget error proves the check ran first.
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record("\ud800" * (limit + 1))
    # A str inside the character budget but over the encoded-byte
    # budget is still rejected on its exact encoded size.
    with pytest.raises(ValueError, match="MAX_RECORD_BYTES"):
        git_format.loads_record("é" * limit)


def test_unsupported_value_types_rejected():
    with pytest.raises(TypeError):
        git_format.dumps_record({"x": (1, 2)})
    with pytest.raises(TypeError):
        git_format.dumps_record({"x": {1: "non-str key"}})


def test_record_paths_from_fixed_types():
    assert git_format.record_path(
        "fact_revision", fact_uid="f-1", revision=3
    ) == "facts/f-1/revisions/3.json"
    assert git_format.record_path(
        "fact_current", fact_uid="f-1"
    ) == "facts/f-1/current.json"
    assert git_format.record_path(
        "operation", operation_id="op-9"
    ) == "operations/op-9.json"
    assert git_format.record_path("repository_meta") == "meta/repository.json"
    assert git_format.record_path(
        "consumed_candidate", candidate_uid="c1", candidate_revision=2
    ) == "consumed/c1/2.json"
    assert git_format.record_path(
        "taxonomy_leaf", pack_version="v2", leaf_id="L-07"
    ) == "taxonomy/v2/leaves/L-07.json"


def test_unknown_record_type_cannot_form_path():
    with pytest.raises(ValueError, match="unknown record type"):
        git_format.record_path("../../etc/passwd")
    with pytest.raises(ValueError, match="unknown record type"):
        git_format.record_path("fact_current/../../secrets", fact_uid="x")
    with pytest.raises(ValueError, match="unknown record type"):
        git_format.record_path(None)


def test_wrong_id_set_rejected():
    with pytest.raises(ValueError):
        git_format.record_path("fact_current")
    with pytest.raises(ValueError):
        git_format.record_path("fact_current", fact_uid="x", extra="y")
    with pytest.raises(ValueError):
        git_format.record_path("fact_revision", fact_uid="x")


def test_traversal_and_hostile_ids_cannot_escape_layout():
    hostile = [
        "..", ".", "../..", "../../etc/passwd", "a/b", "a\\b",
        "a\x00b", "~", "~2E~2E", "/etc/passwd", "公司/机密",
    ]
    for bad in hostile:
        path = git_format.record_path("fact_current", fact_uid=bad)
        segments = path.split("/")
        assert len(segments) == 3
        assert segments[0] == "facts"
        assert segments[2] == "current.json"
        assert segments[1] not in ("", ".", "..")
        for segment in segments:
            assert "/" not in segment
            assert "\\" not in segment
            assert "\x00" not in segment
            assert not segment.startswith(".")


def test_invalid_ids_rejected():
    with pytest.raises(ValueError):
        git_format.record_path("fact_current", fact_uid="")
    with pytest.raises(TypeError):
        git_format.record_path("fact_current", fact_uid=7)
    with pytest.raises(ValueError):
        git_format.record_path(
            "fact_current", fact_uid="x" * (git_format.MAX_ID_CHARS + 1))


# DEC-030 approved appendix 97 section 2.1: numeric revision parameters.
# Each sample is (record_type, other required ids, revision parameter
# name, expected path pattern with one %s slot for the canonical
# decimal segment).
_REVISION_SAMPLES = [
    ("entity_revision", {"entity_uid": "ent-1"}, "revision",
     "entities/ent-1/revisions/%s.json"),
    ("fact_revision", {"fact_uid": "f-1"}, "revision",
     "facts/f-1/revisions/%s.json"),
    ("source_lifecycle", {"source_id": "src-1"}, "revision",
     "sources/src-1/lifecycle/%s.json"),
    ("link_revision", {"link_id": "lk-1"}, "revision",
     "links/lk-1/revisions/%s.json"),
    ("placement_revision", {"fact_uid": "f-1"}, "revision",
     "placements/f-1/revisions/%s.json"),
    ("input_candidate", {"candidate_uid": "cand-1"}, "candidate_revision",
     "inputs/cand-1/%s.json"),
    ("consumed_candidate", {"candidate_uid": "c1"}, "candidate_revision",
     "consumed/c1/%s.json"),
]


def test_positive_int_revisions_form_canonical_decimal_paths():
    for record_type, base, name, pattern in _REVISION_SAMPLES:
        for revision in (1, 2, 7, 42, 99999999999999999999):
            path = git_format.record_path(
                record_type, **dict(base, **{name: revision}))
            assert path == pattern % revision
            assert git_format.canonical_record_type(path) == record_type


def test_revision_rejects_bool_str_float_and_other_types():
    # bool is never an int for a revision ordinal, and no non-int type
    # (str, float, Decimal, None, containers, bytes) is coerced either.
    for record_type, base, name, _pattern in _REVISION_SAMPLES:
        for bad in (True, False, "1", "r1", 1.0, Decimal("2"), None, [1],
                    b"1", (1,)):
            with pytest.raises(TypeError):
                git_format.record_path(
                    record_type, **dict(base, **{name: bad}))


def test_revision_rejects_zero_and_negative_ints():
    for record_type, base, name, _pattern in _REVISION_SAMPLES:
        for bad in (0, -1, -99999999999999999999):
            with pytest.raises(ValueError):
                git_format.record_path(
                    record_type, **dict(base, **{name: bad}))


def test_encode_revision_is_canonical_ascii_decimal():
    assert git_format.encode_revision(1) == "1"
    assert git_format.encode_revision(10**30) == "1" + "0" * 30
    token = git_format.encode_revision(12345678901234567890)
    assert token.isascii() and token.isdigit()
    assert not token.startswith("0")
    with pytest.raises(TypeError):
        git_format.encode_revision(True)
    with pytest.raises(TypeError):
        git_format.encode_revision("3")
    with pytest.raises(ValueError):
        git_format.encode_revision(0)


def test_canonical_record_type_rejects_non_revision_tokens():
    for path in [
        "facts/f-1/revisions/r1.json",      # legacy opaque test token
        "facts/f-1/revisions/0.json",       # zero is not a revision
        "facts/f-1/revisions/007.json",     # leading zeros are not canonical
        "facts/f-1/revisions/+3.json",      # signed form is not canonical
        "facts/f-1/revisions/-3.json",
        "facts/f-1/revisions/3.0.json",
        "facts/f-1/revisions/٣.json",  # Arabic-Indic digit: non-ASCII
        "facts/f-1/revisions/３.json",  # fullwidth digit: non-ASCII
        "entities/ent-1/revisions/r3.json",
        "sources/src-1/lifecycle/01.json",
        "links/lk-1/revisions/r1.json",
        "placements/f-1/revisions/0.json",
        "inputs/cand-1/r2.json",            # legacy candidate token
        "inputs/cand-1/02.json",
        "consumed/c1/0.json",
        "facts/f-1/revisions/%s.json" % ("9" * 5000),  # over the int limit
    ]:
        assert git_format.canonical_record_type(path) is None


def test_opaque_ids_keep_their_string_rules():
    # version_id, pack_version and event_id stay opaque host-issued str
    # IDs: the numeric revision rule does not apply to them, so a
    # leading-zero decimal string remains legal and canonical, and an
    # int value is still rejected.
    assert git_format.record_path(
        "source_version", source_id="src-1", version_id="007"
    ) == "sources/src-1/versions/007.json"
    assert git_format.canonical_record_type(
        "sources/src-1/versions/007.json") == "source_version"
    assert git_format.record_path(
        "taxonomy_manifest", pack_version="v2"
    ) == "taxonomy/v2/manifest.json"
    assert git_format.canonical_record_type(
        "taxonomy/v2/manifest.json") == "taxonomy_manifest"
    assert git_format.record_path(
        "event", event_id="evt-1") == "events/evt-1.json"
    with pytest.raises(TypeError):
        git_format.record_path(
            "source_version", source_id="src-1", version_id=7)
    with pytest.raises(TypeError):
        git_format.record_path("taxonomy_manifest", pack_version=2)
    with pytest.raises(TypeError):
        git_format.record_path("event", event_id=1)


def test_id_encoding_reversible_and_deterministic():
    samples = [
        "plain-1_OK", "公司α", "a b/c\\d.e~f", "..", "🦀",
        "x" * git_format.MAX_ID_CHARS, "0", "修订.3",
    ]
    for sample in samples:
        first = git_format.encode_id(sample)
        second = git_format.encode_id(sample)
        assert first == second
        assert git_format.decode_id(first) == sample
    assert git_format.encode_id("a/b") == "a~2Fb"
    assert git_format.encode_id("~") == "~7E"
    assert git_format.decode_id("~2E~2E") == ".."


def test_decode_rejects_malformed_tokens():
    for bad in ["", "a b", "a/b", "~2", "~zz", "a~2", "~2g", "x~"]:
        with pytest.raises((ValueError, TypeError)):
            git_format.decode_id(bad)
    with pytest.raises(UnicodeDecodeError):
        git_format.decode_id("~FF~FE")
    with pytest.raises(TypeError):
        git_format.decode_id(None)


def test_canonical_record_type_accepts_every_template_output():
    samples = [
        ("repository_meta", {}),
        ("taxonomy_manifest", {"pack_version": "v2"}),
        ("taxonomy_leaf", {"pack_version": "generic-v2", "leaf_id": "L-07"}),
        ("bindings_current", {}),
        ("entity_revision", {"entity_uid": "ent-1", "revision": 3}),
        ("entity_current", {"entity_uid": "ent-1"}),
        ("fact_revision", {"fact_uid": "f-1", "revision": 3}),
        ("fact_current", {"fact_uid": "f-1"}),
        ("source_version", {"source_id": "src-1", "version_id": "v-1"}),
        ("source_lifecycle", {"source_id": "src-1", "revision": 2}),
        ("evidence", {"evidence_id": "ev-1"}),
        ("link_revision", {"link_id": "lk-1", "revision": 1}),
        ("placement_revision", {"fact_uid": "f-1", "revision": 4}),
        ("placement_current", {"fact_uid": "f-1"}),
        ("issue_event", {"issue_uid": "is-1", "event_id": "ev-9"}),
        ("proposal_event", {"proposal_uid": "pr-1", "event_id": "ev-9"}),
        ("input_candidate",
         {"candidate_uid": "cand-1", "candidate_revision": 2}),
        ("operation", {"operation_id": "op-9"}),
        ("consumed_candidate", {"candidate_uid": "c1",
                                "candidate_revision": 2}),
        ("event", {"event_id": "evt-1"}),
    ]
    assert {record_type for record_type, _ids in samples} == \
        set(git_format.RECORD_TYPES)
    for record_type, ids in samples:
        path = git_format.record_path(record_type, **ids)
        assert git_format.canonical_record_type(path) == record_type


def test_canonical_record_type_accepts_canonical_escaped_ids():
    path = git_format.record_path("fact_current", fact_uid="公司 α/1")
    assert "~" in path  # the escapes were actually exercised
    assert git_format.canonical_record_type(path) == "fact_current"
    # "~" itself encodes to "~7E"; that canonical escape form decodes
    # and re-encodes to the identical token, so it is accepted.
    tilde = git_format.record_path("fact_current", fact_uid="~")
    assert tilde == "facts/~7E/current.json"
    assert git_format.canonical_record_type(tilde) == "fact_current"


def test_canonical_record_type_rejects_paths_outside_fixed_templates():
    for path in [
        "notes/freeform.json",            # clean and safe, but unregistered
        "a",
        "facts",                          # a directory prefix, not a record
        "facts/f-1/current.json.bak",
        "facts/f-1/current.json/child.json",
        "meta/repository.json/extra.json",
        "Facts/f-1/current.json",         # literal segments are case-exact
        "facts/f-1/revisions/1",          # missing the .json suffix
        "facts/f-1/revisions/.json",      # empty placeholder token
        "meta/repository.json/",
        "/facts/f-1/current.json",
        "facts//current.json",
        "facts/../current.json",
        "facts/f-1/revisions/1.5.json",   # "." is never left unescaped
    ]:
        assert git_format.canonical_record_type(path) is None


def test_canonical_record_type_rejects_decodable_escape_aliases():
    canonical = git_format.record_path("fact_current", fact_uid="f-1")
    assert canonical == "facts/f-1/current.json"
    # "~2D" decodes to the safe character "-", so each alias decodes to
    # a legitimate ID but is not the canonical encoding of that ID.
    aliases = [
        "facts/f~2D1/current.json",       # "-" rewritten as ~2D
        "facts/~66act-0001/current.json",  # "f" rewritten as ~66
        "facts/f~2d1/current.json",       # lowercase hex is not canonical
    ]
    assert git_format.decode_id("f~2D1") == "f-1"
    assert git_format.decode_id("~66act-0001") == "fact-0001"
    for alias in aliases:
        assert git_format.canonical_record_type(alias) is None


def test_canonical_record_type_rejects_non_str():
    with pytest.raises(TypeError):
        git_format.canonical_record_type(None)
    with pytest.raises(TypeError):
        git_format.canonical_record_type(b"facts/f-1/current.json")


def test_record_types_cover_approved_layout():
    expected = {
        "repository_meta", "taxonomy_manifest", "taxonomy_leaf",
        "bindings_current", "entity_revision", "entity_current",
        "fact_revision", "fact_current", "source_version",
        "source_lifecycle", "evidence", "link_revision",
        "placement_revision", "placement_current", "issue_event",
        "proposal_event", "input_candidate", "operation",
        "consumed_candidate", "event",
    }
    assert set(git_format.RECORD_TYPES) == expected
