"""E3b slice 1: the pure admission decision boundary (freeze revD §7).

Only ``decide_recovery_admission`` and its value objects are exercised.
Reports/views are the real ``ApprovalAuditReport``/``RecoveryView`` types;
markers and origins are structural stand-ins because the decision module
never type-checks them (it reads frozen attributes only). No database,
subprocess or host service is involved.
"""
from dataclasses import dataclass, fields, MISSING

import pytest

from app.facts_tree.recovery_approval_audit import ApprovalAuditReport
from app.facts_tree.recovery_state_store import RecoveryView
from app.facts_tree.recovery_epoch import EvidenceKey, EvidenceState, merge_evidence
from app.facts_tree.recovery_approval_decision import (
    AdmissionDecisionError,
    CurrentLedgerProof,
    NEEDS_EVIDENCE,
    NOT_OPENABLE,
    OPENABLE,
    R_APPROVAL_CLEAR,
    R_APPROVAL_WITHOUT_PROOF,
    R_CURRENT_AUTHORIZATION_INVALID,
    R_IMMUTABLE_DEPENDENCIES_INVALID,
    R_INDEPENDENT_PROOF_INCOMPLETE,
    R_INDEPENDENT_PROOF_MISSING,
    R_PERMANENT_CONSUMED,
    R_PERMANENT_POLICY_RESTRICTED,
    R_PERMANENT_RESERVED,
    R_PERMANENT_REVOKED,
    R_PERMANENT_UNKNOWN,
    R_RECONCILIATION_UNRESOLVED,
    decide_recovery_admission,
)

CASE = "case-e3b"
OBSERVED_AT = "2026-11-04T00:00:00.000000+00:00"
JOURNAL_ANCHOR = (7, "a" * 64)


def key(n=1):
    return EvidenceKey(f"task-e3b-{n}", f"approval-e3b-{n}", f"operation-{n}")


@dataclass(frozen=True)
class _Origin:
    kind: str = "Git"
    commit_oid: str = "1" * 40
    record_path: str = "payload/r/approval-use-1.json"
    digest: str = "b" * 64
    table: str | None = None


@dataclass(frozen=True)
class _Marker:
    key: tuple[str, str, str] | None
    category: str
    states: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    origins: tuple[_Origin, ...] = (_Origin(),)


def _marker(key, category, *, states=(), reasons=("approval_use",), origins=(_Origin(),)):
    coordinates = None if key is None else (key.task_id, key.approval_id, key.operation_id)
    return _Marker(coordinates, category, tuple(states), tuple(reasons), tuple(origins))


def _filler(field):
    annotation = str(field.type)
    name = field.name
    if name.endswith("_at"):
        return OBSERVED_AT
    if "digest" in name:
        return "0" * 64
    if "bool" in annotation:
        return False
    if "int" in annotation:
        return 0
    if annotation.startswith("tuple"):
        return ()
    if annotation.startswith("frozenset"):
        return frozenset()
    if annotation.startswith(("list", "set")):
        return annotation.startswith("list") and [] or set()
    if annotation.startswith("dict"):
        return {}
    return None


def _build(cls, **overrides):
    kwargs = {}
    for field in fields(cls):
        if field.name in overrides:
            kwargs[field.name] = overrides[field.name]
        elif field.default is not MISSING:
            kwargs[field.name] = field.default
        elif field.default_factory is not MISSING:
            kwargs[field.name] = field.default_factory()
        else:
            kwargs[field.name] = _filler(field)
    return cls(**kwargs)


def view(case_id=CASE, merges=()):
    return _build(RecoveryView, case_id=case_id, merges=tuple(merges))


def report(case_id=CASE, markers=()):
    return _build(ApprovalAuditReport, case_id=case_id, markers=tuple(markers),
                  journal_anchor=JOURNAL_ANCHOR, core_digest="c" * 64,
                  handoff_digest="h" * 64)


def proof(*, unused=(), restricted=(), anchors=("ledger:current:row-1",)):
    return CurrentLedgerProof(
        trust_anchor_id="ta-e3b",
        observed_at=OBSERVED_AT,
        repo_id="repo-e3b",
        installation_id="inst-e3b",
        company_id="company-e3b",
        watermark=(1, "w" * 64),
        unused_keys=frozenset(unused),
        restricted=tuple(restricted),
        anchors=tuple(anchors),
    )


def decide(key, report_obj, *, view_obj=None, proof_obj=None, auth=False, deps=False):
    return decide_recovery_admission(
        view=view_obj or view(), report=report_obj,
        approval_required=(key,), current_proof=proof_obj,
        current_authorization_valid=auth, immutable_dependencies_valid=deps)


# ---------------------------------------------------------------------------
# Input validation: the single fixed code, never an echoed value.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    dict(view=object()),
    dict(report=object()),
    dict(report=report(case_id="case-other")),
    dict(approval_required=[]),
    dict(approval_required=()),
    dict(approval_required=None),
    dict(current_proof=object()),
    dict(current_authorization_valid=0),
    dict(immutable_dependencies_valid="yes"),
])
def test_invalid_inputs_raise_fixed_code(kwargs):
    k = key()
    base = dict(view=view(), report=report(), approval_required=(k,),
                current_authorization_valid=True, immutable_dependencies_valid=True)
    base.update(kwargs)
    with pytest.raises(AdmissionDecisionError, match="^recovery_admission_input_invalid$"):
        decide_recovery_admission(**base)


def test_duplicate_approval_required_raises():
    k = key()
    with pytest.raises(AdmissionDecisionError, match="^recovery_admission_input_invalid$"):
        decide_recovery_admission(view=view(), report=report(),
                                  approval_required=(k, k))


# ---------------------------------------------------------------------------
# Frozen revD §7.2 precedence.
# ---------------------------------------------------------------------------

def test_recorded_r5_restriction_beats_a_c_b_and_d_markers():
    k = key()
    merge = merge_evidence(k, (EvidenceState.CONSUMED,), ())
    markers = (
        _marker(k, "A", states=("revoked",)),
        _marker(k, "C"),
        _marker(k, "B"),
        _marker(k, "D", reasons=("handoff_trace",), origins=()),
    )
    op = decide(k, report(markers=markers), view_obj=view(merges=(merge,))).operations[0]
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_PERMANENT_CONSUMED
    assert any(a.startswith("r5:") for a in op.evidence_anchors)


@pytest.mark.parametrize("state,reason", [
    ("consumed", R_PERMANENT_CONSUMED),
    ("revoked", R_PERMANENT_REVOKED),
    ("reserved", R_PERMANENT_RESERVED),
    ("unknown", R_PERMANENT_UNKNOWN),
    ("denied", R_PERMANENT_POLICY_RESTRICTED),
])
def test_a_marker_states_map_to_fixed_permanent_reasons(state, reason):
    k = key()
    op = decide(k, report(markers=(_marker(k, "A", states=(state,)),))).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, reason)


def test_c_marker_is_unresolved_reconciliation():
    k = key()
    op = decide(k, report(markers=(_marker(k, "C"),))).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_RECONCILIATION_UNRESOLVED)


def test_b_candidate_without_proof_needs_evidence():
    k = key()
    op = decide(k, report(markers=(_marker(k, "B"),))).operations[0]
    assert (op.verdict, op.reason_code) == (NEEDS_EVIDENCE, R_INDEPENDENT_PROOF_MISSING)


def test_b_proof_not_covering_key_is_incomplete():
    k, other = key(1), key(2)
    p = proof(unused=(other,))
    op = decide(k, report(markers=(_marker(k, "B"),)), proof_obj=p).operations[0]
    assert (op.verdict, op.reason_code) == (NEEDS_EVIDENCE, R_INDEPENDENT_PROOF_INCOMPLETE)


def test_b_proof_showing_restricted_state_is_permanent():
    k = key()
    p = proof(restricted=((k, EvidenceState.RESERVED.value),))
    op = decide(k, report(markers=(_marker(k, "B"),)), proof_obj=p).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_PERMANENT_RESERVED)


def test_b_unused_proof_but_authorization_invalid():
    k = key()
    op = decide(k, report(markers=(_marker(k, "B"),)),
                proof_obj=proof(unused=(k,)), auth=False).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_CURRENT_AUTHORIZATION_INVALID)


def test_b_unused_proof_auth_valid_but_dependencies_invalid():
    k = key()
    op = decide(k, report(markers=(_marker(k, "B"),)),
                proof_obj=proof(unused=(k,)), auth=True, deps=False).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_IMMUTABLE_DEPENDENCIES_INVALID)


def test_b_unused_proof_with_preconditions_valid_is_narrowly_openable():
    k = key()
    result = decide(k, report(markers=(_marker(k, "B"),)),
                    proof_obj=proof(unused=(k,)), auth=True, deps=True)
    op = result.operations[0]
    assert (op.verdict, op.reason_code) == (OPENABLE, R_APPROVAL_CLEAR)
    assert result.approval_side_allows(k.operation_id) is True
    assert k in result.openable_keys
    assert any(a.startswith("current-ledger:") for a in op.evidence_anchors)


def test_unmarked_approval_required_operation_does_not_open():
    k = key()
    result = decide(k, report(markers=()))
    op = result.operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_APPROVAL_WITHOUT_PROOF)
    assert result.approval_side_allows(k.operation_id) is False


def test_d_marker_only_grants_nothing():
    k = key()
    op = decide(k, report(markers=(_marker(
        k, "D", reasons=("approval_consumption_without_git",), origins=()),))).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_APPROVAL_WITHOUT_PROOF)


def test_d_marker_kept_as_audit_trace_but_a_still_blocks():
    k = key()
    markers = (
        _marker(k, "A", states=("consumed",)),
        _marker(k, "D", reasons=("approval_consumption_without_git",), origins=()),
    )
    op = decide(k, report(markers=markers)).operations[0]
    assert (op.verdict, op.reason_code) == (NOT_OPENABLE, R_PERMANENT_CONSUMED)
    assert "audit:D:approval_consumption_without_git" in op.evidence_anchors


# ---------------------------------------------------------------------------
# Narrow semantics, digest stability and non-secret evidence.
# ---------------------------------------------------------------------------

def test_approval_side_allows_unknown_operation_is_false():
    k = key()
    result = decide(k, report(markers=()), )
    assert result.approval_side_allows("operation-does-not-exist") is False
    assert result.verdict_for("operation-does-not-exist") is None


def test_decision_digest_is_stable_and_changes_with_verdict():
    k = key()
    markers = (_marker(k, "B"),)
    first = decide(k, report(markers=markers), proof_obj=proof(unused=(k,)),
                   auth=False, deps=False)
    second = decide(k, report(markers=markers), proof_obj=proof(unused=(k,)),
                    auth=False, deps=False)
    assert first.decision_digest == second.decision_digest
    opened = decide(k, report(markers=markers), proof_obj=proof(unused=(k,)),
                    auth=True, deps=True)
    assert opened.decision_digest != first.decision_digest
    assert first.decision_digest.startswith("sha256:")


def test_anchors_are_coordinates_and_reasons_never_echo_input_values():
    k = key()
    secret = "super-secret-marker-state-value"
    op = decide(k, report(markers=(_marker(k, "A", states=(secret,)),))).operations[0]
    assert op.reason_code == R_PERMANENT_UNKNOWN
    assert secret not in op.reason_code
    assert op.evidence_anchors
    for anchor in op.evidence_anchors:
        assert type(anchor) is str and anchor
        assert secret not in anchor
        assert anchor.split(":", 1)[0] in {"git", "audit", "r5", "evidence",
                                           "b", "current-ledger"}
