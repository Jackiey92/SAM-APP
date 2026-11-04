"""E3b slice 3c: E3a real audit reports fed into the E3b admission decision.

End-to-end with zero mocks: every ``ApprovalAuditReport`` is produced by the
real E3a offline audit over a real installed bare/state/handoff case, every
``RecoveryView`` by the real ``RecoveryStateStore``, and every CURRENT-ledger
proof by the real ``verify_current_ledger`` over an independent live host
ledger. ``decide_recovery_admission`` stays a pure function: every step is
proven read-only by before/after directory fingerprints.
"""
import os
from pathlib import Path

import pytest

from app.facts_tree.recovery_approval_decision import (
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
    decide_recovery_admission,
)
from app.facts_tree.recovery_current_ledger import verify_current_ledger
from app.facts_tree.recovery_epoch import EvidenceKey, EvidenceState
from app.facts_tree.recovery_state_store import RecoveryStateStore
from tests import test_recovery_approval_e3a as e3a
from tests.test_facts_tree_backup_assembly import world
from tests.test_facts_tree_recovery_verifier import (
    _a3_fingerprint,
    _manifest,
    package,
)
from tests.test_host_approval_reservations import at
from tests.test_recovery_approval_e3b_current_ledger import (
    _settle,
    build_ledger,
    current_path,
    ts as ledger_ts,
    verify as ledger_verify,
)
from tests.test_recovery_approval_e3b_decision import proof as structural_proof


def _view(scene):
    with RecoveryStateStore(scene['paths']['state']) as store:
        return store.get(e3a.CASE)


def _scene_roots(scene, tmp_path):
    return [
        scene['package'],
        Path(scene['repository'].path),
        scene['paths']['state'],
        scene['handoff'],
        Path(scene['source_repo'].path),
        tmp_path / 'source-host.sqlite',
    ]


@pytest.fixture
def missing_scene(package, tmp_path):
    # Git accepted the approval-use reference while the W backup snapshot
    # predates the intent: the real A2+C+D audit scene.
    scene = e3a.make_audit_scene(package, tmp_path, w_state='missing')
    report = e3a.audit_scene_run(scene)
    return scene, report


# -- i1: a real A2 (Git accepted / W missing) report can never open ---------

def test_git_accepted_w_missing_real_report_forces_permanent_consumed(
        missing_scene, tmp_path):
    scene, report = missing_scene
    key = EvidenceKey(*scene['key'])
    marks = e3a.categories(report, scene['key'])
    assert set(marks) == {'A', 'C', 'D'}
    assert marks['A'].states == ('consumed', 'reserved')

    roots = _scene_roots(scene, tmp_path)
    before = [_a3_fingerprint(root) for root in roots]

    decision = decide_recovery_admission(
        view=_view(scene), report=report, approval_required=(key,))
    op = decision.verdict_for(key.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_PERMANENT_CONSUMED
    assert decision.approval_side_allows(key.operation_id) is False

    # The D handoff marker only anchors the audit trail; it must be visible
    # among the evidence anchors but never cancel the permanent A verdict.
    anchored = "\n".join(op.evidence_anchors)
    d_tokens = [origin.digest for origin in marks['D'].origins if origin.digest]
    assert d_tokens
    assert all(token in anchored for token in d_tokens)

    assert [_a3_fingerprint(root) for root in roots] == before


# -- i2: a real B candidate moves only with an independent CURRENT ledger ---

def test_b_candidate_opens_only_with_independent_current_unused_ledger(
        package, tmp_path):
    # The live host ledger is built first; the backup then receives a copy of
    # it, exactly mirroring "old backup snapshot of a still-live host".
    live_path = current_path(tmp_path / 'current-live')

    def transform(pkg, old, repo, records):
        core = _manifest(pkg)['core']
        history = e3a.host_history(
            live_path, core['identity'], repo.head_oid(),
            state='approved', object_format=repo.object_format)
        e3a.copy_host(pkg, history, declared=True)
        live_key = (history['credential'].task_id,
                    history['decision'].approval_id,
                    history['effect'].operation_id)
        history['db'].close()
        return live_key

    scene = e3a.make_audit_scene(package, tmp_path, accepted=False,
                                 transform=transform)
    report = e3a.audit_scene_run(scene)
    key = EvidenceKey(*scene['key'])
    marks = e3a.categories(report, scene['key'])
    assert 'B' in marks and 'A' not in marks
    assert marks['B'].requires_independent_current
    assert marks['B'].reasons == ('approval_independent_current_required',)

    # A genuinely independent second ledger, whose real key is not the B key.
    foreign_path = current_path(tmp_path / 'foreign')
    fcred, feffect, fdecision = build_ledger(foreign_path, mode='unused')
    foreign_key = EvidenceKey(fcred.task_id, fdecision.approval_id,
                              feffect.operation_id)
    _settle(live_path)

    roots = [*_scene_roots(scene, tmp_path), live_path.parent,
             foreign_path.parent]
    before = [_a3_fingerprint(root) for root in roots]

    foreign_proof = ledger_verify(foreign_path, (foreign_key,))
    assert foreign_key in foreign_proof.unused_keys
    live_proof = verify_current_ledger(
        ledger_path=os.fspath(live_path), expected_identity=scene['identity'],
        keys=(key,), observed_at=ledger_ts(at(100)), now=ledger_ts(at(100)),
        trust_anchor_id='ta-e3b-integration')
    assert key in live_proof.unused_keys

    # A structural proof is only used to depict a restrictive CURRENT state;
    # the decision module reads value-object attributes, never marker types.
    restricted_proof = structural_proof(
        restricted=((key, EvidenceState.CONSUMED.value),))

    view_before = _view(scene)

    def admit(proof=None, *, auth=False, deps=False):
        return decide_recovery_admission(
            view=view_before, report=report, approval_required=(key,),
            current_proof=proof, current_authorization_valid=auth,
            immutable_dependencies_valid=deps)

    op = admit().verdict_for(key.operation_id)
    assert op.verdict == NEEDS_EVIDENCE
    assert op.reason_code == R_INDEPENDENT_PROOF_MISSING

    op = admit(foreign_proof).verdict_for(key.operation_id)
    assert op.verdict == NEEDS_EVIDENCE
    assert op.reason_code == R_INDEPENDENT_PROOF_INCOMPLETE

    op = admit(restricted_proof, auth=True, deps=True).verdict_for(
        key.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_PERMANENT_CONSUMED

    op = admit(live_proof).verdict_for(key.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_CURRENT_AUTHORIZATION_INVALID

    op = admit(live_proof, auth=True).verdict_for(key.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_IMMUTABLE_DEPENDENCIES_INVALID

    opened = admit(live_proof, auth=True, deps=True)
    op = opened.verdict_for(key.operation_id)
    assert op.verdict == OPENABLE
    assert op.reason_code == R_APPROVAL_CLEAR
    assert opened.approval_side_allows(key.operation_id) is True

    # openable is an approval-side predicate only: the pure decision writes
    # nothing and the R5 view/journal anchor are byte-for-byte unchanged.
    assert _view(scene) == view_before
    assert [_a3_fingerprint(root) for root in roots] == before


# -- i3: an approval-required operation the audit cannot attribute fails shut

def test_unattributed_approval_required_operation_fails_closed(
        missing_scene, tmp_path):
    scene, report = missing_scene
    coords = ('task-ghost', 'approval-ghost', 'operation-ghost')
    assert not any(marker.key == coords for marker in report.markers)
    ghost = EvidenceKey(*coords)

    roots = _scene_roots(scene, tmp_path)
    before = [_a3_fingerprint(root) for root in roots]

    decision = decide_recovery_admission(
        view=_view(scene), report=report, approval_required=(ghost,))
    op = decision.verdict_for(ghost.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_APPROVAL_WITHOUT_PROOF
    assert decision.approval_side_allows(ghost.operation_id) is False

    assert [_a3_fingerprint(root) for root in roots] == before
