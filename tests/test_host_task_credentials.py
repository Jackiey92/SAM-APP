"""Tests for app.host_task_credentials (F-007 root-task credentials).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data, no models. Identity and policy
state are built synthetically through the real HostIdentityService
pairing flow and the real HostAuthorizationService ledger.
"""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.host_authorization import (
    HostAuthorizationService,
    PolicyNotFound,
    _build_scope,
    _dumps_scope,
)
from app.host_identity import (
    HostIdentityService,
    MembershipRevoked,
    SessionExpired,
    SessionRevoked,
    Unauthenticated,
)
from app.host_task_credentials import (
    HostTaskCredentialService,
    TaskBindingMismatch,
    TaskCredentialExpired,
    TaskCredentialGrant,
    TaskCredentialInvalid,
    TaskCredentialNotYetValid,
    TaskCredentialRevoked,
    TaskMembershipRevoked,
    TaskPolicyExpired,
    TaskPolicyInactive,
    TaskPolicyNotYetValid,
    TaskPolicyStale,
    TaskRunContext,
    TaskScopeDenied,
    UnknownTaskCredential,
)

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _credential_digest(secret):
    # Recomputed independently of the module so the tests also pin the
    # domain-separation constant.
    return hashlib.sha256(
        b"sam-host-task-credentials credential-v1\x00"
        + secret.encode("utf-8")).hexdigest()


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


def rich_scope_kwargs(**overrides):
    return scope_kwargs(
        actions=["read", "extract", "classify"],
        fields=["title", "date", "amount"],
        trigger_conditions=["on_upload_completed", "on_manual_review"],
        egress="model-house",
        recipients=["board", "founders"],
        budget={"requests": 100, "tokens": 5000},
        **overrides)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-task-credentials.db"


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
def authorization(identity):
    return HostAuthorizationService(identity.db, identity)


@pytest.fixture
def service(identity, authorization):
    return HostTaskCredentialService(identity.db, identity,
                                     authorization)


@pytest.fixture
def grant(identity):
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    return identity.pair(ticket.secret, now=at(20),
                         session_lifetime_seconds=3600)


@pytest.fixture
def policy(authorization, grant):
    return authorization.create_policy(grant.secret, now=at(30),
                                       **scope_kwargs())


@pytest.fixture
def cred(service, grant, policy):
    return service.issue_task_credential(grant.secret, now=at(40),
                                         policy_id=policy.policy_id,
                                         expected_revision=1)


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


def _credential_tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'host_task_credential%'")}


def _issue_kwargs(policy, **overrides):
    kwargs = {"policy_id": policy.policy_id, "expected_revision": 1}
    kwargs.update(overrides)
    return kwargs


def _assert_no_credential_rows(db_path):
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_task_credentials") == 0
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_task_credential_events") \
        == 0
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 0


def _forge_credential_row(db_path, *, secret, task_id,
                          company_id="co-syn-1",
                          installation_id="inst-syn-1",
                          principal_id="user-syn-owner",
                          policy_id="hap_forged00000001",
                          policy_revision=1, scope=None):
    """Insert a credential row plus its issued event through raw SQL,
    simulating forged or tampered persistent state. The issuance rows
    are append-only but direct inserts of NEW rows are what the resolve
    path must defend against by re-checking every binding."""
    if scope is None:
        scope = _build_scope(**scope_kwargs())
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_task_credentials "
            "(task_id, run_id, credential_sha256, parent_task_id, "
            "installation_id, company_id, principal_id, "
            "member_revision, issued_by_session_ref, policy_id, "
            "policy_revision, scope_json, issued_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, 1, 'hss_forged00000001', "
            "?, ?, ?, ?)",
            (task_id, "htr_forged_" + task_id, _credential_digest(secret),
             installation_id, company_id, principal_id, policy_id,
             policy_revision, _dumps_scope(scope), at(30).isoformat()))
        conn.execute(
            "INSERT INTO host_task_credential_events "
            "(task_id, event_seq, event, revocation_seq, "
            "actor_principal_id, actor_member_revision, session_ref, "
            "changed_at) VALUES (?, 1, 'issued', 0, ?, 1, "
            "'hss_forged00000001', ?)",
            (task_id, principal_id, at(30).isoformat()))
        conn.commit()
    finally:
        conn.close()


# -- construction and connection contract ---------------------------------


def test_constructor_requires_explicit_connection_and_bound_services(
        db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    with pytest.raises(TypeError):
        HostTaskCredentialService("host-task-credentials.db", identity,
                                  authorization)
    with pytest.raises(TypeError):
        HostTaskCredentialService(object(), identity, authorization)
    with pytest.raises(TypeError):
        HostTaskCredentialService(conn, object(), authorization)
    with pytest.raises(TypeError):
        HostTaskCredentialService(conn, identity, object())
    conn.close()


def test_constructor_refuses_second_connection_to_same_file(db_path):
    # Same file, different connection object: identity resolution,
    # policy re-verification and the credential write could no longer
    # share one SQLite transaction, so the combination is refused
    # before any DDL runs.
    identity_conn = _connect(db_path)
    identity = HostIdentityService(identity_conn)
    authorization = HostAuthorizationService(identity_conn, identity)
    second_conn = _connect(db_path)
    with pytest.raises(ValueError):
        HostTaskCredentialService(second_conn, identity, authorization)
    assert _credential_tables(second_conn) == set()
    second_conn.close()
    identity_conn.close()


def test_constructor_refuses_divergent_identity_instances(db_path):
    # The authorization ledger is bound to one identity instance; the
    # credential service must be handed the very same one, or identity
    # verification and policy state would come from divergent objects.
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    other_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    with pytest.raises(ValueError):
        HostTaskCredentialService(conn, other_identity, authorization)
    conn.close()


def test_constructor_refuses_active_caller_transaction(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostTaskCredentialService(conn, identity, authorization)
    conn.rollback()
    conn.close()


def test_constructor_ddl_is_idempotent_across_reconnect(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    first = HostTaskCredentialService(conn, identity, authorization)
    conn.close()
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    second = HostTaskCredentialService(conn, identity, authorization)
    assert _credential_tables(conn) >= {
        "host_task_credentials", "host_task_credential_events",
        "host_task_credentials_ledger_state"}
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 0
    conn.close()
    del first, second


# -- issuance: everything is host-bound ------------------------------------


def test_issue_binds_metadata_from_host_state(service, grant, policy,
                                              db_path):
    issued = service.issue_task_credential(
        grant.secret, now=at(40), **_issue_kwargs(policy))
    assert isinstance(issued, TaskCredentialGrant)
    assert isinstance(issued.context, TaskRunContext)
    assert issued.task_id.startswith("htk_")
    assert issued.run_id.startswith("htr_")
    ctx = issued.context
    # Identity metadata is taken from the resolved owner context, the
    # policy binding from the ledger's current revision; task_id and
    # run_id are host-generated. None of them is caller input.
    assert ctx.installation_id == "inst-syn-1"
    assert ctx.company_id == "co-syn-1"
    assert ctx.principal_id == "user-syn-owner"
    assert ctx.member_revision == 1
    assert ctx.policy_id == policy.policy_id
    assert ctx.policy_revision == 1
    # With no narrowing requested, the issuance-time cap equals the
    # approved policy scope exactly, including its validity period.
    assert ctx.scope == policy.scope
    assert issued.expires_at == policy.scope.not_after
    assert ctx.issued_at >= at(40)
    row = _rows(db_path,
                "SELECT installation_id, company_id, principal_id, "
                "member_revision, issued_by_session_ref, policy_id, "
                "policy_revision, parent_task_id "
                "FROM host_task_credentials")
    assert row == [("inst-syn-1", "co-syn-1", "user-syn-owner", 1,
                    grant.session_ref, policy.policy_id, 1, None)]
    assert _rows(db_path,
                 "SELECT task_id, event_seq, event, revocation_seq, "
                 "actor_principal_id, session_ref "
                 "FROM host_task_credential_events") \
        == [(issued.task_id, 1, "issued", 0, "user-syn-owner",
             grant.session_ref)]


def test_caller_cannot_self_report_host_bound_fields(service, grant,
                                                     policy, db_path):
    for forged in ({"actor_principal_id": "attacker"},
                   {"principal_id": "attacker"},
                   {"granted_by": "attacker"},
                   {"installation_id": "inst-other"},
                   {"company_id": "co-other"},
                   {"session_ref": "hss_0000000000000000"},
                   {"task_id": "htk_attackerchosen"},
                   {"run_id": "htr_attackerchosen"},
                   {"policy_revision": 99},
                   {"member_revision": 99},
                   {"target": "other-target"},
                   {"plan_version": "other-plan"}):
        kwargs = _issue_kwargs(policy)
        kwargs.update(forged)
        with pytest.raises(TypeError):
            service.issue_task_credential(grant.secret, now=at(40),
                                          **kwargs)
    _assert_no_credential_rows(db_path)


def test_issue_requires_valid_current_session(service, grant, policy,
                                              identity, db_path):
    with pytest.raises(Unauthenticated):
        service.issue_task_credential(
            "forged-session-secret", now=at(40),
            **_issue_kwargs(policy))
    identity.revoke_session(grant.session_ref, now=at(41))
    with pytest.raises(SessionRevoked):
        service.issue_task_credential(grant.secret, now=at(42),
                                      **_issue_kwargs(policy))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_when_session_expired(service, grant,
                                             authorization, db_path):
    long_policy = authorization.create_policy(
        grant.secret, now=at(30),
        **scope_kwargs(not_before=at(0), not_after=at(7200)))
    # The session expires at at(3620); the policy is still valid, so
    # only the expired session can refuse the issuance.
    with pytest.raises(SessionExpired):
        service.issue_task_credential(grant.secret, now=at(3620),
                                      **_issue_kwargs(long_policy))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_after_member_revocation(service, grant, policy,
                                                identity, db_path):
    identity.revoke_member("user-syn-owner", now=at(40))
    with pytest.raises(MembershipRevoked):
        service.issue_task_credential(grant.secret, now=at(41),
                                      **_issue_kwargs(policy))
    _assert_no_credential_rows(db_path)


def test_issue_requires_exact_current_revision(service, grant, policy,
                                               db_path):
    with pytest.raises(TaskPolicyStale):
        service.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy,
                                                      expected_revision=2))
    with pytest.raises(TypeError):
        service.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy,
                                                      expected_revision=True))
    with pytest.raises(ValueError):
        service.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy,
                                                      expected_revision=0))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_for_paused_or_revoked_policy(
        service, grant, policy, authorization, db_path):
    authorization.pause_policy(grant.secret, now=at(50),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    with pytest.raises(TaskPolicyInactive):
        service.issue_task_credential(grant.secret, now=at(51),
                                      **_issue_kwargs(policy,
                                                      expected_revision=2))
    authorization.revoke_policy(grant.secret, now=at(52),
                                policy_id=policy.policy_id,
                                expected_revision=2)
    with pytest.raises(TaskPolicyInactive):
        service.issue_task_credential(grant.secret, now=at(53),
                                      **_issue_kwargs(policy,
                                                      expected_revision=3))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_for_expired_policy(service, grant, policy,
                                           db_path):
    # The policy validity ends at at(3600); the session is still valid
    # until at(3620), so only the expired policy can refuse here.
    with pytest.raises(TaskPolicyExpired):
        service.issue_task_credential(grant.secret, now=at(3600),
                                      **_issue_kwargs(policy))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_before_policy_validity_starts(
        service, grant, authorization, db_path):
    future_policy = authorization.create_policy(
        grant.secret, now=at(30),
        **scope_kwargs(not_before=at(1000), not_after=at(4000)))
    with pytest.raises(TaskPolicyNotYetValid):
        service.issue_task_credential(
            grant.secret, now=at(40),
            **_issue_kwargs(future_policy))
    _assert_no_credential_rows(db_path)


def test_issue_rejected_for_unknown_policy(service, grant, db_path):
    with pytest.raises(PolicyNotFound):
        service.issue_task_credential(grant.secret, now=at(40),
                                      policy_id="hap_deadbeefcafe0000",
                                      expected_revision=1)
    _assert_no_credential_rows(db_path)


def test_issue_rejects_invalid_boundary_inputs(service, grant, policy):
    with pytest.raises(TypeError):
        service.issue_task_credential(grant.secret, now="at(40)",
                                      **_issue_kwargs(policy))
    with pytest.raises(ValueError):
        service.issue_task_credential(
            grant.secret,
            now=datetime(2026, 10, 24, 12, 0, 40),  # naive
            **_issue_kwargs(policy))
    with pytest.raises(TypeError):
        service.issue_task_credential(object(), now=at(40),
                                      **_issue_kwargs(policy))
    with pytest.raises(ValueError):
        service.issue_task_credential("", now=at(40),
                                      **_issue_kwargs(policy))
    with pytest.raises(ValueError):
        service.issue_task_credential(grant.secret, now=at(40),
                                      policy_id="",
                                      expected_revision=1)
    with pytest.raises(ValueError):
        service.issue_task_credential(grant.secret, now=at(40),
                                      policy_id=5,
                                      expected_revision=1)


def test_issue_refuses_inside_caller_transaction(service, grant,
                                                 policy):
    service.db.execute("BEGIN")
    try:
        with pytest.raises(ValueError):
            service.issue_task_credential(grant.secret, now=at(40),
                                          **_issue_kwargs(policy))
    finally:
        service.db.rollback()


# -- issuance: scope can only narrow ---------------------------------------


def test_narrowed_scope_is_stored(service, grant, authorization):
    rich_policy = authorization.create_policy(
        grant.secret, now=at(30), **rich_scope_kwargs())
    issued = service.issue_task_credential(
        grant.secret, now=at(40),
        **_issue_kwargs(
            rich_policy,
            trigger_conditions=["on_upload_completed"],
            actions=["read"],
            fields=["title"],
            recipients=["board"],
            egress=None,
            not_before=at(60),
            not_after=at(1800),
            budget={"requests": 50, "tokens": 1000},
            max_delegation_depth=0,
            max_concurrency=1,
            max_retries=0))
    scope = issued.context.scope
    # Target, plan version and stop conditions always come from the
    # policy; every requested dimension was narrowed and stored.
    assert scope.target == "uploads-inbox"
    assert scope.plan_version == "plan-v1"
    assert scope.stop_conditions == ("on_owner_stop",)
    assert scope.trigger_conditions == ("on_upload_completed",)
    assert scope.actions == ("read",)
    assert scope.fields == ("title",)
    assert scope.objects == ("sam://original/*",)
    assert scope.recipients == ("board",)
    assert scope.egress is None
    assert scope.not_before == at(60)
    assert scope.not_after == at(1800)
    assert scope.budget == (("requests", 50), ("tokens", 1000))
    assert scope.max_delegation_depth == 0
    assert scope.max_concurrency == 1
    assert scope.max_retries == 0
    assert issued.expires_at == at(1800)
    # The stored cap round-trips byte-identically through the ledger's
    # canonical scope JSON.
    stored = _scalar(_path_of(service),
                     "SELECT scope_json FROM host_task_credentials")
    assert stored == _dumps_scope(scope)


def test_issue_rejects_any_widening_of_the_policy_scope(
        service, grant, authorization, db_path):
    rich_policy = authorization.create_policy(
        grant.secret, now=at(30), **rich_scope_kwargs())
    wider_requests = (
        {"trigger_conditions": ["on_upload_completed", "on_timer"]},
        {"actions": ["read", "extract", "classify", "send"]},
        {"objects": ["sam://original/*", "sam://kv/*"]},
        {"fields": ["title", "date", "amount", "secret_field"]},
        {"recipients": ["board", "founders", "outsider"]},
        {"read_window": "all-history"},
        {"egress": "other-channel"},
        {"stop_conditions": []},
        {"stop_conditions": ["on_owner_stop", "on_budget_hit"]},
        {"not_before": at(-10)},
        {"not_after": at(7200)},
        {"budget": {"requests": 101, "tokens": 5000}},
        {"budget": {"requests": 1, "gpu_hours": 1}},
        {"max_delegation_depth": 2},
        {"max_concurrency": 3},
        {"max_retries": 4},
    )
    for wider in wider_requests:
        with pytest.raises(TaskScopeDenied):
            service.issue_task_credential(
                grant.secret, now=at(40),
                **_issue_kwargs(rich_policy, **wider))
    # A policy without egress can never gain one through a credential.
    plain_policy = authorization.create_policy(
        grant.secret, now=at(31), **scope_kwargs())
    with pytest.raises(TaskScopeDenied):
        service.issue_task_credential(
            grant.secret, now=at(40),
            **_issue_kwargs(plain_policy, egress="model-house"))
    _assert_no_credential_rows(db_path)


def test_issue_rejects_validity_window_already_passed(
        service, grant, policy, db_path):
    with pytest.raises(TaskScopeDenied):
        service.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy,
                                                      not_after=at(39)))
    with pytest.raises(ValueError):
        service.issue_task_credential(
            grant.secret, now=at(40),
            **_issue_kwargs(policy, not_before=at(200),
                            not_after=at(100)))
    _assert_no_credential_rows(db_path)


def test_egress_may_stay_or_narrow_to_none(service, grant,
                                           authorization):
    rich_policy = authorization.create_policy(
        grant.secret, now=at(30), **rich_scope_kwargs())
    same = service.issue_task_credential(grant.secret, now=at(40),
                                         **_issue_kwargs(rich_policy,
                                                         egress="model-house"))
    assert same.context.scope.egress == "model-house"
    none = service.issue_task_credential(grant.secret, now=at(41),
                                         **_issue_kwargs(rich_policy,
                                                         egress=None))
    assert none.context.scope.egress is None


# -- secrets hygiene --------------------------------------------------------


def test_raw_secret_returned_once_and_never_stored(cred, db_path):
    secret = cred.secret
    assert isinstance(secret, str)
    assert len(secret) >= 40
    assert secret not in repr(cred)
    assert secret not in repr(cred.context)
    assert secret not in str(cred.context)
    # Only the domain-separated SHA-256 digest is persisted.
    assert _scalar(db_path, "SELECT credential_sha256 "
                            "FROM host_task_credentials") \
        == _credential_digest(secret)
    conn = _connect(db_path)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")]
        assert tables
        for table in tables:
            for row in conn.execute("SELECT * FROM " + table):
                for value in row:
                    assert value != secret
                    assert secret not in str(value)
    finally:
        conn.close()


def test_credential_secret_is_not_the_session_secret(cred, grant,
                                                     db_path):
    assert cred.secret != grant.secret
    assert _credential_digest(cred.secret) \
        != _credential_digest(grant.secret)
    # The session secret (nor its digest form used by the identity
    # tables) is never copied into the credential tables.
    for table in ("host_task_credentials",
                  "host_task_credential_events"):
        for row in _rows(db_path, "SELECT * FROM " + table):
            for value in row:
                assert value != grant.secret
                assert grant.secret not in str(value)


def test_session_secret_is_not_persisted_by_revocation(
        service, grant, cred, db_path):
    assert service.revoke_task_credential(grant.secret, now=at(50),
                                          task_id=cred.task_id) is True
    for table in ("host_task_credentials",
                  "host_task_credential_events",
                  "host_task_credentials_ledger_state"):
        for row in _rows(db_path, "SELECT * FROM " + table):
            for value in row:
                assert value != grant.secret
                assert grant.secret not in str(value)


def test_error_messages_never_leak_secrets(service, grant, policy,
                                           cred):
    with pytest.raises(TaskScopeDenied) as denied:
        service.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy,
                                                      actions=["fly"]))
    assert grant.secret not in str(denied.value)
    assert cred.secret not in str(denied.value)
    with pytest.raises(TaskCredentialInvalid) as invalid:
        service.resolve_task_credential("unknown-secret", now=at(45))
    assert "unknown-secret" not in str(invalid.value)
    service.revoke_task_credential(grant.secret, now=at(50),
                                   task_id=cred.task_id)
    with pytest.raises(TaskCredentialRevoked) as revoked:
        service.resolve_task_credential(cred.secret, now=at(51))
    assert cred.secret not in str(revoked.value)
    assert grant.secret not in str(revoked.value)


# -- resolution: current state is re-verified every call ---------------------


def test_resolve_returns_bound_context(service, cred):
    ctx = service.resolve_task_credential(cred.secret, now=at(50))
    assert isinstance(ctx, TaskRunContext)
    assert ctx.task_id == cred.task_id
    assert ctx.run_id == cred.run_id
    assert ctx.installation_id == "inst-syn-1"
    assert ctx.company_id == "co-syn-1"
    assert ctx.principal_id == "user-syn-owner"
    assert ctx.policy_revision == 1
    assert ctx.scope == cred.context.scope
    assert ctx.resolved_at == at(50)
    for value in vars(ctx).values():
        assert cred.secret not in str(value)


def test_resolve_rejects_unknown_or_malformed_secret(service):
    with pytest.raises(TaskCredentialInvalid):
        service.resolve_task_credential("forged-credential", now=at(50))
    with pytest.raises(TypeError):
        service.resolve_task_credential(object(), now=at(50))
    with pytest.raises(ValueError):
        service.resolve_task_credential("", now=at(50))


def test_resolve_fails_after_policy_pause_or_revoke(
        service, cred, grant, policy, authorization):
    authorization.pause_policy(grant.secret, now=at(50),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    with pytest.raises(TaskPolicyStale):
        service.resolve_task_credential(cred.secret, now=at(51))
    authorization.revoke_policy(grant.secret, now=at(52),
                                policy_id=policy.policy_id,
                                expected_revision=2)
    with pytest.raises(TaskPolicyStale):
        service.resolve_task_credential(cred.secret, now=at(53))


def test_resolve_fails_after_revision_change_even_when_active_again(
        service, cred, grant, policy, authorization):
    # pause -> resume leaves the policy active, but on a new revision:
    # the credential stays bound to the exact revision it was issued
    # against and never silently follows the new version.
    authorization.pause_policy(grant.secret, now=at(50),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    authorization.resume_policy(grant.secret, now=at(51),
                                policy_id=policy.policy_id,
                                expected_revision=2)
    current = authorization.get_policy(grant.secret, now=at(52),
                                       policy_id=policy.policy_id)
    assert (current.revision, current.status) == (3, "active")
    with pytest.raises(TaskPolicyStale):
        service.resolve_task_credential(cred.secret, now=at(53))


def test_resolve_fails_after_policy_expiry(service, cred):
    # The policy validity ends at at(3600); the credential cap ends
    # there too, and the policy check comes first.
    with pytest.raises(TaskPolicyExpired):
        service.resolve_task_credential(cred.secret, now=at(3600))


def test_resolve_fails_after_member_revocation(service, cred, identity):
    identity.revoke_member("user-syn-owner", now=at(50))
    with pytest.raises(TaskMembershipRevoked):
        service.resolve_task_credential(cred.secret, now=at(51))


def test_resolve_enforces_credential_validity_window(
        service, grant, policy):
    windowed = service.issue_task_credential(
        grant.secret, now=at(40),
        **_issue_kwargs(policy, not_before=at(100), not_after=at(200)))
    with pytest.raises(TaskCredentialNotYetValid):
        service.resolve_task_credential(windowed.secret, now=at(99))
    ctx = service.resolve_task_credential(windowed.secret, now=at(150))
    assert ctx.scope.not_before == at(100)
    with pytest.raises(TaskCredentialExpired):
        service.resolve_task_credential(windowed.secret, now=at(200))


def test_second_service_instance_shares_state_without_cache(
        service, identity, authorization, grant, cred):
    second = HostTaskCredentialService(identity.db, identity,
                                       authorization)
    ctx = second.resolve_task_credential(cred.secret, now=at(50))
    assert ctx.task_id == cred.task_id
    # A revocation through the second instance is immediately visible
    # to the first: state lives in the database, not in any instance.
    assert second.revoke_task_credential(grant.secret, now=at(51),
                                         task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        service.resolve_task_credential(cred.secret, now=at(52))


# -- revocation ---------------------------------------------------------------


def test_revoke_is_authenticated_idempotent_and_auditable(
        service, grant, cred, db_path):
    assert service.revoke_task_credential(grant.secret, now=at(50),
                                          task_id=cred.task_id) is True
    rows = _rows(db_path,
                 "SELECT task_id, event_seq, event, revocation_seq, "
                 "actor_principal_id, actor_member_revision, "
                 "session_ref FROM host_task_credential_events "
                 "ORDER BY event_seq")
    assert rows == [(cred.task_id, 1, "issued", 0, "user-syn-owner", 1,
                     grant.session_ref),
                    (cred.task_id, 2, "revoked", 1, "user-syn-owner", 1,
                     grant.session_ref)]
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 1
    assert _scalar(db_path, "SELECT changed_at FROM "
                            "host_task_credential_events "
                            "WHERE event = 'revoked'") >= at(50).isoformat()
    # Replay of the revoked credential keeps failing closed.
    with pytest.raises(TaskCredentialRevoked):
        service.resolve_task_credential(cred.secret, now=at(51))
    # Repeat revocation reports the current state and consumes no
    # further sequence number.
    assert service.revoke_task_credential(grant.secret, now=at(52),
                                          task_id=cred.task_id) is False
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 1
    assert _scalar(db_path, "SELECT COUNT(*) "
                            "FROM host_task_credential_events") == 2
    with pytest.raises(TaskCredentialRevoked):
        service.resolve_task_credential(cred.secret, now=at(53))


def test_revoke_requires_valid_operator_session(service, grant, cred,
                                                identity, db_path):
    with pytest.raises(Unauthenticated):
        service.revoke_task_credential("forged-secret", now=at(50),
                                       task_id=cred.task_id)
    identity.revoke_session(grant.session_ref, now=at(51))
    with pytest.raises(SessionRevoked):
        service.revoke_task_credential(grant.secret, now=at(52),
                                       task_id=cred.task_id)
    # The credential was not touched by the refused revocations.
    assert _scalar(db_path, "SELECT COUNT(*) "
                            "FROM host_task_credential_events") == 1
    ctx = service.resolve_task_credential(cred.secret, now=at(53))
    assert ctx.task_id == cred.task_id


def test_revoke_after_member_revocation_fails_closed(
        service, grant, cred, identity, db_path):
    identity.revoke_member("user-syn-owner", now=at(50))
    with pytest.raises(MembershipRevoked):
        service.revoke_task_credential(grant.secret, now=at(51),
                                       task_id=cred.task_id)
    assert _scalar(db_path, "SELECT COUNT(*) "
                            "FROM host_task_credential_events") == 1
    with pytest.raises(TaskMembershipRevoked):
        service.resolve_task_credential(cred.secret, now=at(52))


def test_revoke_unknown_or_malformed_task(service, grant, cred):
    with pytest.raises(UnknownTaskCredential):
        service.revoke_task_credential(grant.secret, now=at(50),
                                       task_id="htk_doesnotexist00")
    with pytest.raises(ValueError):
        service.revoke_task_credential(grant.secret, now=at(50),
                                       task_id="")
    with pytest.raises(ValueError):
        service.revoke_task_credential(grant.secret, now=at(50),
                                       task_id=5)
    # The known credential is untouched.
    ctx = service.resolve_task_credential(cred.secret, now=at(51))
    assert ctx.task_id == cred.task_id


def test_revocation_sequence_increments_once_per_revocation(
        service, grant, policy, db_path):
    first = service.issue_task_credential(grant.secret, now=at(40),
                                          **_issue_kwargs(policy))
    second = service.issue_task_credential(grant.secret, now=at(41),
                                           **_issue_kwargs(policy))
    assert service.revoke_task_credential(grant.secret, now=at(50),
                                          task_id=first.task_id) is True
    assert service.revoke_task_credential(grant.secret, now=at(51),
                                          task_id=second.task_id) is True
    assert service.revoke_task_credential(grant.secret, now=at(52),
                                          task_id=first.task_id) is False
    assert _rows(db_path,
                 "SELECT task_id, revocation_seq "
                 "FROM host_task_credential_events "
                 "WHERE event = 'revoked' ORDER BY revocation_seq") \
        == [(first.task_id, 1), (second.task_id, 2)]
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 2


# -- cross-company and forged-state rejection ---------------------------------


def test_resolve_rejects_credential_of_another_company(service, policy,
                                                       db_path):
    _forge_credential_row(db_path, secret="forged-other-company-secret",
                          task_id="htk_forgedco000001",
                          company_id="co-other",
                          policy_id=policy.policy_id)
    with pytest.raises(TaskBindingMismatch):
        service.resolve_task_credential("forged-other-company-secret",
                                        now=at(50))


def test_revoke_rejects_credential_of_another_company(service, grant,
                                                      policy, db_path):
    _forge_credential_row(db_path, secret="forged-other-company-secret",
                          task_id="htk_forgedco000002",
                          company_id="co-other",
                          policy_id=policy.policy_id)
    with pytest.raises(TaskBindingMismatch):
        service.revoke_task_credential(grant.secret, now=at(50),
                                       task_id="htk_forgedco000002")
    assert _rows(db_path, "SELECT event FROM host_task_credential_events "
                          "WHERE task_id = 'htk_forgedco000002'") \
        == [("issued",)]


def test_resolve_rejects_credential_forged_against_another_installation(
        service, policy, db_path):
    _forge_credential_row(db_path, secret="forged-other-inst-secret",
                          task_id="htk_forgedinst0001",
                          installation_id="inst-other",
                          policy_id=policy.policy_id)
    with pytest.raises(TaskBindingMismatch):
        service.resolve_task_credential("forged-other-inst-secret",
                                        now=at(50))


def test_credential_from_another_database_is_unknown(tmp_path):
    # Two fully independent synthetic stacks: a credential issued in
    # one database is simply not recognized in the other.
    other_path = tmp_path / "other-installation.db"
    other_conn = _connect(other_path)
    other_identity = HostIdentityService(other_conn)
    other_identity.initialize_installation(
        installation_id="inst-syn-2", company_id="co-syn-2",
        owner_principal_id="user-syn-owner-2", owner_role="owner",
        now=T0)
    other_authorization = HostAuthorizationService(other_conn,
                                                   other_identity)
    other_service = HostTaskCredentialService(other_conn,
                                              other_identity,
                                              other_authorization)
    db_path = tmp_path / "host-task-credentials.db"
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    identity.initialize_installation(
        installation_id="inst-syn-1", company_id="co-syn-1",
        owner_principal_id="user-syn-owner", owner_role="owner", now=T0)
    authorization = HostAuthorizationService(conn, identity)
    service = HostTaskCredentialService(conn, identity, authorization)
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20),
                          session_lifetime_seconds=3600)
    policy = authorization.create_policy(grant.secret, now=at(30),
                                         **scope_kwargs())
    cred = service.issue_task_credential(grant.secret, now=at(40),
                                         policy_id=policy.policy_id,
                                         expected_revision=1)
    with pytest.raises(TaskCredentialInvalid):
        other_service.resolve_task_credential(cred.secret, now=at(50))
    # The context still resolves in its own stack.
    ctx = service.resolve_task_credential(cred.secret, now=at(50))
    assert ctx.company_id == "co-syn-1"
    conn.close()
    other_conn.close()


def test_resolve_rejects_credential_with_missing_policy(
        service, db_path):
    # A forged row referencing a policy that never existed must fail
    # closed instead of resolving on the stored claims alone.
    _forge_credential_row(db_path, secret="forged-no-policy-secret",
                          task_id="htk_forgedpol00001",
                          policy_id="hap_neverexisted01")
    with pytest.raises(ValueError):
        service.resolve_task_credential("forged-no-policy-secret",
                                        now=at(50))


# -- resolution: stored scope re-validated against the current policy --------


def test_resolve_rejects_forged_row_with_wider_actions(
        service, policy, db_path):
    # Regression for the independently reproduced fail-closed gap: a
    # row with the same active installation, company and principal, an
    # existing policy and its exact current revision, plus a valid
    # issued event, but a syntactically valid scope carrying an extra
    # "admin_write" action beyond the policy's ["read", "extract"].
    # Policy-revision equality alone used to accept this; resolution
    # must fail closed on the wider stored scope.
    wider = _build_scope(**scope_kwargs(
        actions=["read", "extract", "admin_write"]))
    _forge_credential_row(db_path, secret="forged-wide-actions-secret",
                          task_id="htk_forgedwideact1",
                          policy_id=policy.policy_id,
                          policy_revision=1, scope=wider)
    with pytest.raises(TaskScopeDenied):
        service.resolve_task_credential("forged-wide-actions-secret",
                                        now=at(50))


def test_resolve_rejects_forged_rows_widening_any_scope_dimension(
        service, policy, db_path):
    # Each forged row pins the same installation/company, the same
    # principal, the real policy and its exact revision 1, and widens
    # exactly one independent scope dimension beyond the policy's.
    # Every one of them must fail closed on resolution.
    wider_variants = (
        {"actions": ["read", "extract", "admin_write"]},
        {"objects": ["sam://original/*", "sam://kv/*"]},
        {"fields": ["title", "date", "secret_field"]},
        {"trigger_conditions": ["on_upload_completed", "on_timer"]},
        {"stop_conditions": ["on_owner_stop", "on_budget_hit"]},
        {"recipients": ["board"]},
        {"egress": "model-house"},
        {"read_window": "all-history"},
        {"target": "other-target"},
        {"plan_version": "other-plan"},
        {"not_before": at(-10)},
        {"not_after": at(7200)},
        {"budget": {"requests": 101}},
        {"budget": {"requests": 100, "gpu_hours": 1}},
        {"max_delegation_depth": 2},
        {"max_concurrency": 3},
        {"max_retries": 4},
    )
    for index, variant in enumerate(wider_variants):
        scope = _build_scope(**scope_kwargs(**variant))
        secret = "forged-wide-dimension-secret-%d" % index
        _forge_credential_row(db_path, secret=secret,
                              task_id="htk_forgedwide%04d" % index,
                              policy_id=policy.policy_id,
                              policy_revision=1, scope=scope)
        with pytest.raises(TaskScopeDenied):
            service.resolve_task_credential(secret, now=at(50))


def test_resolve_accepts_stored_cap_narrower_than_the_policy(
        service, policy, db_path):
    # The revalidation must not false-positive on legitimate narrowing:
    # a stored cap strictly inside the policy scope still resolves, and
    # the issuance-time cap is returned unchanged - never widened back
    # to the policy scope.
    narrowed = _build_scope(**scope_kwargs(
        actions=["read"], fields=["title"], not_before=at(10),
        not_after=at(1800), budget={"requests": 50},
        max_delegation_depth=0, max_concurrency=1, max_retries=0))
    _forge_credential_row(db_path, secret="forged-narrow-scope-secret",
                          task_id="htk_forgednarrow01",
                          policy_id=policy.policy_id,
                          policy_revision=1, scope=narrowed)
    ctx = service.resolve_task_credential("forged-narrow-scope-secret",
                                          now=at(50))
    assert ctx.scope == narrowed
    assert ctx.scope.actions == ("read",)
    assert ctx.scope != policy.scope


def test_resolve_preserves_issuance_time_cap_across_dimensions(
        service, grant, authorization):
    # End-to-end through the real issuance path: a credential narrowed
    # in several independent dimensions keeps its issuance-time cap on
    # resolution; the wider policy scope never flows back in.
    rich_policy = authorization.create_policy(
        grant.secret, now=at(30), **rich_scope_kwargs())
    issued = service.issue_task_credential(
        grant.secret, now=at(40),
        **_issue_kwargs(rich_policy, actions=["read"],
                        recipients=["board"], egress=None,
                        budget={"requests": 50}, max_concurrency=1))
    ctx = service.resolve_task_credential(issued.secret, now=at(50))
    assert ctx.scope == issued.context.scope
    assert ctx.scope.actions == ("read",)
    assert ctx.scope.recipients == ("board",)
    assert ctx.scope.egress is None
    assert ctx.scope.budget == (("requests", 50),)
    assert ctx.scope.max_concurrency == 1
    assert ctx.scope != rich_policy.scope


# -- append-only persistence ---------------------------------------------------


def test_credential_rows_reject_update_delete_and_replace(
        service, cred, db_path):
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_task_credentials "
                         "SET policy_revision = 99")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_task_credentials "
                         "SET company_id = 'co-other'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_task_credentials")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_task_credentials "
                "(task_id, run_id, credential_sha256, parent_task_id, "
                "installation_id, company_id, principal_id, "
                "member_revision, issued_by_session_ref, policy_id, "
                "policy_revision, scope_json, issued_at) "
                "VALUES (?, 'htr_replaced000001', ?, NULL, "
                "'inst-syn-1', 'co-other', 'attacker', 1, "
                "'hss_replaced000001', 'hap_replaced000001', 9, '{}', "
                "?)",
                (cred.task_id, "0" * 64, at(999).isoformat()))
    finally:
        conn.close()
    # The guards fired before any change: the stored row is intact and
    # still resolves.
    ctx = service.resolve_task_credential(cred.secret, now=at(60))
    assert ctx.company_id == "co-syn-1"
    assert ctx.policy_revision == 1


def test_credential_row_cannot_carry_a_parent(service, cred, db_path):
    # Root tasks only: the schema itself refuses a non-NULL parent.
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_task_credentials "
                "(task_id, run_id, credential_sha256, parent_task_id, "
                "installation_id, company_id, principal_id, "
                "member_revision, issued_by_session_ref, policy_id, "
                "policy_revision, scope_json, issued_at) "
                "VALUES ('htk_child000000001', 'htr_child000000001', "
                "?, ?, 'inst-syn-1', 'co-syn-1', 'user-syn-owner', 1, "
                "'hss_forged00000001', ?, 1, '{}', ?)",
                ("1" * 64, cred.task_id, cred.context.policy_id,
                 at(30).isoformat()))
    finally:
        conn.close()
    assert _scalar(db_path, "SELECT COUNT(*) "
                            "FROM host_task_credentials") == 1


def test_event_history_rejects_rewrite_and_forged_shape(
        service, grant, cred, db_path):
    service.revoke_task_credential(grant.secret, now=at(50),
                                   task_id=cred.task_id)
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_task_credential_events "
                         "SET event = 'issued' WHERE event_seq = 2")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_task_credential_events "
                         "WHERE event_seq = 2")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_task_credential_events")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, 2, 'issued', 0, 'attacker', 1, "
                "'hss_forged00000001', ?)",
                (cred.task_id, at(999).isoformat()))
        # A second issuance event for the same task is refused.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, 3, 'issued', 0, 'attacker', 1, "
                "'hss_forged00000001', ?)",
                (cred.task_id, at(999).isoformat()))
        # A second revocation is refused.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, 3, 'revoked', 9, 'attacker', 1, "
                "'hss_forged00000001', ?)",
                (cred.task_id, at(999).isoformat()))
        # A revocation without an issuance is refused.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES ('htk_ghost000000001', 1, 'revoked', 9, "
                "'attacker', 1, 'hss_forged00000001', ?)",
                (at(999).isoformat(),))
        # Unknown event names are refused by the CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES ('htk_ghost000000002', 1, 'consumed', 0, "
                "'attacker', 1, 'hss_forged00000001', ?)",
                (at(999).isoformat(),))
    finally:
        conn.close()
    assert _rows(db_path, "SELECT event_seq, event, revocation_seq "
                          "FROM host_task_credential_events "
                          "ORDER BY event_seq") == [(1, "issued", 0),
                                                    (2, "revoked", 1)]


def test_ledger_sequence_cannot_be_reset_or_deleted(service, grant,
                                                    cred, db_path):
    service.revoke_task_credential(grant.secret, now=at(50),
                                   task_id=cred.task_id)
    conn = _connect(db_path)
    try:
        # Rewrite to the same value, a decrease and a skip all fail;
        # only +1 through the service is legal.
        for value in (1, 0, 3):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE host_task_credentials_ledger_state "
                             "SET revocation_seq = ?", (value,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM "
                         "host_task_credentials_ledger_state")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO "
                         "host_task_credentials_ledger_state "
                         "(singleton, revocation_seq) VALUES (1, 0)")
    finally:
        conn.close()
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 1


# -- transaction atomicity ------------------------------------------------------


class _FailOn(sqlite3.Connection):
    """Fault-injection connection failing one chosen SQL statement."""

    needle = ""

    def execute(self, sql, parameters=()):
        if self.needle and isinstance(sql, str) \
                and self.needle in sql:
            raise RuntimeError("injected failure")
        return super().execute(sql, parameters)


def _reopen_failing(db_path):
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    return conn, HostTaskCredentialService(conn, identity,
                                           authorization)


def test_failed_issue_rolls_back_completely(identity, grant, policy,
                                            db_path):
    identity.db.close()
    conn, failing = _reopen_failing(db_path)
    conn.needle = "INSERT INTO host_task_credential_events"
    with pytest.raises(RuntimeError):
        failing.issue_task_credential(grant.secret, now=at(40),
                                      **_issue_kwargs(policy))
    conn.close()
    # No credential row without its issuance event, and no event at all.
    _assert_no_credential_rows(db_path)


def test_failed_revoke_rolls_back_completely(identity, grant, cred,
                                             db_path):
    identity.db.close()
    conn, failing = _reopen_failing(db_path)
    conn.needle = "UPDATE host_task_credentials_ledger_state"
    with pytest.raises(RuntimeError):
        failing.revoke_task_credential(grant.secret, now=at(50),
                                       task_id=cred.task_id)
    conn.close()
    # The credential is untouched: still exactly one issued event, the
    # sequence is still 0, and the credential still resolves.
    assert _rows(db_path, "SELECT event_seq, event, revocation_seq "
                          "FROM host_task_credential_events") \
        == [(1, "issued", 0)]
    assert _scalar(db_path, "SELECT revocation_seq FROM "
                            "host_task_credentials_ledger_state") == 0
    conn2 = _connect(db_path)
    try:
        identity2 = HostIdentityService(conn2)
        authorization2 = HostAuthorizationService(conn2, identity2)
        service2 = HostTaskCredentialService(conn2, identity2,
                                             authorization2)
        ctx = service2.resolve_task_credential(cred.secret, now=at(51))
        assert ctx.task_id == cred.task_id
    finally:
        conn2.close()


# -- write-lock wait expiry races ------------------------------------------------


class _LockWaitClock:
    """Deterministic monotonic-ns source simulating a write-lock wait,
    matching the pattern used by the authorization ledger tests."""

    def __init__(self, wait_microseconds):
        self._t = 1_000_000_000_000
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
    identity.db.close()
    clock = _LockWaitClock(wait_microseconds)
    conn = sqlite3.connect(db_path, factory=_WaitOnBegin)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.clock = clock
    monkeypatch.setattr("app.host_task_credentials._monotonic_ns",
                        clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    service = HostTaskCredentialService(conn, new_identity,
                                        authorization)
    return service


def test_issue_rejected_when_lock_wait_crosses_policy_expiry(
        identity, grant, policy, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                               monkeypatch)
    # Narrowest crossing: the policy validity ends at at(3600) and the
    # call enters exactly one microsecond before that boundary. The
    # simulated 2us write-lock wait moves the in-transaction effective
    # time one microsecond past not_after, so the issuance must be
    # refused at microsecond resolution instead of passing on the
    # stale pre-lock time.
    with pytest.raises(TaskPolicyExpired):
        service.issue_task_credential(
            grant.secret,
            now=at(3600) - timedelta(microseconds=1),
            **_issue_kwargs(policy))
    service.db.close()
    _assert_no_credential_rows(db_path)


def test_issue_rejected_when_lock_wait_crosses_session_expiry(
        identity, grant, authorization, db_path, monkeypatch):
    long_policy = authorization.create_policy(
        grant.secret, now=at(30),
        **scope_kwargs(not_before=at(0), not_after=at(7200)))
    service = _reopen_with_simulated_lock_wait(identity, db_path,
                                               120_000_000, monkeypatch)
    # Entering at at(3600) is valid for both session and policy, but
    # the simulated 120s write-lock wait crosses the session expiry at
    # at(3620): the in-transaction effective time must refuse.
    with pytest.raises(SessionExpired):
        service.issue_task_credential(grant.secret, now=at(3600),
                                      **_issue_kwargs(long_policy))
    service.db.close()
    _assert_no_credential_rows(db_path)


# -- isolation contract ----------------------------------------------------------


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
    authorization = HostAuthorizationService(conn, identity)
    service = HostTaskCredentialService(conn, identity, authorization)
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20),
                          session_lifetime_seconds=3600)
    policy = authorization.create_policy(grant.secret, now=at(30),
                                         **scope_kwargs())
    cred = service.issue_task_credential(grant.secret, now=at(40),
                                         policy_id=policy.policy_id,
                                         expected_revision=1)
    ctx = service.resolve_task_credential(cred.secret, now=at(50))
    assert ctx.company_id == "co-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()
