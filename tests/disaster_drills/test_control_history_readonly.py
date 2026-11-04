"""§8-7/8/9 read-only evidence only: never claims live admission prevents revival."""
from contextlib import closing
import sqlite3

from app.facts_tree.recovery_verifier import verify_recovery_package
from tests import test_facts_tree_recovery_verifier as existing
from tests.test_facts_tree_backup_assembly import _fingerprint


def test_unknown_old_approval_history_is_blocked_without_source_writes(host_package, world):
    package, ops, active, revoked = host_package
    roots = [package, world['tmp'] / 'host-source.sqlite', world['repo'].path,
             world['retention'], world['two_a'], world['ledger_root']]
    before = [_fingerprint(root) for root in roots]
    existing.test_host_real_history_unknown_is_blocked_and_never_replayed(host_package)
    assert [_fingerprint(root) for root in roots] == before
    # No executor runs here; this conclusion only concerns the read-only checker.


def test_revoked_reserved_approval_tombstone_survives_readonly_verification(host_package, world):
    from app.host_identity import HostIdentityService
    from app.host_authorization import HostAuthorizationService
    from app.host_task_credentials import HostTaskCredentialService
    from tests.test_host_prepared_effects import at

    package, ops, active, _ = host_package
    core = existing._manifest(package)['core']
    source_path = world['tmp'] / 'host-source.sqlite'
    path = 'payload/w/w_items.sqlite'
    with closing(sqlite3.connect(source_path)) as source:
        identity = HostIdentityService(source)
        auth = HostAuthorizationService(source, identity)
        credentials = HostTaskCredentialService(source, identity, auth)
        ticket = identity.issue_pairing_credential(now=at(80), lifetime_seconds=600)
        grant = identity.pair(ticket.secret, now=at(90), session_lifetime_seconds=3600)
        reservation_before = source.execute('SELECT * FROM host_approval_reservations').fetchall()
        assert len(reservation_before) == 1
        assert credentials.revoke_task_credential(grant.secret, now=at(100), task_id=active)
        assert source.execute('SELECT * FROM host_approval_reservations').fetchall() == reservation_before
        (package / path).unlink()
        with closing(sqlite3.connect(package / path)) as target:
            source.backup(target)
    existing._refresh(package, core, path)
    before = (_fingerprint(package), _fingerprint(source_path))
    verdict = verify_recovery_package(package)
    assert verdict.blockers == ('host_unknown_operation:' + ops['unknown'],)
    assert 'credential_revoked:' + active in verdict.advisories
    assert 'credential_rebind_required:' + active not in verdict.advisories
    assert 'host_decided_operation:' + ops['pending'] + ':approved' in verdict.advisories
    with closing(sqlite3.connect((package / path).as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('SELECT * FROM host_approval_reservations').fetchall() == reservation_before
    assert (_fingerprint(package), _fingerprint(source_path)) == before


def test_known_inflight_history_is_only_a_readonly_verdict_not_revival_protection(host_package):
    existing.test_host_in_flight_real_operation_and_role_discovery(
        host_package, move_role=False, foreign_scope=False)
    package = host_package[0]
    before = _fingerprint(package)
    assert verify_recovery_package(package).recoverable
    assert _fingerprint(package) == before


def test_consumed_approval_history_needs_later_live_recovery_evidence(package, tmp_path):
    """A consumed approval is a permanent restriction; only an independent
    CURRENT ledger could ever move a candidate, and a consumed key can never
    appear as unused there -- reservation/history alone never revives it."""
    from pathlib import Path

    from app.facts_tree.recovery_approval_decision import (
        NOT_OPENABLE,
        R_PERMANENT_CONSUMED,
        decide_recovery_admission,
    )
    from app.facts_tree.recovery_epoch import EvidenceKey
    from app.facts_tree.recovery_state_store import RecoveryStateStore
    from tests import test_recovery_approval_e3a as e3a
    from tests.test_recovery_approval_e3b_decision import proof as current_proof

    # Real host stack: paired owner, policy, credential, prepared effect,
    # approval, reservation and a recorded admission (consumed chain).
    scene = e3a.make_audit_scene(package, tmp_path)
    audit = e3a.audit_scene_run(scene)
    key = EvidenceKey(*scene['key'])
    marks = e3a.categories(audit, scene['key'])
    assert 'A' in marks and marks['A'].states == ('consumed', 'reserved')

    roots = [package, tmp_path / 'source-host.sqlite',
             Path(scene['repository'].path), scene['paths']['state'],
             scene['handoff']]
    before = [_fingerprint(root) for root in roots]

    # 1) The read-only verifier records the consumed operation in its frozen
    #    approval-audit vocabulary (never as something a rebind could reopen);
    #    the operation-keyed advisory is the terminal decision only.
    verdict = verify_recovery_package(package)
    assert verdict.recoverable
    audit_info = e3a.report(verdict)
    assert audit_info['declared']
    assert audit_info['consumption_operations'] == (key.operation_id,)
    assert audit_info['failures'] == ()
    flat = repr(tuple(verdict.approval_audit)) + repr(verdict.blockers)
    assert 'rebind' not in flat and 'revival' not in flat and 'replay' not in flat
    assert [item for item in verdict.advisories if key.operation_id in item] == [
        'host_decided_operation:' + key.operation_id + ':approved']

    # 2) Read-only evidence leaves package and host source byte-identical.
    assert [_fingerprint(root) for root in roots] == before

    # 3) Even with approval and immutable dependencies valid, a CURRENT proof
    #    that cannot show this key as unused (consumed keys never can) leaves
    #    the operation permanently shut at the admission decision boundary.
    with RecoveryStateStore(scene['paths']['state']) as store:
        view = store.get(e3a.CASE)
    decision = decide_recovery_admission(
        view=view, report=audit, approval_required=(key,),
        current_proof=current_proof(),
        current_authorization_valid=True, immutable_dependencies_valid=True)
    op = decision.verdict_for(key.operation_id)
    assert op.verdict == NOT_OPENABLE
    assert op.reason_code == R_PERMANENT_CONSUMED
    assert [_fingerprint(root) for root in roots] == before
