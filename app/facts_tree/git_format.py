"""Versioned deterministic JSON record format and safe in-repo paths.

Pure helpers for the F-001 facts Git repository layout (approved design
baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f, sections 10.2.2-10.2.3).
This module is not connected to SQLite, the facts-tree write flow, or any
business entry point: it only serializes record payloads and maps fixed
record types plus host-issued IDs to repository-relative paths.

Serializer contract (FORMAT_VERSION = 1):

- UTF-8 JSON, LF newlines, two-space indent, exactly one trailing newline.
- Object keys are sorted by Unicode code point at every nesting level;
  array order is preserved exactly as supplied. Original text, negation
  words, precision and citation order are never normalized away for a
  stable diff.
- Strings are emitted as raw UTF-8 (no \\uXXXX normalization).
- Integers keep arbitrary precision. Non-integer decimals must be given as
  decimal.Decimal (loads_record parses JSON fractions as Decimal, so exact
  decimal text round-trips); they are written in explicit fixed-point
  decimal notation. Python float is rejected outright, so a non-integer
  business number can never be silently rewritten through binary floating
  point. NaN and Infinity (float or Decimal), duplicate JSON object keys
  on load, lone surrogates left over from unpaired \\uXXXX escapes, and
  invalid UTF-8 input bytes are all rejected. Decimals are
  checked before any fixed-point expansion on three independent budgets:
  the adjusted exponent (magnitude), the raw exponent and the
  coefficient digit count must each lie within +/- MAX_DECIMAL_EXPONENT.
  An extreme-exponent zero (e.g. 0E-999999999) or a non-zero value whose
  adjusted exponent looks harmless but whose coefficient drags a huge
  trailing-zero tail (e.g. 1.000...0) is therefore rejected instead of
  expanding into a giant zero-padded string.
- Records nested deeper than MAX_DEPTH container levels (the root object
  counts as level 1) or larger than MAX_RECORD_BYTES encoded bytes are
  rejected; serialization accounts encoded bytes chunk by chunk and
  aborts as soon as the cap is crossed, so an over-budget record never
  materializes an unbounded intermediate string. Strings and object keys
  are measured at their exact escaped size before escaping, so an
  over-budget JSON fragment is rejected before it is built; loads_record
  likewise checks the input size before copying bytes or encoding str,
  and measures a memoryview by byte count, not element count. Schema
  versioning of individual record payloads is the caller's
  responsibility (e.g. a schema_version field per section 10.3); this
  module versions only the serializer rules above.

Path contract:

- Paths are produced only by record_path(record_type, **ids) from a fixed
  table of record types; there is no free-form path parameter, so a
  candidate or management model can never supply an authorized path.
- IDs are opaque host-issued identifiers encoded by encode_id into a
  single safe path segment (reversible via decode_id): ASCII letters,
  digits, '-' and '_' pass through, every other UTF-8 byte becomes '~'
  plus two uppercase hex digits. An encoded ID can therefore never contain
  '/', '..', a leading '.', NUL, a backslash or any traversal primitive.
- Numeric revision parameters ('revision' and 'candidate_revision') are
  the one exception to the opaque rule: per the DEC-030 approved appendix
  97 section 2.1, a revision ordinal is a strictly positive int and its
  path segment is the canonical ASCII decimal string with no leading
  zeros and no sign. Only the built-in int type itself is accepted here
  (type(value) is int): bool and every int subclass are rejected, so a
  subclass overriding __str__ can never produce a segment that disagrees
  with the underlying value; str, float, Decimal, 0, negative values and
  every other non-int type are rejected as well. Opaque IDs such as
  version_id, pack_version and event_id keep the encode_id rules
  unchanged.
- The assembled relative path is length-capped (MAX_PATH_BYTES) and
  re-checked for clean relative segments before being returned.
- canonical_record_type(path) decides the reverse question: it returns
  the fixed record type whose record_path() output is exactly the given
  path, or None when no template matches. A revision placeholder segment
  only matches a canonical decimal token ([1-9][0-9]*), so "0", leading
  zeros, signs, non-ASCII digits and legacy opaque tokens such as "r1"
  never qualify. Every other placeholder segment only matches a token
  that decode_id accepts and that re-encodes through encode_id to the
  identical token, so a decodable but non-canonical escape alias (for
  example "~2D" standing in for the safe character "-") never qualifies.
"""
from decimal import Decimal
import json

FORMAT_VERSION = 1
MAX_DEPTH = 32
MAX_RECORD_BYTES = 1024 * 1024
MAX_ID_CHARS = 128
MAX_SEGMENT_BYTES = 384
MAX_PATH_BYTES = 1024
MAX_DECIMAL_EXPONENT = 512

_INDENT = "  "
_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_HEX = "0123456789ABCDEF"

# Numeric revision parameter names (DEC-030 approved appendix 97 section
# 2.1): their path segments are canonical ASCII decimal strings produced
# by encode_revision, not encode_id outputs. Every other placeholder in
# the fixed table stays an opaque host-issued ID.
_REVISION_NAMES = frozenset({"revision", "candidate_revision"})
_ASCII_DIGITS = frozenset("0123456789")

# Fixed record layout from the approved design section 10.2.2. Each entry
# maps a record type to (relative path pattern, required id names); every
# {placeholder} segment is filled only with an encode_id output, except
# the _REVISION_NAMES placeholders, which are filled with an
# encode_revision canonical decimal output.
_TEMPLATES = {
    "repository_meta": ("meta/repository.json", ()),
    "taxonomy_manifest": (
        "taxonomy/{pack_version}/manifest.json", ("pack_version",)),
    "taxonomy_leaf": (
        "taxonomy/{pack_version}/leaves/{leaf_id}.json",
        ("pack_version", "leaf_id")),
    "bindings_current": ("bindings/current.json", ()),
    "entity_revision": (
        "entities/{entity_uid}/revisions/{revision}.json",
        ("entity_uid", "revision")),
    "entity_current": ("entities/{entity_uid}/current.json", ("entity_uid",)),
    "fact_revision": (
        "facts/{fact_uid}/revisions/{revision}.json",
        ("fact_uid", "revision")),
    "fact_current": ("facts/{fact_uid}/current.json", ("fact_uid",)),
    "source_version": (
        "sources/{source_id}/versions/{version_id}.json",
        ("source_id", "version_id")),
    "source_lifecycle": (
        "sources/{source_id}/lifecycle/{revision}.json",
        ("source_id", "revision")),
    "evidence": ("evidence/{evidence_id}.json", ("evidence_id",)),
    "link_revision": (
        "links/{link_id}/revisions/{revision}.json", ("link_id", "revision")),
    "placement_revision": (
        "placements/{fact_uid}/revisions/{revision}.json",
        ("fact_uid", "revision")),
    "placement_current": (
        "placements/{fact_uid}/current.json", ("fact_uid",)),
    "issue_event": (
        "issues/{issue_uid}/events/{event_id}.json",
        ("issue_uid", "event_id")),
    "proposal_event": (
        "proposals/{proposal_uid}/events/{event_id}.json",
        ("proposal_uid", "event_id")),
    "input_candidate": (
        "inputs/{candidate_uid}/{candidate_revision}.json",
        ("candidate_uid", "candidate_revision")),
    "operation": ("operations/{operation_id}.json", ("operation_id",)),
    "consumed_candidate": (
        "consumed/{candidate_uid}/{candidate_revision}.json",
        ("candidate_uid", "candidate_revision")),
    "event": ("events/{event_id}.json", ("event_id",)),
}

RECORD_TYPES = tuple(sorted(_TEMPLATES))


def encode_id(value):
    """Encode an opaque host-issued ID into one safe path segment."""
    if not isinstance(value, str):
        raise TypeError("record id must be a host-issued str")
    if not value:
        raise ValueError("record id must not be empty")
    if len(value) > MAX_ID_CHARS:
        raise ValueError("record id exceeds MAX_ID_CHARS")
    out = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _SAFE:
            out.append(char)
        else:
            out.append("~" + _HEX[byte >> 4] + _HEX[byte & 0x0F])
    token = "".join(out)
    if len(token) > MAX_SEGMENT_BYTES:
        raise ValueError("encoded record id exceeds MAX_SEGMENT_BYTES")
    return token


def decode_id(token):
    """Reverse encode_id; rejects anything outside the encoded alphabet."""
    if not isinstance(token, str):
        raise TypeError("encoded record id must be a str")
    if not token:
        raise ValueError("encoded record id must not be empty")
    data = bytearray()
    index = 0
    while index < len(token):
        char = token[index]
        if char in _SAFE:
            data.append(ord(char))
            index += 1
            continue
        if char != "~":
            raise ValueError("encoded record id contains an unsafe character")
        if index + 3 > len(token):
            raise ValueError("truncated escape in encoded record id")
        pair = token[index + 1:index + 3]
        if any(digit not in _HEX for digit in pair):
            raise ValueError("invalid escape in encoded record id")
        data.append(int(pair, 16))
        index += 3
    return data.decode("utf-8")


def encode_revision(value):
    """Encode a numeric revision ordinal into its canonical path segment.

    Approved appendix 97 section 2.1: a revision is a strictly positive
    int and its path token is the canonical decimal string. Only the
    built-in int type itself passes (type(value) is int): bool and every
    int subclass raise TypeError, because a subclass could override
    __str__ and make str(value) disagree with the underlying integer,
    yielding a wrong or non-canonical segment; str/float/Decimal/None and
    every other non-int type raise TypeError as well, and 0 or negative
    values raise ValueError. For a true built-in positive int, str(value)
    is exactly the ASCII decimal representation with no leading zeros
    and no sign.
    """
    if type(value) is not int:
        raise TypeError(
            "revision must be a positive int, got %s" % type(value).__name__)
    if value <= 0:
        raise ValueError("revision must be a strictly positive int")
    return str(value)


def _is_revision_token(token):
    """True only for a canonical decimal revision segment [1-9][0-9]*.

    "0", leading zeros ("007"), signs, non-ASCII digit characters and
    legacy opaque tokens such as "r1" all fail here, so they can never
    identify a revision path.
    """
    if not token or token[0] not in "123456789":
        return False
    return all(char in _ASCII_DIGITS for char in token)


def record_path(record_type, **ids):
    """Build a repository-relative path from a fixed record type and IDs.

    Callers pass a record type name and the IDs that type requires;
    there is intentionally no way to supply a raw path. Revision
    parameters ('revision', 'candidate_revision') must be strictly
    positive ints and become canonical decimal segments; every other
    parameter is an opaque host-issued str encoded by encode_id.
    """
    if not isinstance(record_type, str) or record_type not in _TEMPLATES:
        raise ValueError("unknown record type: %r" % (record_type,))
    pattern, names = _TEMPLATES[record_type]
    if set(ids) != set(names):
        raise ValueError(
            "record type %r requires exactly the ids %s" % (record_type, names))
    encoded = {}
    for name in names:
        value = ids[name]
        if name in _REVISION_NAMES:
            encoded[name] = encode_revision(value)
        else:
            encoded[name] = encode_id(value)
    path = pattern
    for name in names:
        path = path.replace("{" + name + "}", encoded[name])
    _check_path(path)
    return path


def canonical_record_type(path):
    """Return the record type whose record_path() output is exactly path.

    The path must match one fixed template segment by segment: literal
    segments must be identical, a revision placeholder segment must carry
    a canonical decimal token ([1-9][0-9]*), and every other placeholder
    segment must carry a token that decode_id accepts and that encode_id
    re-encodes to the identical token. A path that is merely safe, one
    whose escape sequences decode but are not the canonical encoding of
    the decoded ID (an alias such as "~2D" for "-"), or one whose
    revision segment carries "0", a leading zero, a sign, a non-ASCII
    digit or a legacy opaque token such as "r1", returns None; so does
    any path outside the fixed layout. The exact reconstruction through
    record_path() must equal the input, so all record_path() limits
    apply to a match as well. Returns None for any non-matching str;
    raises TypeError for a non-str argument.
    """
    if not isinstance(path, str):
        raise TypeError(
            "record path must be a str, got %s" % type(path).__name__)
    for record_type in RECORD_TYPES:
        if _match_template(record_type, path) is not None:
            return record_type
    return None


def _match_template(record_type, path):
    # Returns the {name: id} mapping when path is exactly
    # record_path(record_type, **ids) for some host-issued ids (opaque
    # IDs decoded to str, revision segments parsed back to int), else
    # None. RECORD_TYPES is sorted, so callers see deterministic
    # behaviour even if two templates could ever match the same path.
    pattern, names = _TEMPLATES[record_type]
    pattern_segments = pattern.split("/")
    path_segments = path.split("/")
    if len(pattern_segments) != len(path_segments):
        return None
    ids = {}
    for pattern_segment, path_segment in zip(pattern_segments, path_segments):
        if "{" not in pattern_segment:
            if path_segment != pattern_segment:
                return None
            continue
        # Every placeholder segment in the fixed table holds exactly one
        # {name} between a literal prefix and a literal suffix.
        prefix, rest = pattern_segment.split("{", 1)
        name, suffix = rest.split("}", 1)
        if not path_segment.startswith(prefix):
            return None
        if suffix:
            if not path_segment.endswith(suffix):
                return None
            token = path_segment[len(prefix):-len(suffix)]
        else:
            token = path_segment[len(prefix):]
        if name in _REVISION_NAMES:
            # Only the canonical decimal token [1-9][0-9]* identifies a
            # revision segment: "0", leading zeros, signs, non-ASCII
            # digits and legacy opaque tokens such as "r1" never match.
            if not _is_revision_token(token):
                return None
            try:
                ids[name] = int(token)
            except ValueError:
                # Fail closed on a digit string beyond the int/str
                # conversion limit; it is not a usable revision.
                return None
            continue
        try:
            value = decode_id(token)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        # The token must be the canonical encoding of its decoded ID:
        # a decodable alias like "~2D" for "-" re-encodes to "-" and is
        # rejected, and an over-limit decoded ID fails encode_id.
        try:
            if encode_id(value) != token:
                return None
        except (TypeError, ValueError):
            return None
        ids[name] = value
    if set(ids) != set(names):
        return None
    # Membership is defined as exact equality with a record_path()
    # output, so the length cap and clean-segment recheck in
    # record_path() are part of the match, not a parallel reimplementation.
    try:
        if record_path(record_type, **ids) == path:
            return ids
    except (TypeError, ValueError):
        pass
    return None


def _check_path(path):
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError("record path exceeds MAX_PATH_BYTES")
    if path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError("record path is not a clean relative path")
    if "{" in path or "}" in path:
        raise ValueError("record path contains an unsubstituted placeholder")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError("record path contains a traversal segment")


def dumps_record(payload):
    """Serialize a record payload to canonical UTF-8 JSON bytes.

    Output is produced chunk by chunk with a running encoded-byte total;
    as soon as the total crosses MAX_RECORD_BYTES the record is rejected,
    so the full serialized text of an over-budget record is never
    materialized. Strings and keys are pre-measured at their exact
    escaped size against the remaining budget, so an over-budget JSON
    fragment is rejected before json.dumps builds it. The final byte is
    reserved for the trailing newline.
    """
    if not isinstance(payload, dict):
        raise TypeError("a record payload must be a JSON object (dict)")
    budget = [MAX_RECORD_BYTES - 1]
    chunks = []
    for chunk in _render_chunks(payload, 1, budget):
        piece = chunk.encode("utf-8")
        budget[0] -= len(piece)
        if budget[0] < 0:
            raise ValueError("record exceeds MAX_RECORD_BYTES")
        chunks.append(piece)
    chunks.append(b"\n")
    return b"".join(chunks)


def loads_record(data):
    """Parse canonical record bytes back into a payload, failing closed.

    Fractions come back as decimal.Decimal so exact decimal text is never
    routed through binary floating point; duplicate keys, NaN/Infinity,
    lone surrogates, invalid UTF-8, over-budget size and over-deep
    nesting are rejected.
    The size check runs before any copy or re-encoding of the input, and
    a memoryview is measured by byte count, not element count.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        # bytes() would duplicate bytearray and memoryview contents, so
        # the budget is enforced first; a memoryview element count is
        # not its byte count.
        size = data.nbytes if isinstance(data, memoryview) else len(data)
        if size > MAX_RECORD_BYTES:
            raise ValueError("record exceeds MAX_RECORD_BYTES")
        text = bytes(data).decode("utf-8")
    elif isinstance(data, str):
        # UTF-8 never encodes to fewer bytes than the string has
        # characters, so an over-long str is rejected before paying for
        # an encoded copy; the exact encoded length is verified on the
        # bounded copy for strings that pass the character check.
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError("record exceeds MAX_RECORD_BYTES")
        if len(data.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("record exceeds MAX_RECORD_BYTES")
        text = data
    else:
        raise TypeError("record data must be bytes or str")
    payload = json.loads(
        text,
        object_pairs_hook=_object_without_duplicates,
        parse_float=Decimal,
        parse_constant=_reject_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("a record must be a JSON object")
    _validate(payload, 1)
    return payload


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %r" % (key,))
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError("non-finite number %s is not valid in a record" % name)


def _check_text(text):
    # json.loads keeps an unpaired \uXXXX escape as a lone surrogate,
    # which can never be encoded back to UTF-8; a record carrying one on
    # any key or string value is rejected before it is returned.
    for char in text:
        if "\ud800" <= char <= "\udfff":
            raise ValueError("lone surrogate is not valid record text")


def _validate(value, depth):
    if isinstance(value, dict):
        if depth > MAX_DEPTH:
            raise ValueError("record nesting exceeds MAX_DEPTH")
        for key, item in value.items():
            _check_text(key)
            _validate(item, depth + 1)
        return
    if isinstance(value, list):
        if depth > MAX_DEPTH:
            raise ValueError("record nesting exceeds MAX_DEPTH")
        for item in value:
            _validate(item, depth + 1)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        _check_text(value)
        return
    if isinstance(value, int):
        return
    if isinstance(value, Decimal):
        _check_decimal(value)
        return
    raise ValueError(
        "unsupported value type in record: %s" % type(value).__name__)


def _check_decimal(value):
    if not value.is_finite():
        raise ValueError("NaN and Infinity are not valid record numbers")
    if value:
        # The adjusted exponent only locates the most significant digit;
        # 1.000...0 keeps adjusted() at 0 no matter how long the
        # trailing-zero tail is, so the magnitude alone cannot stop a
        # huge coefficient from exploding during fixed-point expansion.
        # The raw exponent and the coefficient digit count are capped
        # separately below; all three checks run before any expansion.
        if not (-MAX_DECIMAL_EXPONENT <= value.adjusted()
                <= MAX_DECIMAL_EXPONENT):
            raise ValueError("decimal exponent exceeds MAX_DECIMAL_EXPONENT")
    _sign, digits, exponent = value.as_tuple()
    if not -MAX_DECIMAL_EXPONENT <= exponent <= MAX_DECIMAL_EXPONENT:
        raise ValueError("decimal exponent exceeds MAX_DECIMAL_EXPONENT")
    if len(digits) > MAX_DECIMAL_EXPONENT:
        raise ValueError("decimal coefficient exceeds MAX_DECIMAL_EXPONENT")


def _render_decimal(value):
    _check_decimal(value)
    return format(value, "f")


# Short JSON escapes and their escaped lengths; every other character
# below U+0020 is escaped as \u00XX (6 bytes) and nothing else is ever
# escaped with ensure_ascii=False, so the exact escaped size of a string
# is computable without building the fragment.
_SHORT_ESCAPES = {
    '"': 2, "\\": 2, "\b": 2, "\f": 2, "\n": 2, "\r": 2, "\t": 2,
}


def _escaped_json_size(value):
    """Exact UTF-8 byte size of json.dumps(value, ensure_ascii=False)."""
    total = 2  # the surrounding quotes
    for char in value:
        escape = _SHORT_ESCAPES.get(char)
        if escape is not None:
            total += escape
        elif char < " ":
            total += 6
        elif char.isascii():
            total += 1
        else:
            total += len(char.encode("utf-8"))
    return total


def _render_string(value, budget):
    # Every character costs at least one escaped byte and the quotes
    # cost two, so a character count already over the budget proves
    # rejection without scanning the whole (possibly huge) string;
    # only strings that pass the lower bound are measured exactly.
    if len(value) + 2 > budget[0]:
        raise ValueError("record exceeds MAX_RECORD_BYTES")
    if _escaped_json_size(value) > budget[0]:
        raise ValueError("record exceeds MAX_RECORD_BYTES")
    return json.dumps(value, ensure_ascii=False)


def _render_chunks(value, depth, budget):
    """Yield the canonical text of a value in small ordered chunks.

    Keeping this a generator lets dumps_record account encoded bytes as
    they appear and abort on budget instead of joining one full string.
    budget is a one-item list holding the remaining encoded-byte
    allowance, shared with _render_string so an over-budget string or
    key is rejected from its exact escaped size before its fragment is
    built.
    """
    if isinstance(value, dict):
        if depth > MAX_DEPTH:
            raise ValueError("record nesting exceeds MAX_DEPTH")
        if not value:
            yield "{}"
            return
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be str")
        pad = _INDENT * depth
        yield "{\n"
        first = True
        for key, item in sorted(value.items()):
            if first:
                first = False
            else:
                yield ",\n"
            yield pad
            yield _render_string(key, budget)
            yield ": "
            yield from _render_chunks(item, depth + 1, budget)
        yield "\n" + _INDENT * (depth - 1) + "}"
        return
    if isinstance(value, list):
        if depth > MAX_DEPTH:
            raise ValueError("record nesting exceeds MAX_DEPTH")
        if not value:
            yield "[]"
            return
        pad = _INDENT * depth
        yield "[\n"
        first = True
        for item in value:
            if first:
                first = False
            else:
                yield ",\n"
            yield pad
            yield from _render_chunks(item, depth + 1, budget)
        yield "\n" + _INDENT * (depth - 1) + "]"
        return
    if isinstance(value, str):
        yield _render_string(value, budget)
        return
    if value is None:
        yield "null"
        return
    if isinstance(value, bool):
        yield "true" if value else "false"
        return
    if isinstance(value, int):
        yield str(value)
        return
    if isinstance(value, Decimal):
        yield _render_decimal(value)
        return
    raise TypeError(
        "unsupported record value type: %s; use str, int, bool, None, list, "
        "dict or decimal.Decimal (float is never accepted)"
        % type(value).__name__
    )
