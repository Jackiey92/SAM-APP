"""Tests for app.host_authorization (F-007 step 2 policy ledger).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data. The identity side is initialized
synthetically through the real HostIdentityService pairing flow.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.host_authorization import (
    HostAuthorizationService,
    PolicyAlreadyRevoked,
    PolicyExpired,
    PolicyNotFound,
    PolicyRecord,
    PolicyScope,
    PolicyStale,
    PolicyStateConflict,
)
from app.host_identity import (
    HostIdentityService,
    MembershipRevoked,
    SessionExpired,
    SessionRevoked,
    Unauthenticated,
)

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def scope_kwargs(**overrides):
    kwargs = {
        "target": "uploads-inbox",
        "plan_version": "plan-v1",
        "trigger_conditions": ["on_upload_completed"],
        "stop_conditions": ["on_owner_stop"],
        "actions": ["read", "extract"],
        "objects": ["sam://original/*"],
        "fields": ["title", "date"],
        "read_window": "current-only",
        "egress": None,
        "recipients": [],
        "not_before": at(0),
        "not_after": at(3600),
        "budget": {"requests": 100},
        "max_delegation_depth": 1,
        "max_concurrency": 2,
        "max_retries": 3,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-authorization.db"


@pytest.fixture
def identity(db_path):
    conn = _connect(db_path)
    svc = HostIdentityService(conn)
    svc.initialize_installation(
        installation_id="inst-syn-1",
        company_id="co-syn-1",
        owner_principal_id="user-syn-owner",
        owner_role="owner",
        now=T0,
    )
    yield svc
    conn.close()


@pytest.fixture
def service(identity):
    return HostAuthorizationService(identity.db, identity)


@pytest.fixture
def grant(identity):
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    return identity.pair(ticket.secret, now=at(20),
                         session_lifetime_seconds=3600)


@pytest.fixture
def policy(service, grant):
    return service.create_policy(grant.secret, now=at(30),
                                 **scope_kwargs())


def _path_of(service):
    return service.db.execute("PRAGMA database_list").fetchone()[2]


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


def test_constructor_requires_explicit_connection_and_identity(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    with pytest.raises(TypeError):
        HostAuthorizationService()  # noqa: F841 - missing arguments
    with pytest.raises(TypeError):
        HostAuthorizationService("host-authorization.db", identity)
    with pytest.raises(TypeError):
        HostAuthorizationService(object(), identity)
    with pytest.raises(TypeError):
        HostAuthorizationService(conn, object())
    conn.close()


def test_constructor_refuses_active_caller_transaction(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostAuthorizationService(conn, identity)
    conn.rollback()
    conn.close()


def _authorization_tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'host_authorization%'")}


def test_constructor_refuses_identity_on_another_database(tmp_path):
    # Identity lives in one database file; the ledger connection points
    # at a different file. Combining them would verify identity against
    # one database while writing policies to another, so it is refused
    # before any DDL runs.
    identity_conn = _connect(tmp_path / "identity.db")
    identity = HostIdentityService(identity_conn)
    ledger_conn = _connect(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        HostAuthorizationService(ledger_conn, identity)
    # No authorization table or trigger was created on either database.
    assert _authorization_tables(ledger_conn) == set()
    assert _authorization_tables(identity_conn) == set()
    ledger_conn.close()
    identity_conn.close()


def test_constructor_refuses_second_connection_to_same_file(db_path):
    # Same file, different connection object: still refused, because the
    # two handles could never share one SQLite transaction. The refusal
    # happens before DDL, so no authorization table appears.
    identity_conn = _connect(db_path)
    identity = HostIdentityService(identity_conn)
    second_conn = _connect(db_path)
    with pytest.raises(ValueError):
        HostAuthorizationService(second_conn, identity)
    assert _authorization_tables(second_conn) == set()
    # And the refusal left the identity service itself fully usable.
    identity.initialize_installation(
        installation_id="inst-syn-1", company_id="co-syn-1",
        owner_principal_id="user-syn-owner", owner_role="owner", now=T0)
    second_conn.close()
    identity_conn.close()


# -- creation: metadata comes from the authenticated owner -----------------


def test_create_binds_metadata_from_authenticated_owner(
        service, grant, db_path):
    record = service.create_policy(grant.secret, now=at(30),
                                   **scope_kwargs())
    assert isinstance(record, PolicyRecord)
    assert isinstance(record.scope, PolicyScope)
    assert record.policy_id.startswith("hap_")
    assert record.revision == 1
    assert record.status == "active"
    assert record.revocation_seq == 0
    # Identity metadata is taken from the resolved owner context, never
    # from caller input.
    assert record.installation_id == "inst-syn-1"
    assert record.company_id == "co-syn-1"
    assert record.granted_by == "user-syn-owner"
    assert record.actor_principal_id == "user-syn-owner"
    assert record.actor_member_revision == 1
    assert record.session_ref == grant.session_ref
    # Stored write times are the trusted now advanced by the monotonic
    # elapsed time: never earlier than the caller-provided base, and
    # creation stamps the policy row and revision 1 with one identical
    # in-transaction effective time.
    assert record.created_at >= at(30)
    assert record.changed_at >= at(30)
    assert record.changed_at == record.created_at
    row = _rows(db_path,
                "SELECT installation_id, company_id, granted_by "
                "FROM host_authorization_policies")
    assert row == [("inst-syn-1", "co-syn-1", "user-syn-owner")]


def test_caller_cannot_self_report_actor_installation_or_company(
        service, grant):
    for forged in ({"actor_principal_id": "attacker"},
                   {"granted_by": "attacker"},
                   {"installation_id": "inst-other"},
                   {"company_id": "co-other"},
                   {"session_ref": "hss_0000000000000000"}):
        kwargs = scope_kwargs()
        kwargs.update(forged)
        with pytest.raises(TypeError):
            service.create_policy(grant.secret, now=at(30), **kwargs)
    assert _scalar(_path_of(service),
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0


def test_create_requires_valid_current_session(service, grant, identity):
    with pytest.raises(Unauthenticated):
        service.create_policy("forged-secret", now=at(30),
                              **scope_kwargs())
    identity.revoke_session(grant.session_ref, now=at(35))
    with pytest.raises(SessionRevoked):
        service.create_policy(grant.secret, now=at(36), **scope_kwargs())


def test_member_revocation_refuses_all_reads_and_writes(
        service, grant, identity, policy):
    identity.revoke_member("user-syn-owner", now=at(40))
    with pytest.raises(MembershipRevoked):
        service.create_policy(grant.secret, now=at(41), **scope_kwargs())
    with pytest.raises(MembershipRevoked):
        service.get_policy(grant.secret, now=at(41),
                           policy_id=policy.policy_id)
    with pytest.raises(MembershipRevoked):
        service.pause_policy(grant.secret, now=at(41),
                             policy_id=policy.policy_id,
                             expected_revision=1)


def test_session_revocation_refuses_all_reads_and_writes(
        service, grant, identity, policy):
    identity.revoke_session(grant.session_ref, now=at(40))
    with pytest.raises(SessionRevoked):
        service.get_policy(grant.secret, now=at(41),
                           policy_id=policy.policy_id)
    with pytest.raises(SessionRevoked):
        service.get_policy_revision(grant.secret, now=at(41),
                                    policy_id=policy.policy_id,
                                    revision=1)
    with pytest.raises(SessionRevoked):
        service.revoke_policy(grant.secret, now=at(41),
                              policy_id=policy.policy_id,
                              expected_revision=1)


def test_expired_session_is_refused(service, grant, identity):
    ticket = identity.issue_pairing_credential(now=at(50),
                                               lifetime_seconds=100)
    short = identity.pair(ticket.secret, now=at(60),
                          session_lifetime_seconds=60)
    with pytest.raises(SessionExpired):
        service.create_policy(short.secret, now=at(120),
                              **scope_kwargs())


# -- creation: strict required / type / expiry validation ------------------


def test_create_requires_every_scope_field(service, grant):
    for missing in scope_kwargs():
        kwargs = scope_kwargs()
        del kwargs[missing]
        with pytest.raises(TypeError):
            service.create_policy(grant.secret, now=at(30), **kwargs)


def test_create_validates_scope_types_and_values(service, grant):
    bad_overrides = [
        {"target": ""},
        {"target": 123},
        {"plan_version": ""},
        {"trigger_conditions": []},
        {"trigger_conditions": "on_upload"},
        {"trigger_conditions": ["ok", ""]},
        {"stop_conditions": []},
        {"actions": []},
        {"actions": ("read", 5)},
        {"objects": []},
        {"fields": []},
        {"read_window": ""},
        {"egress": 7},
        {"recipients": ["ok", None]},
        {"not_before": "2026-10-24"},
        {"not_before": datetime(2026, 10, 24)},  # naive
        {"not_after": at(0) - timedelta(seconds=3600),
         "not_before": at(0)},  # not_after before not_before
        {"budget": {}},
        {"budget": {"requests": 0}},
        {"budget": {"requests": True}},
        {"budget": [("requests", 1)]},
        {"max_delegation_depth": True},
        {"max_delegation_depth": -1},
        {"max_delegation_depth": "1"},
        {"max_concurrency": -1},
        {"max_retries": 1.5},
    ]
    for overrides in bad_overrides:
        with pytest.raises((TypeError, ValueError)):
            service.create_policy(grant.secret, now=at(30),
                                  **scope_kwargs(**overrides))
    # None of the rejected attempts left a row behind.
    assert _scalar(_path_of(service),
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0
    assert _scalar(_path_of(service),
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 0


def test_create_rejects_already_expired_policy(service, grant, db_path):
    with pytest.raises(PolicyExpired):
        service.create_policy(grant.secret, now=at(30),
                              **scope_kwargs(not_before=at(0),
                                             not_after=at(30)))
    with pytest.raises(PolicyExpired):
        service.create_policy(grant.secret, now=at(30),
                              **scope_kwargs(not_before=at(0),
                                             not_after=at(10)))
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0


def test_scope_is_stored_losslessly(service, grant):
    record = service.create_policy(
        grant.secret, now=at(30),
        **scope_kwargs(egress="model-provider-a",
                       recipients=["rcpt-a", "rcpt-b"],
                       budget={"tokens": 1000, "requests": 10}))
    scope = record.scope
    assert scope.egress == "model-provider-a"
    assert scope.recipients == ("rcpt-a", "rcpt-b")
    # Budget is held as a canonical sorted tuple of pairs.
    assert scope.budget == (("requests", 10), ("tokens", 1000))
    # A read back from storage reconstructs the identical scope.
    loaded = service.get_policy(grant.secret, now=at(31),
                                policy_id=record.policy_id)
    assert loaded.scope == scope
    assert loaded.scope.not_before == at(0)
    assert loaded.scope.not_after == at(3600)


# -- reads: current and historical -----------------------------------------


def test_current_and_historical_reads(service, grant, policy):
    paused = service.pause_policy(grant.secret, now=at(40),
                                  policy_id=policy.policy_id,
                                  expected_revision=1)
    assert paused.revision == 2
    assert paused.status == "paused"
    current = service.get_policy(grant.secret, now=at(41),
                                 policy_id=policy.policy_id)
    assert current.revision == 2
    assert current.status == "paused"
    # The fixed historical revision is unchanged by the transition.
    first = service.get_policy_revision(grant.secret, now=at(42),
                                        policy_id=policy.policy_id,
                                        revision=1)
    assert first.revision == 1
    assert first.status == "active"
    assert first.revocation_seq == 0
    # The historical revision is the same stored row the creation
    # returned: its timestamp reads back identical and is never
    # earlier than the trusted now passed at creation.
    assert first.changed_at == policy.changed_at
    assert first.changed_at >= at(30)
    assert first.scope == policy.scope
    with pytest.raises(PolicyNotFound):
        service.get_policy(grant.secret, now=at(43),
                           policy_id="hap_0000000000000000")
    with pytest.raises(PolicyNotFound):
        service.get_policy_revision(grant.secret, now=at(43),
                                    policy_id=policy.policy_id,
                                    revision=99)


def test_read_argument_validation(service, grant, policy):
    with pytest.raises(ValueError):
        service.get_policy(grant.secret, now=at(31), policy_id="")
    with pytest.raises(TypeError):
        service.get_policy(grant.secret, now="now",
                           policy_id=policy.policy_id)
    with pytest.raises(TypeError):
        service.get_policy_revision(grant.secret, now=at(31),
                                    policy_id=policy.policy_id,
                                    revision=True)
    with pytest.raises(ValueError):
        service.get_policy_revision(grant.secret, now=at(31),
                                    policy_id=policy.policy_id,
                                    revision=0)


# -- status transitions ------------------------------------------------------


def test_legal_transitions_append_revisions_and_preserve_scope(
        service, grant, policy, db_path):
    paused = service.pause_policy(grant.secret, now=at(40),
                                  policy_id=policy.policy_id,
                                  expected_revision=1)
    resumed = service.resume_policy(grant.secret, now=at(50),
                                    policy_id=policy.policy_id,
                                    expected_revision=2)
    revoked = service.revoke_policy(grant.secret, now=at(60),
                                    policy_id=policy.policy_id,
                                    expected_revision=3)
    assert (paused.revision, paused.status, paused.revocation_seq) \
        == (2, "paused", 1)
    assert (resumed.revision, resumed.status, resumed.revocation_seq) \
        == (3, "active", 2)
    assert (revoked.revision, revoked.status, revoked.revocation_seq) \
        == (4, "revoked", 3)
    # Every revision preserves the original scope byte-for-byte.
    scope_jsons = [row[0] for row in _rows(
        db_path,
        "SELECT scope_json FROM host_authorization_policy_revisions "
        "WHERE policy_id = ? ORDER BY revision",
        (policy.policy_id,))]
    assert len(scope_jsons) == 4
    assert len(set(scope_jsons)) == 1
    for record in (paused, resumed, revoked):
        assert record.scope == policy.scope
        # created_at comes from the immutable policy row: every
        # revision carries the one creation time first returned, and
        # it is never earlier than the trusted now passed at creation.
        assert record.created_at == policy.created_at
        assert record.created_at >= at(30)
        assert record.granted_by == "user-syn-owner"
    # Each transition is stamped no earlier than its own trusted now,
    # and revision times never run backwards in creation order.
    assert paused.changed_at >= at(40)
    assert resumed.changed_at >= at(50)
    assert revoked.changed_at >= at(60)
    assert policy.changed_at <= paused.changed_at \
        <= resumed.changed_at <= revoked.changed_at


def test_revoke_directly_from_active(service, grant, policy):
    revoked = service.revoke_policy(grant.secret, now=at(40),
                                    policy_id=policy.policy_id,
                                    expected_revision=1)
    assert revoked.status == "revoked"
    assert revoked.revocation_seq == 1


def test_revoked_is_terminal(service, grant, policy):
    service.revoke_policy(grant.secret, now=at(40),
                          policy_id=policy.policy_id,
                          expected_revision=1)
    with pytest.raises(PolicyAlreadyRevoked):
        service.pause_policy(grant.secret, now=at(50),
                             policy_id=policy.policy_id,
                             expected_revision=2)
    with pytest.raises(PolicyAlreadyRevoked):
        service.resume_policy(grant.secret, now=at(50),
                              policy_id=policy.policy_id,
                              expected_revision=2)
    with pytest.raises(PolicyAlreadyRevoked):
        service.revoke_policy(grant.secret, now=at(50),
                              policy_id=policy.policy_id,
                              expected_revision=2)
    # PolicyAlreadyRevoked is a PolicyStateConflict for coarse callers.
    assert issubclass(PolicyAlreadyRevoked, PolicyStateConflict)


def test_stale_expected_revision_is_rejected(service, grant, policy,
                                             db_path):
    service.pause_policy(grant.secret, now=at(40),
                         policy_id=policy.policy_id,
                         expected_revision=1)
    for stale in (1, 3, 99):
        with pytest.raises(PolicyStale):
            service.resume_policy(grant.secret, now=at(50),
                                  policy_id=policy.policy_id,
                                  expected_revision=stale)
        with pytest.raises(PolicyStale):
            service.pause_policy(grant.secret, now=at(50),
                                 policy_id=policy.policy_id,
                                 expected_revision=stale)
    # Nothing moved: still revision 2, still paused, sequence at 1.
    current = service.get_policy(grant.secret, now=at(51),
                                 policy_id=policy.policy_id)
    assert (current.revision, current.status,
            current.revocation_seq) == (2, "paused", 1)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 2


def test_illegal_transitions_are_rejected(service, grant, policy):
    with pytest.raises(PolicyStateConflict):
        service.resume_policy(grant.secret, now=at(40),
                              policy_id=policy.policy_id,
                              expected_revision=1)
    service.pause_policy(grant.secret, now=at(40),
                         policy_id=policy.policy_id,
                         expected_revision=1)
    with pytest.raises(PolicyStateConflict):
        service.pause_policy(grant.secret, now=at(50),
                             policy_id=policy.policy_id,
                             expected_revision=2)


def test_transition_argument_validation(service, grant, policy):
    with pytest.raises(TypeError):
        service.pause_policy(grant.secret, now=at(40),
                             policy_id=policy.policy_id,
                             expected_revision=True)
    with pytest.raises(ValueError):
        service.pause_policy(grant.secret, now=at(40),
                             policy_id=policy.policy_id,
                             expected_revision=0)
    with pytest.raises(ValueError):
        service.pause_policy(grant.secret, now=at(40), policy_id="",
                             expected_revision=1)
    with pytest.raises(TypeError):
        service.pause_policy(grant.secret, now="now",
                             policy_id=policy.policy_id,
                             expected_revision=1)
    with pytest.raises(PolicyNotFound):
        service.pause_policy(grant.secret, now=at(40),
                             policy_id="hap_0000000000000000",
                             expected_revision=1)


def test_transitions_accept_no_scope_edits(service, grant, policy):
    # Status transitions take no scope input at all: a changed scope
    # can only become a new policy.
    with pytest.raises(TypeError):
        service.pause_policy(grant.secret, now=at(40),
                             policy_id=policy.policy_id,
                             expected_revision=1, target="forged")
    with pytest.raises(TypeError):
        service.revoke_policy(grant.secret, now=at(40),
                              policy_id=policy.policy_id,
                              expected_revision=1, actions=["x"])


def test_resume_of_expired_policy_is_refused(service, grant, db_path):
    record = service.create_policy(grant.secret, now=at(30),
                                   **scope_kwargs(not_before=at(0),
                                                  not_after=at(100)))
    service.pause_policy(grant.secret, now=at(50),
                         policy_id=record.policy_id,
                         expected_revision=1)
    # at(100) is the exact expiry boundary: not effective any more.
    with pytest.raises(PolicyExpired):
        service.resume_policy(grant.secret, now=at(100),
                              policy_id=record.policy_id,
                              expected_revision=2)
    # The failed resume left state untouched.
    current = service.get_policy(grant.secret, now=at(101),
                                 policy_id=record.policy_id)
    assert (current.revision, current.status) == (2, "paused")
    # Revoking an expired policy is still possible bookkeeping.
    revoked = service.revoke_policy(grant.secret, now=at(102),
                                    policy_id=record.policy_id,
                                    expected_revision=2)
    assert revoked.status == "revoked"


def test_revocation_sequence_is_ledger_wide_and_monotonic(
        service, grant, db_path):
    first = service.create_policy(grant.secret, now=at(30),
                                  **scope_kwargs())
    second = service.create_policy(grant.secret, now=at(31),
                                   **scope_kwargs(target="other"))
    one = service.pause_policy(grant.secret, now=at(40),
                               policy_id=first.policy_id,
                               expected_revision=1)
    two = service.revoke_policy(grant.secret, now=at(41),
                                policy_id=second.policy_id,
                                expected_revision=1)
    three = service.revoke_policy(grant.secret, now=at(42),
                                  policy_id=first.policy_id,
                                  expected_revision=2)
    assert (one.revocation_seq, two.revocation_seq,
            three.revocation_seq) == (1, 2, 3)
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 3


# -- append-only storage guards ----------------------------------------------


def test_policy_and_revision_rows_reject_update_and_delete(
        service, grant, policy, db_path):
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE host_authorization_policies "
                "SET granted_by = 'attacker'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_authorization_policies")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE host_authorization_policy_revisions "
                "SET status = 'active'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM host_authorization_policy_revisions")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE host_authorization_policy_revisions "
                "SET scope_json = '{}'")
    finally:
        conn.close()
    # The guards fired before any change: stored rows are intact.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 1
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 1


def test_revocation_sequence_rejects_non_increment_writes(
        service, grant, policy, db_path):
    service.pause_policy(grant.secret, now=at(40),
                         policy_id=policy.policy_id,
                         expected_revision=1)
    conn = _connect(db_path)
    try:
        # Rewrite to the same value, a decrease and a skip all fail.
        for value in (1, 0, 3):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE host_authorization_ledger_state "
                    "SET revocation_seq = ?", (value,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_authorization_ledger_state")
    finally:
        conn.close()
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 1


def test_replace_cannot_overwrite_policy_or_revision_rows(
        service, grant, policy, db_path):
    # With the SQLite default recursive_triggers=0 the implicit delete
    # inside INSERT OR REPLACE does not fire the DELETE triggers, so the
    # UPDATE/DELETE guards alone would be bypassable. A BEFORE INSERT
    # guard must refuse any insert over an existing primary key.
    assert _scalar(db_path, "PRAGMA recursive_triggers") == 0
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_authorization_policies "
                "(policy_id, installation_id, company_id, granted_by, "
                "created_at) VALUES (?, 'inst-syn-1', 'co-syn-1', "
                "'attacker', ?)",
                (policy.policy_id, at(999).isoformat()))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO "
                "host_authorization_policy_revisions "
                "(policy_id, revision, status, revocation_seq, "
                "scope_json, installation_id, company_id, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) VALUES (?, 1, 'revoked', "
                "999, '{}', 'inst-syn-1', 'co-syn-1', 'attacker', 1, "
                "?, ?)",
                (policy.policy_id, grant.session_ref,
                 at(999).isoformat()))
    finally:
        conn.close()
    # Neither row was deleted, overwritten or reset by the refused
    # REPLACE attempts.
    assert _rows(db_path,
                 "SELECT installation_id, company_id, granted_by "
                 "FROM host_authorization_policies") \
        == [("inst-syn-1", "co-syn-1", "user-syn-owner")]
    revision_rows = _rows(
        db_path,
        "SELECT status, revocation_seq, scope_json, actor_principal_id "
        "FROM host_authorization_policy_revisions "
        "WHERE policy_id = ? AND revision = 1", (policy.policy_id,))
    assert len(revision_rows) == 1
    status, revocation_seq, scope_json, actor = revision_rows[0]
    assert (status, revocation_seq, actor) == ("active", 0,
                                               "user-syn-owner")
    assert scope_json != "{}"
    # And the untouched ledger still reads back through the service.
    current = service.get_policy(grant.secret, now=at(100),
                                 policy_id=policy.policy_id)
    assert (current.revision, current.status, current.revocation_seq) \
        == (1, "active", 0)


def test_replace_cannot_reset_revocation_sequence(
        service, grant, policy, db_path):
    service.pause_policy(grant.secret, now=at(40),
                         policy_id=policy.policy_id,
                         expected_revision=1)
    assert _scalar(db_path, "PRAGMA recursive_triggers") == 0
    conn = _connect(db_path)
    try:
        # A reset, an in-place rewrite and a forged jump are all
        # refused: the only legal sequence write is the +1 UPDATE.
        for value in (0, 1, 999):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT OR REPLACE INTO "
                    "host_authorization_ledger_state "
                    "(singleton, revocation_seq) VALUES (1, ?)",
                    (value,))
    finally:
        conn.close()
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 1


def test_normal_appends_and_reopen_survive_the_replace_guards(
        service, grant, policy, db_path):
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_authorization_policies "
                "(policy_id, installation_id, company_id, granted_by, "
                "created_at) VALUES (?, 'inst-syn-1', 'co-syn-1', "
                "'attacker', ?)",
                (policy.policy_id, at(999).isoformat()))
    finally:
        conn.close()
    # A genuinely new policy still appends normally on the same ledger.
    second = service.create_policy(grant.secret, now=at(50),
                                   **scope_kwargs(target="other"))
    assert second.policy_id != policy.policy_id
    assert (second.revision, second.status) == (1, "active")
    # A status transition still appends a new revision and moves the
    # ledger-wide sequence by exactly one.
    paused = service.pause_policy(grant.secret, now=at(60),
                                  policy_id=policy.policy_id,
                                  expected_revision=1)
    assert (paused.revision, paused.status, paused.revocation_seq) \
        == (2, "paused", 1)
    # Re-opening the database re-runs the idempotent schema (including
    # the singleton initialization) without error and without touching
    # the stored state.
    conn2 = _connect(db_path)
    try:
        reopened = HostAuthorizationService(conn2,
                                            HostIdentityService(conn2))
        current = reopened.get_policy(grant.secret, now=at(70),
                                      policy_id=policy.policy_id)
        assert (current.revision, current.status,
                current.revocation_seq) == (2, "paused", 1)
        assert _scalar(db_path,
                       "SELECT revocation_seq FROM "
                       "host_authorization_ledger_state") == 1
    finally:
        conn2.close()


# -- caller transaction isolation ----------------------------------------------


def test_write_methods_refuse_and_never_commit_caller_transaction(
        service, grant, policy):
    conn = service.db
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError):
            service.create_policy(grant.secret, now=at(40),
                                  **scope_kwargs())
        with pytest.raises(ValueError):
            service.pause_policy(grant.secret, now=at(40),
                                 policy_id=policy.policy_id,
                                 expected_revision=1)
        # The caller transaction is still owned by the caller.
        assert conn.in_transaction
    finally:
        conn.rollback()
    current = service.get_policy(grant.secret, now=at(41),
                                 policy_id=policy.policy_id)
    assert (current.revision, current.status) == (1, "active")


def test_reads_inside_caller_transaction_never_close_it(
        service, grant, policy):
    conn = service.db
    conn.execute("BEGIN")
    try:
        current = service.get_policy(grant.secret, now=at(40),
                                     policy_id=policy.policy_id)
        assert current.revision == 1
        assert conn.in_transaction
    finally:
        conn.rollback()


# -- persistence across reconnect and failure rollback ------------------------


def test_state_survives_connection_reopen(identity, grant, policy,
                                          db_path):
    identity.db.close()
    conn = _connect(db_path)
    reopened_identity = HostIdentityService(conn)
    reopened = HostAuthorizationService(conn, reopened_identity)
    current = reopened.get_policy(grant.secret, now=at(50),
                                  policy_id=policy.policy_id)
    assert current.policy_id == policy.policy_id
    assert current.status == "active"
    assert current.scope == policy.scope
    paused = reopened.pause_policy(grant.secret, now=at(60),
                                   policy_id=policy.policy_id,
                                   expected_revision=1)
    assert paused.status == "paused"
    conn.close()
    again_conn = _connect(db_path)
    again = HostAuthorizationService(again_conn,
                                     HostIdentityService(again_conn))
    current = again.get_policy(grant.secret, now=at(70),
                               policy_id=policy.policy_id)
    assert (current.revision, current.status,
            current.revocation_seq) == (2, "paused", 1)
    again_conn.close()


class _FailOn(sqlite3.Connection):
    """Fault-injection connection failing one chosen SQL statement."""

    needle = ""

    def execute(self, sql, parameters=()):
        if self.needle and isinstance(sql, str) \
                and self.needle in sql:
            raise RuntimeError("injected failure")
        return super().execute(sql, parameters)


def test_failed_transition_rolls_back_completely(
        identity, grant, policy, db_path):
    identity.db.close()
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    failing_identity = HostIdentityService(conn)
    failing = HostAuthorizationService(conn, failing_identity)
    conn.needle = "UPDATE host_authorization_ledger_state"
    with pytest.raises(RuntimeError):
        failing.pause_policy(grant.secret, now=at(40),
                             policy_id=policy.policy_id,
                             expected_revision=1)
    conn.close()
    # Rollback left no partial state: one revision, still active,
    # sequence still zero, and a clean retry succeeds.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 1
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 0
    conn2 = _connect(db_path)
    service2 = HostAuthorizationService(conn2, HostIdentityService(conn2))
    current = service2.get_policy(grant.secret, now=at(41),
                                  policy_id=policy.policy_id)
    assert (current.revision, current.status) == (1, "active")
    paused = service2.pause_policy(grant.secret, now=at(42),
                                   policy_id=policy.policy_id,
                                   expected_revision=1)
    assert paused.status == "paused"
    conn2.close()


def test_failed_create_rolls_back_completely(identity, grant, db_path):
    identity.db.close()
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    failing = HostAuthorizationService(conn, HostIdentityService(conn))
    conn.needle = "INSERT INTO host_authorization_policy_revisions"
    with pytest.raises(RuntimeError):
        failing.create_policy(grant.secret, now=at(30),
                              **scope_kwargs())
    conn.close()
    # No policy row without its revision and no revision at all.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 0


# -- write-lock wait expiry races ----------------------------------------------


class _LockWaitClock:
    """Deterministic monotonic-ns source simulating a write-lock wait.

    Values are integer nanoseconds, matching time.monotonic_ns, so a
    simulated wait reaches the in-transaction effective time with
    microsecond precision. The clock only moves when the instrumented
    connection reaches BEGIN IMMEDIATE, so the simulated wait lands
    exactly between the entry-time capture of the trusted host
    timestamp and the in-transaction effective-time computation - the
    window a real contended write lock would fill. No real sleeping
    is involved.
    """

    def __init__(self, wait_microseconds):
        self._t = 1_000_000_000_000  # fixed synthetic origin, in ns
        self._wait_ns = int(wait_microseconds) * 1000

    def __call__(self):
        return self._t

    def advance(self):
        self._t += self._wait_ns


class _WaitOnBegin(sqlite3.Connection):
    """Connection advancing the fake clock at BEGIN IMMEDIATE."""

    clock = None

    def execute(self, sql, parameters=()):
        if self.clock is not None and sql == "BEGIN IMMEDIATE":
            self.clock.advance()
        return super().execute(sql, parameters)


def _reopen_with_simulated_lock_wait(identity, db_path,
                                     wait_microseconds, monkeypatch):
    """Reopen the ledger so the next write waits wait_microseconds for
    the lock, simulated purely through the monotonic-ns clock."""
    identity.db.close()
    clock = _LockWaitClock(wait_microseconds)
    conn = sqlite3.connect(db_path, factory=_WaitOnBegin)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.clock = clock
    monkeypatch.setattr("app.host_authorization._monotonic_ns", clock)
    return HostAuthorizationService(conn, HostIdentityService(conn))


def test_create_rejected_when_lock_wait_crosses_session_expiry(
        identity, grant, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path,
                                               120_000_000, monkeypatch)
    # The granted session expires at at(3620). Entering at at(3600) is
    # still valid and passes the entry checks, but the simulated 120s
    # write-lock wait crosses the session expiry: the in-transaction
    # effective time must refuse the creation.
    with pytest.raises(SessionExpired):
        service.create_policy(grant.secret, now=at(3600),
                              **scope_kwargs(not_before=at(0),
                                             not_after=at(7200)))
    service.db.close()
    # The refused creation left no policy and no revision behind.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 0


def test_resume_rejected_when_lock_wait_crosses_policy_expiry(
        identity, grant, service, db_path, monkeypatch):
    record = service.create_policy(grant.secret, now=at(30),
                                   **scope_kwargs(not_before=at(0),
                                                  not_after=at(100)))
    service.pause_policy(grant.secret, now=at(50),
                         policy_id=record.policy_id,
                         expected_revision=1)
    reopened = _reopen_with_simulated_lock_wait(identity, db_path,
                                                20_000_000, monkeypatch)
    # Entering at at(90) is still inside the validity window, but the
    # simulated 20s write-lock wait crosses not_after=at(100): the
    # resume must be refused on the in-transaction effective time.
    with pytest.raises(PolicyExpired):
        reopened.resume_policy(grant.secret, now=at(90),
                               policy_id=record.policy_id,
                               expected_revision=2)
    reopened.db.close()
    # Status, revision and revocation_seq are all untouched.
    assert _rows(db_path,
                 "SELECT revision, status, revocation_seq FROM "
                 "host_authorization_policy_revisions "
                 "WHERE policy_id = ? ORDER BY revision",
                 (record.policy_id,)) == [(1, "active", 0),
                                          (2, "paused", 1)]
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 1


def test_create_rejected_when_microsecond_wait_crosses_session_expiry(
        identity, grant, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                               monkeypatch)
    # Narrowest crossing: the granted session expires at at(3620) and
    # the call enters exactly one microsecond before that boundary,
    # still valid at entry. The simulated 2µs write-lock wait moves
    # the in-transaction effective time one microsecond past the
    # expiry, so the creation must be refused at microsecond
    # resolution instead of passing on the stale pre-lock time.
    with pytest.raises(SessionExpired):
        service.create_policy(
            grant.secret,
            now=at(3620) - timedelta(microseconds=1),
            **scope_kwargs(not_before=at(0), not_after=at(7200)))
    service.db.close()
    # The refused creation left no policy and no revision behind.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_authorization_policies") \
        == 0
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM "
                   "host_authorization_policy_revisions") == 0


def test_resume_rejected_when_microsecond_wait_crosses_policy_expiry(
        identity, grant, service, db_path, monkeypatch):
    record = service.create_policy(grant.secret, now=at(30),
                                   **scope_kwargs(not_before=at(0),
                                                  not_after=at(100)))
    service.pause_policy(grant.secret, now=at(50),
                         policy_id=record.policy_id,
                         expected_revision=1)
    reopened = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                                monkeypatch)
    # Narrowest crossing: entering exactly one microsecond before
    # not_after=at(100) is still inside the validity window, and the
    # simulated 2µs write-lock wait moves the in-transaction
    # effective time one microsecond past the boundary, so the resume
    # must be refused and leave no trace.
    with pytest.raises(PolicyExpired):
        reopened.resume_policy(
            grant.secret,
            now=at(100) - timedelta(microseconds=1),
            policy_id=record.policy_id,
            expected_revision=2)
    reopened.db.close()
    # Status, revision and revocation_seq are all untouched.
    assert _rows(db_path,
                 "SELECT revision, status, revocation_seq FROM "
                 "host_authorization_policy_revisions "
                 "WHERE policy_id = ? ORDER BY revision",
                 (record.policy_id,)) == [(1, "active", 0),
                                          (2, "paused", 1)]
    assert _scalar(db_path,
                   "SELECT revocation_seq FROM "
                   "host_authorization_ledger_state") == 1


# -- test isolation contract ---------------------------------------------------


def test_module_never_opens_the_default_real_database(tmp_path,
                                                      monkeypatch):
    # Even with SAM_DATA_ROOT pointing elsewhere, the service has no
    # path-based entry point at all: only an explicit connection works.
    monkeypatch.setenv("SAM_DATA_ROOT", str(tmp_path / "elsewhere"))
    conn = _connect(tmp_path / "explicit.db")
    identity = HostIdentityService(conn)
    identity.initialize_installation(
        installation_id="inst-syn-9", company_id="co-syn-9",
        owner_principal_id="user-syn-owner-9", owner_role="owner",
        now=T0)
    svc = HostAuthorizationService(conn, identity)
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20),
                          session_lifetime_seconds=3600)
    record = svc.create_policy(grant.secret, now=at(30),
                               **scope_kwargs())
    assert record.company_id == "co-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()


def test_session_secret_is_not_persisted(service, grant, policy,
                                         db_path):
    service.pause_policy(grant.secret, now=at(40),
                         policy_id=policy.policy_id,
                         expected_revision=1)
    for table in ("host_authorization_policies",
                  "host_authorization_policy_revisions"):
        for row in _rows(db_path, "SELECT * FROM " + table):
            for value in row:
                assert value != grant.secret
                assert grant.secret not in str(value)
