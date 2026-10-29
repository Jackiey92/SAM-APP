"""Tests for app.host_approval_reservations (F-007 irreversible
approval reservation binding).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data, no models and no Git access.
Identity, policy, task, preparation and decision state are built
synthetically through the real HostIdentityService pairing flow, the
real HostAuthorizationService ledger, the real HostTaskCredentialService
issuance path, the real HostPreparedEffectService and the real
HostApprovalDecisionService; the row digest is recomputed independently
in this file, including the domain-separation constant. Generic helpers
(clock, connection, fault injection, scope/effect/decision templates,
clean-chain assertion, forged prepared/decision row builders) are
imported from the sibling credential, prepared-effect and decision
tests instead of being copied.
"""
import dataclasses
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from app.facts_tree import git_format
from app.host_approval_decisions import HostApprovalDecisionService
from app.host_approval_reservations import (
    ApprovalReservation,
    ApprovalReservationConflict,
    ApprovalReservationDenied,
    ApprovalReservationExpired,
    ApprovalReservationMismatch,
    ApprovalReservationUnavailable,
    HostApprovalReservationService,
)
from app.host_authorization import HostAuthorizationService
from app.host_identity import HostIdentityService
from app.host_prepared_effects import HostPreparedEffectService
from app.host_task_credentials import (
    HostTaskCredentialService,
    TaskCredentialInvalid,
    TaskCredentialRevoked,
    TaskMembershipRevoked,
    TaskPolicyExpired,
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
    _forged_envelope as _forged_prepared_envelope,
    _insert_forged_row as _insert_forged_prepared_row,
    _rows,
    _scalar,
    _time_text,
    effect_kwargs,
    scope_kwargs,
)
from tests.test_host_approval_decisions import (  # reused pure helpers
    _forged_record as _forged_decision_record,
    _insert_forged_row as _insert_forged_decision_row,
    decision_kwargs,
)

# Domain-separation constant pinned independently of the module.
_ROW_DOMAIN = b"sam-host-approval-reservation row-v1\x00"

EXPECTED_RECORD_KEYS = frozenset({
    "schema_version", "reservation_state", "reservation_id",
    "approval_id", "operation_id", "effect_digest",
    "preparation_digest", "installation_id", "company_id",
    "principal_id", "member_revision", "task_id", "run_id",
    "policy_id", "policy_revision", "action", "target", "created_at",
    "expires_at"})

_COLUMNS = ("reservation_id, approval_id, operation_id, task_id, "
            "run_id, effect_digest, preparation_digest, created_at, "
            "expires_at, record_blob, row_sha256")


def _approve(decisions, secret, effect, now=at(60), **overrides):
    return decisions.approve_effect(secret, now=now,
                                    **decision_kwargs(effect,
                                                      **overrides))


def reserve_kwargs(effect, decision, **overrides):
    kwargs = {
        "operation_id": effect.operation_id,
        "approval_id": decision.approval_id,
        "expected_effect_digest": effect.effect_digest,
        "expected_preparation_digest": effect.preparation_digest,
    }
    kwargs.update(overrides)
    return kwargs


def _reserve(service, secret, effect, decision, now=at(70), **overrides):
    return service.reserve_approval(
        secret, now=now,
        **reserve_kwargs(effect, decision, **overrides))


def _reservation_rows(db_path):
    return _rows(db_path,
                 "SELECT " + _COLUMNS
                 + " FROM host_approval_reservations ORDER BY created_at")


def _assert_no_reservation_rows(db_path):
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 0


def _build_stack(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    prepared = HostPreparedEffectService(conn, identity, authorization,
                                         credentials)
    decisions = HostApprovalDecisionService(conn, identity, authorization,
                                            credentials, prepared)
    service = HostApprovalReservationService(conn, identity, authorization,
                                             credentials, prepared,
                                             decisions)
    return conn, service


# -- forged stored rows (legal digests, illegal content) ------------------------


def _forged_record(ctx, effect, decision, reservation_id, **overrides):
    """Build a self-consistently hashed reservation record the way an
    attacker with storage write access would: a legal row digest over
    illegal content. Read-back must still refuse these rows."""
    record = {
        "schema_version": 1,
        "reservation_state": "reserved",
        "reservation_id": reservation_id,
        "approval_id": decision.approval_id,
        "operation_id": effect.operation_id,
        "effect_digest": effect.effect_digest,
        "preparation_digest": effect.preparation_digest,
        "installation_id": ctx.installation_id,
        "company_id": ctx.company_id,
        "principal_id": ctx.principal_id,
        "member_revision": ctx.member_revision,
        "task_id": ctx.task_id,
        "run_id": ctx.run_id,
        "policy_id": ctx.policy_id,
        "policy_revision": ctx.policy_revision,
        "action": effect.envelope["action"],
        "target": effect.envelope["target"],
        "created_at": _time_text(at(70)),
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
            "INSERT INTO host_approval_reservations (" + _COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["reservation_id"], record["approval_id"],
             record["operation_id"], record["task_id"],
             record["run_id"], record["effect_digest"],
             record["preparation_digest"], record["created_at"],
             record["expires_at"], blob, row_sha256))
        conn.commit()
    finally:
        conn.close()


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-approval-reservations.db"


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
def decisions(identity, authorization, credentials, prepared):
    return HostApprovalDecisionService(identity.db, identity,
                                       authorization, credentials,
                                       prepared)


@pytest.fixture
def service(identity, authorization, credentials, prepared, decisions):
    return HostApprovalReservationService(identity.db, identity,
                                          authorization, credentials,
                                          prepared, decisions)


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


@pytest.fixture
def decision(decisions, grant, effect):
    return _approve(decisions, grant.secret, effect)


@pytest.fixture
def reservation(service, cred, effect, decision):
    return _reserve(service, cred.secret, effect, decision)


# -- constructor binding contract -------------------------------------------------


def test_constructor_requires_connection_and_bound_service_types(
        identity, authorization, credentials, prepared, decisions):
    conn = identity.db
    with pytest.raises(TypeError):
        HostApprovalReservationService("host-approval-reservations.db",
                                       identity, authorization,
                                       credentials, prepared, decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(object(), identity, authorization,
                                       credentials, prepared, decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(conn, object(), authorization,
                                       credentials, prepared, decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(conn, identity, object(),
                                       credentials, prepared, decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(conn, identity, authorization,
                                       object(), prepared, decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(conn, identity, authorization,
                                       credentials, object(), decisions)
    with pytest.raises(TypeError):
        HostApprovalReservationService(conn, identity, authorization,
                                       credentials, prepared, object())


def test_constructor_refuses_second_connection_to_same_file(
        identity, authorization, credentials, prepared, decisions,
        db_path):
    second = _connect(db_path)
    with pytest.raises(ValueError):
        HostApprovalReservationService(second, identity, authorization,
                                       credentials, prepared, decisions)
    # The refusal happens before any DDL: no reservation table or
    # trigger may appear through the rejected connection.
    assert {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'host_approval_reservations%'")} == set()
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
    decisions = HostApprovalDecisionService(conn, identity, authorization,
                                            credentials, prepared)
    other_credentials = HostTaskCredentialService(conn, identity,
                                                  authorization)
    foreign_prepared = HostPreparedEffectService(conn, identity,
                                                 authorization,
                                                 other_credentials)
    foreign_decisions = HostApprovalDecisionService(conn, identity,
                                                    authorization,
                                                    other_credentials,
                                                    foreign_prepared)
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, other_identity,
                                       authorization, credentials,
                                       prepared, decisions)
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, identity,
                                       other_authorization, credentials,
                                       prepared, decisions)
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, identity, authorization,
                                       other_credentials, prepared,
                                       decisions)
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, identity, authorization,
                                       credentials, foreign_prepared,
                                       decisions)
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, identity, authorization,
                                       credentials, prepared,
                                       foreign_decisions)
    conn.close()


def test_constructor_refuses_active_caller_transaction(
        identity, authorization, credentials, prepared, decisions):
    conn = identity.db
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostApprovalReservationService(conn, identity, authorization,
                                       credentials, prepared, decisions)
    assert conn.in_transaction
    conn.rollback()


def test_constructor_ddl_is_idempotent_across_reconnect(db_path):
    conn, _ = _build_stack(db_path)
    conn.close()
    conn, _ = _build_stack(db_path)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "host_approval_reservations" in names
    conn.close()


# -- happy path --------------------------------------------------------------------


def test_reserve_persists_single_verifiable_reservation(
        service, cred, effect, decision, db_path):
    reservation = _reserve(service, cred.secret, effect, decision)
    assert isinstance(reservation, ApprovalReservation)
    assert reservation.reservation_id.startswith("har_")
    assert reservation.approval_id == decision.approval_id
    assert reservation.operation_id == effect.operation_id
    assert reservation.effect_digest == effect.effect_digest
    assert reservation.preparation_digest == effect.preparation_digest
    assert at(70) <= reservation.created_at < at(71)
    # The caller never supplies a validity period: the reservation
    # expires exactly with the decision.
    assert reservation.expires_at == decision.expires_at == at(300)
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    (reservation_id, approval_id, operation_id, task_id, run_id,
     effect_digest, preparation_digest, created_at, expires_at, blob,
     row_sha256) = rows[0]
    assert reservation_id == reservation.reservation_id
    assert approval_id == decision.approval_id
    assert operation_id == effect.operation_id
    assert (task_id, run_id) == (effect.envelope["task_id"],
                                 effect.envelope["run_id"])
    assert effect_digest == effect.effect_digest
    assert preparation_digest == effect.preparation_digest
    assert created_at == _time_text(reservation.created_at)
    assert expires_at == _time_text(at(300))
    assert blob == reservation.record_bytes
    assert row_sha256 == hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()
    # First-binding fact only: reservation_state is 'reserved' and no
    # consumption, execution or approval-use state exists anywhere.
    assert reservation.record["reservation_state"] == "reserved"
    names = {row[0] for row in _rows(
        db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not any("consum" in name or "execut" in name
                   for name in names)
    assert {name for name in names if "reserv" in name} \
        == {"host_approval_reservations"}


def test_record_exact_fields_and_fixed_time_format(service, cred, effect,
                                                   decision):
    reservation = _reserve(service, cred.secret, effect, decision)
    data = reservation.to_dict()
    assert frozenset(data) == EXPECTED_RECORD_KEYS
    assert type(data["schema_version"]) is int
    assert data["schema_version"] == 1
    assert data["reservation_state"] == "reserved"
    assert data["reservation_id"] == reservation.reservation_id
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
    # action and target are copied verbatim from the prepared envelope.
    assert data["action"] == effect.envelope["action"]
    assert data["target"] == effect.envelope["target"]
    for name in ("created_at", "expires_at"):
        text = data[name]
        assert text.endswith("+00:00")
        assert _time_text(datetime.fromisoformat(text)) == text
    assert data["created_at"] < data["expires_at"]


def test_reserve_after_owner_session_revoked_task_still_valid(
        service, identity, grant, cred, effect, decision, db_path):
    # API-008's identity is the task credential alone: the original
    # approving session is historical audit data, and revoking it while
    # the task stays valid never blocks the reservation. The worker
    # never holds the owner's session secret.
    assert identity.revoke_session(grant.session_ref, now=at(65)) is True
    reservation = _reserve(service, cred.secret, effect, decision,
                           now=at(70))
    assert reservation.approval_id == decision.approval_id
    loaded = service.get_approval_reservation(
        cred.secret, now=at(75), operation_id=effect.operation_id)
    assert loaded.to_dict() == reservation.to_dict()
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1


def test_reserve_get_and_reopen_preserves_history(
        service, cred, effect, decision, db_path):
    reservation = _reserve(service, cred.secret, effect, decision)
    loaded = service.get_approval_reservation(
        cred.secret, now=at(75), operation_id=effect.operation_id)
    assert loaded.to_dict() == reservation.to_dict()
    assert loaded.record_bytes == reservation.record_bytes
    service.db.close()
    conn, reopened = _build_stack(db_path)
    try:
        again = reopened.get_approval_reservation(
            cred.secret, now=at(76), operation_id=effect.operation_id)
        assert again.to_dict() == reservation.to_dict()
        # A replay after reconnect returns the original identifiers and
        # adds no row: reopening a connection never resets history.
        replayed = _reserve(reopened, cred.secret, effect, decision,
                            now=at(77))
        assert replayed.reservation_id == reservation.reservation_id
        assert replayed.created_at == reservation.created_at
        assert _scalar(
            db_path,
            "SELECT COUNT(*) FROM host_approval_reservations") == 1
    finally:
        conn.close()


def test_idempotent_replay_returns_original_without_new_row(
        service, cred, effect, decision, db_path):
    first = _reserve(service, cred.secret, effect, decision)
    second = _reserve(service, cred.secret, effect, decision, now=at(75))
    assert second.reservation_id == first.reservation_id
    assert second.created_at == first.created_at
    assert second.to_dict() == first.to_dict()
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1


# -- authentication and chain failures: zero writes -------------------------------


def test_reserve_unknown_secret_zero_write(service, effect, decision,
                                           db_path):
    with pytest.raises(TaskCredentialInvalid):
        _reserve(service, "unknown-secret", effect, decision)
    _assert_no_reservation_rows(db_path)


def test_reserve_revoked_task_credential_zero_write(
        service, credentials, grant, cred, effect, decision, db_path):
    assert credentials.revoke_task_credential(
        grant.secret, now=at(65), task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        _reserve(service, cred.secret, effect, decision, now=at(66))
    _assert_no_reservation_rows(db_path)


def test_reserve_expired_task_and_policy_zero_write(
        service, cred, effect, decision, db_path):
    # At at(3700) the bound policy validity (not_after at(3600)) has
    # passed; resolution fails closed before any reservation work.
    with pytest.raises(TaskPolicyExpired):
        _reserve(service, cred.secret, effect, decision, now=at(3700))
    _assert_no_reservation_rows(db_path)


def test_reserve_member_revoked_zero_write(service, identity, grant,
                                           cred, effect, decision,
                                           db_path):
    assert identity.revoke_member("user-syn-owner", now=at(65)) is True
    with pytest.raises(TaskMembershipRevoked):
        _reserve(service, cred.secret, effect, decision, now=at(66))
    _assert_no_reservation_rows(db_path)


def test_reserve_refused_when_member_revision_advances(
        service, cred, effect, decision, db_path):
    # State simulation: the STILL-ACTIVE member's revision advances
    # 1 -> 2 (controlled synthetic SQLite UPDATE). The task resolution
    # alone still reports the issuance-time stored revision, so only
    # the separate current-member re-read can refuse.
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE host_identity_members SET revision = 2"
                     " WHERE principal_id = 'user-syn-owner'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, now=at(70))
    _assert_no_reservation_rows(db_path)
    assert not service.db.in_transaction


def test_get_refused_when_member_revision_advances(
        service, cred, effect, decision, reservation, db_path):
    original_blob = reservation.record_bytes
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE host_identity_members SET revision = 2"
                     " WHERE principal_id = 'user-syn-owner'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(80), operation_id=effect.operation_id)
    # A replay in the same simulated state is refused before the
    # idempotency lookup and never adds a row.
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, now=at(81))
    assert not service.db.in_transaction
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reservation.reservation_id
    assert rows[0][9] == original_blob


def test_reserve_and_get_refuse_reversioned_policy(
        service, authorization, grant, policy, cred, effect, decision,
        reservation, db_path):
    authorization.pause_policy(grant.secret, now=at(80),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    with pytest.raises(TaskPolicyStale):
        _reserve(service, cred.secret, effect, decision, now=at(81))
    with pytest.raises(TaskPolicyStale):
        service.get_approval_reservation(
            cred.secret, now=at(81), operation_id=effect.operation_id)
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reservation.reservation_id


def test_denied_decision_is_never_reserved(service, grant, cred,
                                           decisions, effect, db_path):
    denied = _approve(decisions, grant.secret, effect, decision="denied")
    with pytest.raises(ApprovalReservationDenied):
        _reserve(service, cred.secret, effect, denied)
    _assert_no_reservation_rows(db_path)
    with pytest.raises(ApprovalReservationUnavailable):
        service.get_approval_reservation(
            cred.secret, now=at(70), operation_id=effect.operation_id)


def test_get_refuses_canonical_row_bound_to_denied_decision(
        service, grant, cred, decisions, effect, db_path):
    # A REAL denied decision plus a complete canonical self-consistent
    # reservation row that binds it: the row digest, the canonical
    # shape and every binding field genuinely verify, so only the
    # semantic decision == 'approved' requirement can refuse the
    # read-back - a forged reservation must never upgrade a denial.
    denied = _approve(decisions, grant.secret, effect, decision="denied")
    record = _forged_record(cred.context, effect, denied,
                            "har_forgedenied0d1")
    _insert_forged_row(db_path, record)
    with pytest.raises(ApprovalReservationUnavailable):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_reserve_expired_decision_zero_write(service, grant, cred,
                                             decisions, effect, db_path):
    expired = _approve(decisions, grant.secret, effect,
                       expires_at=at(100))
    with pytest.raises(ApprovalReservationExpired):
        _reserve(service, cred.secret, effect, expired, now=at(150))
    _assert_no_reservation_rows(db_path)


# -- availability: missing and foreign share one error ----------------------------


def test_reserve_missing_operation_and_missing_decision_unavailable(
        service, cred, effect, db_path):
    # A prepared operation with no decision is indistinguishable from
    # a missing operation: same type, same fixed message.
    with pytest.raises(ApprovalReservationUnavailable) as no_decision:
        service.reserve_approval(
            cred.secret, now=at(70),
            operation_id=effect.operation_id,
            approval_id="had_0000000000000001",
            expected_effect_digest=effect.effect_digest,
            expected_preparation_digest=effect.preparation_digest)
    with pytest.raises(ApprovalReservationUnavailable) as missing:
        service.reserve_approval(
            cred.secret, now=at(70),
            operation_id="hop_0000000000000000",
            approval_id="had_0000000000000001",
            expected_effect_digest="0" * 64,
            expected_preparation_digest="1" * 64)
    assert str(no_decision.value) == str(missing.value)
    _assert_no_reservation_rows(db_path)


def test_cross_task_operation_is_unavailable(
        service, credentials, grant, policy, cred, effect, decision,
        reservation, db_path):
    cred_b = credentials.issue_task_credential(
        grant.secret, now=at(41), policy_id=policy.policy_id,
        expected_revision=1)
    assert cred_b.task_id != cred.task_id
    with pytest.raises(ApprovalReservationUnavailable) as foreign:
        _reserve(service, cred_b.secret, effect, decision, now=at(71))
    with pytest.raises(ApprovalReservationUnavailable) as missing:
        service.reserve_approval(
            cred_b.secret, now=at(71),
            operation_id="hop_0000000000000000",
            approval_id="had_0000000000000001",
            expected_effect_digest="0" * 64,
            expected_preparation_digest="1" * 64)
    # Same type and the identical fixed message: a foreign task's
    # operation is indistinguishable from a missing one.
    assert str(foreign.value) == str(missing.value)
    with pytest.raises(ApprovalReservationUnavailable):
        service.get_approval_reservation(
            cred_b.secret, now=at(72), operation_id=effect.operation_id)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1


# -- cross-installation/company bindings -----------------------------------------
#
# The helper below builds a FULLY valid, digest-self-consistent prepared
# effect AND approved decision on a second real stack (same owner
# principal, a different installation or company) using only the
# original modules' real flows, then copies both stored rows verbatim
# into the main database. Nothing is mocked and no digest verification
# is weakened: the copied rows genuinely pass row verification; only
# their installation/company/task binding is foreign to the reserving
# task.


def _copy_foreign_pair(tmp_path, db_path, *, installation_id,
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
        decisions = HostApprovalDecisionService(conn, identity,
                                                authorization,
                                                credentials, prepared)
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
        decision = _approve(decisions, foreign_grant.secret, effect)
        prepared_row = conn.execute(
            "SELECT operation_id, task_id, run_id, idempotency_key,"
            " effect_digest, preparation_digest, created_at,"
            " expires_at, record_blob, row_sha256"
            " FROM host_prepared_effects WHERE operation_id = ?",
            (effect.operation_id,)).fetchone()
        decision_row = conn.execute(
            "SELECT approval_id, operation_id, task_id, run_id,"
            " effect_digest, preparation_digest, decision, created_at,"
            " expires_at, record_blob, row_sha256"
            " FROM host_approval_decisions WHERE operation_id = ?",
            (effect.operation_id,)).fetchone()
    finally:
        conn.close()
    main = _connect(db_path)
    try:
        main.execute(
            "INSERT INTO host_prepared_effects (operation_id, task_id,"
            " run_id, idempotency_key, effect_digest,"
            " preparation_digest, created_at, expires_at, record_blob,"
            " row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            prepared_row)
        main.execute(
            "INSERT INTO host_approval_decisions (approval_id,"
            " operation_id, task_id, run_id, effect_digest,"
            " preparation_digest, decision, created_at, expires_at,"
            " record_blob, row_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", decision_row)
        main.commit()
    finally:
        main.close()
    return effect, decision


def _assert_foreign_matches_missing(service, secret, foreign_effect,
                                    foreign_decision):
    with pytest.raises(ApprovalReservationUnavailable) as foreign_exc:
        service.reserve_approval(
            secret, now=at(70), operation_id=foreign_effect.operation_id,
            approval_id=foreign_decision.approval_id,
            expected_effect_digest=foreign_effect.effect_digest,
            expected_preparation_digest=
            foreign_effect.preparation_digest)
    with pytest.raises(ApprovalReservationUnavailable) as missing_exc:
        service.reserve_approval(
            secret, now=at(70),
            operation_id="hop_0000000000000000",
            approval_id="had_0000000000000001",
            expected_effect_digest="0" * 64,
            expected_preparation_digest="1" * 64)
    # Same type and the identical fixed message: a foreign binding is
    # indistinguishable from a missing operation.
    assert str(foreign_exc.value) == str(missing_exc.value)


def test_reserve_cross_installation_pair_is_unavailable(
        service, cred, tmp_path, db_path):
    foreign_effect, foreign_decision = _copy_foreign_pair(
        tmp_path, db_path, installation_id="inst-syn-x1",
        company_id="co-syn-1", suffix="inst")
    assert foreign_effect.envelope["principal_id"] == "user-syn-owner"
    assert foreign_effect.envelope["installation_id"] != "inst-syn-1"
    _assert_foreign_matches_missing(service, cred.secret, foreign_effect,
                                    foreign_decision)
    _assert_no_reservation_rows(db_path)


def test_reserve_cross_company_pair_is_unavailable(
        service, cred, tmp_path, db_path):
    foreign_effect, foreign_decision = _copy_foreign_pair(
        tmp_path, db_path, installation_id="inst-syn-1",
        company_id="co-syn-x1", suffix="company")
    assert foreign_effect.envelope["principal_id"] == "user-syn-owner"
    assert foreign_effect.envelope["company_id"] != "co-syn-1"
    _assert_foreign_matches_missing(service, cred.secret, foreign_effect,
                                    foreign_decision)
    _assert_no_reservation_rows(db_path)


# -- caller input validation --------------------------------------------------------


def test_reserve_digest_and_approval_mismatch_zero_write(
        service, cred, effect, decision, db_path):
    with pytest.raises(ApprovalReservationMismatch):
        _reserve(service, cred.secret, effect, decision,
                 expected_effect_digest="0" * 64)
    with pytest.raises(ApprovalReservationMismatch):
        _reserve(service, cred.secret, effect, decision,
                 expected_preparation_digest="0" * 64)
    with pytest.raises(ApprovalReservationMismatch):
        _reserve(service, cred.secret, effect, decision,
                 expected_effect_digest=effect.preparation_digest,
                 expected_preparation_digest=effect.effect_digest)
    with pytest.raises(ApprovalReservationMismatch):
        _reserve(service, cred.secret, effect, decision,
                 approval_id="had_0000000000000001")
    _assert_no_reservation_rows(db_path)


def test_reserve_input_validation(service, cred, effect, decision,
                                  db_path):
    with pytest.raises(ValueError):
        _reserve(service, "", effect, decision)
    with pytest.raises(TypeError):
        _reserve(service, 0, effect, decision)
    with pytest.raises(ValueError):
        service.reserve_approval(
            cred.secret, now=datetime(2026, 10, 24, 12, 1, 10),
            **reserve_kwargs(effect, decision))
    with pytest.raises(TypeError):
        service.reserve_approval(
            cred.secret, now="2026-10-24T12:01:10+00:00",
            **reserve_kwargs(effect, decision))
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, operation_id="")
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, approval_id="")
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision,
                 expected_effect_digest="0" * 63)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision,
                 expected_effect_digest="0" * 63 + "A")
    with pytest.raises(TypeError):
        _reserve(service, cred.secret, effect, decision,
                 expected_effect_digest=0)
    _assert_no_reservation_rows(db_path)


def test_get_input_validation(service, cred, effect, reservation):
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=datetime(2026, 10, 24, 12, 1, 10),
            operation_id=effect.operation_id)
    with pytest.raises(ValueError):
        service.get_approval_reservation("", now=at(70),
                                         operation_id=effect.operation_id)
    with pytest.raises(ValueError):
        service.get_approval_reservation(cred.secret, now=at(70),
                                         operation_id="")


# -- error chains and message safety -------------------------------------------------


def test_lone_surrogate_task_secret_refused_with_clean_chain(
        service, effect, decision, db_path):
    visible = "task-secret-MARKER-4d21"
    bad_secret = visible + chr(0xD800)
    with pytest.raises(ValueError) as excinfo:
        _reserve(service, bad_secret, effect, decision)
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_reservation(
            bad_secret, now=at(70),
            operation_id="hop_0000000000000000")
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    _assert_no_reservation_rows(db_path)


def test_errors_never_echo_caller_input(service, cred, effect, decision,
                                        db_path):
    marker = "MARKER-7a31e0-private"
    cases = [
        {"operation_id": marker + chr(0xD800)},  # encoding failure
        {"approval_id": marker + chr(0xD800)},  # encoding failure
        {"expected_effect_digest": marker},  # digest shape failure
    ]
    for overrides in cases:
        with pytest.raises(Exception) as excinfo:
            _reserve(service, cred.secret, effect, decision, **overrides)
        _assert_clean_chain(excinfo, marker)
    _assert_no_reservation_rows(db_path)


# -- read-back behavior -------------------------------------------------------------


def test_get_missing_operation_unavailable(service, cred, effect,
                                           reservation):
    with pytest.raises(ApprovalReservationUnavailable) as excinfo:
        service.get_approval_reservation(
            cred.secret, now=at(75),
            operation_id="hop_0000000000000000")
    assert "hop_0000000000000000" not in str(excinfo.value)


def test_get_refuses_caller_transaction_and_preserves_it(
        service, cred, effect, reservation):
    conn = service.db
    conn.execute("BEGIN")
    conn.execute(
        "SELECT COUNT(*) FROM host_approval_reservations").fetchone()
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)
    # The refused caller transaction is never closed, committed or
    # rolled back by the module.
    assert conn.in_transaction
    conn.rollback()
    loaded = service.get_approval_reservation(
        cred.secret, now=at(75), operation_id=effect.operation_id)
    assert loaded.operation_id == effect.operation_id


# -- tombstones: expiry and revocation never delete or move a reservation ---------


def test_expired_reservation_stays_tombstone_and_never_reopens(
        service, grant, cred, decisions, effect, db_path):
    decision = _approve(decisions, grant.secret, effect,
                        expires_at=at(100))
    reservation = _reserve(service, cred.secret, effect, decision)
    # A replay after expiry never restores the reservation and never
    # writes a new row; there is no renewal and no re-expiry input.
    with pytest.raises(ApprovalReservationExpired):
        _reserve(service, cred.secret, effect, decision, now=at(150))
    with pytest.raises(ApprovalReservationExpired):
        service.get_approval_reservation(
            cred.secret, now=at(150), operation_id=effect.operation_id)
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reservation.reservation_id
    # Reopening a connection never clears the occupation.
    service.db.close()
    conn, reopened = _build_stack(db_path)
    try:
        with pytest.raises(ApprovalReservationExpired):
            reopened.get_approval_reservation(
                cred.secret, now=at(160),
                operation_id=effect.operation_id)
        assert _scalar(
            db_path,
            "SELECT COUNT(*) FROM host_approval_reservations") == 1
    finally:
        conn.close()


def test_revoked_task_keeps_tombstone(
        service, credentials, grant, cred, effect, decision,
        reservation, db_path):
    assert credentials.revoke_task_credential(
        grant.secret, now=at(80), task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        service.get_approval_reservation(
            cred.secret, now=at(81), operation_id=effect.operation_id)
    with pytest.raises(TaskCredentialRevoked):
        _reserve(service, cred.secret, effect, decision, now=at(81))
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reservation.reservation_id


# -- transaction ownership and atomicity ---------------------------------------------


def test_reserve_refuses_caller_transaction_and_preserves_it(
        service, cred, effect, decision, db_path):
    conn = service.db
    conn.execute("BEGIN")
    conn.execute(
        "SELECT COUNT(*) FROM host_approval_reservations").fetchone()
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision)
    assert conn.in_transaction
    conn.rollback()
    _assert_no_reservation_rows(db_path)
    reservation = _reserve(service, cred.secret, effect, decision)
    assert reservation.reservation_id.startswith("har_")


def test_failed_insert_rolls_back_completely(identity, grant, cred,
                                             effect, decision, db_path):
    identity.db.close()
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    decisions = HostApprovalDecisionService(conn, new_identity,
                                            authorization, credentials,
                                            prepared)
    failing = HostApprovalReservationService(conn, new_identity,
                                             authorization, credentials,
                                             prepared, decisions)
    conn.needle = "INSERT INTO host_approval_reservations"
    with pytest.raises(RuntimeError):
        failing.reserve_approval(cred.secret, now=at(70),
                                 **reserve_kwargs(effect, decision))
    conn.close()
    _assert_no_reservation_rows(db_path)
    # The decision, the prepared effect and the credential ledger are
    # untouched by the rolled-back attempt.
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_decisions") == 1
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
    monkeypatch.setattr("app.host_approval_reservations._monotonic_ns",
                        clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    decisions = HostApprovalDecisionService(conn, new_identity,
                                            authorization, credentials,
                                            prepared)
    return HostApprovalReservationService(conn, new_identity,
                                          authorization, credentials,
                                          prepared, decisions)


def test_reserve_rejected_when_lock_wait_crosses_expires(
        identity, cred, effect, decision, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before the decision's expires_at; the
    # simulated 2us write-lock wait moves the in-transaction effective
    # time one microsecond past it, so nothing may be written on the
    # stale pre-lock time.
    with pytest.raises(ApprovalReservationExpired):
        service.reserve_approval(
            cred.secret, now=at(300) - timedelta(microseconds=1),
            **reserve_kwargs(effect, decision))
    service.db.close()
    _assert_no_reservation_rows(db_path)


class _WaitOnFirstRead(sqlite3.Connection):
    """Connection advancing the fake clock when the fresh read snapshot
    is actually established by the first real SELECT."""

    clock = None

    def execute(self, sql, parameters=()):
        clock = self.clock
        if clock is not None and isinstance(sql, str) \
                and "FROM host_approval_reservations" in sql:
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
    monkeypatch.setattr("app.host_approval_reservations._monotonic_ns",
                        clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    prepared = HostPreparedEffectService(conn, new_identity,
                                         authorization, credentials)
    decisions = HostApprovalDecisionService(conn, new_identity,
                                            authorization, credentials,
                                            prepared)
    return HostApprovalReservationService(conn, new_identity,
                                          authorization, credentials,
                                          prepared, decisions)


def test_get_rejected_when_read_wait_crosses_expiry(
        service, grant, cred, decisions, effect, identity, db_path,
        monkeypatch):
    decision = _approve(decisions, grant.secret, effect,
                        expires_at=at(100))
    _reserve(service, cred.secret, effect, decision)
    service = _reopen_with_simulated_read_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before expires_at; the simulated 2us wait
    # establishing the read snapshot moves the effective time one
    # microsecond past expires_at, so the stale entry time must not
    # pass.
    with pytest.raises(ApprovalReservationExpired):
        service.get_approval_reservation(
            cred.secret, now=at(100) - timedelta(microseconds=1),
            operation_id=effect.operation_id)
    # The refused read closed its own snapshot and wrote nothing.
    assert not service.db.in_transaction
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1
    # A normal read at a valid time still returns the reservation
    # (after its ~at(70) creation, before the at(100) expiry).
    loaded = service.get_approval_reservation(
        cred.secret, now=at(80), operation_id=effect.operation_id)
    assert loaded.operation_id == effect.operation_id


# -- stored-row immutability guards -----------------------------------------------------


def test_rows_reject_update_delete_and_all_replace_classes(
        service, cred, effect, decision, reservation, db_path):
    ctx = cred.context
    forged_params = ("har_guard0000000001", "had_guard0000000001",
                     "hop_guard0000000001", ctx.task_id, ctx.run_id,
                     "0" * 64, "1" * 64, _time_text(at(70)),
                     _time_text(at(300)), b"{}", "2" * 64)
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_approval_reservations "
                         "SET approval_id = 'had_guard0000000009'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_approval_reservations")
        # INSERT OR REPLACE over the primary key (reservation_id).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_approval_reservations ("
                + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reservation.reservation_id,) + forged_params[1:])
        # INSERT OR REPLACE over the approval_id unique key with a
        # fresh reservation_id.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_approval_reservations ("
                + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (forged_params[0], reservation.approval_id)
                + forged_params[2:])
        # INSERT OR REPLACE over the operation_id unique key with fresh
        # reservation and approval ids.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_approval_reservations ("
                + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (forged_params[0], forged_params[1],
                 effect.operation_id) + forged_params[3:])
        # Plain duplicate-key inserts are refused as well.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_approval_reservations (" + _COLUMNS
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("har_guard0000000002", "had_guard0000000002",
                 effect.operation_id) + forged_params[3:])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_approval_reservations (" + _COLUMNS
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("har_guard0000000003", reservation.approval_id,
                 "hop_guard0000000003") + forged_params[3:])
    finally:
        conn.close()
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reservation.reservation_id
    assert rows[0][9] == reservation.record_bytes
    again = service.get_approval_reservation(
        cred.secret, now=at(75), operation_id=effect.operation_id)
    assert again.reservation_id == reservation.reservation_id


# -- forged stored rows: legal digests never excuse illegal content ------------------


def test_forged_row_wrong_row_digest_refused(service, cred, effect,
                                             decision, db_path):
    record = _forged_record(cred.context, effect, decision,
                            "har_forgerowdigesta1")
    _insert_forged_row(db_path, record, row_sha256="0" * 64)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_row_index_blob_mismatch_refused(service, cred, effect,
                                                decision, db_path):
    record = _forged_record(cred.context, effect, decision,
                            "har_forgeindexdrifa2")
    blob = git_format.dumps_record(record)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_approval_reservations (" + _COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["reservation_id"], record["approval_id"],
             record["operation_id"], record["task_id"],
             record["run_id"],
             "9" * 64,  # index digest disagrees with the blob
             record["preparation_digest"], record["created_at"],
             record["expires_at"], blob,
             hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_noncanonical_blob_refused(service, cred, effect,
                                          decision, db_path):
    record = _forged_record(cred.context, effect, decision,
                            "har_forgecanonicala3")
    _insert_forged_row(db_path, record, canonical=False)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_bool_schema_version_refused(service, cred, effect,
                                            decision, db_path):
    record = _forged_record(cred.context, effect, decision,
                            "har_forgeboolschemaa4", schema_version=True)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


@pytest.mark.parametrize("state", ("claimed", "consumed", "executed"))
def test_forged_unknown_reservation_state_refused(service, cred, effect,
                                                  decision, db_path,
                                                  state):
    # 'claimed', 'consumed' or 'executed' states do not exist in this
    # slice; the first-binding fact is the only storable state. Each
    # parametrized state gets its own fresh database, so the forged row
    # binds the REAL approval_id and the unique guard stays untouched.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgestate000c0",
                            reservation_state=state)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


@pytest.mark.parametrize(
    "created,expires",
    ((_time_text(at(400)), _time_text(at(300))),
     (_time_text(at(300)), _time_text(at(300)))),
    ids=("created-after-expires", "created-equals-expires"))
def test_forged_created_at_not_before_expires_refused(
        service, cred, effect, decision, db_path, created, expires):
    # created_at >= expires_at is illegal even when every digest is
    # self-consistent; the record-shape refusal fires on read-back.
    # Each parametrized case gets its own fresh database, so the forged
    # row binds the REAL approval_id and the unique guard stays
    # untouched.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgebadtime0c0",
                            created_at=created, expires_at=expires)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_binding_mismatch_refused(service, cred, effect,
                                         decision, db_path):
    # Self-consistent record and row digest, but the copied action does
    # not match the fully verified prepared envelope.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgebinding00c1", action="extract")
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_expires_mismatch_with_decision_refused(service, cred,
                                                       effect, decision,
                                                       db_path):
    # The reservation expiry is fixed to the decision's; a divergent
    # self-consistent expiry is a binding failure.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgeexpiry000c2",
                            expires_at=_time_text(at(250)))
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_future_created_at_refused(service, cred, effect,
                                          decision, db_path):
    # A reservation created after the current verification time breaks
    # the time chain even when every digest is self-consistent.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgefuture000c3",
                            created_at=_time_text(at(200)))
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)


def test_forged_bad_date_error_carries_no_stored_content(
        service, cred, effect, decision, db_path):
    visible = "MARKER-date-51cd"
    record = _forged_record(cred.context, effect, decision,
                            "har_forgebaddate0d1",
                            created_at="2026-10-24T12:01:10 " + visible)
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)
    _assert_clean_chain(excinfo, visible)


@pytest.mark.parametrize(
    "blob,marker",
    ((b'{"schema_version": 1, "x": MARKER-json-6b38}',
      "MARKER-json-6b38"),
     (b"MARKER-utf8-2e04\xff\xfe", "MARKER-utf8-2e04")),
    ids=("unparseable-json", "invalid-utf8"))
def test_forged_unparseable_blob_errors_carry_no_stored_content(
        service, cred, effect, decision, db_path, blob, marker):
    # Each parametrized case gets its own fresh database, so the forged
    # row binds the REAL approval_id and the unique guard stays
    # untouched; the digest is recomputed over the override blob, so
    # only the parse refusal can fire.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgebadblobd2")
    _insert_forged_row(db_path, record, blob_override=blob)
    with pytest.raises(ValueError) as excinfo:
        service.get_approval_reservation(
            cred.secret, now=at(75), operation_id=effect.operation_id)
    _assert_clean_chain(excinfo, marker)


# -- corrupted dependencies: prepared or decision rows -----------------------------


def test_reserve_refuses_corrupt_prepared_row(service, cred, db_path):
    # A forged prepared row with a wrong row digest fails the full
    # re-verification inside the reservation transaction.
    envelope = _forged_prepared_envelope(cred.context,
                                         "upload-batch-forged",
                                         "hop_forgeprepared01")
    _insert_forged_prepared_row(db_path, envelope, row_sha256="0" * 64)
    with pytest.raises(ValueError):
        service.reserve_approval(
            cred.secret, now=at(70),
            operation_id="hop_forgeprepared01",
            approval_id="had_0000000000000001",
            expected_effect_digest=envelope["effect_digest"],
            expected_preparation_digest=envelope["preparation_digest"])
    _assert_no_reservation_rows(db_path)


def test_reserve_refuses_corrupt_decision_row(service, grant, cred,
                                              effect, db_path):
    record = _forged_decision_record(
        cred.context, grant.session_ref, effect.operation_id,
        "had_forgerowdiges01",
        effect_digest=effect.effect_digest,
        preparation_digest=effect.preparation_digest)
    _insert_forged_decision_row(db_path, record, row_sha256="0" * 64)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect,
                 _FakeDecision("had_forgerowdiges01"), now=at(70))
    _assert_no_reservation_rows(db_path)


class _FakeDecision:
    """Carries only an approval_id for reserve-kwargs construction when
    the stored decision row itself is forged."""

    def __init__(self, approval_id):
        self.approval_id = approval_id


# -- creation-time ordering: the chain this slice adds on top ----------------------


def test_reserve_refuses_decision_predating_prepared(
        service, grant, cred, effect, db_path):
    # The decision service's internal binding check never compares
    # creation times; a self-consistent decision recorded BEFORE its
    # prepared effect is refused by this module's own time chain.
    record = _forged_decision_record(
        cred.context, grant.session_ref, effect.operation_id,
        "had_forgepredate01",
        effect_digest=effect.effect_digest,
        preparation_digest=effect.preparation_digest,
        created_at=_time_text(at(45)))
    _insert_forged_decision_row(db_path, record)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect,
                 _FakeDecision("had_forgepredate01"), now=at(70))
    _assert_no_reservation_rows(db_path)


def test_reserve_refuses_decision_created_in_the_future(
        service, grant, cred, effect, db_path):
    # A decision whose created_at is after the reservation's effective
    # time breaks reservation.created_at >= decision.created_at.
    record = _forged_decision_record(
        cred.context, grant.session_ref, effect.operation_id,
        "had_forgefutured01",
        effect_digest=effect.effect_digest,
        preparation_digest=effect.preparation_digest,
        created_at=_time_text(at(200)))
    _insert_forged_decision_row(db_path, record)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect,
                 _FakeDecision("had_forgefutured01"), now=at(70))
    _assert_no_reservation_rows(db_path)


def test_reserve_refuses_decision_validity_beyond_prepared(
        service, grant, cred, effect, db_path):
    record = _forged_decision_record(
        cred.context, grant.session_ref, effect.operation_id,
        "had_forgelongexp001",
        effect_digest=effect.effect_digest,
        preparation_digest=effect.preparation_digest,
        expires_at=_time_text(at(700)))
    _insert_forged_decision_row(db_path, record)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect,
                 _FakeDecision("had_forgelongexp001"), now=at(70))
    _assert_no_reservation_rows(db_path)


def test_reserve_replay_refuses_row_predating_decision(
        service, cred, effect, decision, db_path):
    # A canonical, digest-self-consistent stored row whose created_at
    # (at(55), after the prepared effect's at(50)) predates the REAL
    # decision's creation (at(60)) is not the first binding: the
    # replay path re-checks the complete time chain against the stored
    # created_at, not only against the newly proposed tx_now.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgepredater1",
                            created_at=_time_text(at(55)))
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, now=at(70))
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == "har_forgepredater1"


def test_reserve_replay_refuses_row_created_in_the_future(
        service, cred, effect, decision, db_path):
    # A canonical, digest-self-consistent stored row created after the
    # current verification time breaks reservation.created_at <=
    # tx_now on the replay path as well.
    record = _forged_record(cred.context, effect, decision,
                            "har_forgefuturer2",
                            created_at=_time_text(at(200)))
    _insert_forged_row(db_path, record)
    with pytest.raises(ValueError):
        _reserve(service, cred.secret, effect, decision, now=at(70))
    rows = _reservation_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == "har_forgefuturer2"


# -- cross-connection concurrency -----------------------------------------------------


def _thread_reserve(db_path, barrier, results, index, secret, call):
    conn = _connect(db_path)
    try:
        identity = HostIdentityService(conn)
        authorization = HostAuthorizationService(conn, identity)
        credentials = HostTaskCredentialService(conn, identity,
                                                authorization)
        prepared = HostPreparedEffectService(conn, identity,
                                             authorization, credentials)
        decisions = HostApprovalDecisionService(conn, identity,
                                                authorization,
                                                credentials, prepared)
        service = HostApprovalReservationService(conn, identity,
                                                 authorization,
                                                 credentials, prepared,
                                                 decisions)
        barrier.wait(timeout=30)
        results[index] = ("ok", _reserve(service, secret, call[0],
                                         call[1], **call[2]))
    except BaseException as exc:
        results[index] = ("error", exc)
    finally:
        conn.close()


def _run_concurrent_pair(db_path, secret, effect, decision,
                         overrides_a, overrides_b):
    barrier = threading.Barrier(2)
    results = [None, None]
    threads = [
        threading.Thread(
            target=_thread_reserve,
            args=(db_path, barrier, results, index, secret,
                  (effect, decision, overrides)))
        for index, overrides in enumerate((overrides_a, overrides_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    for thread in threads:
        assert not thread.is_alive(), \
            "concurrent reserve thread did not finish"
    assert all(entry is not None for entry in results)
    return results


def test_concurrent_same_request_single_row(service, cred, effect,
                                            decision, db_path):
    results = _run_concurrent_pair(db_path, cred.secret, effect,
                                   decision, {}, {})
    for status, value in results:
        if status != "ok":
            raise value
    first, second = results[0][1], results[1][1]
    assert first.reservation_id == second.reservation_id
    assert first.created_at == second.created_at
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1


def test_concurrent_divergent_binding_single_legitimate_row(
        service, cred, effect, decision, db_path):
    # Only the exact verified binding can ever be stored: the divergent
    # request fails and the unique legitimate row wins.
    results = _run_concurrent_pair(
        db_path, cred.secret, effect, decision, {},
        {"expected_effect_digest": "0" * 64})
    statuses = sorted(status for status, _ in results)
    assert statuses == ["error", "ok"]
    error = next(value for status, value in results
                 if status == "error")
    if not isinstance(error, ApprovalReservationMismatch):
        raise error
    rows = _reservation_rows(db_path)
    assert len(rows) == 1


def test_concurrent_insert_race_replays_identical(service, cred, effect,
                                                  decision, db_path,
                                                  monkeypatch):
    # Both threads pass verification and race the INSERT; the loser's
    # IntegrityError backstop replays the winner's row.
    results = _run_concurrent_pair(db_path, cred.secret, effect,
                                   decision, {}, {})
    for status, value in results:
        if status != "ok":
            raise value
    assert results[0][1].reservation_id == results[1][1].reservation_id
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_approval_reservations") == 1


# -- result immutability and secrecy ----------------------------------------------------


def test_result_is_deeply_immutable_and_exports_copies(
        service, cred, effect, decision):
    reservation = _reserve(service, cred.secret, effect, decision)
    with pytest.raises(dataclasses.FrozenInstanceError):
        reservation.reservation_id = "har_changed0000000"
    with pytest.raises(TypeError):
        reservation.record["reservation_state"] = "consumed"
    assert isinstance(reservation.record, MappingProxyType)
    exported = reservation.to_dict()
    exported["reservation_state"] = "consumed"
    exported["nested"] = {"injected": True}
    fresh = reservation.to_dict()
    assert fresh["reservation_state"] == "reserved"
    assert "nested" not in fresh


def test_result_carries_no_secret_or_credential_digest(
        service, grant, cred, effect, decision):
    reservation = _reserve(service, cred.secret, effect, decision)
    digest = hashlib.sha256(
        b"sam-host-task-credentials credential-v1\x00"
        + cred.secret.encode("utf-8")).hexdigest()
    session_digest = hashlib.sha256(
        b"sam-host-identity session-v1\x00"
        + grant.secret.encode("utf-8")).hexdigest()
    blob_text = reservation.record_bytes.decode("utf-8")
    rendered = repr(reservation)
    serialized = json.dumps(reservation.to_dict())
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
    decision = _approve(service.approval_decisions, grant.secret, effect)
    reservation = _reserve(service, cred.secret, effect, decision)
    assert reservation.to_dict()["company_id"] == "co-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()
