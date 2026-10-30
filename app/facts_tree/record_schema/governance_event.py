"""Governance event shapes, frozen by appendix A v1.1 §§0/11/12/13.

Only single-record validation: predecessor chains and operation disposition
closure remain with their respective validators. No business action vocabulary.
"""
from ._primitives import (
    _COMMON, _actor, _enum, _id, _object, _optional, _positive, _ref_key,
    _refs, _result, _str, _ts, git_format,
)
from ._registry import register_sample, register_schema


ISSUE_STATUSES = frozenset({"open", "resolved", "dismissed", "reopened"})
PROPOSAL_STATUSES = frozenset({
    "open", "approved", "rejected", "executed", "cancelled",
})

_EVENT_SPEC = {
    "event_id": _id,
    "ordinal": _positive,
    "predecessor_event_ref": _optional(_ref_key),
    "basis": _optional(_str),
    "affected_refs": _refs,
    "actor": _actor,
    "event_time": _ts,
}
_ISSUE_SPEC = {
    **_EVENT_SPEC,
    "issue_uid": _id,
    "issue_type": _str,
    "status": _enum(ISSUE_STATUSES),
    "candidate_refs": _refs,
    "fact_refs": _refs,
    "conflict_evidence_refs": _refs,
}
_PROPOSAL_SPEC = {
    **_EVENT_SPEC,
    "proposal_uid": _id,
    "proposal_type": _str,
    "status": _enum(PROPOSAL_STATUSES),
    "input_impact": _object({
        "input_refs": _refs,
        "affected_refs": _refs,
        "note": _optional(_str),
    }),
    "execution_operation_ref": _optional(_ref_key),
}

ISSUE_FIELDS = _COMMON | frozenset(_ISSUE_SPEC) | {"disposition_result"}
PROPOSAL_FIELDS = _COMMON | frozenset(_PROPOSAL_SPEC) | {"disposition_result"}


@register_schema("issue_event", fields=ISSUE_FIELDS)
def _validate_issue_event(payload, family, path, oid_length):
    for field, validator in _ISSUE_SPEC.items():
        validator(payload[field], family, path, field)
    _result(payload["disposition_result"], family, path, "disposition_result")


@register_schema("proposal_event", fields=PROPOSAL_FIELDS)
def _validate_proposal_event(payload, family, path, oid_length):
    for field, validator in _PROPOSAL_SPEC.items():
        validator(payload[field], family, path, field)
    _result(payload["disposition_result"], family, path, "disposition_result")


def _event_sample(family, identity_field, stable_id, event_id):
    return {
        "schema_version": 1,
        "format_version": git_format.FORMAT_VERSION,
        "record_type": family,
        "repo_id": "repo-test",
        "company_id": "company-test",
        identity_field: stable_id,
        "event_id": event_id,
        "ordinal": 1,
        "predecessor_event_ref": None,
        "status": "open",
        # Both R1 operation cases use this same nonterminal record. The
        # self-reference resolves in closure 1, including overridden IDs.
        "disposition_result": {
            "terminal": False,
            "result_refs": [{"record_type": family, "stable_id": stable_id,
                             "revision": event_id}],
        },
        "basis": None,
        "affected_refs": [],
        "actor": {
            "schema_version": 1,
            "audit_ref_type": "user_actor",
            "installation_id": "install-test",
            "company_id": "company-test",
            "principal_id": "owner-test",
            "member_revision": 1,
            "session_ref": "session-audit-id",
        },
        "event_time": 0,
    }


@register_sample("issue_event")
def build_issue_event(*, issue_uid="output-1", event_id="output-event"):
    payload = _event_sample("issue_event", "issue_uid", issue_uid, event_id)
    payload.update({"issue_type": "synthetic-issue", "candidate_refs": [],
                    "fact_refs": [], "conflict_evidence_refs": []})
    return payload


@register_sample("proposal_event")
def build_proposal_event(*, proposal_uid="output-1", event_id="output-event"):
    payload = _event_sample("proposal_event", "proposal_uid", proposal_uid, event_id)
    payload.update({"proposal_type": "synthetic-proposal",
                    "input_impact": {"input_refs": [], "affected_refs": [], "note": None},
                    "execution_operation_ref": None})
    return payload
