"""§8-7/8/9 read-only evidence only: never claims live admission prevents revival."""
from contextlib import closing
import sqlite3

import pytest

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


@pytest.mark.skip(reason='app/host_approval_reservations.py:128 + app/facts_tree/recovery_verifier.py:529; §8-7: consumed approval/execution state and live recovery admission deferred to R5+; no executor call is not proof against revival')
def test_consumed_approval_history_needs_later_live_recovery_evidence():
    """No consumed approval fixture can be claimed from reservation-only history."""
