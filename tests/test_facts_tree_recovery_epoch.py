"""Recovery guard behavior: no host modules, credentials, network or real stores."""
from dataclasses import FrozenInstanceError, replace

import pytest

from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState as S, IndependentEvidence, OwnerRecoveryAuthentication,
    PHASES, PHASE_PROOFS, RecoveryEpoch, RecoveryPhase, evaluate_restored_credential,
    merge_evidence, validate_epoch, validate_transition,
)

D = "a" * 64
EPOCH = RecoveryEpoch("recovery-1", "epoch-11", 11)
KEY = EvidenceKey("task-1", "approval-1", "operation-1")


def auth(epoch=EPOCH):
    return OwnerRecoveryAuthentication(epoch.instance_id, epoch.epoch_id, "owner-1", D, D, D, D)


def evidence(state=S.CONSUMED, *, ledger="ledger-1", seq=1, event="event-1", key=KEY, **kw):
    return IndependentEvidence("independent-1", "scope-1", ledger, seq, event, D, key, state, **kw)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "11", 2**63])
def test_epoch_requires_positive_integer(bad):
    with pytest.raises(ValueError):
        RecoveryEpoch("recovery-1", "epoch-11", bad)


@pytest.mark.parametrize("bad", ["", "has space", "../escape", "x" * 129, "é", None])
def test_epoch_reference_shapes(bad):
    with pytest.raises(ValueError):
        RecoveryEpoch("recovery-1", bad, 11)


def test_epoch_advances_from_trusted_history():
    validate_epoch(EPOCH, (RecoveryEpoch("old", "epoch-10", 10),))
    with pytest.raises(FrozenInstanceError):
        EPOCH.ordinal = 2


@pytest.mark.parametrize("candidate", [RecoveryEpoch("new", "epoch-10", 11),
                                       RecoveryEpoch("new", "epoch-11", 10),
                                       RecoveryEpoch("new", "epoch-12", 9)])
def test_epoch_reuse_or_rollback_refused(candidate):
    with pytest.raises(ValueError, match="rollback or reuse"):
        validate_epoch(candidate, (RecoveryEpoch("old", "epoch-10", 10),))


def test_malformed_host_history_is_not_repaired():
    with pytest.raises(ValueError):
        validate_epoch(EPOCH, (RecoveryEpoch("a", "e10", 10), RecoveryEpoch("b", "e9", 9)))


@pytest.mark.parametrize("credential_epoch", ["epoch-10", "epoch-11"])
def test_old_or_matching_backup_never_authenticates_itself(credential_epoch):
    verdict = evaluate_restored_credential(EPOCH, credential_epoch)
    assert verdict.status == "denied" and verdict.effects_allowed is False


def test_independent_auth_only_admits_pending_evaluation():
    result = evaluate_restored_credential(EPOCH, "epoch-10", auth())
    assert result.status == "pending_evaluation" and not result.effects_allowed


@pytest.mark.parametrize("field,value", [("epoch_id", "old"), ("instance_id", "other")])
def test_authentication_is_bound_to_recovery_instance_and_epoch(field, value):
    assert evaluate_restored_credential(EPOCH, "old", replace(auth(), **{field: value})).status == "denied"


@pytest.mark.parametrize("field", ["record_digest", "member_digest", "policy_digest", "task_digest"])
def test_all_independent_reconfirmations_required(field):
    with pytest.raises(ValueError):
        replace(auth(), **{field: ""})


@pytest.mark.parametrize("state", [S.RESERVED, S.CONSUMED, S.REVOKED, S.POLICY_RESTRICTED, S.UNKNOWN])
def test_epoch_and_reauthorization_do_not_clear_tombstones(state):
    assert evaluate_restored_credential(EPOCH, "old", auth(), frozenset({state})).status == "denied"


@pytest.mark.parametrize("backup", [(), (S.UNUSED,), (S.APPROVED,), (S.UNUSED, S.APPROVED)])
def test_missing_independent_evidence_is_unknown(backup):
    merged = merge_evidence(KEY, backup, ())
    assert merged.restrictions == frozenset({S.UNKNOWN})
    assert merged.pending and not merged.usable


@pytest.mark.parametrize("state", [S.REVOKED, S.CONSUMED, S.RESERVED, S.POLICY_RESTRICTED])
@pytest.mark.parametrize("backup", [S.UNUSED, S.APPROVED])
def test_stricter_evidence_dominates_backup_and_conflict_is_recorded(state, backup):
    kw = {"policy_revision": 3} if state == S.POLICY_RESTRICTED else {}
    result = merge_evidence(KEY, (backup,), (evidence(state, **kw),))
    assert state in result.restrictions and not result.usable
    assert "state_conflict_stricter_preserved" in result.conflicts
    assert not result.pending


def test_revoked_and_consumed_are_both_kept_not_ranked_away():
    result = merge_evidence(KEY, (S.APPROVED,),
                            (evidence(S.REVOKED), evidence(S.CONSUMED, seq=2, event="e2")))
    assert result.restrictions == frozenset({S.REVOKED, S.CONSUMED})
    assert len(result.evidence) == 2


@pytest.mark.parametrize("state", [S.UNUSED, S.APPROVED])
def test_independent_positive_status_is_not_fresh_authorization(state):
    result = merge_evidence(KEY, (state,), (evidence(state),))
    assert result.pending and result.restrictions == frozenset({S.UNKNOWN})


def test_unknown_is_sticky_and_new_authorization_cannot_erase_it():
    previous = merge_evidence(KEY, (S.APPROVED,), ())
    merged = merge_evidence(KEY, (S.APPROVED,), (evidence(S.CONSUMED),), previous)
    assert merged.pending and merged.restrictions == frozenset({S.CONSUMED, S.UNKNOWN})


def test_lower_sequence_in_other_ledger_still_preserves_new_consumption():
    result = merge_evidence(KEY, (S.APPROVED,),
                            (evidence(S.APPROVED, seq=999),
                             evidence(S.CONSUMED, ledger="other", seq=1)))
    assert result.restrictions == frozenset({S.CONSUMED})
    assert "ledger_identity_conflict" not in result.conflicts


@pytest.mark.parametrize("field", ["source_id", "scope", "ledger_id"])
def test_ledger_sequence_namespace_includes_source_scope_and_ledger(field):
    first = evidence(S.APPROVED, seq=999)
    second = replace(evidence(), **{field: "other"})
    result = merge_evidence(KEY, (), (first, second))
    assert S.CONSUMED in result.restrictions
    assert "ledger_identity_conflict" not in result.conflicts


@pytest.mark.parametrize("mutation", [{"state": S.REVOKED}, {"event_digest": "b" * 64},
                                      {"event_id": "different"}, {"revocation_seq": 2}])
def test_conflicting_ledger_identity_or_position_is_unknown_and_audited(mutation):
    first = evidence()
    result = merge_evidence(KEY, (), (first, replace(first, **mutation)))
    assert result.pending and "ledger_identity_conflict" in result.conflicts
    assert len(result.evidence) == 2 and S.CONSUMED in result.restrictions


def test_exact_duplicate_evidence_is_deduplicated_without_losing_audit():
    first = merge_evidence(KEY, (), (evidence(), evidence()))
    result = merge_evidence(KEY, (), (evidence(),), first)
    assert result == first and len(result.evidence) == 1
    with pytest.raises(FrozenInstanceError):
        result.pending = False


@pytest.mark.parametrize("mutation", [{"revocation_seq": True}, {"revocation_seq": -1},
                                      {"event_digest": "bad"}, {"scope": ""},
                                      {"session_id": "secret with spaces"},
                                      {"state": "consumed"},
                                      {"state": S.POLICY_RESTRICTED}])
def test_evidence_input_shape_refuses_unprovable_records(mutation):
    with pytest.raises(ValueError):
        replace(evidence(), **mutation)


def test_cross_key_evidence_and_previous_result_refused():
    other = EvidenceKey("task-2", "approval-2", "operation-2")
    with pytest.raises(ValueError):
        merge_evidence(KEY, (), (evidence(key=other),))
    with pytest.raises(ValueError):
        merge_evidence(other, (), (), merge_evidence(KEY, (), (evidence(),)))


@pytest.mark.parametrize("current", PHASES)
@pytest.mark.parametrize("target", PHASES)
def test_all_phase_pairs_only_adjacent_forward_steps_are_legal(current, target):
    proofs = tuple((name, D) for names in PHASE_PROOFS for name in names)
    if PHASES.index(target) == PHASES.index(current) + 1:
        validate_transition(current, target, proofs, True)
    else:
        with pytest.raises(ValueError, match="illegal"):
            validate_transition(current, target, proofs, True)


@pytest.mark.parametrize("target", PHASES[1:])
def test_every_forward_transition_needs_its_proof(target):
    current = PHASES[PHASES.index(target) - 1]
    with pytest.raises(ValueError, match="missing"):
        validate_transition(current, target, (), True)


@pytest.mark.parametrize("target", PHASES[4:])
def test_reauthorization_and_later_stages_require_independent_owner_auth(target):
    proofs = tuple((name, D) for names in PHASE_PROOFS for name in names)
    with pytest.raises(ValueError, match="authentication"):
        validate_transition(PHASES[PHASES.index(target) - 1], target, proofs, False)


def test_later_positive_event_on_same_ledger_does_not_clear_consumption():
    result = merge_evidence(KEY, (S.APPROVED,),
                            (evidence(S.CONSUMED, seq=1),
                             evidence(S.APPROVED, seq=999, event="e999")))
    assert result.restrictions == frozenset({S.CONSUMED}) and not result.usable


def test_policy_restriction_is_preserved_across_newer_revision():
    restricted = evidence(S.POLICY_RESTRICTED, policy_revision=10)
    newer = evidence(S.APPROVED, seq=2, event="e2", policy_revision=11)
    result = merge_evidence(KEY, (S.APPROVED,), (restricted, newer))
    assert result.restrictions == frozenset({S.POLICY_RESTRICTED})
    assert len(result.evidence) == 2 and result.conflicts
