"""Recovery-local safety rules; no credential resolver or execution authority.

The trusted host supplies epochs, independently verified authentication references
and evidence. Shape checks and SHA-256 references do not authenticate a source.
An epoch constrains this recovery instance ONLY. It cannot revoke credentials
already held by another host: host isolation and external rotation are required.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def require_label(value: str) -> None:
    if type(value) is not str or not _LABEL.fullmatch(value):
        raise ValueError("invalid recovery reference")


def require_digest(value: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError("invalid recovery digest")


def require_sequence(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < 2**63:
        raise ValueError("invalid recovery sequence")


@dataclass(frozen=True)
class RecoveryEpoch:
    instance_id: str
    epoch_id: str
    ordinal: int

    def __post_init__(self):
        require_label(self.instance_id)
        require_label(self.epoch_id)
        require_sequence(self.ordinal, minimum=1)


def validate_epoch(candidate: RecoveryEpoch, history: tuple[RecoveryEpoch, ...]) -> None:
    """Host history must include all previously allocated epochs, even retired ones."""
    if type(candidate) is not RecoveryEpoch or type(history) is not tuple:
        raise ValueError("invalid epoch history")
    seen = set()
    last = 0
    for epoch in (*history, candidate):
        if type(epoch) is not RecoveryEpoch:
            raise ValueError("invalid epoch history")
        if epoch.epoch_id in seen or epoch.ordinal <= last:
            raise ValueError("epoch rollback or reuse")
        seen.add(epoch.epoch_id)
        last = epoch.ordinal


@dataclass(frozen=True)
class OwnerRecoveryAuthentication:
    """References to independent host verification, never an old session secret.

    All three reconfirmations are mandatory. This is an attestation input from a
    trusted host, not proof that this module performed authentication itself.
    """
    instance_id: str
    epoch_id: str
    owner_id: str
    record_digest: str
    member_digest: str
    policy_digest: str
    task_digest: str

    def __post_init__(self):
        for value in (self.instance_id, self.epoch_id, self.owner_id):
            require_label(value)
        for value in (self.record_digest, self.member_digest, self.policy_digest,
                      self.task_digest):
            require_digest(value)

    def matches(self, epoch: RecoveryEpoch) -> bool:
        return (self.instance_id, self.epoch_id) == (epoch.instance_id, epoch.epoch_id)


class EvidenceState(StrEnum):
    UNUSED = "unused"
    APPROVED = "approved"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    POLICY_RESTRICTED = "policy_restricted"
    UNKNOWN = "unknown"


_RESTRICTIVE = frozenset({EvidenceState.RESERVED, EvidenceState.CONSUMED,
                          EvidenceState.REVOKED, EvidenceState.POLICY_RESTRICTED,
                          EvidenceState.UNKNOWN})


@dataclass(frozen=True, order=True)
class EvidenceKey:
    task_id: str
    approval_id: str
    operation_id: str

    def __post_init__(self):
        for value in (self.task_id, self.approval_id, self.operation_id):
            require_label(value)


@dataclass(frozen=True)
class IndependentEvidence:
    source_id: str
    scope: str
    ledger_id: str
    revocation_seq: int
    event_id: str
    event_digest: str
    key: EvidenceKey
    state: EvidenceState
    session_id: str | None = None
    policy_revision: int | None = None

    def __post_init__(self):
        for value in (self.source_id, self.scope, self.ledger_id, self.event_id):
            require_label(value)
        require_sequence(self.revocation_seq)
        require_digest(self.event_digest)
        if type(self.key) is not EvidenceKey or type(self.state) is not EvidenceState:
            raise ValueError("invalid independent evidence")
        if self.session_id is not None:
            require_label(self.session_id)
        if self.policy_revision is not None:
            require_sequence(self.policy_revision, minimum=1)
        if self.state == EvidenceState.POLICY_RESTRICTED and self.policy_revision is None:
            raise ValueError("policy restriction requires revision")

    @property
    def ledger(self) -> tuple[str, str, str]:
        return self.source_id, self.scope, self.ledger_id


@dataclass(frozen=True)
class MergeDecision:
    key: EvidenceKey
    restrictions: frozenset[EvidenceState]
    pending: bool
    conflicts: tuple[str, ...]
    evidence: tuple[IndependentEvidence, ...]
    backup_states: tuple[EvidenceState, ...]

    @property
    def usable(self) -> bool:
        # Evidence import is never a fresh authorization or execution grant.
        return False


def merge_evidence(key: EvidenceKey, backup: tuple[EvidenceState, ...],
                   evidence: tuple[IndependentEvidence, ...],
                   previous: MergeDecision | None = None) -> MergeDecision:
    """Accumulate restrictions; no latest-wins rule can resurrect a tombstone.

    Sequence positions belong to (source, scope, ledger), not a global sequence.
    Conflicting identities/positions are retained, flagged and made unknown.
    Missing independent evidence makes even 'approved'/'unused' unknown.
    """
    if type(key) is not EvidenceKey or type(backup) is not tuple or type(evidence) is not tuple:
        raise ValueError("invalid merge input")
    if any(type(state) is not EvidenceState for state in backup):
        raise ValueError("invalid backup state")
    if previous is not None and (type(previous) is not MergeDecision or previous.key != key):
        raise ValueError("merge key mismatch")
    old = previous.evidence if previous else ()
    if any(type(item) is not IndependentEvidence or item.key != key for item in (*old, *evidence)):
        raise ValueError("evidence key mismatch")
    combined = tuple(dict.fromkeys((*old, *evidence)))
    states = tuple(dict.fromkeys((*(previous.backup_states if previous else ()), *backup)))
    restrictions = set(previous.restrictions if previous else ())
    restrictions.update(state for state in states if state in _RESTRICTIVE)
    restrictions.update(item.state for item in combined if item.state in _RESTRICTIVE)
    conflicts = set(previous.conflicts if previous else ())
    for index, item in enumerate(combined):
        for other in combined[:index]:
            if item.ledger == other.ledger:
                if ((item.event_id == other.event_id or item.revocation_seq == other.revocation_seq)
                        and item != other):
                    conflicts.add("ledger_identity_conflict")
                    restrictions.add(EvidenceState.UNKNOWN)
    all_states = set(states) | {item.state for item in combined}
    if len(all_states) > 1:
        conflicts.add("state_conflict_stricter_preserved")
    if not combined or not restrictions:
        restrictions.add(EvidenceState.UNKNOWN)
    return MergeDecision(key, frozenset(restrictions), EvidenceState.UNKNOWN in restrictions,
                         tuple(sorted(conflicts)), combined, states)


@dataclass(frozen=True)
class CredentialAdmission:
    status: str
    effects_allowed: bool = False


def evaluate_restored_credential(epoch: RecoveryEpoch, credential_epoch_id: str,
                                authentication: OwnerRecoveryAuthentication | None = None,
                                tombstones: frozenset[EvidenceState] = frozenset()) -> CredentialAdmission:
    """Matching backup and expiry are insufficient, including for old approvals."""
    if type(epoch) is not RecoveryEpoch:
        raise ValueError("invalid recovery epoch")
    require_label(credential_epoch_id)
    if type(tombstones) is not frozenset or any(type(s) is not EvidenceState for s in tombstones):
        raise ValueError("invalid tombstone input")
    if tombstones & _RESTRICTIVE:
        return CredentialAdmission("denied")
    if (type(authentication) is not OwnerRecoveryAuthentication
            or not authentication.matches(epoch)):
        return CredentialAdmission("denied")
    return CredentialAdmission("pending_evaluation")


class RecoveryPhase(StrEnum):
    QUARANTINED = "quarantined"
    PACKAGE_VERIFIED = "package_verified"
    MATERIALS_STAGED = "materials_staged"
    EVIDENCE_MERGED = "evidence_merged"
    REAUTHORIZED = "reauthorized"
    READONLY_OPEN = "readonly_open"
    TASKS_OPEN = "tasks_open"
    EFFECTS_OPEN = "effects_open"


PHASES = tuple(RecoveryPhase)
# Safety milestones, not business classifications. Values are verified-host
# decision digests, not instructions to perform the named action.
PHASE_PROOFS = (
    (), ("package_verified",), ("materials_staged",), ("evidence_merged",),
    ("current_authorization",), ("read_review",), ("task_review",), ("effects_review",),
)


def validate_transition(current: RecoveryPhase, target: RecoveryPhase,
                        proofs: tuple[tuple[str, str], ...],
                        authenticated: bool) -> None:
    if type(current) is not RecoveryPhase or type(target) is not RecoveryPhase:
        raise ValueError("invalid recovery phase")
    if PHASES.index(target) != PHASES.index(current) + 1:
        raise ValueError("illegal recovery transition")
    if type(proofs) is not tuple or type(authenticated) is not bool:
        raise ValueError("invalid recovery prerequisites")
    checked = {}
    for pair in proofs:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("invalid recovery prerequisites")
        name, digest = pair
        if name not in {name for names in PHASE_PROOFS for name in names} or name in checked:
            raise ValueError("invalid recovery prerequisites")
        require_digest(digest)
        checked[name] = digest
    required = {name for names in PHASE_PROOFS[:PHASES.index(target) + 1] for name in names}
    if not required <= checked.keys():
        raise ValueError("missing recovery prerequisites")
    if PHASES.index(target) >= PHASES.index(RecoveryPhase.REAUTHORIZED) and not authenticated:
        raise ValueError("independent owner recovery authentication required")
