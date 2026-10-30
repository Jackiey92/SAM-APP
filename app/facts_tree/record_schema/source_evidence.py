"""Source/evidence field schemas: appendix A v1.1 §§0, 5–7, 13 (303eed2).

Only single-record shapes are checked here. Reference resolution, version
chains, hash availability and quote bindings belong to later closures.
"""
from ._primitives import (
    _COMMON, _actor, _audit_refs, _digest, _enum, _id, _object, _optional,
    _positive, _ref_key, _require_together, _str, _ts, git_format,
)
from ._registry import register_sample, register_schema


SOURCE_LIFECYCLE_STATUSES = frozenset({"active", "pending_removal", "removed"})


def _typed_ref(value, family, path, field):
    # _ref_key closes the keys and validates the type/id/optional revision;
    # it does not look up the target or enforce a cross-family relationship.
    _ref_key(value, family, path, field)


def _locator(value, family, path, field):
    if type(value) is str:
        _str(value, family, path, field)
    else:
        _object({"kind": _str, "uri": _str, "digest": _optional(_digest)})(
            value, family, path, field)


def _attribution_index(value, family, path, field):
    if type(value) is str:
        _str(value, family, path, field)
    else:
        _positive(value, family, path, field)


_SOURCE_VERSION = {
    "source_id": _id,
    "version_id": _id,
    "seq": _positive,
    "kind": _str,
    "origin_zone": _optional(_str),
    "origin_key": _optional(_str),
    "display_name": _str,
    "locator": _optional(_locator),
    "content_fingerprint": _optional(_digest),
    "fingerprint_algorithm": _optional(_str),
    "file_hash": _optional(_digest),
    "source_description_ref": _optional(_str),
    "mapped_object_ref": _optional(_typed_ref),
    "adapter_rule_version": _optional(_str),
    "mapping_rule_version": _optional(_str),
    "prior_version_ref": _optional(_typed_ref),
    "captured_at": _ts,
    "actor": _actor,
}

_SOURCE_LIFECYCLE = {
    "source_id": _id,
    "revision": _positive,
    "predecessor_ref": _optional(_typed_ref),
    "source_description_ref": _optional(_str),
    "status": _enum(SOURCE_LIFECYCLE_STATUSES),
    "version_scope": _object({"version_ref": _typed_ref,
                              "scope_note": _optional(_str)}),
    "reason": _str,
    "actor": _actor,
    "approval_use_refs": _audit_refs,
    "event_time": _ts,
}

_EVIDENCE = {
    "evidence_id": _id,
    "source_version_ref": _typed_ref,
    "original_hash": _optional(_digest),
    "mapped_fingerprint": _optional(_digest),
    "source_loc": _optional(_str),
    "attribution_index": _optional(_attribution_index),
    "coordinate": _optional(_str),
    "quote_excerpt_ref": _optional(_typed_ref),
    "quote_digest": _optional(_digest),
    "legacy_normalized_fingerprint": _optional(_digest),
    "legacy_fingerprint_algorithm": _optional(_str),
    "usage": _str,
    "read_audit_ref": _optional(_str),
}


@register_schema("source_version", fields=_COMMON | _SOURCE_VERSION.keys())
def _validate_source_version(payload, family, path, oid_length):
    for field, validator in _SOURCE_VERSION.items():
        validator(payload[field], family, path, field)
    _require_together("fingerprint_algorithm", "content_fingerprint")(
        payload, family, path, "$")


@register_sample("source_version")
def build_source_version(*, source_id="source-1", version_id="v-1"):
    return {
        "schema_version": 1, "format_version": git_format.FORMAT_VERSION,
        "record_type": "source_version", "repo_id": "repo-test",
        "company_id": "company-test", "source_id": source_id,
        "version_id": version_id, "seq": 1, "kind": "document",
        "origin_zone": None, "origin_key": None, "display_name": "Test source",
        "locator": None, "content_fingerprint": None,
        "fingerprint_algorithm": None, "file_hash": None,
        "source_description_ref": None, "mapped_object_ref": None,
        "adapter_rule_version": None, "mapping_rule_version": None,
        "prior_version_ref": None, "captured_at": 0,
        "actor": {"schema_version": 1, "audit_ref_type": "user_actor",
                  "installation_id": "install-test", "company_id": "company-test",
                  "principal_id": "owner-test", "member_revision": 1,
                  "session_ref": "session-audit-id"},
    }


@register_schema("source_lifecycle", fields=_COMMON | _SOURCE_LIFECYCLE.keys())
def _validate_source_lifecycle(payload, family, path, oid_length):
    for field, validator in _SOURCE_LIFECYCLE.items():
        validator(payload[field], family, path, field)


@register_sample("source_lifecycle")
def build_source_lifecycle(*, source_id="source-1", revision=1):
    return {
        "schema_version": 1, "format_version": git_format.FORMAT_VERSION,
        "record_type": "source_lifecycle", "repo_id": "repo-test",
        "company_id": "company-test", "source_id": source_id,
        "revision": revision, "predecessor_ref": None,
        "source_description_ref": None, "status": "active",
        "version_scope": {"version_ref": {"record_type": "source_version",
                                          "stable_id": source_id,
                                          "revision": "v-1"},
                          "scope_note": None},
        "reason": "Test lifecycle",
        "actor": {"schema_version": 1, "audit_ref_type": "user_actor",
                  "installation_id": "install-test", "company_id": "company-test",
                  "principal_id": "owner-test", "member_revision": 1,
                  "session_ref": "session-audit-id"},
        "approval_use_refs": [], "event_time": 0,
    }


@register_schema("evidence", fields=_COMMON | _EVIDENCE.keys())
def _validate_evidence(payload, family, path, oid_length):
    for field, validator in _EVIDENCE.items():
        validator(payload[field], family, path, field)
    _require_together("legacy_normalized_fingerprint", "legacy_fingerprint_algorithm")(
        payload, family, path, "$")


@register_sample("evidence")
def build_evidence(*, evidence_id="output-1"):
    # R1 has only this output node: closure 1 requires a resolvable self-ref.
    # The real source-version relationship is covered by later multi-node trees.
    return {
        "schema_version": 1, "format_version": git_format.FORMAT_VERSION,
        "record_type": "evidence", "repo_id": "repo-test",
        "company_id": "company-test", "evidence_id": evidence_id,
        "source_version_ref": {"record_type": "evidence", "stable_id": evidence_id},
        "original_hash": None, "mapped_fingerprint": None, "source_loc": None,
        "attribution_index": None, "coordinate": None, "quote_excerpt_ref": None,
        "quote_digest": None, "legacy_normalized_fingerprint": None,
        "legacy_fingerprint_algorithm": None, "usage": "Test evidence",
        "read_audit_ref": None,
    }
