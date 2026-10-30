"""Strict input_candidate record schema."""
from ._primitives import (
    _CANDIDATE, _COMMON, _ENVELOPE_SUMMARY, _WHITELIST,
    _candidate, _digest, _exact, _fail, _id,
)
from ._registry import register_schema


FIELDS = _COMMON | frozenset({
    "candidate_uid", "candidate_revision", "payload_digest",
    "whitelisted_fields", "envelope_summary", "candidate_rule_version",
    "adapter_rule_version", "validation_rule_version",
    "coverage_manifest_version", "source_id", "version_id",
    "mapped_fingerprint", "payload_ref", "envelope_ref",
})


@register_schema("input_candidate", fields=FIELDS)
def _validate_input(payload, family, path, oid_length):
    _candidate({k: payload[k] for k in _CANDIDATE}, family, path, "candidate")
    for field in ("candidate_rule_version", "adapter_rule_version",
                  "validation_rule_version", "coverage_manifest_version",
                  "source_id", "version_id"):
        _id(payload[field], family, path, field)
    for field in ("mapped_fingerprint", "payload_ref", "envelope_ref"):
        _digest(payload[field], family, path, field)
    fields = payload["whitelisted_fields"]
    _exact(fields, _WHITELIST, family, path, "whitelisted_fields")
    for field in fields:
        _id(fields[field], family, path, "whitelisted_fields." + field)
    summary = payload["envelope_summary"]
    _exact(summary, _ENVELOPE_SUMMARY, family, path, "envelope_summary")
    for field in summary:
        if summary[field] != payload[field]:
            _fail(family, path, "envelope_binding", "envelope_summary." + field)
    if payload["payload_ref"] != payload["payload_digest"]:
        _fail(family, path, "payload_address_digest", "payload_ref")
