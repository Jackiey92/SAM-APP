"""Strict consumed_candidate record schema."""
from ._primitives import (
    _CANDIDATE, _COMMON, _candidate, _fail, _id, _result,
)
from ._registry import register_schema


FIELDS = _COMMON | frozenset({
    "candidate_uid", "candidate_revision", "payload_digest",
    "operation_id", "disposition_result",
})


@register_schema("consumed_candidate", fields=FIELDS)
def _validate_consumed(payload, family, path, oid_length):
    _candidate({k: payload[k] for k in _CANDIDATE}, family, path, "candidate")
    _id(payload["operation_id"], family, path, "operation_id")
    _result(payload["disposition_result"], family, path, "disposition_result")
    if not payload["disposition_result"]["terminal"]:
        _fail(family, path, "nonterminal_consumed", "disposition_result")
