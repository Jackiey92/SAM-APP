"""E3b: pure module-level recovery admission decision boundary.

Inputs are a read-only R5 ``RecoveryView`` snapshot, the E3a
``ApprovalAuditReport`` and an OPTIONAL independently verified current-ledger
proof (``CurrentLedgerProof`` is produced out-of-band by
``recovery_current_ledger.verify_current_ledger``; nothing in this module or
any production path constructs one, so the B route defaults to closed).

Per approval-requiring operation this module returns one stable verdict triple
(verdict, fixed reason code, non-secret evidence coordinates):

* ``openable`` is a NARROW approval-side statement: this operation's one-time
  approval restriction does not block under current evidence. It replaces no
  R5 phase gate (read/tasks/effects), no old-instance isolation check and no
  credential rebind check; it advances no phase, opens no gate, performs no
  write. Future I-slice wiring may only AND ``approval_side_allows`` into the
  existing open conditions.
* ``needs_evidence``: a B candidate whose independent, current, complete
  unused proof is absent or incomplete. Never a grant.
* ``not_openable``: permanent tombstones (R5 merge, A/A2 markers, current
  ledger hard facts), unresolved C markers, failed authorization/dependency
  preconditions, or no valid proof at all. Absence of a marker never opens an
  approval-requiring operation; D markers only audit-trace.

Priority is frozen in e_freeze revD §7.2: recorded A (R5 merges include
cross-epoch inheritance) then A markers, then C, then the B/current-ledger
branch, then fail-closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from app.facts_tree.recovery_approval_audit import ApprovalAuditReport
from app.facts_tree.recovery_epoch import EvidenceKey, EvidenceState
from app.facts_tree.recovery_state_store import RecoveryView

OPENABLE = "openable"
NOT_OPENABLE = "not_openable"
NEEDS_EVIDENCE = "needs_evidence"

# Fixed reason codes. None of these may carry input values or secrets.
R_PERMANENT_CONSUMED = "permanent_consumed"
R_PERMANENT_REVOKED = "permanent_revoked"
R_PERMANENT_POLICY_RESTRICTED = "permanent_policy_restricted"
R_PERMANENT_RESERVED = "permanent_reserved"
R_PERMANENT_UNKNOWN = "permanent_unknown"
R_RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
R_INDEPENDENT_PROOF_MISSING = "independent_current_proof_missing"
R_INDEPENDENT_PROOF_INCOMPLETE = "independent_current_proof_incomplete"
R_CURRENT_AUTHORIZATION_INVALID = "current_authorization_invalid"
R_IMMUTABLE_DEPENDENCIES_INVALID = "immutable_dependencies_invalid"
R_APPROVAL_WITHOUT_PROOF = "approval_without_valid_proof"
R_APPROVAL_CLEAR = "approval_side_clear"

_STATE_PRECEDENCE = (
    EvidenceState.CONSUMED,
    EvidenceState.REVOKED,
    EvidenceState.POLICY_RESTRICTED,
    EvidenceState.RESERVED,
    EvidenceState.UNKNOWN,
)
_STATE_REASON = {
    EvidenceState.CONSUMED: R_PERMANENT_CONSUMED,
    EvidenceState.REVOKED: R_PERMANENT_REVOKED,
    EvidenceState.POLICY_RESTRICTED: R_PERMANENT_POLICY_RESTRICTED,
    EvidenceState.RESERVED: R_PERMANENT_RESERVED,
    EvidenceState.UNKNOWN: R_PERMANENT_UNKNOWN,
}
_RESTRICTIVE_CODE = frozenset(_STATE_REASON)


class AdmissionDecisionError(ValueError):
    """Fixed code only; never echoes a marker value or dependency text."""


@dataclass(frozen=True)
class CurrentLedgerProof:
    """Verified observation of a current ledger independent of backup media.

    Only ``recovery_current_ledger.verify_current_ledger`` constructs these;
    the decision boundary itself never opens a database. ``unused_keys`` are
    keys whose complete current chain is approved/unreserved/unconsumed and
    within validity at ``observed_at``; ``restricted`` carries hard current
    restrictive states (consumed/reserved/policy_restricted). A key in neither
    tuple is simply not covered by this proof. Empty ledger => empty tuples.
    """

    trust_anchor_id: str
    observed_at: str
    repo_id: str
    installation_id: str
    company_id: str
    watermark: tuple[int, str]
    unused_keys: frozenset[EvidenceKey]
    restricted: tuple[tuple[EvidenceKey, str], ...]
    anchors: tuple[str, ...]

    def __post_init__(self):
        if type(self.trust_anchor_id) is not str or not self.trust_anchor_id:
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        if type(self.observed_at) is not str or len(self.observed_at) != 32 \
                or self.observed_at[26:] != "+00:00":
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        for value in (self.repo_id, self.installation_id, self.company_id):
            if type(value) is not str or not value:
                raise AdmissionDecisionError("recovery_admission_input_invalid")
        if (type(self.watermark) is not tuple or len(self.watermark) != 2
                or type(self.watermark[0]) is not int or self.watermark[0] < 1
                or type(self.watermark[1]) is not str
                or len(self.watermark[1]) != 64):
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        if type(self.unused_keys) is not frozenset or not all(
                type(k) is EvidenceKey for k in self.unused_keys):
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        if type(self.restricted) is not tuple or any(
                type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not EvidenceKey
                or EvidenceState(pair[1]) not in _RESTRICTIVE_CODE
                for pair in self.restricted):
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        if type(self.anchors) is not tuple or any(
                type(a) is not str or not a for a in self.anchors):
            raise AdmissionDecisionError("recovery_admission_input_invalid")

    def state_for(self, key: EvidenceKey):
        for restricted_key, state in self.restricted:
            if restricted_key == key:
                return EvidenceState(state)
        if key in self.unused_keys:
            return EvidenceState.UNUSED
        return None


@dataclass(frozen=True)
class OperationAdmission:
    key: EvidenceKey
    verdict: str
    reason_code: str
    evidence_anchors: tuple[str, ...]


@dataclass(frozen=True)
class AdmissionDecisionReport:
    case_id: str
    journal_anchor: tuple[int, str]
    core_digest: str
    handoff_digest: str
    operations: tuple[OperationAdmission, ...]
    decision_digest: str

    def verdict_for(self, operation_id: str) -> OperationAdmission | None:
        hits = [op for op in self.operations if op.key.operation_id == operation_id]
        if len(hits) > 1:
            raise AdmissionDecisionError("recovery_admission_input_invalid")
        return hits[0] if hits else None

    def approval_side_allows(self, operation_id: str) -> bool:
        """The ONLY composable predicate: AND it into existing open checks.

        An unknown operation is not allowed; a narrow ``openable`` says
        nothing about phases, isolation or credential rebinding.
        """
        verdict = self.verdict_for(operation_id)
        return verdict is not None and verdict.verdict == OPENABLE

    @property
    def openable_keys(self) -> frozenset[EvidenceKey]:
        return frozenset(op.key for op in self.operations
                         if op.verdict == OPENABLE)

    @property
    def blocked_keys(self) -> frozenset[EvidenceKey]:
        return frozenset(op.key for op in self.operations
                         if op.verdict == NOT_OPENABLE)


def _restriction_reason(states) -> str:
    for state in _STATE_PRECEDENCE:
        if state in states:
            return _STATE_REASON[state]
    return R_PERMANENT_UNKNOWN


def _marker_restriction_states(states):
    # Audit markers carry the W decision spelling "denied"; the freeze
    # vocabulary for a hard stop is policy_restricted. Anything else the
    # audit never should have emitted still fails closed as unknown.
    mapped = set()
    for raw in states:
        name = "policy_restricted" if raw == "denied" else raw
        try:
            state = EvidenceState(name)
        except ValueError:
            state = EvidenceState.UNKNOWN
        mapped.add(state if state in _RESTRICTIVE_CODE
                   else EvidenceState.UNKNOWN)
    return frozenset(mapped) or frozenset({EvidenceState.UNKNOWN})


def _origin_anchor(origin):
    if origin.kind == "Git" and origin.commit_oid and origin.record_path:
        return "git:" + origin.commit_oid + ":" + origin.record_path + ":" + origin.digest
    coordinate = origin.table or origin.record_path
    if coordinate:
        return origin.kind.lower() + ":" + coordinate + ":" + origin.digest
    return origin.kind.lower() + ":" + origin.digest


def _marker_anchors(markers):
    anchors = []
    for marker in markers:
        if marker.origins:
            for origin in marker.origins:
                anchors.append(_origin_anchor(origin))
        else:
            anchors.append("audit:" + marker.category + ":"
                           + ",".join(marker.reasons))
    return tuple(dict.fromkeys(anchors))


def _merge_anchors(merge, journal_digest):
    anchors = ["r5:" + journal_digest] if journal_digest else ["r5:0"]
    for item in merge.evidence:
        anchors.append("evidence:" + item.source_id + ":" + item.ledger_id
                       + ":" + item.event_digest)
    return tuple(dict.fromkeys(anchors))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def decide_recovery_admission(*, view: RecoveryView, report: ApprovalAuditReport,
        approval_required: tuple[EvidenceKey, ...],
        current_proof: CurrentLedgerProof | None = None,
        current_authorization_valid: bool = False,
        immutable_dependencies_valid: bool = False) -> AdmissionDecisionReport:
    """Adjudicate every approval-requiring operation; pure and read-only.

    See module docstring for the frozen priority. Failing closed is the
    default: an operation the caller cannot account for must not be in
    ``approval_required``, and every operation in it receives a verdict.
    """
    if (type(view) is not RecoveryView or type(report) is not ApprovalAuditReport
            or view.case_id is None or report.case_id != view.case_id):
        raise AdmissionDecisionError("recovery_admission_input_invalid")
    if (type(approval_required) is not tuple or not approval_required
            or any(type(k) is not EvidenceKey for k in approval_required)
            or len(set(approval_required)) != len(approval_required)):
        raise AdmissionDecisionError("recovery_admission_input_invalid")
    if current_proof is not None and type(current_proof) is not CurrentLedgerProof:
        raise AdmissionDecisionError("recovery_admission_input_invalid")
    if (type(current_authorization_valid) is not bool
            or type(immutable_dependencies_valid) is not bool):
        raise AdmissionDecisionError("recovery_admission_input_invalid")

    markers = {}
    for marker in report.markers:
        if marker.key is None:
            continue
        key = EvidenceKey(*marker.key)
        markers.setdefault(key, []).append(marker)
    merges = {merge.key: merge for merge in view.merges}
    journal_digest = report.journal_anchor[1] if report.journal_anchor[0] else ""

    operations = []
    for key in approval_required:
        key_markers = markers.get(key, ())
        categories = {m.category: m for m in key_markers}
        related = tuple(m for m in key_markers if m.category in ("A", "C", "D"))
        anchors = list(_marker_anchors(related))
        merge = merges.get(key)

        # 1) Restrictions already recorded in the current R5 state. The
        #    replay propagates these across every epoch, so a new epoch,
        #    re-authentication or re-approval can never reach this branch
        #    clean again.
        if merge is not None and merge.restrictions:
            verdict = NOT_OPENABLE
            reason = _restriction_reason(merge.restrictions)
            anchors = [*_merge_anchors(merge, journal_digest), *anchors]
        elif "A" in categories:
            # 1b) Permanent markers from W three-table facts, A2 Git-accepted/
            #     W-missing repairs and D handoff evidence.
            states = _marker_restriction_states(categories["A"].states)
            verdict = NOT_OPENABLE
            reason = _restriction_reason(states)
        elif "C" in categories:
            # 2) An unresolved reconciliation block must stay shut. It may be
            #    clarified later; until then opening is impossible.
            verdict = NOT_OPENABLE
            reason = R_RECONCILIATION_UNRESOLVED
        elif "B" in categories or (current_proof is not None
                and current_proof.state_for(key) is not None):
            # 3) B candidate: only an independent CURRENT ledger can move it.
            anchors.append("b:backup:" + (categories["B"].origins[0].digest
                if "B" in categories and categories["B"].origins else "candidate"))
            if current_proof is None:
                verdict = NEEDS_EVIDENCE
                reason = R_INDEPENDENT_PROOF_MISSING
            else:
                live = current_proof.state_for(key)
                proof_anchor = "current-ledger:" + current_proof.trust_anchor_id \
                    + ":" + str(current_proof.watermark[0]) + ":" + current_proof.watermark[1]
                if live is None:
                    verdict = NEEDS_EVIDENCE
                    reason = R_INDEPENDENT_PROOF_INCOMPLETE
                    anchors = [*anchors, proof_anchor, *current_proof.anchors]
                elif live in _RESTRICTIVE_CODE:
                    verdict = NOT_OPENABLE
                    reason = _STATE_REASON[live]
                    anchors = [*anchors, proof_anchor, *current_proof.anchors]
                elif live is EvidenceState.UNUSED:
                    if not current_authorization_valid:
                        verdict = NOT_OPENABLE
                        reason = R_CURRENT_AUTHORIZATION_INVALID
                        anchors = [*anchors, proof_anchor, *current_proof.anchors]
                    elif not immutable_dependencies_valid:
                        verdict = NOT_OPENABLE
                        reason = R_IMMUTABLE_DEPENDENCIES_INVALID
                        anchors = [*anchors, proof_anchor, *current_proof.anchors]
                    else:
                        verdict = OPENABLE
                        reason = R_APPROVAL_CLEAR
                        anchors = [*anchors, proof_anchor, *current_proof.anchors]
                else:  # pragma: no cover - defensive, state_for is exhaustive
                    verdict = NOT_OPENABLE
                    reason = R_APPROVAL_WITHOUT_PROOF
        else:
            # 4) An approval-requiring operation with no valid proof of any
            #    kind never passes just because no marker exists. D-only
            #    markers audit-trace but grant/block nothing themselves.
            verdict = NOT_OPENABLE
            reason = R_APPROVAL_WITHOUT_PROOF

        operations.append(OperationAdmission(
            key=key, verdict=verdict, reason_code=reason,
            evidence_anchors=tuple(dict.fromkeys(anchors))))

    digest = "sha256:" + hashlib.sha256(_canonical([
        "sam.f001.e3b.admission-decision.v1", report.case_id,
        list(report.journal_anchor), report.core_digest, report.handoff_digest,
        [[[op.key.task_id, op.key.approval_id, op.key.operation_id], op.verdict,
          op.reason_code, list(op.evidence_anchors)] for op in operations],
    ]).encode("ascii")).hexdigest()
    return AdmissionDecisionReport(
        case_id=report.case_id, journal_anchor=report.journal_anchor,
        core_digest=report.core_digest, handoff_digest=report.handoff_digest,
        operations=tuple(operations), decision_digest=digest)
