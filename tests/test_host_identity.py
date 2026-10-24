"""Tests for app.host_identity (F-007 P01 step 1 trusted identity core).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data.
"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.host_identity import (
    HostIdentityService,
    InstallationAlreadyBound,
    InstallationRequired,
    MembershipRevoked,
    OwnerContext,
    PairingCredentialConsumed,
    PairingCredentialExpired,
    PairingCredentialInvalid,
    PairingFailed,
    SessionExpired,
    SessionRevoked,
    Unauthenticated,
    UnknownMember,
    UnknownSession,
)

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-identity.db"


@pytest.fixture
def service(db_path):
    conn = _connect(db_path)
    svc = HostIdentityService(conn)
    yield svc
    conn.close()


@pytest.fixture
def bound(service):
    service.initialize_installation(
        installation_id="inst-syn-1",
        company_id="co-syn-1",
        owner_principal_id="user-syn-owner",
        owner_role="owner",
        now=T0,
    )
    return service


@pytest.fixture
def ticket(bound):
    return bound.issue_pairing_credential(now=at(10), lifetime_seconds=600)


def _scalar(db_path, sql, parameters=()):
    conn = _connect(db_path)
    try:
        return conn.execute(sql, parameters).fetchone()[0]
    finally:
        conn.close()


def _rows(db_path, sql, parameters=()):
    conn = _connect(db_path)
    try:
        return conn.execute(sql, parameters).fetchall()
    finally:
        conn.close()


# -- construction and connection contract ---------------------------------


def test_constructor_requires_explicit_sqlite_connection():
    with pytest.raises(TypeError):
        HostIdentityService()  # noqa: F841 - missing required connection
    with pytest.raises(TypeError):
        HostIdentityService("host-identity.db")
    with pytest.raises(TypeError):
        HostIdentityService(object())


def test_constructor_refuses_active_caller_transaction(db_path):
    conn = _connect(db_path)
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostIdentityService(conn)
    conn.rollback()
    conn.close()


def test_write_methods_refuse_caller_transaction(db_path):
    conn = _connect(db_path)
    svc = HostIdentityService(conn)
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        svc.initialize_installation(
            installation_id="inst-syn-1", company_id="co-syn-1",
            owner_principal_id="user-syn-owner", owner_role="owner",
            now=T0)
    conn.rollback()
    conn.close()


# -- one-time controlled installation binding (coverage item 1) ------------


def test_initialize_binds_owner_and_company_exactly_once(bound, db_path):
    record = bound.get_installation()
    assert record == {
        "installation_id": "inst-syn-1",
        "company_id": "co-syn-1",
        "owner_principal_id": "user-syn-owner",
        "owner_role": "owner",
        "bound_at": T0,
    }
    with pytest.raises(InstallationAlreadyBound):
        bound.initialize_installation(
            installation_id="inst-syn-2", company_id="co-syn-2",
            owner_principal_id="user-syn-other", owner_role="owner",
            now=at(60))
    # The original binding is untouched.
    assert bound.get_installation()["installation_id"] == "inst-syn-1"
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_members") == 1


def test_initialize_validates_boundary_values(service):
    with pytest.raises(ValueError):
        service.initialize_installation(
            installation_id="", company_id="co-syn-1",
            owner_principal_id="user-syn-owner", owner_role="owner",
            now=T0)
    with pytest.raises(ValueError):
        service.initialize_installation(
            installation_id="inst-syn-1", company_id="co-syn-1",
            owner_principal_id="user-syn-owner", owner_role="owner",
            now=datetime(2026, 10, 24))  # naive datetime rejected
    with pytest.raises(TypeError):
        service.initialize_installation(
            installation_id="inst-syn-1", company_id="co-syn-1",
            owner_principal_id="user-syn-owner", owner_role="owner",
            now="2026-10-24T12:00:00+00:00")
    assert service.get_installation() is None


def test_uninitialized_installation_establishes_no_identity(service):
    assert service.get_installation() is None
    with pytest.raises(InstallationRequired):
        service.issue_pairing_credential(now=T0, lifetime_seconds=600)
    with pytest.raises(PairingCredentialInvalid):
        service.pair("forged-secret", now=T0, session_lifetime_seconds=60)
    with pytest.raises(Unauthenticated):
        service.resolve_session("forged-secret", now=T0)


def test_forged_request_fields_cannot_establish_identity(bound, ticket):
    # The pairing and resolution interfaces accept no client-reported
    # actor, company or role; forged fields are a plain TypeError.
    with pytest.raises(TypeError):
        bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=60,
                   actor_id="attacker")
    with pytest.raises(TypeError):
        bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=60,
                   company_id="co-other")
    with pytest.raises(TypeError):
        bound.resolve_session(ticket.secret, now=at(20), role="admin")
    # And no identity was established by those attempts.
    with pytest.raises(Unauthenticated):
        bound.resolve_session(ticket.secret, now=at(20))


def test_concurrent_initialize_binds_at_most_once(db_path):
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(index):
        conn = _connect(db_path)
        try:
            svc = HostIdentityService(conn)
            barrier.wait(timeout=10)
            svc.initialize_installation(
                installation_id="inst-syn-%d" % index,
                company_id="co-syn-1",
                owner_principal_id="user-syn-owner",
                owner_role="owner", now=T0)
            outcomes.append("bound")
        except InstallationAlreadyBound:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["bound", "refused"]
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_installation") == 1
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_members") == 1


# -- pairing: legitimate, wrong, expired, consumed (coverage item 2) -------


def test_legitimate_pairing_returns_bound_owner_context(bound, ticket):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    context = grant.context
    assert isinstance(context, OwnerContext)
    assert context.installation_id == "inst-syn-1"
    assert context.company_id == "co-syn-1"
    assert context.principal_id == "user-syn-owner"
    assert context.role == "owner"
    assert context.member_revision == 1
    assert context.session_ref == grant.session_ref
    assert context.auth_method == "local_owner_pairing"
    assert context.session_issued_at == at(20)
    assert context.session_expires_at == at(20 + 3600)
    assert context.resolved_at == at(20)
    assert grant.expires_at == at(20 + 3600)
    # The same session secret resolves to the same bound owner context.
    resolved = bound.resolve_session(grant.secret, now=at(30))
    assert resolved.session_ref == grant.session_ref
    assert resolved.principal_id == "user-syn-owner"


def test_pairing_rejects_unknown_credential(bound, db_path):
    with pytest.raises(PairingCredentialInvalid):
        bound.pair("not-a-real-credential", now=at(20),
                   session_lifetime_seconds=60)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 0


def test_pairing_rejects_empty_and_wrong_type_secret(bound):
    with pytest.raises(ValueError):
        bound.pair("", now=at(20), session_lifetime_seconds=60)
    with pytest.raises(TypeError):
        bound.pair(None, now=at(20), session_lifetime_seconds=60)


def test_pairing_rejects_expired_credential(bound, ticket, db_path):
    # Issued at T0+10 with lifetime 600: expires exactly at T0+610.
    with pytest.raises(PairingCredentialExpired):
        bound.pair(ticket.secret, now=at(610), session_lifetime_seconds=60)
    with pytest.raises(PairingCredentialExpired):
        bound.pair(ticket.secret, now=at(9999), session_lifetime_seconds=60)
    # Expiry is not consumption: no session exists.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 0


def test_pairing_credential_is_one_time(bound, ticket, db_path):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    with pytest.raises(PairingCredentialConsumed):
        bound.pair(ticket.secret, now=at(30), session_lifetime_seconds=3600)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 1
    # The originally issued session still resolves.
    bound.resolve_session(grant.secret, now=at(40))


def test_pairing_validates_lifetime_arguments(bound):
    for bad in (True, False, "600", 60.0):
        with pytest.raises(TypeError):
            bound.issue_pairing_credential(now=T0, lifetime_seconds=bad)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            bound.issue_pairing_credential(now=T0, lifetime_seconds=bad)
    ticket = bound.issue_pairing_credential(now=T0, lifetime_seconds=600)
    with pytest.raises(TypeError):
        bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=True)
    with pytest.raises(ValueError):
        bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=0)


def test_pairing_after_owner_revocation_fails_without_consuming(
        bound, ticket, db_path):
    assert bound.revoke_member("user-syn-owner", now=at(15)) is True
    with pytest.raises(MembershipRevoked):
        bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=60)
    # Failure is closed and clean: the credential was not consumed and
    # no session was issued.
    assert _scalar(
        db_path,
        "SELECT status FROM host_identity_pairing_credentials "
        "WHERE credential_id = ?", (ticket.credential_id,)) == "pending"
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 0


# -- concurrency and failure atomicity (coverage item 3) -------------------


def test_concurrent_pairing_with_same_credential_issues_at_most_one_session(
        bound, ticket, db_path):
    barrier = threading.Barrier(2)
    grants = []
    failures = []

    def worker():
        conn = _connect(db_path)
        try:
            svc = HostIdentityService(conn)
            barrier.wait(timeout=10)
            grants.append(svc.pair(ticket.secret, now=at(20),
                                   session_lifetime_seconds=3600))
        except PairingFailed as exc:
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(grants) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PairingCredentialConsumed)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 1


class _FailOnSessionInsert(sqlite3.Connection):
    """Fault-injection connection: fails the session INSERT mid-pairing."""

    def execute(self, sql, parameters=()):
        if isinstance(sql, str) \
                and "INSERT INTO host_identity_sessions" in sql:
            raise RuntimeError("injected session insert failure")
        return super().execute(sql, parameters)


def test_failed_pairing_leaves_no_partial_consumption_or_ghost_session(
        bound, ticket, db_path):
    conn = sqlite3.connect(db_path, factory=_FailOnSessionInsert)
    conn.execute("PRAGMA busy_timeout=30000")
    failing = HostIdentityService(conn)
    with pytest.raises(RuntimeError):
        failing.pair(ticket.secret, now=at(20), session_lifetime_seconds=60)
    conn.close()
    # The transaction rolled back: the credential stayed pending and no
    # session row exists.
    assert _scalar(
        db_path,
        "SELECT status FROM host_identity_pairing_credentials "
        "WHERE credential_id = ?", (ticket.credential_id,)) == "pending"
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_identity_sessions") == 0
    # The same credential can still pair cleanly afterwards.
    grant = bound.pair(ticket.secret, now=at(25),
                       session_lifetime_seconds=60)
    assert grant.context.principal_id == "user-syn-owner"


# -- session storage and resolution failures (coverage item 4) -------------


def test_session_and_pairing_secrets_are_not_persisted(
        bound, ticket, db_path):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    session_rows = _rows(db_path, "SELECT * FROM host_identity_sessions")
    assert len(session_rows) == 1
    for value in session_rows[0]:
        assert value != grant.secret
        assert grant.secret not in str(value)
    credential_rows = _rows(
        db_path, "SELECT * FROM host_identity_pairing_credentials")
    assert len(credential_rows) == 1
    for value in credential_rows[0]:
        assert value != ticket.secret
        assert ticket.secret not in str(value)
    # Stored digests are not the raw secrets.
    assert _scalar(db_path,
                   "SELECT secret_sha256 FROM host_identity_sessions") \
        != grant.secret


def test_resolve_rejects_unknown_secret(bound, ticket):
    bound.pair(ticket.secret, now=at(20), session_lifetime_seconds=3600)
    with pytest.raises(Unauthenticated):
        bound.resolve_session("some-other-secret", now=at(30))


def test_resolve_rejects_expired_session(bound, ticket):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=100)
    bound.resolve_session(grant.secret, now=at(119))
    with pytest.raises(SessionExpired):
        bound.resolve_session(grant.secret, now=at(120))  # exact boundary
    with pytest.raises(SessionExpired):
        bound.resolve_session(grant.secret, now=at(9999))


def test_resolve_rejects_revoked_session_and_rereads_state(
        bound, ticket):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    bound.resolve_session(grant.secret, now=at(30))
    assert bound.revoke_session(grant.session_ref, now=at(40)) is True
    with pytest.raises(SessionRevoked):
        bound.resolve_session(grant.secret, now=at(41))
    # No stale cache is trusted: every later resolution still fails.
    with pytest.raises(SessionRevoked):
        bound.resolve_session(grant.secret, now=at(9999))
    # Revocation is idempotent.
    assert bound.revoke_session(grant.session_ref, now=at(50)) is False


def test_session_secret_from_another_installation_is_rejected(
        bound, ticket, tmp_path):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    other_path = tmp_path / "other-installation.db"
    other = HostIdentityService(_connect(other_path))
    other.initialize_installation(
        installation_id="inst-syn-2", company_id="co-syn-2",
        owner_principal_id="user-syn-owner-2", owner_role="owner", now=T0)
    with pytest.raises(Unauthenticated):
        other.resolve_session(grant.secret, now=at(30))


def test_revoke_session_unknown_reference(bound):
    with pytest.raises(UnknownSession):
        bound.revoke_session("hss_0000000000000000", now=T0)


# -- member revocation and context hygiene (coverage item 5) ---------------


def test_member_revocation_invalidates_existing_sessions(
        bound, ticket, db_path):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    context = bound.resolve_session(grant.secret, now=at(30))
    assert context.role == "owner"
    assert bound.revoke_member("user-syn-owner", now=at(40)) is True
    with pytest.raises(MembershipRevoked):
        bound.resolve_session(grant.secret, now=at(41))
    # The session row itself is untouched; resolution fails because it
    # re-reads the current membership state, not a cached context.
    assert _scalar(
        db_path, "SELECT status FROM host_identity_sessions") == "active"
    assert _scalar(
        db_path, "SELECT revision FROM host_identity_members") == 2
    # Idempotent re-revocation.
    assert bound.revoke_member("user-syn-owner", now=at(50)) is False
    with pytest.raises(MembershipRevoked):
        bound.resolve_session(grant.secret, now=at(60))


def test_revoke_member_unknown_principal(bound):
    with pytest.raises(UnknownMember):
        bound.revoke_member("user-syn-nobody", now=T0)


def test_context_has_no_secret_and_no_authority_methods(bound, ticket):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    context = grant.context
    expected_fields = {
        "installation_id", "company_id", "principal_id", "role",
        "member_revision", "session_ref", "auth_method",
        "session_issued_at", "session_expires_at", "resolved_at",
    }
    assert {f for f in dir(context) if not f.startswith("__")} \
        == expected_fields
    # Every field is plain data; no callable authority method exists.
    for name in expected_fields:
        value = getattr(context, name)
        assert not callable(value)
        assert isinstance(value, (str, int, datetime))
    # Neither the grant secret nor the pairing secret leaks via context.
    for needle in (grant.secret, ticket.secret):
        assert needle not in repr(context)
        assert needle not in str(context)


# -- persistence and secret hygiene (coverage item 6) -----------------------


def test_state_survives_connection_reopen(bound, ticket, db_path):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=3600)
    bound.db.close()
    reopened = HostIdentityService(_connect(db_path))
    # Binding, consumption and the live session all persist.
    with pytest.raises(InstallationAlreadyBound):
        reopened.initialize_installation(
            installation_id="inst-syn-1", company_id="co-syn-1",
            owner_principal_id="user-syn-owner", owner_role="owner",
            now=at(70))
    with pytest.raises(PairingCredentialConsumed):
        reopened.pair(ticket.secret, now=at(71),
                      session_lifetime_seconds=60)
    context = reopened.resolve_session(grant.secret, now=at(72))
    assert context.session_ref == grant.session_ref
    assert context.principal_id == "user-syn-owner"
    # Revocation also persists across reopen.
    reopened.revoke_session(grant.session_ref, now=at(80))
    reopened.db.close()
    again = HostIdentityService(_connect(db_path))
    with pytest.raises(SessionRevoked):
        again.resolve_session(grant.secret, now=at(81))
    again.db.close()


def test_repr_and_exceptions_never_contain_raw_secrets(bound, ticket):
    grant = bound.pair(ticket.secret, now=at(20),
                       session_lifetime_seconds=100)
    assert grant.secret not in repr(grant)
    assert ticket.secret not in repr(ticket)
    assert grant.session_ref in repr(grant)  # public reference is fine

    expired = bound.issue_pairing_credential(now=at(30),
                                             lifetime_seconds=10)
    with pytest.raises(PairingCredentialExpired) as excinfo:
        bound.pair(expired.secret, now=at(100), session_lifetime_seconds=60)
    exc = excinfo.value
    for needle in (expired.secret, ticket.secret, grant.secret):
        assert needle not in str(exc)
        assert needle not in repr(exc)
        assert all(needle not in str(arg) for arg in exc.args)

    bound.revoke_session(grant.session_ref, now=at(30))
    with pytest.raises(SessionRevoked) as excinfo:
        bound.resolve_session(grant.secret, now=at(31))
    assert grant.secret not in str(excinfo.value)
    assert grant.secret not in repr(excinfo.value)


# -- test isolation contract (coverage item 7) ------------------------------


def test_module_never_opens_the_default_real_database(tmp_path, monkeypatch):
    # Even with SAM_DATA_ROOT pointing elsewhere, the service has no
    # path-based entry point at all: only an explicit connection works.
    monkeypatch.setenv("SAM_DATA_ROOT", str(tmp_path / "elsewhere"))
    conn = _connect(tmp_path / "explicit.db")
    svc = HostIdentityService(conn)
    svc.initialize_installation(
        installation_id="inst-syn-9", company_id="co-syn-9",
        owner_principal_id="user-syn-owner-9", owner_role="owner", now=T0)
    assert svc.get_installation()["installation_id"] == "inst-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()
