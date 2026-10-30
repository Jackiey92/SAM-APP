"""Entity/fact shapes from appendix A v1.1, SAM-DOCS 303eed2, §§0–4/13.

Only single-record structure is checked here. Reference resolution, version
chains, LocatedText bindings and business qualification belong to closures.
"""
from app.facts_tree import git_format
from app.facts_tree.candidate_schema import POLARITIES, STATEMENT_KINDS, VALUE_TYPES

from ._primitives import (
    _COMMON, _actor, _array_of, _digest, _enum, _id, _int_plus, _json_value,
    _located_text, _object, _optional, _ref_key, _refs, _require_together,
    _str, _ts,
)
from ._registry import register_sample, register_schema


ENTITY_KINDS = frozenset({"company", "organization", "person", "other"})
ENTITY_STATUSES = frozenset({"first_seen", "active", "merged", "retracted"})
FACT_REVISION_KINDS = frozenset({"correct", "supersede", "retract", "lineage", "initial"})
FACT_CURRENT_STATUSES = frozenset({"active", "superseded", "retracted"})
LINEAGE_KINDS = frozenset({"merge", "correct", "retract"})


def _typed_ref(value, family, path, field):
    # §0 permits an optional revision and any of the twenty record types.
    # _object supplies child coordinates; _ref_key retains the shared stable
    # identity/revision rules, including the logical binding exception.
    spec = {"record_type": _enum(git_format.RECORD_TYPES), "stable_id": _id}
    if isinstance(value, dict) and "revision" in value:
        spec["revision"] = _json_value
    _object(spec)(value, family, path, field)
    _ref_key(value, family, path, field)


_ENTITY_REVISION = {
    "entity_uid": _id,
    "revision": _int_plus,
    "entity_kind": _enum(ENTITY_KINDS),
    "raw_name": _str,
    "canonical_name": _str,
    "aliases": _array_of(_object({
        "alias": _str, "kind": _optional(_str), "source_refs": _refs,
        "basis": _optional(_str),
    })),
    "status": _enum(ENTITY_STATUSES),
    "predecessor_ref": _optional(_typed_ref),
    "lineage": _optional(_object({
        "kind": _enum(LINEAGE_KINDS), "target_refs": _refs,
    })),
    "disambiguation_issue_ref": _optional(_typed_ref),
    "actor": _actor,
    "reason": _str,
    "event_time": _ts,
}

_ENTITY_CURRENT = {
    "entity_uid": _id, "revision": _int_plus, "status": _enum(ENTITY_STATUSES),
}

_FACT_REVISION = {
    "fact_uid": _id,
    "revision": _int_plus,
    "entity_ref": _typed_ref,
    "statement_raw": _str,
    "value_raw": _optional(_str),
    "value_type": _enum(VALUE_TYPES),
    "canonical_value": _optional(_json_value),
    "value_unit": _located_text,
    "conversion_rule_version": _optional(_str),
    "conversion_input_digest": _optional(_digest),
    "statement_kind": _enum(STATEMENT_KINDS),
    "polarity": _enum(POLARITIES),
    "qualifiers": _object({
        "period": _located_text, "currency": _located_text,
        "scope": _located_text, "basis": _located_text,
        "claimant": _located_text, "entity": _located_text,
    }),
    "primary_evidence_refs": _refs,
    "field_evidence": _array_of(_object({
        "field": _str, "evidence_refs": _refs, "mapped_from": _optional(_str),
        "mapping_basis": _optional(_str),
    })),
    "unresolved": _array_of(_object({
        "code": _str, "coordinate": _optional(_str), "note": _optional(_str),
    })),
    "qualification_basis_refs": _refs,
    "confirmation_basis_refs": _refs,
    "predecessor_ref": _optional(_typed_ref),
    "valid_from": _located_text,
    "valid_to": _located_text,
    "revision_kind": _enum(FACT_REVISION_KINDS),
    "actor": _actor,
    "reason": _str,
    "event_time": _ts,
}

_FACT_CURRENT = {
    "fact_uid": _id, "revision": _int_plus, "status": _enum(FACT_CURRENT_STATUSES),
}

ENTITY_REVISION_FIELDS = _COMMON | frozenset(_ENTITY_REVISION)
ENTITY_CURRENT_FIELDS = _COMMON | frozenset(_ENTITY_CURRENT)
FACT_REVISION_FIELDS = _COMMON | frozenset(_FACT_REVISION)
FACT_CURRENT_FIELDS = _COMMON | frozenset(_FACT_CURRENT)


def _validate_fields(spec, payload, family, path):
    # The package entry point already checks the common envelope, exact top
    # level keys, versions and identities reversed from git_format's template.
    for field, validator in spec.items():
        validator(payload[field], family, path, field)


@register_schema("entity_revision", fields=ENTITY_REVISION_FIELDS)
def _validate_entity_revision(payload, family, path, oid_length):
    _validate_fields(_ENTITY_REVISION, payload, family, path)


@register_schema("entity_current", fields=ENTITY_CURRENT_FIELDS)
def _validate_entity_current(payload, family, path, oid_length):
    _validate_fields(_ENTITY_CURRENT, payload, family, path)


@register_schema("fact_revision", fields=FACT_REVISION_FIELDS)
def _validate_fact_revision(payload, family, path, oid_length):
    _validate_fields(_FACT_REVISION, payload, family, path)
    _require_together("conversion_rule_version", "conversion_input_digest")(
        payload, family, path, "$")


@register_schema("fact_current", fields=FACT_CURRENT_FIELDS)
def _validate_fact_current(payload, family, path, oid_length):
    _validate_fields(_FACT_CURRENT, payload, family, path)


def _sample_common(family):
    return {"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
            "record_type": family, "repo_id": "repo-test", "company_id": "company-test"}


def _sample_actor():
    return {"schema_version": 1, "audit_ref_type": "user_actor",
            "installation_id": "install-test", "company_id": "company-test",
            "principal_id": "owner-test", "member_revision": 1,
            "session_ref": "session-audit-id"}


def _sample_located_text():
    return {"status": "not_stated", "text": None, "value": None,
            "evidence_refs": [], "reason": None}


@register_sample("entity_revision")
def build_entity_revision(*, entity_uid="entity-1", revision=1):
    return {**_sample_common("entity_revision"), "entity_uid": entity_uid,
            "revision": revision, "entity_kind": "company", "raw_name": "示例公司",
            "canonical_name": "示例公司", "aliases": [], "status": "first_seen",
            "predecessor_ref": None, "lineage": None, "disambiguation_issue_ref": None,
            "actor": _sample_actor(), "reason": "首次记录", "event_time": 1793318400}


@register_sample("entity_current")
def build_entity_current(*, entity_uid="entity-1"):
    return {**_sample_common("entity_current"), "entity_uid": entity_uid,
            "revision": 1, "status": "first_seen"}


@register_sample("fact_revision")
def build_fact_revision(*, fact_uid="output-1", revision=1):
    # R1's structural fixture has only this business result node. This self
    # reference keeps closure 1 resolvable; entity-target semantics are outside
    # this schema card and require a real multi-node tree in the next wave.
    return {**_sample_common("fact_revision"), "fact_uid": fact_uid, "revision": revision,
            "entity_ref": {"record_type": "fact_revision", "stable_id": fact_uid,
                           "revision": revision},
            "statement_raw": "来源记录了示例陈述", "value_raw": None,
            "value_type": "text", "canonical_value": None,
            "value_unit": _sample_located_text(), "conversion_rule_version": None,
            "conversion_input_digest": None, "statement_kind": "unknown",
            "polarity": "uncertain", "qualifiers": {
                key: _sample_located_text()
                for key in ("period", "currency", "scope", "basis", "claimant", "entity")},
            "primary_evidence_refs": [], "field_evidence": [], "unresolved": [],
            "qualification_basis_refs": [], "confirmation_basis_refs": [],
            "predecessor_ref": None, "valid_from": _sample_located_text(),
            "valid_to": _sample_located_text(), "revision_kind": "initial",
            "actor": _sample_actor(), "reason": "首次记录", "event_time": 1793318400}


@register_sample("fact_current")
def build_fact_current(*, fact_uid="output-1"):
    return {**_sample_common("fact_current"), "fact_uid": fact_uid,
            "revision": 1, "status": "active"}
