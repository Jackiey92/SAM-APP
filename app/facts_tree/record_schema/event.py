"""Strict event record schema."""
from ._primitives import (
    ActorAuditRefInvalid, _COMMON, _array, _digest, _fail, _id,
    _oid, _positive, _refs, bindings_current, validate_actor_audit_ref,
)
from ._registry import register_schema


FIELDS = _COMMON | frozenset({
    "event_id", "operation_id", "base_commit", "ordinal", "event_type",
    "changed_refs", "before", "after", "invalidation_domains",
    "affected_refs",
})


@register_schema("event", fields=FIELDS)
def _validate_event(payload, family, path, oid_length):
    for field in ("event_id", "operation_id", "event_type"):
        _id(payload[field], family, path, field)
    _oid(payload["base_commit"], oid_length, family, path)
    if type(payload["ordinal"]) is not int or payload["ordinal"] < 0:
        _fail(family, path, "event_ordinal", "ordinal")
    refs = _refs(payload["changed_refs"], family, path, "changed_refs")
    if not refs:
        _fail(family, path, "missing_changed_ref", "changed_refs")
    _refs(payload["affected_refs"], family, path, "affected_refs")
    if payload["event_type"] in bindings_current.BINDING_EVENT_TYPES:
        try:
            bindings_current.validate_binding_event(
                payload, repo_id=payload["repo_id"], company_id=payload["company_id"])
            validate_actor_audit_ref(payload["actor"])
        except (TypeError, ValueError, ActorAuditRefInvalid):
            _fail(family, path, "binding_event_schema", "before/after/actor")
        if payload["actor"]["company_id"] != payload["company_id"]:
            _fail(family, path, "audit_scope", "actor.company_id")
        if type(payload["reason"]) is not str or not payload["reason"].strip():
            _fail(family, path, "reason_text", "reason")
        _array(payload["approval_use_refs"], family, path, "approval_use_refs")
        if payload["approval_use_refs"]:
            _fail(family, path, "unsupported_approval_audit_ref", "approval_use_refs")
        _array(payload["invalidation_domains"], family, path, "invalidation_domains")
        for domain in payload["invalidation_domains"]:
            _id(domain, family, path, "invalidation_domains")
        return
    for field in ("before", "after"):
        values = payload[field]
        _array(values, family, path, field)
        if len(values) != len(refs):
            _fail(family, path, "event_snapshot_count", field)
        for value in values:
            if value is None:
                continue
            if type(value) is int:
                _positive(value, family, path, field)
            else:
                _digest(value, family, path, field)
    for before, after in zip(payload["before"], payload["after"]):
        if before == after:
            _fail(family, path, "empty_event_change", "before/after")
    _array(payload["invalidation_domains"], family, path, "invalidation_domains")
    for domain in payload["invalidation_domains"]:
        _id(domain, family, path, "invalidation_domains")
