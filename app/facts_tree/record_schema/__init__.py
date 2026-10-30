"""F-001-R1: pure schemas and closure 1/5 for a synthetic fixed tree C.

Design 97 sections 2.1-2.3, table 3 (lines 75-78), section 4 items 1/5
(lines 98/102), and section 5 (109-111). Table 3 specifies *semantics*
(line 55); SCHEMA_FIELDS below publishes their v1 machine spelling.
Candidate identities/candidate_refs retain candidate_reference_closure's
existing spelling. No business action names, synonym tables or heuristics
are used. ``terminal`` and ``result_refs`` encode the table's terminal
disposition and output references: non-terminal issue/proposal results
represent hold/propose_revision; a terminal issue result represents
record_conflict and cannot claim a fact output (section 4, line 102).

Only four families have strict business schemas. Other RECORD_TYPES are
reference targets checked for canonical path identity and repo/company,
with an explicit schema-registration seam; their business schemas and
closures 2/3/4/6 are NOT implemented. External object references are
SHA-256 content addresses, not business typed refs; existence, persistence
and host authorization are not asserted. Actor audit_ref uses the existing
F-007 pure validator. Approval-use refs fail closed until F-007 publishes
their schema. An empty approval list makes no authorization assertion.

The path vocabulary comes exclusively from git_format, including its
private _match_template helper for reverse identity extraction. Typed refs
have record_type/stable_id and revision iff the fixed path has a second
identity component; opaque version/event IDs retain encode_id semantics.
Mutable current records cannot serve as historical targets. The already
frozen logical binding exception resolves an exact revision to its event
after snapshot; binding version-chain closure remains outside this card.

validate_record_schema_closure accepts a dict of path -> payload dict and
returns None or raises RecordSchemaClosureError (family/path/code/field).
loads_schema_record is the bytes/text boundary: duplicate keys cannot be
recovered from an already materialized dict. Both entries reuse git_format
for exact Decimal, UTF-8, canonical JSON and all serialization budgets.
No Git, Broker, file, database, model, network or feature-gate operations.
"""

from importlib import import_module as _import_module
from pkgutil import iter_modules as _iter_modules

from ._primitives import (
    RecordSchemaClosureError, _canonical, _exact, _fail, _id, _path_identity,
    _positive, _target_index, bindings_current, git_format,
)
from ._registry import _freeze_registries, get_sample_builder, register_sample


# Every non-private module in this package is a self-registering plugin.
# Sorting module names makes import and registration order deterministic.
for _module_name in sorted(info.name for info in _iter_modules(__path__)
                           if not info.name.startswith("_")):
    _import_module(f"{__name__}.{_module_name}")

SCHEMA_VALIDATORS, CLOSURE_VALIDATORS, SCHEMA_FIELDS, SCHEMA_BUILDERS = _freeze_registries()
assert set(SCHEMA_BUILDERS) <= set(SCHEMA_FIELDS), "sample registered without schema"
_BINDING_EVENT_FIELDS = SCHEMA_FIELDS["event"] | {"actor", "reason", "approval_use_refs"}


__all__ = [
    "RecordSchemaClosureError", "SCHEMA_FIELDS", "SCHEMA_VALIDATORS",
    "CLOSURE_VALIDATORS", "SCHEMA_BUILDERS", "get_sample_builder", "register_sample",
    "validate_record_schema", "loads_schema_record",
    "validate_record_schema_closure",
]


def validate_record_schema(path, payload, *, repo_id, company_id,
                           object_format="sha1"):
    """Validate one record; no cross-record closure or authorization claim."""
    if object_format not in ("sha1", "sha256"):
        _fail("tree", "<tree>", "object_format", "object_format")
    for field, value in (("repo_id", repo_id), ("company_id", company_id)):
        _id(value, "tree", "<tree>", field)
    family, _ = _path_identity(path, payload, repo_id, company_id)
    if family in SCHEMA_VALIDATORS:
        fields = SCHEMA_FIELDS[family]
        if family == "event" and payload.get("event_type") in tuple(bindings_current.BINDING_EVENT_TYPES):
            fields = _BINDING_EVENT_FIELDS
        _exact(payload, fields, family, path, "$")
        for field, expected in (("schema_version", 1),
                                ("format_version", git_format.FORMAT_VERSION)):
            _positive(payload[field], family, path, field)
            if payload[field] != expected:
                _fail(family, path, "unsupported_version", field)
        SCHEMA_VALIDATORS[family](
            payload, family, path, 40 if object_format == "sha1" else 64)
    return None


def loads_schema_record(data, *, path, repo_id, company_id, object_format="sha1"):
    """Parse canonical bytes/text, rejecting duplicates before dict conversion."""
    try:
        family = git_format.canonical_record_type(path) or "tree"
    except (TypeError, ValueError):
        _fail("tree", "<invalid-path>", "record_path", "path")
    try:
        payload = git_format.loads_record(data)
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail(family, path, "record_encoding", "$")
    encoded = _canonical(payload, family, path)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if raw != encoded:
        _fail(family, path, "noncanonical_encoding", "$")
    validate_record_schema(path, payload, repo_id=repo_id, company_id=company_id,
                           object_format=object_format)
    return payload


def validate_record_schema_closure(tree, *, repo_id, company_id,
                                   object_format="sha1"):
    """Validate strict four-family schemas, all typed refs, and operation closure.

    C is the supplied memory tree, including historical input/operation/
    consumed/output records. Duplicate paths cannot be represented by a
    dict; canonical path-to-identity equality also prevents identity aliases.
    Ordinals use the bounded zero-based run [0, event_count), independently
    of event_ids order, UUIDs or server time. Input objects are never changed.
    """
    if type(tree) is not dict:
        _fail("tree", "<tree>", "tree_type", "$")
    if object_format not in ("sha1", "sha256"):
        _fail("tree", "<tree>", "object_format", "object_format")
    for field, value in (("repo_id", repo_id), ("company_id", company_id)):
        _id(value, "tree", "<tree>", field)
    if any(type(path) is not str for path in tree):
        _fail("tree", "<invalid-path>", "record_path", "path")
    ordered = {path: tree[path] for path in sorted(tree)}
    for path, payload in ordered.items():
        validate_record_schema(path, payload, repo_id=repo_id,
                               company_id=company_id, object_format=object_format)
    for _n in sorted(CLOSURE_VALIDATORS):
        CLOSURE_VALIDATORS[_n](ordered, _target_index(ordered))
    return None
