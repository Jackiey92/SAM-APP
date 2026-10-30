"""Appendix A v1.1 (303eed2), §§0/8–10/13: field schemas only.

Typed refs are checked for shape, without resolving targets or enforcing
lineage cardinality, version chains, source qualification or primary leaves.
"""
from app.facts_tree.fact_identity import EVENT_RELATIONS
from app.facts_tree.source_registry import EVIDENCE_RELATIONS

from ._primitives import (
    _COMMON, _actor, _array_of, _audit_refs, _digest, _enum, _exact,
    _id, _object, _optional, _positive, _ref_key, _refs, _str,
    _tagged_union, _ts, git_format,
)
from ._registry import register_sample, register_schema


LINK_STATUSES = frozenset({"active", "superseded", "retracted"})
STATEMENT_OR_FIELD = frozenset({"statement", "field"})
BASIS_STATUSES = frozenset({"known", "unknown"})
LINEAGE_TYPES = frozenset(EVENT_RELATIONS)
# Appendix A §9; fact_placements has no importable action-set constant.
PLACEMENT_ACTIONS = frozenset({"initial", "reassign"})


def _ref(value, family, path, field):
    # Optional revision may be numeric or opaque; target semantics belong to
    # closures. _ref_key also rejects unstable aliases and unknown families.
    keys = {"record_type", "stable_id"}
    if isinstance(value, dict) and "revision" in value:
        keys.add("revision")
    _exact(value, keys, family, path, field)
    _ref_key(value, family, path, field)


_LINK = _tagged_union("subtype", {
    "fact_evidence": {
        "role": _enum(EVIDENCE_RELATIONS), "fact_ref": _ref,
        "statement_or_field": _enum(STATEMENT_OR_FIELD),
        "field": _optional(_str),
    },
    "fact_lineage": {
        "edges": _array_of(_object({
            "parent_ref": _ref, "child_ref": _optional(_ref),
            "lineage_type": _enum(LINEAGE_TYPES),
        })),
    },
    "source_dependency": {
        "source_version_ref": _ref, "basis_status": _enum(BASIS_STATUSES),
        "dependency_ref": _optional(_ref),
    },
})

_LINK_SPEC = {
    "link_id": _id, "revision": _positive,
    "predecessor_ref": _optional(_ref), "link": _LINK,
    "status": _enum(LINK_STATUSES), "evidence_refs": _refs,
    "actor": _actor, "reason": _str, "event_time": _ts,
    "superseded_by": _optional(_ref),
}
_PLACEMENT_SPEC = {
    "fact_uid": _id, "revision": _positive, "fact_ref": _ref,
    "predecessor_ref": _optional(_ref), "pack_path_id": _id,
    "template_pack": _id, "pack_version": _positive,
    "manifest_sha256": _digest, "binding_revision": _positive,
    "trunk_code": _str, "leaf_code": _str,
    "action": _enum(PLACEMENT_ACTIONS), "group_key": _str, "order_key": _str,
    "actor": _actor, "reason": _str, "migration_ref": _optional(_ref),
    "approval_use_refs": _audit_refs, "event_time": _ts,
}
_CURRENT_SPEC = {"fact_uid": _id, "revision": _positive}

LINK_FIELDS = _COMMON | frozenset(_LINK_SPEC)
PLACEMENT_FIELDS = _COMMON | frozenset(_PLACEMENT_SPEC)
CURRENT_FIELDS = _COMMON | frozenset(_CURRENT_SPEC)


@register_schema("link_revision", fields=LINK_FIELDS)
def _validate_link(payload, family, path, oid_length):
    for field, validator in _LINK_SPEC.items():
        validator(payload[field], family, path, field)


@register_schema("placement_revision", fields=PLACEMENT_FIELDS)
def _validate_placement(payload, family, path, oid_length):
    for field, validator in _PLACEMENT_SPEC.items():
        validator(payload[field], family, path, field)


@register_schema("placement_current", fields=CURRENT_FIELDS)
def _validate_current(payload, family, path, oid_length):
    for field, validator in _CURRENT_SPEC.items():
        validator(payload[field], family, path, field)


def _common(record_type):
    return {"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
            "record_type": record_type, "repo_id": "repo-test",
            "company_id": "company-test"}


def _sample_actor():
    return {"schema_version": 1, "audit_ref_type": "user_actor",
            "installation_id": "install-test", "company_id": "company-test",
            "principal_id": "owner-test", "member_revision": 1,
            "session_ref": "session-audit-id"}


@register_sample("link_revision")
def build_link_revision(*, link_id="output-1", revision=1):
    # R1 closure 1 can resolve this self-reference. Unlike fact_evidence or
    # fact_lineage it introduces no nested formal-fact output in closure 5.
    return {
        **_common("link_revision"), "link_id": link_id, "revision": revision,
        "predecessor_ref": None,
        "link": {"subtype": "source_dependency",
                 "source_version_ref": {"record_type": "link_revision",
                                        "stable_id": link_id, "revision": revision},
                 "basis_status": "unknown", "dependency_ref": None},
        "status": "active", "evidence_refs": [], "actor": _sample_actor(),
        "reason": "schema sample", "event_time": 0, "superseded_by": None,
    }


@register_sample("placement_revision")
def build_placement_revision(*, fact_uid="output-1", revision=1):
    # Shape-only fact_ref deliberately points at this placement, keeping the
    # R1 frame resolvable without asserting a formal-fact output or eligibility.
    return {
        **_common("placement_revision"), "fact_uid": fact_uid,
        "revision": revision,
        "fact_ref": {"record_type": "placement_revision", "stable_id": fact_uid,
                     "revision": revision},
        "predecessor_ref": None, "pack_path_id": "pack-path-1",
        "template_pack": "pack-test", "pack_version": 1,
        "manifest_sha256": "a" * 64, "binding_revision": 1,
        "trunk_code": "trunk-test", "leaf_code": "leaf-test",
        "action": "initial", "group_key": "group-test", "order_key": "order-test",
        "actor": _sample_actor(), "reason": "schema sample",
        "migration_ref": None, "approval_use_refs": [], "event_time": 0,
    }


@register_sample("placement_current")
def build_placement_current(*, fact_uid="output-1"):
    return {**_common("placement_current"), "fact_uid": fact_uid, "revision": 1}
