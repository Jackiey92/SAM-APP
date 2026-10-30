"""Shared pure validation primitives for record-schema plugins."""
from dataclasses import dataclass

from app.facts_tree import bindings_current, git_format
from app.host_audit_refs import ActorAuditRefInvalid, validate_actor_audit_ref


_COMMON = frozenset({
    "schema_version", "format_version", "record_type", "repo_id", "company_id",
})

_CANDIDATE = frozenset({"candidate_uid", "candidate_revision", "payload_digest"})

_RESULT = frozenset({"terminal", "result_refs"})

_HEX = frozenset("0123456789abcdef")

_NONTERMINAL_RESULTS = frozenset({"issue_event", "proposal_event"})

_TERMINAL_RESULTS = frozenset({
    "fact_revision", "evidence", "link_revision", "placement_revision",
    "issue_event",
})

_UNSTABLE = frozenset({"HEAD", "current", "latest"})

_WHITELIST = frozenset({"local_id", "value_type", "statement_kind", "polarity"})

_ENVELOPE_SUMMARY = frozenset({"source_id", "version_id", "mapped_fingerprint"})



@dataclass(frozen=True)
class RecordSchemaClosureError(ValueError):
    """Stable, non-payload-bearing diagnostic; no naked validation errors."""

    family: str
    path: str
    code: str
    field: str

    def __str__(self):
        return f"{self.family}:{self.path}:{self.code}:{self.field}"


def _fail(family, path, code, field):
    raise RecordSchemaClosureError(family, path, code, field)


def _exact(value, keys, family, path, field):
    if not isinstance(value, dict) or any(type(k) is not str for k in value):
        _fail(family, path, "object_type", field)
    if set(value) - keys:
        _fail(family, path, "unknown_field", field)
    if keys - set(value):
        _fail(family, path, "missing_field", field)


def _id(value, family, path, field):
    try:
        git_format.encode_id(value)
    except (TypeError, ValueError):
        _fail(family, path, "invalid_id", field)


def _positive(value, family, path, field):
    try:
        git_format.encode_revision(value)
    except (TypeError, ValueError):
        _fail(family, path, "positive_integer", field)


_int_plus = _positive
_int_positive = _positive


def _str(value, family, path, field):
    if type(value) is not str or not value:
        _fail(family, path, "nonempty_string", field)


def _bool(value, family, path, field):
    if type(value) is not bool:
        _fail(family, path, "boolean_type", field)


def _ts(value, family, path, field):
    """Non-negative builtin Unix seconds, as in operation.server_time."""
    if type(value) is not int or value < 0:
        _fail(family, path, "timestamp", field)


def _enum(allowed):
    """Build a validator from a caller-owned controlled set; no coercion."""
    allowed = frozenset(allowed)

    def validate(value, family, path, field):
        # Typed equality also refuses True for 1 and unhashable input safely.
        if not any(type(value) is type(item) and value == item for item in allowed):
            _fail(family, path, "enum_value", field)
    return validate


def _optional(validator):
    """Allow explicit null; the enclosing object still requires the key."""
    def validate(value, family, path, field):
        if value is not None:
            return validator(value, family, path, field)
    return validate


def _child(field, key):
    return f"{field}.{key}"


def _closed_keys(value, allowed, required, family, path, field):
    """New composable objects report the offending child, not the parent."""
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _fail(family, path, "object_type", field)
    unknown = set(value) - allowed
    if unknown:
        _fail(family, path, "unknown_field", _child(field, min(unknown)))
    missing = required - set(value)
    if missing:
        _fail(family, path, "missing_field", _child(field, min(missing)))


def _object(spec):
    """Exact required keys mapped to (value, family, path, field) validators."""
    spec = dict(spec)
    keys = frozenset(spec)

    def validate(value, family, path, field):
        _closed_keys(value, keys, keys, family, path, field)
        for key, validator in spec.items():
            validator(value[key], family, path, _child(field, key))
    return validate


def _array_of(item_validator):
    """Validate every item with its index; duplicates are caller policy."""
    def validate(value, family, path, field):
        _array(value, family, path, field)
        for index, item in enumerate(value):
            item_validator(item, family, path, f"{field}[{index}]")
    return validate


def _require_any(fields):
    """Require one present, non-null key; false/zero/empty containers count.

    Compose after _object for field types. This only checks presence, never
    truthiness or business meaning; it does not make missing keys optional.
    """
    fields = tuple(fields)

    def validate(value, family, path, field):
        if not isinstance(value, dict):
            _fail(family, path, "object_type", field)
        if not any(value.get(key) is not None for key in fields):
            _fail(family, path, "require_any", field)
    return validate


def _require_together(a, b):
    """Require both keys to be non-null together, or both absent/null."""
    def validate(value, family, path, field):
        if not isinstance(value, dict):
            _fail(family, path, "object_type", field)
        if (value.get(a) is None) != (value.get(b) is None):
            missing = a if value.get(a) is None else b
            _fail(family, path, "require_together", _child(field, missing))
    return validate


def _json_value(value, family, path, field):
    """Keep the serializer's exact JSON types/budgets, with a local coordinate."""
    try:
        git_format.dumps_record({"value": value})
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail(family, path, "record_encoding", field)


def _located_text(value, family, path, field, *, require_known=False,
                  value_validator=_json_value):
    """Frozen LocatedText shape; closure-3 known binding is opt-in only.

    Non-null values default to canonical JSON; callers may supply the
    field-specific normalized-value validator. No defaults are inferred.
    """
    _object({
        "status": _enum(frozenset({"known", "not_stated", "not_applicable", "ambiguous"})),
        "text": _optional(_str), "value": _optional(value_validator),
        "evidence_refs": _refs, "reason": _optional(_str),
    })(value, family, path, field)
    if require_known:
        known = value["status"] == "known"
        for key in ("text", "value"):
            if (value[key] is not None) != known:
                _fail(family, path, "located_text_binding", _child(field, key))


def _tagged_union(discriminator, branches):
    """Select an exact branch spec; the discriminator is always required.

    Branch specs may omit the discriminator validator: selection supplies
    its exact typed value, and the key is part of the branch's closed shape.
    """
    branches = dict(branches)
    validators = {tag: _object({**spec, discriminator: _enum(frozenset({tag}))})
                  for tag, spec in branches.items()}
    tags = _enum(frozenset(branches))

    def validate(value, family, path, field):
        if not isinstance(value, dict) or any(type(key) is not str for key in value):
            _fail(family, path, "object_type", field)
        if discriminator not in value:
            _fail(family, path, "missing_field", _child(field, discriminator))
        try:
            tags(value[discriminator], family, path, _child(field, discriminator))
        except RecordSchemaClosureError:
            _fail(family, path, "unknown_subtype", _child(field, discriminator))
        validators[value[discriminator]](value, family, path, field)
    return validate


def _actor(value, family, path, field):
    """Reuse the host actor shape only; this does not assert authorization."""
    try:
        validate_actor_audit_ref(value)
    except ActorAuditRefInvalid:
        invalid = True
    else:
        invalid = False
    if invalid:
        _fail(family, path, "actor_audit_ref", field)


def _audit_refs(value, family, path, field):
    """F-007 has not published approval-use shapes: only [] is supported."""
    _array(value, family, path, field)
    if value:
        _fail(family, path, "unsupported_approval_audit_ref", field)


def _legacy_block(allowed_keys):
    """Optional legacy keys are caller-whitelisted; values stay canonical JSON."""
    allowed = frozenset(allowed_keys)

    def validate(value, family, path, field):
        _closed_keys(value, allowed, frozenset(), family, path, field)
        for key, item in value.items():
            _json_value(item, family, path, _child(field, key))
    return validate


def _digest(value, family, path, field):
    if type(value) is not str or len(value) != 64 or not set(value) <= _HEX:
        _fail(family, path, "sha256_digest", field)


def _array(value, family, path, field):
    if type(value) is not list:
        _fail(family, path, "array_type", field)


def _canonical(payload, family, path):
    try:
        return git_format.dumps_record(payload)
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail(family, path, "record_encoding", "$")


def _path_identity(path, payload, repo_id, company_id):
    family = "tree"
    try:
        family = git_format.canonical_record_type(path)
    except (TypeError, ValueError):
        _fail(family, "<invalid-path>", "record_path", "path")
    if family is None:
        _fail("tree", path, "record_path", "path")
    _canonical(payload, family, path)
    if not isinstance(payload, dict):
        _fail(family, path, "object_type", "$")
    # §2.1 explicitly exempts the frozen repository/taxonomy wrappers.
    # They are carried through, not upgraded to a new business schema.
    frozen_wrapper = family in {"repository_meta", "taxonomy_manifest", "taxonomy_leaf"}
    if not frozen_wrapper and payload.get("record_type") != family:
        _fail(family, path, "record_type", "record_type")
    for field, expected in (("repo_id", repo_id), ("company_id", company_id)):
        if family in {"taxonomy_manifest", "taxonomy_leaf"} and field not in payload:
            continue
        _id(payload.get(field), family, path, field)
        if payload[field] != expected:
            _fail(family, path, "scope_mismatch", field)
    # Reverse through the original template, then reconstruct exclusively
    # through record_path. No copy of the twenty-type path/id vocabulary.
    ids = git_format._match_template(family, path)
    for field, value in ids.items():
        if family in {"taxonomy_manifest", "taxonomy_leaf"}:
            # Opaque pack_version is intentionally not the business version.
            # Only a leaf's code has a corresponding payload path identity.
            if field == "leaf_id" and payload.get("code") != value:
                _fail(family, path, "path_identity", "code")
            continue
        actual = payload.get(field)
        if type(actual) is not type(value) or actual != value:
            _fail(family, path, "path_identity", field)
    try:
        rebuilt = git_format.record_path(family, **(
            ids if frozen_wrapper else {k: payload[k] for k in ids}))
    except (TypeError, ValueError, KeyError):
        _fail(family, path, "path_identity", "path")
    if rebuilt != path:
        _fail(family, path, "path_identity", "path")
    return family, ids


def _candidate(ref, family, path, field):
    _exact(ref, _CANDIDATE, family, path, field)
    _id(ref["candidate_uid"], family, path, field + ".candidate_uid")
    _positive(ref["candidate_revision"], family, path, field + ".candidate_revision")
    _digest(ref["payload_digest"], family, path, field + ".payload_digest")
    return ref["candidate_uid"], ref["candidate_revision"]


def _ref_key(ref, family, path, field):
    if not isinstance(ref, dict):
        _fail(family, path, "typed_ref_shape", field)
    kind = ref.get("record_type")
    if type(kind) is not str or kind not in git_format.RECORD_TYPES:
        _fail(family, path, "typed_ref_type", field)
    keys = {"record_type", "stable_id"}
    if "revision" in ref:
        keys.add("revision")
    _exact(ref, keys, family, path, field)
    _id(ref["stable_id"], family, path, field + ".stable_id")
    revision = ref.get("revision")
    logical_binding = kind == "bindings_current" and ref["stable_id"] == bindings_current.CURRENT_STABLE_ID
    if (ref["stable_id"] in _UNSTABLE and not logical_binding) or revision in ("HEAD", "current", "latest"):
        _fail(family, path, "unstable_history_ref", field)
    if logical_binding:
        _positive(revision, family, path, field + ".revision")
    if "revision" in ref:
        if type(revision) is int:
            _positive(revision, family, path, field + ".revision")
        else:
            _id(revision, family, path, field + ".revision")
    return kind, ref["stable_id"], revision


def _refs(value, family, path, field):
    _array(value, family, path, field)
    keys = [_ref_key(ref, family, path, f"{field}[{i}]")
            for i, ref in enumerate(value)]
    if len(set(keys)) != len(keys):
        _fail(family, path, "duplicate_ref", field)
    return keys


def _result(value, family, path, field):
    _exact(value, _RESULT, family, path, field)
    if type(value["terminal"]) is not bool:
        _fail(family, path, "terminal_boolean", field)
    keys = _refs(value["result_refs"], family, path, field + ".result_refs")
    if not keys:
        _fail(family, path, "missing_result_ref", field)
    kinds = {key[0] for key in keys}
    if not value["terminal"]:
        if not kinds <= _NONTERMINAL_RESULTS:
            _fail(family, path, "nonterminal_result_type", field)
        if len(kinds) != 1:
            _fail(family, path, "multiple_primary_result_types", field)
    elif not kinds <= _TERMINAL_RESULTS:
        _fail(family, path, "terminal_result_type", field)
    elif "issue_event" in kinds and "fact_revision" in kinds:
        _fail(family, path, "conflict_fact_output", field)
    return keys


def _oid(value, length, family, path):
    if type(value) is not str or len(value) != length or not set(value) <= _HEX:
        _fail(family, path, "base_commit_oid", "base_commit")


def _target_index(tree):
    index = {}
    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        ids = git_format._match_template(family, path)
        values = tuple(ids.values())
        # Mutable pointer paths have fewer identity components than their
        # actual historical target; require explicit revision resolution.
        if family in {"entity_current", "fact_current", "placement_current",
                      "bindings_current"}:
            continue
        if values:
            key = (family, values[0], values[1] if len(values) == 2 else None)
            index[key] = path
        if family == "event" and payload["event_type"] in bindings_current.BINDING_EVENT_TYPES:
            key = ("bindings_current", bindings_current.CURRENT_STABLE_ID,
                   payload["after"]["binding_revision"])
            if key in index:
                _fail("event", path, "ambiguous_binding_ref", "after.binding_revision")
            index[key] = path
    return index
