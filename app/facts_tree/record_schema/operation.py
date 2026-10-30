"""Strict operation record schema."""
from ._primitives import (
    ActorAuditRefInvalid, _CANDIDATE, _COMMON, _RESULT,
    _array, _candidate, _digest, _exact, _fail, _id, _oid, _refs, _result,
    validate_actor_audit_ref,
)
from ._registry import register_schema


FIELDS = _COMMON | frozenset({
    "operation_id", "request_digest", "effect_digest", "request_id",
    "base_commit", "candidate_refs", "coverage_id", "coverage_digest",
    "coverage_validation_version", "unit_id", "disposition_result",
    "formal_fact_refs", "rule_version", "model_version", "actor",
    "approval_use_refs", "event_ids", "server_time",
})


@register_schema("operation", fields=FIELDS)
def _validate_operation(payload, family, path, oid_length):
    for field in ("operation_id", "request_id", "coverage_id",
                  "coverage_validation_version", "unit_id", "rule_version",
                  "model_version"):
        _id(payload[field], family, path, field)
    for field in ("request_digest", "effect_digest", "coverage_digest"):
        _digest(payload[field], family, path, field)
    _oid(payload["base_commit"], oid_length, family, path)
    _array(payload["candidate_refs"], family, path, "candidate_refs")
    for i, candidate in enumerate(payload["candidate_refs"]):
        _candidate(candidate, family, path, f"candidate_refs[{i}]")
    _array(payload["disposition_result"], family, path, "disposition_result")
    for i, item in enumerate(payload["disposition_result"]):
        field = f"disposition_result[{i}]"
        _exact(item, _CANDIDATE | _RESULT, family, path, field)
        _candidate({k: item[k] for k in _CANDIDATE}, family, path, field)
        _result({k: item[k] for k in _RESULT}, family, path, field)
    facts = _refs(payload["formal_fact_refs"], family, path, "formal_fact_refs")
    if any(ref[0] != "fact_revision" for ref in facts):
        _fail(family, path, "formal_fact_type", "formal_fact_refs")
    try:
        validate_actor_audit_ref(payload["actor"])
    except ActorAuditRefInvalid:
        _fail(family, path, "actor_audit_ref", "actor")
    if payload["actor"]["company_id"] != payload["company_id"]:
        _fail(family, path, "audit_scope", "actor.company_id")
    _array(payload["approval_use_refs"], family, path, "approval_use_refs")
    if payload["approval_use_refs"]:
        _fail(family, path, "unsupported_approval_audit_ref", "approval_use_refs")
    _array(payload["event_ids"], family, path, "event_ids")
    for event_id in payload["event_ids"]:
        _id(event_id, family, path, "event_ids")
    if len(set(payload["event_ids"])) != len(payload["event_ids"]):
        _fail(family, path, "duplicate_event_id", "event_ids")
    # Server time is an explicit non-negative integer timestamp, never
    # used to derive event ordering or an accepted commit (design §5).
    if type(payload["server_time"]) is not int or payload["server_time"] < 0:
        _fail(family, path, "server_time", "server_time")
