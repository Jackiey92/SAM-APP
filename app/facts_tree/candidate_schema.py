"""Pure structural schema and validator for fact-candidate/1 batches.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 sections 3.1-3.5, 4 and 7. Per section 7 this module is the
structure layer only: a batch that passes validate_batch() has a legal
shape — no unknown keys at any object level, all required keys present,
exact types (a JSON boolean is never an integer), exact enum tokens,
in-candidate resolvable evidence references and the budget limits
below. Passing does not show that the source is authentic, that a read
grant is real or currently valid, that a quote matches the authorized
original text, or that any fact has entered the tree; those are
host-side checks on the trusted envelope and the original documents,
and this module deliberately never performs them.

Boundary contract:

- The input is an already-parsed JSON-compatible object. Validation is
  pure: no disk, no Git, no company data, no model and no network, and
  the input object is never mutated.
- Every error carries a field path (rooted at "$", array positions as
  "[i]"), a stable rule code and a correction suggestion, so the host
  can build per-field receipts without re-deriving locations.
- Unknown fields are rejected at every object level, so
  employee-supplied host or facts-tree fields (company_id, module_key,
  leaf_code, fact_uid, verified, ...) fail closed instead of being
  silently ignored.
- Fields whose identifier shape P01 fixes only by meaning
  (read_grant_id, source_loc, evidence_ref, local_id) are validated as
  non-empty strings without any inferred identity structure.
  attribution_index 0 is allowed at this layer, null is accepted only
  for evidence that field_bindings.statement does not list (the
  header/footnote-only qualifiers of P01 section 3.5), and whether an
  index or a remaining source_loc really belongs to the host's current
  grant or window is left to the later host check, exactly as P01
  sections 3.3 and 3.5 require.

Budgets (P01 section 3.3 initial suggested values, issued per task by
the host; they are versioned configuration that sample review and
performance measurement may adjust, and this module does not change
that status):

- MAX_BATCH_ITEMS = 32 candidates per batch
- MAX_EVIDENCE_PER_CANDIDATE = 8 evidence items per candidate
- MAX_QUOTE_CHARS = 4000 characters per quote
- MAX_ENTITY_CHARS = 256 and MAX_ATTRIBUTE_CHARS = 256 characters
- MAX_STATEMENT_CHARS = 2000 characters
- MAX_RESPONSE_BYTES = 256 KiB per response

The response budget is enforced on the canonical UTF-8 size of the
already-parsed object via git_format.dumps_record. That measures the
normalized structure, not the raw HTTP bytes the host received; hosts
must check the actual transferred bytes before parsing through the
standalone validate_response_bytes() entry point (same
MAX_RESPONSE_BYTES constant).

Rule codes used in errors: type_object, type_array, type_string,
type_boolean, type_integer, bool_as_int, unknown_field, missing_field,
enum_value, schema_version, empty_string, string_too_long,
array_too_short, array_too_long, integer_range, line_order,
located_text_state, remaining_source_locs_state, empty_reason_state,
duplicate_local_id, duplicate_evidence_ref, unresolvable_evidence_ref,
binding_mismatch, missing_attribution_index, missing_primary_evidence,
missing_field_evidence, missing_unresolved, response_budget.

FACT_CANDIDATE_BATCH_SCHEMA is the published machine-readable JSON
Schema (draft 2020-12) for the same contract; cross-field rules JSON
Schema cannot express (LocatedText status/binding equality, evidence
ref resolution inside one candidate, local_id uniqueness across the
batch, required unresolved entries, the canonical size budget) are
documented in its descriptions and enforced by the Python validator.
"""
from dataclasses import dataclass

from app.facts_tree import git_format

SCHEMA_VERSION = "fact-candidate/1"

MAX_BATCH_ITEMS = 32
MAX_EVIDENCE_PER_CANDIDATE = 8
MAX_QUOTE_CHARS = 4000
MAX_ENTITY_CHARS = 256
MAX_ATTRIBUTE_CHARS = 256
MAX_STATEMENT_CHARS = 2000
MAX_RESPONSE_BYTES = 256 * 1024

VALUE_TYPES = ("number", "text", "boolean", "date", "range")
STATEMENT_KINDS = ("event", "state", "plan", "forecast", "opinion",
                   "unknown")
POLARITIES = ("affirmed", "negated", "uncertain")
LOCATED_TEXT_STATUSES = ("known", "not_stated", "not_applicable",
                         "ambiguous")

FIELD_BINDING_KEYS = (
    "statement", "entity", "attribute", "value_raw", "period", "unit",
    "currency", "scope", "basis", "claimant", "statement_kind",
    "polarity",
)
LOCATED_TEXT_FIELDS = (
    "entity", "period", "unit", "currency", "scope", "basis", "claimant",
)

_BATCH_KEYS = (
    "schema_version", "items", "complete", "remaining_source_locs",
    "empty_reason",
)
_CANDIDATE_REQUIRED_KEYS = (
    "local_id", "statement", "entity", "attribute", "value_type",
    "value_raw", "period", "unit", "currency", "scope", "basis",
    "claimant", "statement_kind", "polarity", "evidence",
    "field_bindings", "unresolved",
)
_CANDIDATE_ALLOWED_KEYS = frozenset(
    _CANDIDATE_REQUIRED_KEYS + ("topic_hint",))
_LOCATED_TEXT_KEYS = ("status", "text", "evidence_refs", "reason")
_EVIDENCE_KEYS = (
    "evidence_ref", "read_grant_id", "source_loc", "attribution_index",
    "quote", "start_line", "end_line",
)
_UNRESOLVED_KEYS = ("field_path", "reason", "needed_clarification")

_BATCH_KEY_SET = frozenset(_BATCH_KEYS)
_LOCATED_TEXT_KEY_SET = frozenset(_LOCATED_TEXT_KEYS)
_EVIDENCE_KEY_SET = frozenset(_EVIDENCE_KEYS)
_UNRESOLVED_KEY_SET = frozenset(_UNRESOLVED_KEYS)
_FIELD_BINDING_KEY_SET = frozenset(FIELD_BINDING_KEYS)

# Sentinel for "attribution_index key absent"; distinct from an
# explicit null, which is a real (sometimes legal) value.
_MISSING = object()

_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
_REF_ARRAY = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}


def _located_text_json_schema(text_max_chars=None):
    """JSON Schema for one LocatedText value with per-status rules."""
    text_schema = {"type": "string", "minLength": 1}
    if text_max_chars is not None:
        text_schema["maxLength"] = text_max_chars
    null_schema = {"const": None}
    reason_schema = {"type": "string", "minLength": 1}

    def _rule(status, then_properties):
        return {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": status}},
            },
            "then": {"properties": then_properties},
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "text", "evidence_refs", "reason"],
        "properties": {
            "status": {"enum": list(LOCATED_TEXT_STATUSES)},
            "text": {"type": ["string", "null"]},
            "evidence_refs": dict(_REF_ARRAY),
            "reason": {"type": ["string", "null"]},
        },
        "allOf": [
            _rule("known", {
                "text": text_schema,
                "evidence_refs": dict(_REF_ARRAY, minItems=1),
                "reason": null_schema,
            }),
            _rule("not_stated", {
                "text": null_schema,
                "evidence_refs": {"type": "array", "maxItems": 0},
                "reason": reason_schema,
            }),
            _rule("not_applicable", {
                "text": null_schema,
                "evidence_refs": {"type": "array", "maxItems": 0},
                "reason": reason_schema,
            }),
            _rule("ambiguous", {
                "text": null_schema,
                "reason": reason_schema,
            }),
        ],
    }


_EVIDENCE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_EVIDENCE_KEYS),
    "properties": {
        "evidence_ref": dict(_NON_EMPTY_STRING),
        "read_grant_id": dict(_NON_EMPTY_STRING),
        "source_loc": dict(_NON_EMPTY_STRING),
        "attribution_index": {"type": ["integer", "null"], "minimum": 0},
        "quote": {"type": "string", "minLength": 1,
                  "maxLength": MAX_QUOTE_CHARS},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    "description": (
        "start_line <= end_line, and the real meaning of "
        "attribution_index (including 0) against the read grant's "
        "persisted attribution list, are enforced by the companion "
        "validator and later host checks, not by this schema alone."
    ),
}

_UNRESOLVED_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_UNRESOLVED_KEYS),
    "properties": {
        "field_path": dict(_NON_EMPTY_STRING),
        "reason": dict(_NON_EMPTY_STRING),
        "needed_clarification": dict(_NON_EMPTY_STRING),
    },
}

_CANDIDATE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_CANDIDATE_REQUIRED_KEYS),
    "properties": {
        "local_id": dict(_NON_EMPTY_STRING),
        "statement": {"type": "string", "minLength": 1,
                      "maxLength": MAX_STATEMENT_CHARS},
        "entity": _located_text_json_schema(MAX_ENTITY_CHARS),
        "attribute": {"type": "string", "minLength": 1,
                      "maxLength": MAX_ATTRIBUTE_CHARS},
        "value_type": {"enum": list(VALUE_TYPES)},
        "value_raw": dict(_NON_EMPTY_STRING),
        "period": _located_text_json_schema(),
        "unit": _located_text_json_schema(),
        "currency": _located_text_json_schema(),
        "scope": _located_text_json_schema(),
        "basis": _located_text_json_schema(),
        "claimant": _located_text_json_schema(),
        "statement_kind": {"enum": list(STATEMENT_KINDS)},
        "polarity": {"enum": list(POLARITIES)},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_EVIDENCE_PER_CANDIDATE,
            "items": _EVIDENCE_JSON_SCHEMA,
        },
        "field_bindings": {
            "type": "object",
            "additionalProperties": False,
            "required": list(FIELD_BINDING_KEYS),
            "properties": {
                key: dict(_REF_ARRAY) for key in FIELD_BINDING_KEYS
            },
        },
        "unresolved": {"type": "array", "items": _UNRESOLVED_JSON_SCHEMA},
        "topic_hint": {"type": "string"},
    },
    "description": (
        "Enforced by the companion validator beyond this schema: "
        "evidence_ref uniqueness inside the candidate, every evidence "
        "reference resolving to this same candidate's evidence, "
        "field_bindings entries exactly matching the corresponding "
        "LocatedText evidence_refs, per-status binding cardinality, at "
        "least one primary statement evidence, a non-null integer "
        "attribution_index on every evidence item listed in "
        "field_bindings.statement, and unresolved entries for every "
        "not_stated/ambiguous field and for statement_kind 'unknown'. "
        "Host and facts-tree fields (module_key, leaf_code, fact_uid, "
        "verified, ...) are rejected as unknown keys."
    ),
}

FACT_CANDIDATE_BATCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:sam:schemas:fact-candidate:1:batch",
    "title": "fact-candidate/1 extraction batch",
    "type": "object",
    "additionalProperties": False,
    "required": list(_BATCH_KEYS),
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "items": {
            "type": "array",
            "maxItems": MAX_BATCH_ITEMS,
            "items": _CANDIDATE_JSON_SCHEMA,
        },
        "complete": {"type": "boolean"},
        "remaining_source_locs": dict(_REF_ARRAY),
        "empty_reason": {"type": ["string", "null"]},
    },
    "allOf": [
        {
            "if": {
                "required": ["complete"],
                "properties": {"complete": {"const": True}},
            },
            "then": {
                "properties": {
                    "remaining_source_locs": {"type": "array",
                                              "maxItems": 0},
                },
            },
        },
        {
            "if": {
                "required": ["complete"],
                "properties": {"complete": {"const": False}},
            },
            "then": {
                "properties": {
                    "remaining_source_locs": {
                        "type": "array", "minItems": 1, "maxItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        {
            "if": {
                "required": ["complete", "items"],
                "properties": {
                    "complete": {"const": True},
                    "items": {"type": "array", "maxItems": 0},
                },
            },
            "then": {
                "properties": {
                    "empty_reason": {"type": "string", "minLength": 1,
                                     "pattern": "\\S"},
                },
            },
            "else": {
                "properties": {"empty_reason": {"const": None}},
            },
        },
    ],
    "description": (
        "Enforced by the companion validator beyond this schema: "
        "local_id uniqueness across items and the canonical UTF-8 "
        "response size budget (MAX_RESPONSE_BYTES, measured with "
        "git_format.dumps_record on the parsed object — not the raw "
        "transferred bytes, which hosts check pre-parse via "
        "validate_response_bytes)."
    ),
}


@dataclass(frozen=True)
class CandidateSchemaError:
    """One structural violation: field path, rule code and fix hint."""
    path: str
    code: str
    message: str
    suggestion: str

    def as_dict(self):
        """Return the error as a plain JSON-compatible dict."""
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def validate_response_bytes(data):
    """Check a raw, not-yet-parsed response body against the budget.

    This is the host's pre-parse entry point for MAX_RESPONSE_BYTES: it
    measures the actual transferred bytes (or the exact UTF-8 encoding
    of a str), which validate_batch() cannot see. Returns an empty list
    or a single response_budget error.
    """
    if isinstance(data, str):
        # UTF-8 never encodes to fewer bytes than the string has
        # characters, so an over-long str is rejected before encoding.
        if len(data) > MAX_RESPONSE_BYTES:
            return [_response_budget_error()]
        size = len(data.encode("utf-8"))
    elif isinstance(data, memoryview):
        size = data.nbytes
    elif isinstance(data, (bytes, bytearray)):
        size = len(data)
    else:
        raise TypeError("response data must be bytes or str")
    if size > MAX_RESPONSE_BYTES:
        return [_response_budget_error()]
    return []


def validate_batch(payload):
    """Validate one fact-candidate/1 batch; return a list of errors.

    Structural rules run first; when (and only when) the structure is
    fully valid, the canonical UTF-8 size of the parsed object is
    checked against MAX_RESPONSE_BYTES via git_format.dumps_record. An
    empty list means the shape is legal — nothing more; see the module
    docstring for what this layer cannot prove.
    """
    errors = []
    _validate_batch_structure(payload, "$", errors)
    if errors:
        return errors
    canonical = git_format.dumps_record(payload)
    if len(canonical) > MAX_RESPONSE_BYTES:
        errors.append(_response_budget_error())
    return errors


def validate_candidate(candidate, path="$"):
    """Validate one candidate object outside a batch.

    Batch-level rules (schema_version, the complete /
    remaining_source_locs / empty_reason combinations, local_id
    uniqueness across items and the response size budget) are not part
    of this entry point.
    """
    errors = []
    _validate_candidate_structure(candidate, path, errors)
    return errors


def _response_budget_error():
    return CandidateSchemaError(
        "$",
        "response_budget",
        "response exceeds the %d-byte budget" % MAX_RESPONSE_BYTES,
        "split the extraction into smaller batches; never truncate "
        "evidence quotes to fit",
    )


def _err(errors, path, code, message, suggestion):
    errors.append(CandidateSchemaError(path, code, message, suggestion))


def _child(path, key):
    return "%s.%s" % (path, key)


def _index(path, position):
    return "%s[%d]" % (path, position)


def _reject_unknown_keys(value, allowed, path, errors, owner):
    for key in value:
        if key not in allowed:
            if owner == "candidate":
                suggestion = (
                    "remove it; host and facts-tree fields (module_key, "
                    "leaf_code, fact_uid, verified, company_id, ...) "
                    "are never accepted from the employee")
            else:
                suggestion = (
                    "remove it; only the documented fact-candidate/1 "
                    "keys are accepted and unknown keys are never "
                    "silently ignored")
            _err(errors, _child(path, key), "unknown_field",
                 "unknown %s field %r" % (owner, key), suggestion)


def _require_keys(value, required, path, errors, owner):
    for key in required:
        if key not in value:
            _err(errors, _child(path, key), "missing_field",
                 "required %s field %r is missing" % (owner, key),
                 "add the key; use the explicit empty/null state the "
                 "contract defines instead of omitting it")


def _string_field(value, key, path, errors, *, max_chars=None,
                  allow_empty=False):
    """Validate value[key] as a string; return it or None.

    Unless allow_empty is set the string must be non-empty; P01 fixes
    no non-empty rule for topic_hint, so its caller opts out.
    """
    if key not in value:
        return None
    item = value[key]
    field_path = _child(path, key)
    if not isinstance(item, str):
        _err(errors, field_path, "type_string",
             "%s must be a string, got %s" % (key, type(item).__name__),
             "pass a JSON string")
        return None
    if not item and not allow_empty:
        _err(errors, field_path, "empty_string",
             "%s must be a non-empty string" % key,
             "state the value explicitly; an empty string is never a "
             "valid placeholder")
        return None
    if max_chars is not None and len(item) > max_chars:
        _err(errors, field_path, "string_too_long",
             "%s exceeds the %d-character budget (%d chars)"
             % (key, max_chars, len(item)),
             "shorten the text or split the extraction into more "
             "batches")
        return None
    return item


def _enum_field(value, key, allowed, path, errors):
    """Validate value[key] as an exact enum token; return it or None."""
    if key not in value:
        return None
    item = value[key]
    field_path = _child(path, key)
    if not isinstance(item, str):
        _err(errors, field_path, "type_string",
             "%s must be a string, got %s" % (key, type(item).__name__),
             "pass one of the enum tokens as a JSON string")
        return None
    if item not in allowed:
        _err(errors, field_path, "enum_value",
             "%s must be one of %s, got %r"
             % (key, "/".join(allowed), item),
             "use the exact lower-case token; enum matching is "
             "case-sensitive")
        return None
    return item


def _validate_batch_structure(payload, path, errors):
    if not isinstance(payload, dict):
        _err(errors, path, "type_object",
             "the batch must be a JSON object, got %s"
             % type(payload).__name__,
             "submit one JSON object with the five batch keys")
        return
    _reject_unknown_keys(payload, _BATCH_KEY_SET, path, errors, "batch")
    _require_keys(payload, _BATCH_KEYS, path, errors, "batch")

    if "schema_version" in payload:
        version = payload["schema_version"]
        version_path = _child(path, "schema_version")
        if not isinstance(version, str):
            _err(errors, version_path, "type_string",
                 "schema_version must be a string, got %s"
                 % type(version).__name__,
                 "pass the schema version as a JSON string")
        elif version != SCHEMA_VERSION:
            _err(errors, version_path, "schema_version",
                 "schema_version must be exactly %r, got %r"
                 % (SCHEMA_VERSION, version),
                 "use the schema_version the host published for this "
                 "task; never downgrade it yourself")

    items_ok = False
    items = payload.get("items")
    if "items" in payload:
        items_path = _child(path, "items")
        if not isinstance(items, list):
            _err(errors, items_path, "type_array",
                 "items must be an array, got %s"
                 % type(items).__name__,
                 "pass an array of candidate objects (possibly empty)")
        else:
            items_ok = True
            if len(items) > MAX_BATCH_ITEMS:
                _err(errors, items_path, "array_too_long",
                     "items has %d candidates; the budget is %d"
                     % (len(items), MAX_BATCH_ITEMS),
                     "split the extraction into several batches")
            seen_local_ids = set()
            for position, item in enumerate(items):
                item_path = _index(items_path, position)
                _validate_candidate_structure(item, item_path, errors)
                if isinstance(item, dict):
                    local_id = item.get("local_id")
                    if isinstance(local_id, str) and local_id:
                        if local_id in seen_local_ids:
                            _err(errors, _child(item_path, "local_id"),
                                 "duplicate_local_id",
                                 "local_id %r is used by more than one "
                                 "candidate in this batch" % local_id,
                                 "give every candidate its own non-empty "
                                 "local_id for receipt mapping")
                        else:
                            seen_local_ids.add(local_id)

    complete_ok = False
    complete = payload.get("complete")
    if "complete" in payload:
        if isinstance(complete, bool):
            complete_ok = True
        else:
            _err(errors, _child(path, "complete"), "type_boolean",
                 "complete must be a JSON boolean, got %s"
                 % type(complete).__name__,
                 "pass true or false, not a number or string")

    remaining_ok = False
    remaining = payload.get("remaining_source_locs")
    remaining_path = _child(path, "remaining_source_locs")
    if "remaining_source_locs" in payload:
        if not isinstance(remaining, list):
            _err(errors, remaining_path, "type_array",
                 "remaining_source_locs must be an array, got %s"
                 % type(remaining).__name__,
                 "pass an array of source_loc strings")
        else:
            remaining_ok = True
            for position, loc in enumerate(remaining):
                loc_path = _index(remaining_path, position)
                if not isinstance(loc, str):
                    _err(errors, loc_path, "type_string",
                         "remaining_source_locs entries must be "
                         "strings, got %s" % type(loc).__name__,
                         "pass the source_loc as a JSON string")
                elif not loc:
                    _err(errors, loc_path, "empty_string",
                         "remaining_source_locs entries must be "
                         "non-empty source_loc strings",
                         "pass the current host source_loc")
    if complete_ok and remaining_ok:
        if complete and remaining:
            _err(errors, remaining_path, "remaining_source_locs_state",
                 "complete=true requires remaining_source_locs to be "
                 "exactly [], got %d entries" % len(remaining),
                 "return an empty array once the current window is "
                 "finished")
        if not complete and len(remaining) != 1:
            _err(errors, remaining_path, "remaining_source_locs_state",
                 "complete=false requires remaining_source_locs to "
                 "hold exactly one entry, got %d" % len(remaining),
                 "list exactly the current host source_loc; whether it "
                 "matches the host's window is a later host check")

    if "empty_reason" in payload:
        reason = payload["empty_reason"]
        reason_path = _child(path, "empty_reason")
        reason_ok = False
        if reason is None:
            reason_ok = True
        elif not isinstance(reason, str):
            _err(errors, reason_path, "type_string",
                 "empty_reason must be a string or null, got %s"
                 % type(reason).__name__,
                 "pass a JSON string or null")
        else:
            reason_ok = True
            # A whitespace-only string is no explanation either; treat
            # it exactly like the empty string, without trimming or
            # rewriting the submitted value.
            if not reason.strip():
                _err(errors, reason_path, "empty_string",
                     "empty_reason must be a non-empty explanation or "
                     "null",
                     "explain why the window contains no candidates, "
                     "or pass null")
        if reason_ok and items_ok and complete_ok:
            must_explain = complete and not items
            if must_explain and reason is None:
                _err(errors, reason_path, "empty_reason_state",
                     "an empty completed batch must explain why with a "
                     "non-empty empty_reason",
                     "state why this window legitimately contains no "
                     "candidates; never invent one")
            if not must_explain and reason is not None:
                _err(errors, reason_path, "empty_reason_state",
                     "empty_reason must be null unless items is empty "
                     "and complete is true",
                     "set empty_reason to null")


def _validate_candidate_structure(candidate, path, errors):
    if not isinstance(candidate, dict):
        _err(errors, path, "type_object",
             "each candidate must be a JSON object, got %s"
             % type(candidate).__name__,
             "pass one object per candidate with the documented keys")
        return
    _reject_unknown_keys(candidate, _CANDIDATE_ALLOWED_KEYS, path,
                         errors, "candidate")
    _require_keys(candidate, _CANDIDATE_REQUIRED_KEYS, path, errors,
                  "candidate")

    _string_field(candidate, "local_id", path, errors)
    _string_field(candidate, "statement", path, errors,
                  max_chars=MAX_STATEMENT_CHARS)
    _string_field(candidate, "attribute", path, errors,
                  max_chars=MAX_ATTRIBUTE_CHARS)
    _string_field(candidate, "value_raw", path, errors)
    _string_field(candidate, "topic_hint", path, errors,
                  allow_empty=True)
    _enum_field(candidate, "value_type", VALUE_TYPES, path, errors)
    statement_kind = _enum_field(candidate, "statement_kind",
                                 STATEMENT_KINDS, path, errors)
    _enum_field(candidate, "polarity", POLARITIES, path, errors)

    located = {}
    for field in LOCATED_TEXT_FIELDS:
        text_max = MAX_ENTITY_CHARS if field == "entity" else None
        located[field] = _located_text(candidate, field, path, errors,
                                       text_max=text_max)

    evidence_refs, attribution_indexes = _evidence_items(
        candidate, path, errors)

    for field in LOCATED_TEXT_FIELDS:
        refs = located[field][1]
        if refs:
            refs_path = _child(_child(path, field), "evidence_refs")
            for position, ref in enumerate(refs):
                if ref not in evidence_refs:
                    _err(errors, _index(refs_path, position),
                         "unresolvable_evidence_ref",
                         "%s.evidence_refs references %r, which is not "
                         "declared in this candidate's evidence"
                         % (field, ref),
                         "reference only evidence_ref values declared "
                         "in this same candidate")

    _field_bindings(candidate, path, errors, evidence_refs,
                    attribution_indexes, located, statement_kind)
    _unresolved(candidate, path, errors, located, statement_kind)


def _located_text(candidate, field, path, errors, text_max=None):
    """Validate candidate[field]; return (status, clean refs or None)."""
    if field not in candidate:
        return None, None
    value = candidate[field]
    field_path = _child(path, field)
    if not isinstance(value, dict):
        _err(errors, field_path, "type_object",
             "%s must be a LocatedText object, got %s"
             % (field, type(value).__name__),
             "pass an object with status, text, evidence_refs and "
             "reason")
        return None, None
    _reject_unknown_keys(value, _LOCATED_TEXT_KEY_SET, field_path,
                         errors, "LocatedText")
    _require_keys(value, _LOCATED_TEXT_KEYS, field_path, errors,
                  "LocatedText")

    status = value.get("status")
    status_path = _child(field_path, "status")
    if "status" in value:
        if not isinstance(status, str):
            _err(errors, status_path, "type_string",
                 "%s status must be a string, got %s"
                 % (field, type(status).__name__),
                 "pass one of the status tokens as a JSON string")
            return None, None
        if status not in LOCATED_TEXT_STATUSES:
            _err(errors, status_path, "enum_value",
                 "%s status must be one of %s, got %r"
                 % (field, "/".join(LOCATED_TEXT_STATUSES), status),
                 "use the exact lower-case token; enum matching is "
                 "case-sensitive")
            return None, None
    if status not in LOCATED_TEXT_STATUSES:
        # Missing status was already reported as a missing field.
        return None, None

    refs = value.get("evidence_refs")
    refs_path = _child(field_path, "evidence_refs")
    refs_clean = None
    if "evidence_refs" in value:
        if not isinstance(refs, list):
            _err(errors, refs_path, "type_array",
                 "%s evidence_refs must be an array, got %s"
                 % (field, type(refs).__name__),
                 "pass an array of evidence_ref strings")
        else:
            clean = True
            for position, ref in enumerate(refs):
                if not isinstance(ref, str):
                    _err(errors, _index(refs_path, position),
                         "type_string",
                         "%s evidence_refs entries must be strings, "
                         "got %s" % (field, type(ref).__name__),
                         "pass evidence_ref values as JSON strings")
                    clean = False
                elif not ref:
                    _err(errors, _index(refs_path, position),
                         "empty_string",
                         "%s evidence_refs entries must be non-empty "
                         "evidence_ref strings" % field,
                         "reference a declared evidence_ref")
                    clean = False
            if clean:
                refs_clean = refs

    text = value.get("text")
    reason = value.get("reason")
    text_path = _child(field_path, "text")
    reason_path = _child(field_path, "reason")

    if status == "known":
        if "text" in value:
            if not isinstance(text, str):
                _err(errors, text_path, "type_string",
                     "%s text must be the original passage as a "
                     "string, got %s" % (field, type(text).__name__),
                     "quote the original text for a known field")
            elif not text:
                _err(errors, text_path, "empty_string",
                     "%s has status 'known' and needs the original "
                     "text" % field,
                     "quote the original passage; a known field cannot "
                     "have empty text")
            elif text_max is not None and len(text) > text_max:
                _err(errors, text_path, "string_too_long",
                     "%s text exceeds the %d-character budget (%d "
                     "chars)" % (field, text_max, len(text)),
                     "keep the entity text within the budget")
        if refs_clean is not None and not refs_clean:
            _err(errors, refs_path, "located_text_state",
                 "%s has status 'known' and must cite at least one "
                 "evidence ref" % field,
                 "bind the original passage supporting this field")
        if "reason" in value and reason is not None:
            _err(errors, reason_path, "located_text_state",
                 "%s has status 'known' and reason must be null"
                 % field,
                 "set reason to null; reasons belong to the other "
                 "states")
        return status, refs_clean

    # not_stated / not_applicable / ambiguous: text is null and the
    # reason is a mandatory non-empty explanation.
    if "text" in value and text is not None:
        _err(errors, text_path, "located_text_state",
             "%s has status %r and text must be null"
             % (field, status),
             "do not fill in a convenient value for a field that is "
             "not stated, not applicable or ambiguous")
    if "reason" in value:
        if not isinstance(reason, str):
            _err(errors, reason_path, "type_string",
                 "%s reason must be a non-empty string for status %r"
                 % (field, status),
                 "explain the state explicitly")
        elif not reason:
            suggestion = "explain why the field is %s" % status
            if status == "not_applicable":
                suggestion += ("; cite the applicability rule relied "
                               "on, the manager reviews it")
            _err(errors, reason_path, "empty_string",
                 "%s reason must be a non-empty string for status %r"
                 % (field, status), suggestion)
    if status in ("not_stated", "not_applicable") and refs_clean:
        _err(errors, refs_path, "located_text_state",
             "%s has status %r and evidence_refs must be []"
             % (field, status),
             "leave evidence_refs empty; only ambiguous may list the "
             "involved evidence")
    return status, refs_clean


def _evidence_items(candidate, path, errors):
    """Validate candidate['evidence']; return (refs, attribution map).

    The first element is the set of cleanly declared evidence_ref
    values. The second maps each of them to (attribution_index path,
    raw attribution_index value), with _MISSING when the key is absent;
    _field_bindings uses it for the primary-evidence rule.
    """
    declared = set()
    attribution = {}
    if "evidence" not in candidate:
        return declared, attribution
    items = candidate["evidence"]
    items_path = _child(path, "evidence")
    if not isinstance(items, list):
        _err(errors, items_path, "type_array",
             "evidence must be an array, got %s"
             % type(items).__name__,
             "pass an array of evidence objects")
        return declared, attribution
    if not items:
        _err(errors, items_path, "array_too_short",
             "evidence must contain at least one item",
             "add the primary statement evidence; every candidate "
             "needs at least one")
    if len(items) > MAX_EVIDENCE_PER_CANDIDATE:
        _err(errors, items_path, "array_too_long",
             "evidence has %d items; the budget is %d"
             % (len(items), MAX_EVIDENCE_PER_CANDIDATE),
             "keep at most %d evidence items per candidate"
             % MAX_EVIDENCE_PER_CANDIDATE)
    for position, item in enumerate(items):
        _evidence_item(item, _index(items_path, position), errors,
                       declared, attribution)
    return declared, attribution


def _evidence_item(item, item_path, errors, declared, attribution):
    if not isinstance(item, dict):
        _err(errors, item_path, "type_object",
             "each evidence item must be a JSON object, got %s"
             % type(item).__name__,
             "pass an object with the seven evidence keys")
        return
    _reject_unknown_keys(item, _EVIDENCE_KEY_SET, item_path, errors,
                         "evidence")
    _require_keys(item, _EVIDENCE_KEYS, item_path, errors, "evidence")

    ref = item.get("evidence_ref")
    if "evidence_ref" in item:
        ref_path = _child(item_path, "evidence_ref")
        if not isinstance(ref, str):
            _err(errors, ref_path, "type_string",
                 "evidence_ref must be a string, got %s"
                 % type(ref).__name__,
                 "pass a short candidate-local identifier as a string")
        elif not ref:
            _err(errors, ref_path, "empty_string",
                 "evidence_ref must be a non-empty string",
                 "give the evidence item a short identifier for "
                 "field_bindings to reference")
        elif ref in declared:
            _err(errors, ref_path, "duplicate_evidence_ref",
                 "evidence_ref %r is declared twice in this candidate"
                 % ref,
                 "give each evidence item of this candidate its own "
                 "ref; field_bindings entries point at these")
        else:
            declared.add(ref)
            attribution[ref] = (
                _child(item_path, "attribution_index"),
                item["attribution_index"]
                if "attribution_index" in item else _MISSING,
            )

    if "read_grant_id" in item:
        _string_field(item, "read_grant_id", item_path, errors)
    if "source_loc" in item:
        _string_field(item, "source_loc", item_path, errors)
    if "quote" in item:
        _string_field(item, "quote", item_path, errors,
                      max_chars=MAX_QUOTE_CHARS)

    if "attribution_index" in item:
        value = item["attribution_index"]
        value_path = _child(item_path, "attribution_index")
        if value is None:
            pass
        elif isinstance(value, bool):
            _err(errors, value_path, "bool_as_int",
                 "attribution_index must be a real integer or null, "
                 "never a boolean",
                 "pass the integer index from the read grant's "
                 "persisted attribution list, or null for "
                 "header/footnote-only evidence")
        elif not isinstance(value, int):
            _err(errors, value_path, "type_integer",
                 "attribution_index must be an integer or null, got "
                 "%s" % type(value).__name__,
                 "pass a JSON integer or null")
        elif value < 0:
            _err(errors, value_path, "integer_range",
                 "attribution_index must be zero or a positive "
                 "integer, got %d" % value,
                 "use the index into the read grant's persisted "
                 "attribution list")

    start = item.get("start_line")
    end = item.get("end_line")
    start_ok = end_ok = False
    for key in ("start_line", "end_line"):
        if key not in item:
            continue
        value = item[key]
        value_path = _child(item_path, key)
        if isinstance(value, bool):
            _err(errors, value_path, "bool_as_int",
                 "%s must be a real integer, never a boolean" % key,
                 "pass a 1-based line number as a JSON integer")
        elif not isinstance(value, int):
            _err(errors, value_path, "type_integer",
                 "%s must be an integer, got %s"
                 % (key, type(value).__name__),
                 "pass a 1-based line number as a JSON integer")
        elif value < 1:
            _err(errors, value_path, "integer_range",
                 "%s must be a positive integer, got %d"
                 % (key, value),
                 "line numbers are 1-based; the host re-computes and "
                 "verifies them against the window")
        elif key == "start_line":
            start_ok = True
        else:
            end_ok = True
    if start_ok and end_ok and start > end:
        _err(errors, _child(item_path, "end_line"), "line_order",
             "start_line %d must not exceed end_line %d"
             % (start, end),
             "give the quote's real line range with start <= end")


def _field_bindings(candidate, path, errors, evidence_refs,
                    attribution_indexes, located, statement_kind):
    if "field_bindings" not in candidate:
        return
    bindings = candidate["field_bindings"]
    bindings_path = _child(path, "field_bindings")
    if not isinstance(bindings, dict):
        _err(errors, bindings_path, "type_object",
             "field_bindings must be an object, got %s"
             % type(bindings).__name__,
             "pass an object mapping each of the 12 fixed keys to an "
             "array of evidence refs")
        return
    _reject_unknown_keys(bindings, _FIELD_BINDING_KEY_SET,
                         bindings_path, errors, "field_bindings")
    _require_keys(bindings, FIELD_BINDING_KEYS, bindings_path, errors,
                  "field_bindings")

    clean_bindings = {}
    for key in FIELD_BINDING_KEYS:
        if key not in bindings:
            continue
        value = bindings[key]
        value_path = _child(bindings_path, key)
        if not isinstance(value, list):
            _err(errors, value_path, "type_array",
                 "field_bindings.%s must be an array of evidence "
                 "refs, got %s" % (key, type(value).__name__),
                 "pass an array (empty only where the status rules "
                 "allow it)")
            continue
        clean = True
        for position, ref in enumerate(value):
            if not isinstance(ref, str):
                _err(errors, _index(value_path, position),
                     "type_string",
                     "field_bindings.%s entries must be strings, got "
                     "%s" % (key, type(ref).__name__),
                     "pass evidence_ref values as JSON strings")
                clean = False
            elif not ref:
                _err(errors, _index(value_path, position),
                     "empty_string",
                     "field_bindings.%s entries must be non-empty "
                     "evidence_ref strings" % key,
                     "reference a declared evidence_ref")
                clean = False
        if not clean:
            continue
        clean_bindings[key] = value
        for position, ref in enumerate(value):
            if ref not in evidence_refs:
                _err(errors, _index(value_path, position),
                     "unresolvable_evidence_ref",
                     "field_bindings.%s references %r, which is not "
                     "declared in this candidate's evidence"
                     % (key, ref),
                     "reference only evidence_ref values declared in "
                     "this same candidate")

    statement_refs = clean_bindings.get("statement")
    if statement_refs is not None and not statement_refs:
        _err(errors, _child(bindings_path, "statement"),
             "missing_primary_evidence",
             "field_bindings.statement must list at least one primary "
             "statement evidence ref",
             "cite the evidence carrying the statement itself; a "
             "header or footnote alone is not enough")
    checked_primary = set()
    for ref in statement_refs or ():
        if ref in checked_primary:
            continue
        checked_primary.add(ref)
        found = attribution_indexes.get(ref)
        if found is not None and found[1] is None:
            _err(errors, found[0], "missing_attribution_index",
                 "evidence %r is listed in field_bindings.statement, "
                 "so its attribution_index must be a real integer "
                 "index, not null" % ref,
                 "pass the integer index into the read grant's "
                 "persisted attribution list; null is only legal for "
                 "header/footnote-only evidence that "
                 "field_bindings.statement does not list")
    for key in ("attribute", "value_raw", "polarity"):
        refs = clean_bindings.get(key)
        if refs is not None and not refs:
            _err(errors, _child(bindings_path, key),
                 "missing_field_evidence",
                 "%s is a definite direct-statement field and needs "
                 "at least one evidence ref" % key,
                 "cite the original passage this field comes from")
    kind_refs = clean_bindings.get("statement_kind")
    if (kind_refs is not None and not kind_refs
            and statement_kind is not None
            and statement_kind != "unknown"):
        _err(errors, _child(bindings_path, "statement_kind"),
             "missing_field_evidence",
             "statement_kind %r is a definite classification and "
             "needs at least one evidence ref" % statement_kind,
             "cite the passage showing the statement's nature, or use "
             "'unknown' with an unresolved entry")

    for field in LOCATED_TEXT_FIELDS:
        located_refs = located[field][1]
        bound = clean_bindings.get(field)
        if located_refs is None or bound is None:
            continue
        if sorted(located_refs) != sorted(bound):
            _err(errors, _child(bindings_path, field),
                 "binding_mismatch",
                 "field_bindings.%s (%r) must reference exactly the "
                 "same evidence as %s.evidence_refs (%r)"
                 % (field, bound, field, located_refs),
                 "keep the LocatedText refs and the field binding "
                 "identical for this field")


def _unresolved(candidate, path, errors, located, statement_kind):
    if "unresolved" not in candidate:
        return
    items = candidate["unresolved"]
    items_path = _child(path, "unresolved")
    if not isinstance(items, list):
        _err(errors, items_path, "type_array",
             "unresolved must be an array, got %s"
             % type(items).__name__,
             "pass an array of unresolved entries (possibly empty)")
        return
    explained = set()
    for position, entry in enumerate(items):
        entry_path = _index(items_path, position)
        if not isinstance(entry, dict):
            _err(errors, entry_path, "type_object",
                 "each unresolved entry must be a JSON object, got %s"
                 % type(entry).__name__,
                 "pass an object with field_path, reason and "
                 "needed_clarification")
            continue
        _reject_unknown_keys(entry, _UNRESOLVED_KEY_SET, entry_path,
                             errors, "unresolved entry")
        _require_keys(entry, _UNRESOLVED_KEYS, entry_path, errors,
                      "unresolved entry")
        for key in _UNRESOLVED_KEYS:
            _string_field(entry, key, entry_path, errors)
        field_path = entry.get("field_path")
        if isinstance(field_path, str) and field_path:
            explained.add(field_path)

    for field in LOCATED_TEXT_FIELDS:
        status = located[field][0]
        if (status in ("not_stated", "ambiguous")
                and field not in explained):
            _err(errors, _child(path, field), "missing_unresolved",
                 "%s has status %r and needs a matching unresolved "
                 "entry" % (field, status),
                 "add an unresolved entry with field_path %r giving "
                 "the reason and the clarification needed" % field)
    if statement_kind == "unknown" and "statement_kind" not in explained:
        _err(errors, _child(path, "statement_kind"),
             "missing_unresolved",
             "statement_kind 'unknown' needs a matching unresolved "
             "entry",
             "add an unresolved entry with field_path "
             "'statement_kind' explaining what is unclear")
