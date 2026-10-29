"""Tests for app.host_audit_refs (F-007 / CHG-068 actor audit refs).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data, no models. Identity, policy and
task-credential state are built synthetically through the real
HostIdentityService pairing flow, the real HostAuthorizationService
ledger and the real HostTaskCredentialService issuance path.
"""
import copy
import hashlib
import json
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone

import pytest

from app.host_audit_refs import (
    ActorAuditRefInvalid,
    ActorAuditRefMismatch,
    HostAuditReferenceService,
    validate_actor_audit_ref,
)
from app.host_authorization import (
    HostAuthorizationService,
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
    TaskCredentialInvalid,
    TaskCredentialRevoked,
    TaskMembershipRevoked,
    TaskPolicyExpired,
    TaskPolicyStale,
    TaskScopeDenied,
)

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _credential_digest(secret):
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


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-audit-refs.db"


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
    return HostTaskCredentialService(identity.db, identity,
                                     authorization)


@pytest.fixture
def audit(identity, credentials):
    return HostAuditReferenceService(identity, credentials)


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
def user_ref(audit, grant):
    return audit.user_actor_ref(grant.secret, now=at(50))


@pytest.fixture
def task_ref(audit, cred):
    return audit.task_actor_ref(cred.secret, now=at(50))


class _IntSub(int):
    """An int subclass: never a valid schema_version or revision."""


# -- pure structural validation ----------------------------------------------

def _user_ref_dict(**overrides):
    ref = {
        "schema_version": 1,
        "audit_ref_type": "user_actor",
        "installation_id": "inst-syn-1",
        "company_id": "co-syn-1",
        "principal_id": "user-syn-owner",
        "member_revision": 1,
        "session_ref": "hss_0123456789abcdef",
    }
    ref.update(overrides)
    return ref


def _task_ref_dict(**overrides):
    ref = {
        "schema_version": 1,
        "audit_ref_type": "task_actor",
        "installation_id": "inst-syn-1",
        "company_id": "co-syn-1",
        "principal_id": "user-syn-owner",
        "member_revision": 1,
        "task_id": "htk_0123456789abcdef",
        "run_id": "htr_0123456789abcdef",
        "policy_id": "hap_0123456789abcdef",
        "policy_revision": 1,
    }
    ref.update(overrides)
    return ref


def test_validate_accepts_both_frozen_shapes():
    assert validate_actor_audit_ref(_user_ref_dict()) is None
    assert validate_actor_audit_ref(_task_ref_dict()) is None


@pytest.mark.parametrize("bad", [None, "user_actor", 1, True, 1.0,
                                 [("audit_ref_type", "user_actor")],
                                 {"user_actor"}])
def test_validate_rejects_non_dict(bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(bad)


def test_validate_rejects_non_string_keys():
    ref = _user_ref_dict()
    ref[7] = "x"
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(ref)
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref({7: "x", "audit_ref_type": "user_actor"})


@pytest.mark.parametrize("field", ["schema_version", "audit_ref_type",
                                   "installation_id", "company_id",
                                   "principal_id", "member_revision",
                                   "session_ref"])
def test_validate_rejects_missing_user_fields(field):
    ref = _user_ref_dict()
    del ref[field]
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(ref)


@pytest.mark.parametrize("field", ["schema_version", "audit_ref_type",
                                   "installation_id", "company_id",
                                   "principal_id", "member_revision",
                                   "task_id", "run_id", "policy_id",
                                   "policy_revision"])
def test_validate_rejects_missing_task_fields(field):
    ref = _task_ref_dict()
    del ref[field]
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(ref)


@pytest.mark.parametrize("extra", ["approval_id", "record_type", "repo_id",
                                   "typed_ref", "role", "scope", "unknown"])
def test_validate_rejects_unknown_fields(extra):
    for ref in (_user_ref_dict(), _task_ref_dict()):
        ref[extra] = "whatever"
        with pytest.raises(ActorAuditRefInvalid):
            validate_actor_audit_ref(ref)


@pytest.mark.parametrize("bad", [0, 2, -1, "1", 1.0, True, False, None,
                                 _IntSub(1)])
def test_validate_rejects_bad_schema_version(bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_user_ref_dict(schema_version=bad))


@pytest.mark.parametrize("bad", ["approval", "approval_actor", "user",
                                 "task", "", "USER_ACTOR", 1, None, True])
def test_validate_rejects_unknown_ref_type(bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_user_ref_dict(audit_ref_type=bad))


@pytest.mark.parametrize("field", ["installation_id", "company_id",
                                   "principal_id", "session_ref"])
@pytest.mark.parametrize("bad", ["", None, 123, True, b"bytes",
                                 "x" * 129])
def test_validate_rejects_invalid_user_ids(field, bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_user_ref_dict(**{field: bad}))


@pytest.mark.parametrize("field", ["task_id", "run_id", "policy_id"])
@pytest.mark.parametrize("bad", ["", None, 123, True, b"bytes",
                                 "x" * 129])
def test_validate_rejects_invalid_task_ids(field, bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_task_ref_dict(**{field: bad}))


@pytest.mark.parametrize("field", ["member_revision"])
@pytest.mark.parametrize("bad", [0, -3, "2", 1.5, True, None, _IntSub(2)])
def test_validate_rejects_bad_user_revision(field, bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_user_ref_dict(**{field: bad}))


@pytest.mark.parametrize("field", ["member_revision", "policy_revision"])
@pytest.mark.parametrize("bad", [0, -3, "2", 1.5, True, None, _IntSub(2)])
def test_validate_rejects_bad_task_revision(field, bad):
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(_task_ref_dict(**{field: bad}))


def test_validate_never_modifies_input():
    valid = _user_ref_dict()
    snapshot = copy.deepcopy(valid)
    validate_actor_audit_ref(valid)
    assert valid == snapshot
    invalid = _user_ref_dict(member_revision=-7)
    invalid_snapshot = copy.deepcopy(invalid)
    with pytest.raises(ActorAuditRefInvalid):
        validate_actor_audit_ref(invalid)
    assert invalid == invalid_snapshot


def test_validate_error_messages_never_echo_values():
    marker = "MARKER-SECRET-VALUE-9f3c"
    cases = [
        _user_ref_dict(audit_ref_type="approval_" + marker),
        _user_ref_dict(principal_id=12345678),
        {**_user_ref_dict(), "approval_id": marker},
        _task_ref_dict(member_revision=-999999,
                       policy_id="pol-" + marker + "-x" * 200),
    ]
    for ref in cases:
        with pytest.raises(ActorAuditRefInvalid) as excinfo:
            validate_actor_audit_ref(ref)
        assert marker not in str(excinfo.value)
        assert "12345678" not in str(excinfo.value)
        assert "999999" not in str(excinfo.value)


_SURROGATE_MARKER = "SURROGATE-ID-SECRET-MARKER-7d21"
# A host-id-shaped value whose lone surrogate makes UTF-8 encoding
# fail inside git_format.encode_id; the marker must never leak through
# the raised exception, its chain or its formatted traceback.
_SURROGATE_ID = "hss_" + _SURROGATE_MARKER + "-\ud800"


@pytest.mark.parametrize("ref,field", [
    (_user_ref_dict(session_ref=_SURROGATE_ID), "session_ref"),
    (_user_ref_dict(principal_id=_SURROGATE_ID), "principal_id"),
    (_task_ref_dict(task_id=_SURROGATE_ID), "task_id"),
    (_task_ref_dict(run_id=_SURROGATE_ID), "run_id"),
    (_task_ref_dict(policy_id=_SURROGATE_ID), "policy_id"),
], ids=["user-session_ref", "user-principal_id", "task-task_id",
        "task-run_id", "task-policy_id"])
def test_surrogate_id_exception_chain_leaks_nothing(ref, field):
    with pytest.raises(ActorAuditRefInvalid) as excinfo:
        validate_actor_audit_ref(ref)
    exc = excinfo.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert _SURROGATE_MARKER not in str(exc)
    assert _SURROGATE_MARKER not in repr(exc)
    formatted = "".join(traceback.format_exception(exc))
    assert _SURROGATE_MARKER not in formatted


# -- constructor contract ------------------------------------------------------

def _second_stack(tmp_path):
    conn = _connect(tmp_path / "host-audit-refs-second.db")
    identity = HostIdentityService(conn)
    identity.initialize_installation(
        installation_id="inst-syn-2", company_id="co-syn-2",
        owner_principal_id="user-syn-owner-2", owner_role="owner",
        now=T0)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity,
                                            authorization)
    return conn, identity, credentials


def _audit_tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'host_audit%'")}


def test_constructor_accepts_real_bound_services(identity, credentials):
    service = HostAuditReferenceService(identity, credentials)
    assert service.identity is identity
    assert service.task_credentials is credentials


def test_constructor_runs_no_ddl(identity, credentials):
    assert _audit_tables(identity.db) == set()
    HostAuditReferenceService(identity, credentials)
    assert _audit_tables(identity.db) == set()


def test_constructor_rejects_wrong_types(identity, credentials):
    with pytest.raises(TypeError):
        HostAuditReferenceService(object(), credentials)
    with pytest.raises(TypeError):
        HostAuditReferenceService(identity, object())
    # A real identity service is still not a task credential service.
    with pytest.raises(TypeError):
        HostAuditReferenceService(identity, identity)
    # Nor does swapping the arguments type-check.
    with pytest.raises(TypeError):
        HostAuditReferenceService(credentials, identity)


def test_constructor_rejects_divergent_identity_instance(identity,
                                                         credentials):
    # A second HostIdentityService on the same connection is still a
    # divergent identity binding: the task credential service is bound
    # to the first instance.
    other_identity = HostIdentityService(identity.db)
    with pytest.raises(ValueError):
        HostAuditReferenceService(other_identity, credentials)


def test_constructor_rejects_cross_connection_assembly(tmp_path, identity,
                                                       credentials):
    conn2, _identity2, credentials2 = _second_stack(tmp_path)
    try:
        with pytest.raises(ValueError):
            HostAuditReferenceService(identity, credentials2)
        assert _audit_tables(identity.db) == set()
        assert _audit_tables(conn2) == set()
    finally:
        conn2.close()


# -- generation and round-trip -------------------------------------------------

def test_user_actor_ref_exact_shape_and_roundtrip(audit, grant):
    ref = audit.user_actor_ref(grant.secret, now=at(50))
    assert ref == {
        "schema_version": 1,
        "audit_ref_type": "user_actor",
        "installation_id": "inst-syn-1",
        "company_id": "co-syn-1",
        "principal_id": "user-syn-owner",
        "member_revision": grant.context.member_revision,
        "session_ref": grant.session_ref,
    }
    assert validate_actor_audit_ref(ref) is None
    assert audit.verify_user_actor_ref(ref, grant.secret,
                                       now=at(60)) is None


def test_task_actor_ref_exact_shape_and_roundtrip(audit, cred, policy):
    ref = audit.task_actor_ref(cred.secret, now=at(50))
    assert ref == {
        "schema_version": 1,
        "audit_ref_type": "task_actor",
        "installation_id": "inst-syn-1",
        "company_id": "co-syn-1",
        "principal_id": "user-syn-owner",
        "member_revision": cred.context.member_revision,
        "task_id": cred.task_id,
        "run_id": cred.run_id,
        "policy_id": policy.policy_id,
        "policy_revision": 1,
    }
    assert validate_actor_audit_ref(ref) is None
    assert audit.verify_task_actor_ref(ref, cred.secret,
                                       now=at(60)) is None


def test_refs_carry_no_secret_material(audit, grant, cred):
    for ref in (audit.user_actor_ref(grant.secret, now=at(50)),
                audit.task_actor_ref(cred.secret, now=at(50))):
        blob = json.dumps(ref, sort_keys=True) + repr(ref)
        assert grant.secret not in blob
        assert cred.secret not in blob


# -- verification refusals -------------------------------------------------------

def test_verify_rejects_structurally_invalid_ref(audit, grant, cred):
    with pytest.raises(ActorAuditRefInvalid):
        audit.verify_user_actor_ref({"audit_ref_type": "user_actor"},
                                    grant.secret, now=at(50))
    with pytest.raises(ActorAuditRefInvalid):
        audit.verify_task_actor_ref(_task_ref_dict(policy_revision=0),
                                    cred.secret, now=at(50))


def test_verify_rejects_the_other_ref_type(audit, grant, cred,
                                           user_ref, task_ref):
    with pytest.raises(ActorAuditRefMismatch):
        audit.verify_user_actor_ref(task_ref, grant.secret, now=at(50))
    with pytest.raises(ActorAuditRefMismatch):
        audit.verify_task_actor_ref(user_ref, cred.secret, now=at(50))


@pytest.mark.parametrize("field,value", [
    ("installation_id", "inst-syn-other"),
    ("company_id", "co-syn-other"),
    ("principal_id", "user-syn-other"),
    ("member_revision", 2),
    ("session_ref", "hss_ffffffffffffffff"),
])
def test_verify_user_ref_rejects_tampered_fields(audit, grant, user_ref,
                                                 field, value):
    tampered = dict(user_ref)
    tampered[field] = value
    with pytest.raises(ActorAuditRefMismatch) as excinfo:
        audit.verify_user_actor_ref(tampered, grant.secret, now=at(50))
    message = str(excinfo.value)
    assert str(value) not in message
    assert str(user_ref[field]) not in message
    assert tampered != user_ref  # the copy, not the input, was changed


@pytest.mark.parametrize("field,value", [
    ("installation_id", "inst-syn-other"),
    ("company_id", "co-syn-other"),
    ("principal_id", "user-syn-other"),
    ("member_revision", 2),
    ("task_id", "htk_ffffffffffffffff"),
    ("run_id", "htr_ffffffffffffffff"),
    ("policy_id", "hap_ffffffffffffffff"),
    ("policy_revision", 2),
])
def test_verify_task_ref_rejects_tampered_fields(audit, cred, task_ref,
                                                 field, value):
    tampered = dict(task_ref)
    tampered[field] = value
    with pytest.raises(ActorAuditRefMismatch) as excinfo:
        audit.verify_task_actor_ref(tampered, cred.secret, now=at(50))
    message = str(excinfo.value)
    assert str(value) not in message
    assert str(task_ref[field]) not in message


def test_verify_does_not_modify_the_input_ref(audit, grant, user_ref):
    snapshot = copy.deepcopy(user_ref)
    assert audit.verify_user_actor_ref(user_ref, grant.secret,
                                       now=at(50)) is None
    assert user_ref == snapshot
    tampered = dict(user_ref, member_revision=9)
    tampered_snapshot = copy.deepcopy(tampered)
    with pytest.raises(ActorAuditRefMismatch):
        audit.verify_user_actor_ref(tampered, grant.secret, now=at(50))
    assert tampered == tampered_snapshot


def test_verify_rejects_unknown_secrets(audit, user_ref, task_ref):
    with pytest.raises(Unauthenticated):
        audit.verify_user_actor_ref(user_ref, "wrong-secret-value",
                                    now=at(50))
    with pytest.raises(TaskCredentialInvalid):
        audit.verify_task_actor_ref(task_ref, "wrong-secret-value",
                                    now=at(50))
    with pytest.raises(Unauthenticated):
        audit.user_actor_ref("wrong-secret-value", now=at(50))
    with pytest.raises(TaskCredentialInvalid):
        audit.task_actor_ref("wrong-secret-value", now=at(50))


def test_old_ref_does_not_match_a_new_session(audit, identity, grant,
                                              user_ref):
    ticket = identity.issue_pairing_credential(now=at(70),
                                               lifetime_seconds=600)
    grant2 = identity.pair(ticket.secret, now=at(80),
                           session_lifetime_seconds=3600)
    # The old reference names the old session_ref; resolving the new
    # session's secret yields a different session_ref, so the old ref
    # can never verify against the new session.
    with pytest.raises(ActorAuditRefMismatch):
        audit.verify_user_actor_ref(user_ref, grant2.secret, now=at(90))
    assert audit.verify_user_actor_ref(user_ref, grant.secret,
                                       now=at(90)) is None


# -- live re-resolution: revocation and expiry ---------------------------------

def test_session_revocation_fails_the_next_calls(audit, identity, grant,
                                                 user_ref):
    assert audit.verify_user_actor_ref(user_ref, grant.secret,
                                       now=at(100)) is None
    assert identity.revoke_session(grant.session_ref, now=at(110)) is True
    with pytest.raises(SessionRevoked):
        audit.user_actor_ref(grant.secret, now=at(120))
    with pytest.raises(SessionRevoked):
        audit.verify_user_actor_ref(user_ref, grant.secret, now=at(120))


def test_session_expiry_fails_the_next_calls(audit, grant, user_ref):
    # The session was issued at at(20) with a 3600-second lifetime.
    with pytest.raises(SessionExpired):
        audit.user_actor_ref(grant.secret, now=at(4000))
    with pytest.raises(SessionExpired):
        audit.verify_user_actor_ref(user_ref, grant.secret, now=at(4000))


def test_member_revocation_fails_user_and_task_resolution(
        audit, identity, grant, cred, user_ref, task_ref):
    assert audit.verify_user_actor_ref(user_ref, grant.secret,
                                       now=at(100)) is None
    assert audit.verify_task_actor_ref(task_ref, cred.secret,
                                       now=at(100)) is None
    assert identity.revoke_member("user-syn-owner", now=at(110)) is True
    with pytest.raises(MembershipRevoked):
        audit.user_actor_ref(grant.secret, now=at(120))
    with pytest.raises(MembershipRevoked):
        audit.verify_user_actor_ref(user_ref, grant.secret, now=at(120))
    with pytest.raises(TaskMembershipRevoked):
        audit.task_actor_ref(cred.secret, now=at(120))
    with pytest.raises(TaskMembershipRevoked):
        audit.verify_task_actor_ref(task_ref, cred.secret, now=at(120))


def test_task_revocation_fails_the_next_calls(audit, credentials, grant,
                                              cred, task_ref):
    assert audit.verify_task_actor_ref(task_ref, cred.secret,
                                       now=at(100)) is None
    assert credentials.revoke_task_credential(
        grant.secret, now=at(110), task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        audit.task_actor_ref(cred.secret, now=at(120))
    with pytest.raises(TaskCredentialRevoked):
        audit.verify_task_actor_ref(task_ref, cred.secret, now=at(120))


def test_policy_pause_stales_the_task_ref(audit, authorization, grant,
                                          cred, task_ref):
    assert audit.verify_task_actor_ref(task_ref, cred.secret,
                                       now=at(100)) is None
    authorization.pause_policy(grant.secret, now=at(110),
                               policy_id=task_ref["policy_id"],
                               expected_revision=1)
    # The pause appended revision 2; a credential bound to revision 1
    # never silently flows into the new revision.
    with pytest.raises(TaskPolicyStale):
        audit.task_actor_ref(cred.secret, now=at(120))
    with pytest.raises(TaskPolicyStale):
        audit.verify_task_actor_ref(task_ref, cred.secret, now=at(120))


def test_policy_revocation_stales_the_task_ref(audit, authorization,
                                               grant, cred, task_ref):
    authorization.revoke_policy(grant.secret, now=at(110),
                                policy_id=task_ref["policy_id"],
                                expected_revision=1)
    with pytest.raises(TaskPolicyStale):
        audit.verify_task_actor_ref(task_ref, cred.secret, now=at(120))


def test_policy_and_credential_expiry_fail_the_next_calls(audit, cred,
                                                          task_ref):
    # Policy and credential validity both end at at(3600); the policy
    # boundary is checked first.
    with pytest.raises(TaskPolicyExpired):
        audit.task_actor_ref(cred.secret, now=at(4000))
    with pytest.raises(TaskPolicyExpired):
        audit.verify_task_actor_ref(task_ref, cred.secret, now=at(4000))


def test_stored_scope_widening_is_refused_live(audit, db_path, policy,
                                               grant):
    """A forged stored credential whose scope is wider than its policy
    fails closed through the real resolver even while naming the exact
    bound policy revision."""
    forged_scope = _build_scope(
        **scope_kwargs(actions=["read", "extract", "delete"]))
    secret = "forged-task-secret-marker"
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_task_credentials "
            "(task_id, run_id, credential_sha256, parent_task_id, "
            "installation_id, company_id, principal_id, "
            "member_revision, issued_by_session_ref, policy_id, "
            "policy_revision, scope_json, issued_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            ("htk_forged00000001", "htr_forged00000001",
             _credential_digest(secret), "inst-syn-1", "co-syn-1",
             "user-syn-owner", grant.session_ref, policy.policy_id, 1,
             _dumps_scope(forged_scope), at(40).isoformat()))
        conn.execute(
            "INSERT INTO host_task_credential_events "
            "(task_id, event_seq, event, revocation_seq, "
            "actor_principal_id, actor_member_revision, session_ref, "
            "changed_at) "
            "VALUES (?, 1, 'issued', 0, ?, 1, ?, ?)",
            ("htk_forged00000001", "user-syn-owner", grant.session_ref,
             at(40).isoformat()))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(TaskScopeDenied):
        audit.task_actor_ref(secret, now=at(50))
