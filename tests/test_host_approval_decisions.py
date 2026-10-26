"""Tests for app.host_approval_decisions (F-007 one-time approval/denial
decision ledger).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data, no models and no Git access.
Identity, policy, task and preparation state are built synthetically
through the real HostIdentityService pairing flow, the real
HostAuthorizationService ledger, the real HostTaskCredentialService
issuance path and the real HostPreparedEffectService; the expected
digests are recomputed independently in this file, including the
domain-separation constant. Generic helpers (clock, connection, fault
injection, scope/effect templates, clean-chain assertion) are imported
from the sibling credential and prepared-effect tests instead of being
copied.
"""
import dataclasses
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import app.host_approval_decisions as decision_module
from app.facts_tree import git_format
from app.host_approval_decisions import (
    ApprovalDecision,
    ApprovalDecisionConflict,
    ApprovalDecisionExpired,
    ApprovalDecisionUnavailable,
    ApprovalOwnerRequired,
    ApprovalPreparationMismatch,
    ApprovalValidityDenied,
    HostApprovalDecisionService,
)
from app.host_authorization import HostAuthorizationService
from app.host_identity import (
    HostIdentityService,
    MembershipRevoked,
    SessionExpired,
    SessionRevoked,
    Unauthenticated,
)
from app.host_prepared_effects import HostPreparedEffectService
from app.host_task_credentials import (
    HostTaskCredentialService,
    TaskCredentialRevoked,
    TaskPolicyStale,
)
from tests.test_host_task_credentials import (  # reused pure helpers
    T0,
    _FailOn,
    _LockWaitClock,
    _WaitOnBegin,
    _connect,
    at,
)
from tests.test_host_prepared_effects import (  # reused pure helpers
    _assert_clean_chain,
    _rows,
    _scalar,
    _time_text,
    effect_kwargs,
    scope_kwargs,
)

# Domain-separation constant pinned independently of the module.
_ROW_DOMAIN = b"sam-host-approval-decision row-v1\x00"

EXPECTED_RECORD_KEYS = frozenset({
    "schema_version", "approval_id", "operation_id", "effect_digest",
    "preparation_digest", "installation_id", "company_id",
    "principal_id", "member_revision", "task_id", "run_id",
    "policy_id", "policy_revision", "session_ref", "decision",
    "created_at", "expires_at"})

_COLUMNS = ("approval_id, operation_id, task_id, run_id, effect_digest,"
            " preparation_digest, decision, created_at, expires_at,"
            " record_blob, row_sha256")


def decision_kwargs(effect, **overrides):
    kwargs = {
        "operation_id": effect.operation_id,
        "expected_effect_digest": effect.effect_digest,
        "expected_preparation_digest": effect.preparation_digest,
        "decision": "approved",
        "expires_at": at(300),
    }
    kwargs.update(overrides)
    return kwargs


def _approve(service, secret, effect, now=at(60), **overrides):
    return service.approve_effect(secret, now=now,
                                  **decision_kwargs(effect, **overrides))


def _decision_rows(db_path):
    return _rows(db_path,
                 "SELECT " + _COLUMNS
                 + " FROM host_approval_decisions ORDER BY created_at")


def _assert_no_decision_rows(db_path):
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 0


def _build_stack(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    prepared = HostPreparedEffectService(conn, identity, authorization,
                                         credentials)
    service = HostApprovalDecisionService(conn, identity, authorization,
                                          credentials, prepared)
    return conn, service


# -- forged stored rows (legal digests, illegal content) ------------------------


def _forged_record(ctx, session_ref, operation_id, approval_id,
                   **overrides):
    """Build a self-consistently hashed decision record the way an
    attacker with storage write access would: a legal row digest over
    illegal content. Read-back must still refuse these rows."""
    record = {
        "schema_version": 1,
        "approval_id": approval_id,
        "operation_id": operation_id,
        "effect_digest": "0" * 64,
        "preparation_digest": "1" * 64,
        "installation_id": ctx.installation_id,
        "company_id": ctx.company_id,
        "principal_id": ctx.principal_id,
        "member_revision": ctx.member_revision,
        "task_id": ctx.task_id,
        "run_id": ctx.run_id,
        "policy_id": ctx.policy_id,
        "policy_revision": ctx.policy_revision,
        "session_ref": session_ref,
        "decision": "approved",
        "created_at": _time_text(at(60)),
        "expires_at": _time_text(at(300)),
    }
    record.update(overrides)
    return record


def _insert_forged_row(db_path, record, *, canonical=True,
                       blob_override=None, row_sha256=None):
    if blob_override is not None:
        blob = blob_override
    elif canonical:
        blob = git_format.dumps_record(record)
    else:
        blob = (json.dumps(record, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
                + "\n").encode("utf-8")
    if row_sha256 is None:
        row_sha256 = hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_approval_decisions (" + _COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["approval_id"], record["operation_id"],
             record["task_id"], record["run_id"],
             record["effect_digest"], record["preparation_digest"],
             record["decision"], record["created_at"],
             record["expires_at"], blob, row_sha256))
        conn.commit()
    finally:
        conn.close()


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-approval-decisions.db"


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
def credentials(identity, authorization):
    return HostTaskCredentialService(identity.db, identity, authorization)


@pytest.fixture
def prepared(identity, authorization, credentials):
    return HostPreparedEffectService(identity.db, identity, authorization,
                                     credentials)


@pytest.fixture
def service(identity, authorization, credentials, prepared):
    return HostApprovalDecisionService(identity.db, identity,
                                       authorization, credentials,
                                       prepared)


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
def cred(credentials, grant, policy):
    return credentials.issue_task_credential(
        grant.secret, now=at(40), policy_id=policy.policy_id,
        expected_revision=1)


@pytest.fixture
def effect(prepared, cred):
    return prepared.prepare_effect(cred.secret, now=at(50),
                                   **effect_kwargs())


# -- constructor binding contract -------------------------------------------------


def test_constructor_requires_connection_and_bound_service_types(
        identity, authorization, credentials, prepared):
    conn = identity.db
    with pytest.raises(TypeError):
        HostApprovalDecisionService("host-approval-decisions.db",
                                    identity, authorization, credentials,
                                    prepared)
    with pytest.raises(TypeError):
        HostApprovalDecisionService(object(), identity, authorization,
                                    credentials, prepared)
    with pytest.raises(TypeError):
        HostApprovalDecisionService(conn, object(), authorization,
                                    credentials, prepared)
    with pytest.raises(TypeError):
        HostApprovalDecisionService(conn, identity, object(),
                                    credentials, prepared)
    with pytest.raises(TypeError):
        HostApprovalDecisionService(conn, identity, authorization,
                                    object(), prepared)
    with pytest.raises(TypeError):
        HostApprovalDecisionService(conn, identity, authorization,
                                    credentials, object())


def test_constructor_refuses_second_connection_to_same_file(
        identity, authorization, credentials, prepared, db_path):
    second = _connect(db_path)
    with pytest.raises(ValueError):
        HostApprovalDecisionService(second, identity, authorization,
                                    credentials, prepared)
    # The refusal happens before any DDL: no decision table or trigger
    # may appear through the rejected connection.
    assert {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'host_approval_decisions%'")} == set()
    second.close()


def test_constructor_refuses_divergent_service_instances(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    other_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    other_authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    prepared = HostPreparedEffectService(conn, identity, authorization,
                                         credentials)
    other_credentials = HostTaskCredentialService(conn, identity,
                                                  authorization)
    foreign_prepared = HostPreparedEffectService(conn, identity,
                                                 authorization,
                                                 other_credentials)
    with pytest.raises(ValueError):
        HostApprovalDecisionService(conn, other_identity, authorization,
                                    credentials, prepared)
    with pytest.raises(ValueError):
        HostApprovalDecisionService(conn, identity, other_authorization,
                                    credentials, prepared)
    with pytest.raises(ValueError):
        HostApprovalDecisionService(conn, identity, authorization,
                                    other_credentials, prepared)
    with pytest.raises(ValueError):
        HostApprovalDecisionService(conn, identity, authorization,
                                    credentials, foreign_prepared)
    conn.close()


def test_constructor_refuses_active_caller_transaction(
        identity, authorization, credentials, prepared):
    conn = identity.db
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostApprovalDecisionService(conn, identity, authorization,
                                    credentials, prepared)
    assert conn.in_transaction
    conn.rollback()


def test_constructor_ddl_is_idempotent_across_reconnect(db_path):
    conn, _ = _build_stack(db_path)
    conn.close()
    conn, _ = _build_stack(db_path)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "host_approval_decisions" in names
    conn.close()


# -- happy path --------------------------------------------------------------------


def test_approve_persists_single_verifiable_decision(service, grant,
                                                     effect, db_path):
    decision = _approve(service, grant.secret, effect)
    assert isinstance(decision, ApprovalDecision)
    assert decision.approval_id.startswith("had_")
    assert decision.decision == "approved"
    assert decision.operation_id == effect.operation_id
    assert decision.effect_digest == effect.effect_digest
    assert decision.preparation_digest == effect.preparation_digest
    assert at(60) <= decision.created_at < at(61)
    assert decision.expires_at == at(300)
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    (approval_id, operation_id, task_id, run_id, effect_digest,
     preparation_digest, stored_decision, created_at, expires_at, blob,
     row_sha256) = rows[0]
    assert approval_id == decision.approval_id
    assert operation_id == effect.operation_id
    assert (task_id, run_id) == (effect.envelope["task_id"],
                                 effect.envelope["run_id"])
    assert effect_digest == effect.effect_digest
    assert preparation_digest == effect.preparation_digest
    assert stored_decision == "approved"
    assert created_at == _time_text(decision.created_at)
    assert expires_at == _time_text(at(300))
    assert blob == decision.record_bytes
    assert row_sha256 == hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()
    # Persistence only: no consumption, reservation or execution state.
    names = {row[0] for row in _rows(
        db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not any("consum" in name or "reserv" in name
                   or "execut" in name for name in names)


def test_deny_persists_denied_decision(service, grant, effect, db_path):
    decision = _approve(service, grant.secret, effect, decision="denied")
    assert decision.decision == "denied"
    loaded = service.get_approval_decision(
        grant.secret, now=at(70), operation_id=effect.operation_id)
    assert loaded.decision == "denied"
    assert loaded.to_dict() == decision.to_dict()
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


def test_record_exact_fields_and_fixed_time_format(service, grant,
                                                   cred, effect):
    decision = _approve(service, grant.secret, effect)
    data = decision.to_dict()
    assert frozenset(data) == EXPECTED_RECORD_KEYS
    assert type(data["schema_version"]) is int
    assert data["schema_version"] == 1
    assert data["approval_id"] == decision.approval_id
    assert data["operation_id"] == effect.operation_id
    assert data["effect_digest"] == effect.effect_digest
    assert data["preparation_digest"] == effect.preparation_digest
    assert data["installation_id"] == "inst-syn-1"
    assert data["company_id"] == "co-syn-1"
    assert data["principal_id"] == "user-syn-owner"
    assert type(data["member_revision"]) is int
    assert data["member_revision"] == 1
    assert data["task_id"] == cred.task_id
    assert data["run_id"] == cred.run_id
    assert data["policy_id"] == cred.context.policy_id
    assert type(data["policy_revision"]) is int
    assert data["policy_revision"] == 1
    assert data["session_ref"] == grant.session_ref
    assert data["decision"] == "approved"
    for name in ("created_at", "expires_at"):
        text = data[name]
        assert text.endswith("+00:00")
        assert _time_text(datetime.fromisoformat(text)) == text
    assert data["created_at"] < data["expires_at"]


def test_custom_owner_role_still_accepted(tmp_path):
    # Owner eligibility is the installation binding, never the role
    # label: a custom owner_role text decides nothing.
    path = tmp_path / "custom-role.db"
    conn = _connect(path)
    try:
        identity = HostIdentityService(conn)
        identity.initialize_installation(
            installation_id="inst-syn-2", company_id="co-syn-2",
            owner_principal_id="user-syn-owner-2",
            owner_role="proprietor", now=T0)
        authorization = HostAuthorizationService(conn, identity)
        credentials = HostTaskCredentialService(conn, identity,
                                                authorization)
        prepared = HostPreparedEffectService(conn, identity,
                                             authorization, credentials)
        service = HostApprovalDecisionService(conn, identity,
                                              authorization, credentials,
                                              prepared)
        ticket = identity.issue_pairing_credential(now=at(10),
                                                   lifetime_seconds=600)
        grant = identity.pair(ticket.secret, now=at(20),
                              session_lifetime_seconds=3600)
        policy = authorization.create_policy(grant.secret, now=at(30),
                                             **scope_kwargs())
        cred = credentials.issue_task_credential(
            grant.secret, now=at(40), policy_id=policy.policy_id,
            expected_revision=1)
        effect = prepared.prepare_effect(cred.secret, now=at(50),
                                         **effect_kwargs())
        decision = _approve(service, grant.secret, effect)
        assert decision.to_dict()["principal_id"] == "user-syn-owner-2"
        assert decision.to_dict()["company_id"] == "co-syn-2"
    finally:
        conn.close()


def test_approve_get_and_reopen_preserves_history(service, grant,
                                                  effect, db_path):
    decision = _approve(service, grant.secret, effect)
    loaded = service.get_approval_decision(
        grant.secret, now=at(70), operation_id=effect.operation_id)
    assert loaded.to_dict() == decision.to_dict()
    assert loaded.record_bytes == decision.record_bytes
    service.db.close()
    conn, reopened = _build_stack(db_path)
    try:
        again = reopened.get_approval_decision(
            grant.secret, now=at(71), operation_id=effect.operation_id)
        assert again.to_dict() == decision.to_dict()
        # A replay after reconnect returns the original identifiers and
        # adds no row: reopening a connection never resets history.
        replayed = _approve(reopened, grant.secret, effect, now=at(72))
        assert replayed.approval_id == decision.approval_id
        assert replayed.created_at == decision.created_at
        assert _scalar(db_path,
                       "SELECT COUNT(*) FROM host_approval_decisions") == 1
    finally:
        conn.close()


def test_expires_may_equal_prepared_expiry(service, grant, effect):
    decision = _approve(service, grant.secret, effect, expires_at=at(600))
    assert decision.expires_at == at(600)


# -- idempotent replay and conflict -------------------------------------------------


def test_idempotent_replay_returns_original_without_new_row(
        service, grant, effect, db_path):
    first = _approve(service, grant.secret, effect)
    second = _approve(service, grant.secret, effect, now=at(65))
    assert second.approval_id == first.approval_id
    assert second.created_at == first.created_at
    assert second.to_dict() == first.to_dict()
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


def test_replay_with_new_session_same_owner_returns_first_record(
        service, identity, grant, effect, db_path):
    first = _approve(service, grant.secret, effect)
    # A second real pairing issues a second real session for the same
    # owner; the replay must be really authenticated but is never
    # compared to the stored session_ref.
    ticket = identity.issue_pairing_credential(now=at(65),
                                               lifetime_seconds=600)
    other = identity.pair(ticket.secret, now=at(66),
                          session_lifetime_seconds=3600)
    assert other.session_ref != grant.session_ref
    replayed = _approve(service, other.secret, effect, now=at(67))
    assert replayed.approval_id == first.approval_id
    assert replayed.created_at == first.created_at
    # The original audit record stands: the first session_ref is kept.
    assert replayed.to_dict()["session_ref"] == grant.session_ref
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1
    loaded = service.get_approval_decision(
        other.secret, now=at(68), operation_id=effect.operation_id)
    assert loaded.to_dict()["session_ref"] == grant.session_ref


def test_same_operation_opposite_decision_conflicts_and_never_overwrites(
        service, grant, effect, db_path):
    first = _approve(service, grant.secret, effect)
    original_blob = first.record_bytes
    with pytest.raises(ApprovalDecisionConflict) as excinfo:
        _approve(service, grant.secret, effect, decision="denied")
    assert excinfo.value.__context__ is None
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == first.approval_id
    assert rows[0][6] == "approved"
    assert rows[0][9] == original_blob


def test_same_operation_changed_expiry_conflicts(service, grant, effect,
                                                 db_path):
    _approve(service, grant.secret, effect)
    with pytest.raises(ApprovalDecisionConflict):
        _approve(service, grant.secret, effect, expires_at=at(400))
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


def test_denied_decision_blocks_later_approval(service, grant, effect,
                                               db_path):
    first = _approve(service, grant.secret, effect, decision="denied")
    with pytest.raises(ApprovalDecisionConflict):
        _approve(service, grant.secret, effect, decision="approved")
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][6] == "denied"
    assert rows[0][0] == first.approval_id


def test_expired_decision_stays_expired_and_unique(service, grant,
                                                   effect, db_path):
    first = _approve(service, grant.secret, effect, expires_at=at(100))
    # An identical replay never restores the expired decision.
    with pytest.raises(ApprovalDecisionExpired):
        _approve(service, grant.secret, effect, now=at(150),
                 expires_at=at(100))
    # A new expiry can never reopen the operation.
    with pytest.raises(ApprovalDecisionConflict):
        _approve(service, grant.secret, effect, now=at(150),
                 expires_at=at(500))
    with pytest.raises(ApprovalDecisionExpired):
        service.get_approval_decision(
            grant.secret, now=at(150), operation_id=effect.operation_id)
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == first.approval_id


# -- authentication, owner and chain failures: zero writes -------------------------


def test_approve_unknown_session_zero_write(service, effect, db_path):
    with pytest.raises(Unauthenticated):
        _approve(service, "unknown-secret", effect)
    _assert_no_decision_rows(db_path)


def test_approve_revoked_session_zero_write(service, identity, grant,
                                            effect, db_path):
    assert identity.revoke_session(grant.session_ref, now=at(70)) is True
    with pytest.raises(SessionRevoked):
        _approve(service, grant.secret, effect, now=at(71))
    _assert_no_decision_rows(db_path)


def test_approve_expired_session_zero_write(service, grant, effect,
                                            db_path):
    # The decision validity must outlive now so the session expiry is
    # the failure that actually fires.
    with pytest.raises(SessionExpired):
        _approve(service, grant.secret, effect, now=at(3700),
                 expires_at=at(4000))
    _assert_no_decision_rows(db_path)


def _insert_non_owner_session(db_path, secret):
    """Insert a synthetically valid second member and session directly;
    the public pairing flow only ever pairs the owner, so a non-owner
    session can only exist through storage."""
    digest = hashlib.sha256(
        b"sam-host-identity session-v1\x00"
        + secret.encode("utf-8")).hexdigest()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_identity_members (principal_id,"
            " installation_id, company_id, role, status, revision,"
            " created_at, revoked_at) VALUES ('user-syn-other',"
            " 'inst-syn-1', 'co-syn-1', 'member', 'active', 1, ?, NULL)",
            (at(15).isoformat(),))
        conn.execute(
            "INSERT INTO host_identity_sessions (session_ref,"
            " secret_sha256, installation_id, company_id, principal_id,"
            " auth_method, issued_at, expires_at, status, revoked_at)"
            " VALUES ('hss_other00000001', ?, 'inst-syn-1', 'co-syn-1',"
            " 'user-syn-other', 'local_owner_pairing', ?, ?, 'active',"
            " NULL)",
            (digest, at(15).isoformat(), at(3600).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_non_owner_session_refused_for_decide_and_read(
        service, grant, effect, db_path):
    secret = "synthetic-other-member-session"
    _insert_non_owner_session(db_path, secret)
    # The session really resolves; only the owner binding may decide.
    with pytest.raises(ApprovalOwnerRequired):
        _approve(service, secret, effect)
    _assert_no_decision_rows(db_path)
    _approve(service, grant.secret, effect)
    with pytest.raises(ApprovalOwnerRequired):
        service.get_approval_decision(secret, now=at(70),
                                      operation_id=effect.operation_id)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


def test_approve_revoked_task_credential_zero_write(
        service, credentials, grant, cred, effect, db_path):
    assert credentials.revoke_task_credential(
        grant.secret, now=at(70), task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        _approve(service, grant.secret, effect, now=at(71))
    _assert_no_decision_rows(db_path)


def test_approve_member_revoked_zero_write(service, identity, grant,
                                           effect, db_path):
    assert identity.revoke_member("user-syn-owner", now=at(70)) is True
    with pytest.raises(MembershipRevoked):
        _approve(service, grant.secret, effect, now=at(71))
    _assert_no_decision_rows(db_path)


def test_approve_and_get_refuse_reversioned_policy(
        service, authorization, grant, policy, effect, db_path):
    decision = _approve(service, grant.secret, effect)
    authorization.pause_policy(grant.secret, now=at(70),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    with pytest.raises(TaskPolicyStale):
        _approve(service, grant.secret, effect, now=at(71))
    with pytest.raises(TaskPolicyStale):
        service.get_approval_decision(
            grant.secret, now=at(71), operation_id=effect.operation_id)
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == decision.approval_id


def test_approve_after_prepared_expiry_denied(service, grant, prepared,
                                              cred, db_path):
    effect = prepared.prepare_effect(cred.secret, now=at(50),
                                     **effect_kwargs(expires_at=at(100)))
    # The task and session are still valid; only the preparation has
    # lapsed, and any decision validity beyond now would outlive it.
    with pytest.raises(ApprovalValidityDenied):
        _approve(service, grant.secret, effect, now=at(150),
                 expires_at=at(200))
    _assert_no_decision_rows(db_path)


# -- caller input validation --------------------------------------------------------


def test_approve_missing_operation_unavailable(service, grant, db_path):
    with pytest.raises(ApprovalDecisionUnavailable):
        service.approve_effect(
            grant.secret, now=at(60),
            operation_id="hop_0000000000000000",
            expected_effect_digest="0" * 64,
            expected_preparation_digest="1" * 64,
            decision="approved", expires_at=at(300))
    _assert_no_decision_rows(db_path)


# -- cross-installation/company prepared bindings --------------------------------
#
# The fixtures below build a FULLY valid, digest-self-consistent
# prepared effect on a second real stack (same owner principal, a
# different installation or company) using only the original modules'
# real canonical-record and digest primitives, then copy the stored
# row verbatim into the main database. Nothing is mocked and no digest
# verification is weakened: the copied row genuinely passes the
# prepared-effect row verification; only its installation/company
# binding is foreign to the deciding session.


def _copy_foreign_prepared(tmp_path, db_path, *, installation_id,
                           company_id, suffix):
    path = tmp_path / ("foreign-%s.db" % suffix)
    conn = _connect(path)
    try:
        identity = HostIdentityService(conn)
        identity.initialize_installation(
            installation_id=installation_id, company_id=company_id,
            owner_principal_id="user-syn-owner", owner_role="owner",
            now=T0)
        authorization = HostAuthorizationService(conn, identity)
        credentials = HostTaskCredentialService(conn, identity,
                                                authorization)
        prepared = HostPreparedEffectService(conn, identity,
                                             authorization, credentials)
        ticket = identity.issue_pairing_credential(now=at(10),
                                                   lifetime_seconds=600)
        foreign_grant = identity.pair(ticket.secret, now=at(20),
                                      session_lifetime_seconds=3600)
        policy = authorization.create_policy(foreign_grant.secret,
                                             now=at(30),
                                             **scope_kwargs())
        cred = credentials.issue_task_credential(
            foreign_grant.secret, now=at(40), policy_id=policy.policy_id,
            expected_revision=1)
        effect = prepared.prepare_effect(
            cred.secret, now=at(50),
            **effect_kwargs(idempotency_key="upload-batch-x-" + suffix))
        row = conn.execute(
            "SELECT operation_id, task_id, run_id, idempotency_key,"
            " effect_digest, preparation_digest, created_at,"
            " expires_at, record_blob, row_sha256"
            " FROM host_prepared_effects WHERE operation_id = ?",
            (effect.operation_id,)).fetchone()
    finally:
        conn.close()
    main = _connect(db_path)
    try:
        main.execute(
            "INSERT INTO host_prepared_effects (operation_id, task_id,"
            " run_id, idempotency_key, effect_digest,"
            " preparation_digest, created_at, expires_at, record_blob,"
            " row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        main.commit()
    finally:
        main.close()
    return effect


def _assert_foreign_matches_missing(service, secret, foreign):
    with pytest.raises(ApprovalDecisionUnavailable) as foreign_exc:
        service.approve_effect(
            secret, now=at(60), operation_id=foreign.operation_id,
            expected_effect_digest=foreign.effect_digest,
            expected_preparation_digest=foreign.preparation_digest,
            decision="approved", expires_at=at(300))
    with pytest.raises(ApprovalDecisionUnavailable) as missing_exc:
        service.approve_effect(
            secret, now=at(60),
            operation_id="hop_0000000000000000",
            expected_effect_digest="0" * 64,
            expected_preparation_digest="1" * 64,
            decision="approved", expires_at=at(300))
    # Same type and the identical fixed message: a foreign binding is
    # indistinguishable from a missing operation.
    assert str(foreign_exc.value) == str(missing_exc.value)


def test_approve_cross_installation_prepared_is_unavailable(
        service, grant, tmp_path, db_path):
    foreign = _copy_foreign_prepared(
        tmp_path, db_path, installation_id="inst-syn-x1",
        company_id="co-syn-1", suffix="inst")
    assert foreign.envelope["principal_id"] == "user-syn-owner"
    assert foreign.envelope["installation_id"] != "inst-syn-1"
    _assert_foreign_matches_missing(service, grant.secret, foreign)
    _assert_no_decision_rows(db_path)


def test_approve_cross_company_prepared_is_unavailable(
        service, grant, tmp_path, db_path):
    foreign = _copy_foreign_prepared(
        tmp_path, db_path, installation_id="inst-syn-1",
        company_id="co-syn-x1", suffix="company")
    assert foreign.envelope["principal_id"] == "user-syn-owner"
    assert foreign.envelope["company_id"] != "co-syn-1"
    _assert_foreign_matches_missing(service, grant.secret, foreign)
    _assert_no_decision_rows(db_path)


def test_approve_digest_mismatch_zero_write(service, grant, effect,
                                            db_path):
    with pytest.raises(ApprovalPreparationMismatch):
        _approve(service, grant.secret, effect,
                 expected_effect_digest="0" * 64)
    with pytest.raises(ApprovalPreparationMismatch):
        _approve(service, grant.secret, effect,
                 expected_preparation_digest="0" * 64)
    with pytest.raises(ApprovalPreparationMismatch):
        _approve(service, grant.secret, effect,
                 expected_effect_digest=effect.preparation_digest,
                 expected_preparation_digest=effect.effect_digest)
    _assert_no_decision_rows(db_path)


def test_approve_validity_beyond_prepared_denied(service, grant, effect,
                                                 db_path):
    with pytest.raises(ApprovalValidityDenied):
        _approve(service, grant.secret, effect, expires_at=at(601))
    _assert_no_decision_rows(db_path)


def test_approve_expired_request_zero_write(service, grant, effect,
                                            db_path):
    with pytest.raises(ApprovalDecisionExpired):
        _approve(service, grant.secret, effect, expires_at=at(60))
    with pytest.raises(ApprovalDecisionExpired):
        _approve(service, grant.secret, effect, expires_at=at(50))
    _assert_no_decision_rows(db_path)


def test_approve_input_validation(service, grant, effect, db_path):
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect, decision="maybe")
    with pytest.raises(TypeError):
        _approve(service, grant.secret, effect, decision=True)
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect,
                 expected_effect_digest="0" * 63)
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect,
                 expected_effect_digest="0" * 63 + "A")
    with pytest.raises(TypeError):
        _approve(service, grant.secret, effect,
                 expected_effect_digest=0)
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect, operation_id="")
    with pytest.raises(ValueError):
        service.approve_effect(
            grant.secret, now=datetime(2026, 10, 24, 12, 1, 0),
            **decision_kwargs(effect))
    with pytest.raises(ValueError):
        _approve(service, "", effect)
    with pytest.raises(TypeError):
        _approve(service, grant.secret, effect,
                 expires_at="2026-10-24T12:05:00+00:00")
    _assert_no_decision_rows(db_path)


def test_get_input_validation(service, grant, effect):
    _approve(service, grant.secret, effect)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=datetime(2026, 10, 24, 12, 1, 10),
            operation_id=effect.operation_id)
    with pytest.raises(ValueError):
        service.get_approval_decision("", now=at(70),
                                      operation_id=effect.operation_id)
    with pytest.raises(ValueError):
        service.get_approval_decision(grant.secret, now=at(70),
                                      operation_id="")


# -- error chains and message safety -------------------------------------------------


def test_lone_surrogate_session_secret_refused_with_clean_chain(
        service, effect, db_path):
    visible = "session-secret-MARKER-4d21"
    bad_secret = visible + chr(0xD800)
    with pytest.raises(ValueError) as excinfo:
        _approve(service, bad_secret, effect)
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_decision(bad_secret, now=at(60),
                                      operation_id="hop_0000000000000000")
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    _assert_no_decision_rows(db_path)


def test_errors_never_echo_caller_input(service, grant, effect, db_path):
    marker = "MARKER-7a31e0-private"
    cases = [
        {"operation_id": marker + chr(0xD800)},  # encoding failure
        {"decision": marker},  # unknown decision, fixed message
        {"expected_effect_digest": marker},  # digest shape failure
    ]
    for overrides in cases:
        with pytest.raises(Exception) as excinfo:
            _approve(service, grant.secret, effect, **overrides)
        _assert_clean_chain(excinfo, marker)
    _assert_no_decision_rows(db_path)


def test_conflict_error_carries_no_request_content(service, grant,
                                                   effect, db_path):
    _approve(service, grant.secret, effect)
    with pytest.raises(ApprovalDecisionConflict) as excinfo:
        _approve(service, grant.secret, effect, decision="denied",
                 expires_at=at(400))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


# -- read-back behavior -------------------------------------------------------------


def test_get_missing_operation_unavailable(service, grant, effect):
    _approve(service, grant.secret, effect)
    with pytest.raises(ApprovalDecisionUnavailable) as excinfo:
        service.get_approval_decision(
            grant.secret, now=at(70),
            operation_id="hop_0000000000000000")
    assert "hop_0000000000000000" not in str(excinfo.value)


def test_get_forged_foreign_record_is_unavailable_not_exposed(
        service, grant, cred, effect, db_path):
    # A self-consistently hashed record naming another principal is
    # indistinguishable from a missing one; the same fixed error covers
    # both, before any binding detail is reported.
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgeforeign00a1",
                            principal_id="user-syn-other",
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    _insert_forged_row(db_path, record)
    with pytest.raises(ApprovalDecisionUnavailable) as foreign:
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)
    with pytest.raises(ApprovalDecisionUnavailable) as missing:
        service.get_approval_decision(
            grant.secret, now=at(70),
            operation_id="hop_0000000000000000")
    assert str(foreign.value) == str(missing.value)


def test_get_refuses_decision_after_member_revision_advances(
        service, grant, effect, db_path):
    decision = _approve(service, grant.secret, effect)
    original_blob = decision.record_bytes
    # State simulation: the STILL-ACTIVE member's revision advances
    # 1 -> 2 (controlled synthetic SQLite UPDATE; installation and
    # company unchanged). The existing revoke API cannot produce this
    # state - revoking a member revokes the session outright and is
    # already refused - and this test does not claim it can.
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE host_identity_members SET revision = 2"
                     " WHERE principal_id = 'user-syn-owner'")
        conn.commit()
    finally:
        conn.close()
    # The real session now resolves member_revision = 2 while the
    # stored decision and preparation both record 1; the task
    # re-resolution alone still reports the issuance-time stored
    # revision, so only the exact session-revision match can refuse.
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)
    # The member-revision refusal approve already had holds in the
    # same simulated state.
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect, now=at(71))
    # The refusals wrote nothing: the old row is neither deleted nor
    # overwritten and no caller transaction is left behind.
    assert not service.db.in_transaction
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == decision.approval_id
    assert rows[0][9] == original_blob


def test_get_refuses_caller_transaction_and_preserves_it(
        service, grant, effect):
    _approve(service, grant.secret, effect)
    conn = service.db
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_approval_decisions").fetchone()
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)
    # The refused caller transaction is never closed, committed or
    # rolled back by the module.
    assert conn.in_transaction
    conn.rollback()
    loaded = service.get_approval_decision(
        grant.secret, now=at(70), operation_id=effect.operation_id)
    assert loaded.operation_id == effect.operation_id


def test_get_refuses_stale_caller_snapshot_hiding_revocation(
        service, identity, grant, cred, effect, db_path):
    _approve(service, grant.secret, effect)
    conn = service.db
    # WAL lets the revocation below commit while this connection keeps
    # its old snapshot, exactly the situation the refusal protects.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_approval_decisions").fetchone()
    other = _connect(db_path)
    try:
        other_identity = HostIdentityService(other)
        other_authorization = HostAuthorizationService(other,
                                                       other_identity)
        other_credentials = HostTaskCredentialService(
            other, other_identity, other_authorization)
        assert other_credentials.revoke_task_credential(
            grant.secret, now=at(80), task_id=cred.task_id) is True
    finally:
        other.close()
    # get must NOT read through the stale snapshot (which still shows
    # the credential as valid): it refuses the caller transaction.
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(81), operation_id=effect.operation_id)
    assert conn.in_transaction
    conn.rollback()
    # On a fresh snapshot the revocation is visible and the read fails
    # closed instead of returning content.
    with pytest.raises(TaskCredentialRevoked):
        service.get_approval_decision(
            grant.secret, now=at(81), operation_id=effect.operation_id)


# -- transaction ownership and atomicity ---------------------------------------------


def test_approve_refuses_caller_transaction_and_preserves_it(
        service, grant, effect, db_path):
    conn = service.db
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_approval_decisions").fetchone()
    with pytest.raises(ValueError):
        _approve(service, grant.secret, effect)
    assert conn.in_transaction
    conn.rollback()
    _assert_no_decision_rows(db_path)
    decision = _approve(service, grant.secret, effect)
    assert decision.approval_id.startswith("had_")


def test_failed_insert_rolls_back_completely(identity, grant, cred,
                                             effect, db_path):
    identity.db.close()
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    failing = HostApprovalDecisionService(conn, new_identity,
                                          authorization, credentials,
                                          prepared)
    conn.needle = "INSERT INTO host_approval_decisions"
    with pytest.raises(RuntimeError):
        failing.approve_effect(grant.secret, now=at(60),
                               **decision_kwargs(effect))
    conn.close()
    _assert_no_decision_rows(db_path)
    # The prepared effect and the credential ledger are untouched by
    # the rolled-back attempt.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_task_credentials") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM "
                            "host_task_credential_events") == 1


# -- effective-time regressions --------------------------------------------------------


def _reopen_with_simulated_lock_wait(identity, db_path,
                                     wait_microseconds, monkeypatch):
    identity.db.close()
    clock = _LockWaitClock(wait_microseconds)
    conn = sqlite3.connect(db_path, factory=_WaitOnBegin)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.clock = clock
    monkeypatch.setattr("app.host_approval_decisions._monotonic_ns",
                        clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    return HostApprovalDecisionService(conn, new_identity, authorization,
                                       credentials, prepared)


def test_approve_rejected_when_lock_wait_crosses_expires(
        identity, grant, effect, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before expires_at; the simulated 2us
    # write-lock wait moves the in-transaction effective time one
    # microsecond past expires_at, so nothing may be written on the
    # stale pre-lock time.
    with pytest.raises(ApprovalDecisionExpired):
        service.approve_effect(
            grant.secret, now=at(300) - timedelta(microseconds=1),
            **decision_kwargs(effect))
    service.db.close()
    _assert_no_decision_rows(db_path)


class _WaitOnFirstRead(sqlite3.Connection):
    """Connection advancing the fake clock when the fresh read snapshot
    is actually established by the first real SELECT."""

    clock = None

    def execute(self, sql, parameters=()):
        clock = self.clock
        if clock is not None and isinstance(sql, str) \
                and "FROM host_approval_decisions" in sql:
            self.clock = None  # advance exactly once
            clock.advance()
        return super().execute(sql, parameters)


def _reopen_with_simulated_read_wait(identity, db_path,
                                     wait_microseconds, monkeypatch):
    identity.db.close()
    clock = _LockWaitClock(wait_microseconds)
    conn = sqlite3.connect(db_path, factory=_WaitOnFirstRead)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.clock = clock
    monkeypatch.setattr("app.host_approval_decisions._monotonic_ns",
                        clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    return HostApprovalDecisionService(conn, new_identity, authorization,
                                       credentials, prepared)


def test_get_rejected_when_read_wait_crosses_decision_expiry(
        service, identity, grant, effect, db_path, monkeypatch):
    _approve(service, grant.secret, effect, expires_at=at(100))
    service = _reopen_with_simulated_read_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before expires_at; the simulated 2us wait
    # establishing the read snapshot moves the effective time one
    # microsecond past expires_at, so the stale entry time must not
    # pass.
    with pytest.raises(ApprovalDecisionExpired):
        service.get_approval_decision(
            grant.secret, now=at(100) - timedelta(microseconds=1),
            operation_id=effect.operation_id)
    # The refused read closed its own snapshot and wrote nothing.
    assert not service.db.in_transaction
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1
    # A normal read at a valid time still returns the decision.
    loaded = service.get_approval_decision(
        grant.secret, now=at(70), operation_id=effect.operation_id)
    assert loaded.operation_id == effect.operation_id


# -- stored-row immutability guards -----------------------------------------------------


def test_rows_reject_update_delete_and_both_replace_classes(
        service, grant, cred, effect, db_path):
    decision = _approve(service, grant.secret, effect)
    ctx = cred.context
    forged_params = ("had_guard0000000001", "hop_guard0000000001",
                     ctx.task_id, ctx.run_id, "0" * 64, "1" * 64,
                     "denied", _time_text(at(60)), _time_text(at(300)),
                     b"{}", "2" * 64)
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_approval_decisions "
                         "SET decision = 'denied'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_approval_decisions")
        # INSERT OR REPLACE over the primary key (approval_id).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_approval_decisions ("
                + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision.approval_id,) + forged_params[1:])
        # INSERT OR REPLACE over the operation_id unique key with a
        # fresh approval_id.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_approval_decisions ("
                + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (forged_params[0], effect.operation_id)
                + forged_params[2:])
        # A plain duplicate-key insert is refused by the same guard.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_approval_decisions (" + _COLUMNS
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("had_guard0000000002", effect.operation_id)
                + forged_params[2:])
    finally:
        conn.close()
    rows = _decision_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == decision.approval_id
    assert rows[0][9] == decision.record_bytes
    again = service.get_approval_decision(
        grant.secret, now=at(70), operation_id=effect.operation_id)
    assert again.approval_id == decision.approval_id


# -- forged stored rows: legal digests never excuse illegal content ------------------


def test_forged_row_wrong_row_digest_refused(service, grant, cred,
                                             effect, db_path):
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgerowdigesta1",
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    _insert_forged_row(db_path, record, row_sha256="0" * 64)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_row_index_blob_mismatch_refused(service, grant, cred,
                                                effect, db_path):
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgeindexdrifa2",
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    blob = git_format.dumps_record(record)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_approval_decisions (" + _COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["approval_id"], record["operation_id"],
             record["task_id"], record["run_id"],
             "9" * 64,  # index digest disagrees with the blob
             record["preparation_digest"], record["decision"],
             record["created_at"], record["expires_at"], blob,
             hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_noncanonical_blob_refused(service, grant, cred, effect,
                                          db_path):
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgecanonicala3",
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    _insert_forged_row(db_path, record, canonical=False)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_bool_schema_version_refused(service, grant, cred,
                                            effect, db_path):
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgeboolschemaa4",
                            schema_version=True,
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_created_at_not_before_expires_refused(service, grant,
                                                      cred, db_path):
    # created_at >= expires_at is illegal even when every digest is
    # self-consistent; the record-shape refusal fires on read-back.
    for index, (created, expires) in enumerate((
            (_time_text(at(400)), _time_text(at(300))),
            (_time_text(at(300)), _time_text(at(300))))):
        operation_id = "hop_forgebadtime0b%d" % index
        record = _forged_record(
            cred.context, grant.session_ref, operation_id,
            "had_forgebadtime0c%d" % index,
            created_at=created, expires_at=expires)
        _insert_forged_row(db_path, record)
        with pytest.raises(ValueError):
            service.get_approval_decision(
                grant.secret, now=at(70), operation_id=operation_id)


def test_forged_binding_mismatch_refused(service, grant, cred, effect,
                                         db_path):
    # Self-consistent record and row digest, but the bound digests do
    # not match the fully verified prepared effect.
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgebinding00c1")
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_expires_beyond_prepared_refused(service, grant, cred,
                                                effect, db_path):
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgelongexp00c2",
                            expires_at=_time_text(at(700)),
                            effect_digest=effect.effect_digest,
                            preparation_digest=effect.preparation_digest)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)


def test_forged_bad_date_error_carries_no_stored_content(
        service, grant, cred, effect, db_path):
    visible = "MARKER-date-51cd"
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgebaddate0d1",
                            created_at="2026-10-24T12:01:00 " + visible)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)
    _assert_clean_chain(excinfo, visible)


def test_forged_unparseable_blob_errors_carry_no_stored_content(
        service, grant, cred, effect, db_path):
    visible_json = "MARKER-json-6b38"
    record = _forged_record(cred.context, grant.session_ref,
                            effect.operation_id, "had_forgebadjson0d2")
    _insert_forged_row(
        db_path, record,
        blob_override=b'{"schema_version": 1, "x": '
        + visible_json.encode("utf-8") + b'}')
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_decision(
            grant.secret, now=at(70), operation_id=effect.operation_id)
    _assert_clean_chain(excinfo, visible_json)

    visible_utf8 = "MARKER-utf8-2e04"
    record = _forged_record(cred.context, grant.session_ref,
                            "hop_forgebadutf80d3", "had_forgebadutf80d3")
    _insert_forged_row(
        db_path, record,
        blob_override=visible_utf8.encode("utf-8") + b"\xff\xfe")
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_decision(
            grant.secret, now=at(70),
            operation_id="hop_forgebadutf80d3")
    _assert_clean_chain(excinfo, visible_utf8)


# -- cross-connection concurrency -----------------------------------------------------


def _thread_approve(db_path, barrier, results, index, secret, overrides):
    conn = _connect(db_path)
    try:
        identity = HostIdentityService(conn)
        authorization = HostAuthorizationService(conn, identity)
        credentials = HostTaskCredentialService(conn, identity,
                                                authorization)
        prepared = HostPreparedEffectService(conn, identity,
                                             authorization, credentials)
        service = HostApprovalDecisionService(conn, identity,
                                              authorization, credentials,
                                              prepared)
        barrier.wait(timeout=30)
        results[index] = ("ok", _approve(service, secret, overrides[0],
                                         **overrides[1]))
    except BaseException as exc:
        results[index] = ("error", exc)
    finally:
        conn.close()


def _run_concurrent_pair(db_path, secret, effect, overrides_a,
                         overrides_b):
    barrier = threading.Barrier(2)
    results = [None, None]
    threads = [
        threading.Thread(
            target=_thread_approve,
            args=(db_path, barrier, results, index, secret,
                  (effect, overrides)))
        for index, overrides in enumerate((overrides_a, overrides_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    for thread in threads:
        assert not thread.is_alive(), \
            "concurrent approve thread did not finish"
    assert all(entry is not None for entry in results)
    return results


def test_concurrent_same_decision_single_row(service, grant, effect,
                                             db_path):
    results = _run_concurrent_pair(db_path, grant.secret, effect, {}, {})
    for status, value in results:
        if status != "ok":
            raise value
    first, second = results[0][1], results[1][1]
    assert first.approval_id == second.approval_id
    assert first.created_at == second.created_at
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1


def test_concurrent_opposite_decisions_single_winner(service, grant,
                                                     effect, db_path):
    results = _run_concurrent_pair(db_path, grant.secret, effect, {},
                                   {"decision": "denied"})
    statuses = sorted(status for status, _ in results)
    assert statuses == ["error", "ok"]
    error = next(value for status, value in results
                 if status == "error")
    if not isinstance(error, ApprovalDecisionConflict):
        raise error
    # The insert race failure never chains the database exception.
    assert error.__context__ is None
    rows = _decision_rows(db_path)
    assert len(rows) == 1


# -- result immutability and secrecy ----------------------------------------------------


def test_result_is_deeply_immutable_and_exports_copies(service, grant,
                                                       effect):
    decision = _approve(service, grant.secret, effect)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.approval_id = "had_changed0000000"
    with pytest.raises(TypeError):
        decision.record["decision"] = "denied"
    assert isinstance(decision.record, MappingProxyType)
    exported = decision.to_dict()
    exported["decision"] = "denied"
    exported["nested"] = {"injected": True}
    fresh = decision.to_dict()
    assert fresh["decision"] == "approved"
    assert "nested" not in fresh


def test_result_carries_no_secret_or_credential_digest(service, grant,
                                                       cred, effect):
    decision = _approve(service, grant.secret, effect)
    digest = hashlib.sha256(
        b"sam-host-task-credentials credential-v1\x00"
        + cred.secret.encode("utf-8")).hexdigest()
    session_digest = hashlib.sha256(
        b"sam-host-identity session-v1\x00"
        + grant.secret.encode("utf-8")).hexdigest()
    blob_text = decision.record_bytes.decode("utf-8")
    rendered = repr(decision)
    serialized = json.dumps(decision.to_dict())
    for marker in (cred.secret, grant.secret, digest, session_digest):
        assert marker not in blob_text
        assert marker not in rendered
        assert marker not in serialized


# -- default-database isolation ----------------------------------------------------------


def test_module_never_opens_the_default_real_database(tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("SAM_DATA_ROOT", str(tmp_path / "elsewhere"))
    path = tmp_path / "explicit.db"
    conn, service = _build_stack(path)
    identity = service.identity
    identity.initialize_installation(
        installation_id="inst-syn-9", company_id="co-syn-9",
        owner_principal_id="user-syn-owner-9", owner_role="owner",
        now=T0)
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20),
                          session_lifetime_seconds=3600)
    policy = service.authorization.create_policy(
        grant.secret, now=at(30), **scope_kwargs())
    cred = service.task_credentials.issue_task_credential(
        grant.secret, now=at(40), policy_id=policy.policy_id,
        expected_revision=1)
    effect = service.prepared_effects.prepare_effect(
        cred.secret, now=at(50), **effect_kwargs())
    decision = _approve(service, grant.secret, effect)
    assert decision.to_dict()["company_id"] == "co-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()
