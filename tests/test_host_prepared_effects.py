"""Tests for app.host_prepared_effects (F-007 immutable prepared-effect
envelopes).

All tests use tmp_path-isolated SQLite files and synthetic identifiers,
per the F-007 card boundary: no default real database, no environment
configuration, no real company data, no models and no Git access.
Identity and policy state are built synthetically through the real
HostIdentityService pairing flow, the real HostAuthorizationService
ledger and the real HostTaskCredentialService issuance path; the
expected digests are recomputed independently in this file, including
the domain-separation constants. Generic helpers (clock, connection,
scope template, fault injection) are imported from the sibling root-task
credential tests instead of being copied.
"""
import dataclasses
import hashlib
import json
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import app.host_prepared_effects as prepared_module
from app.facts_tree import git_format
from app.host_authorization import HostAuthorizationService
from app.host_identity import HostIdentityService
from app.host_prepared_effects import (
    HostPreparedEffectService,
    PreparedEffect,
    PreparedEffectConflict,
    PreparedEffectExpired,
    PreparedEffectUnavailable,
    PreparedScopeDenied,
)
from app.host_task_credentials import (
    HostTaskCredentialService,
    TaskCredentialExpired,
    TaskCredentialInvalid,
    TaskCredentialRevoked,
    TaskMembershipRevoked,
    TaskPolicyStale,
)
from tests.test_host_task_credentials import (  # reused pure helpers
    T0,
    _FailOn,
    _LockWaitClock,
    _WaitOnBegin,
    _connect,
    at,
    scope_kwargs as _credential_scope_kwargs,
)

BASE40 = "a" * 40
BASE64 = "b" * 64

# Domain-separation constants pinned independently of the module.
_EFFECT_DOMAIN = b"sam-host-prepared-effect effect-v1\x00"
_PREPARATION_DOMAIN = b"sam-host-prepared-effect preparation-v1\x00"
_ROW_DOMAIN = b"sam-host-prepared-effect row-v1\x00"

_EFFECT_PAYLOAD_KEYS = (
    "action", "target", "objects", "effect_manifest", "object_format",
    "base_commit", "dependency_versions")
_PREPARATION_PAYLOAD_KEYS = (
    "schema_version", "installation_id", "company_id", "principal_id",
    "member_revision", "task_id", "run_id", "policy_id",
    "policy_revision", "idempotency_key", "expires_at", "effect_digest")

EXPECTED_ENVELOPE_KEYS = frozenset({
    "schema_version", "preparation_state", "operation_id",
    "installation_id", "company_id", "principal_id", "member_revision",
    "task_id", "run_id", "policy_id", "policy_revision",
    "idempotency_key", "action", "target", "objects",
    "effect_manifest", "object_format", "base_commit",
    "dependency_versions", "effect_digest", "preparation_digest",
    "created_at", "expires_at"})


def _time_text(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds")


def scope_kwargs(**overrides):
    # Literal object names only: the prepared-effect slice checks exact
    # scope membership and never interprets wildcards or URI prefixes.
    kwargs = _credential_scope_kwargs(
        objects=["sam://original/a.txt", "sam://original/b.txt",
                 "sam://original/c.txt"])
    kwargs.update(overrides)
    return kwargs


def effect_kwargs(**overrides):
    kwargs = {
        "idempotency_key": "upload-batch-7",
        "action": "read",
        "target": "uploads-inbox",
        "objects": ["sam://original/b.txt", "sam://original/a.txt"],
        "effect_manifest": {"source_batch": "batch-7",
                            "notes": "synthetic"},
        "object_format": "sha1",
        "base_commit": BASE40,
        "dependency_versions": {"taxonomy_pack": "generic-v1",
                                "plan": "plan-v1"},
        "expires_at": at(600),
    }
    kwargs.update(overrides)
    return kwargs


def _prepare(service, cred, now=at(50), **overrides):
    return service.prepare_effect(cred.secret, now=now,
                                  **effect_kwargs(**overrides))


def _expected_effect_digest(action, target, sorted_objects, manifest,
                            fmt, base, deps):
    payload = {
        "action": action,
        "target": target,
        "objects": list(sorted_objects),
        "effect_manifest": manifest,
        "object_format": fmt,
        "base_commit": base,
        "dependency_versions": deps,
    }
    return hashlib.sha256(
        _EFFECT_DOMAIN + git_format.dumps_record(payload)).hexdigest()


def _expected_preparation_digest(ctx, idempotency_key, expires_at,
                                 effect_digest):
    payload = {
        "schema_version": 1,
        "installation_id": ctx.installation_id,
        "company_id": ctx.company_id,
        "principal_id": ctx.principal_id,
        "member_revision": ctx.member_revision,
        "task_id": ctx.task_id,
        "run_id": ctx.run_id,
        "policy_id": ctx.policy_id,
        "policy_revision": ctx.policy_revision,
        "idempotency_key": idempotency_key,
        "expires_at": _time_text(expires_at),
        "effect_digest": effect_digest,
    }
    return hashlib.sha256(
        _PREPARATION_DOMAIN
        + git_format.dumps_record(payload)).hexdigest()


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


def _effect_rows(db_path):
    return _rows(db_path,
                 "SELECT operation_id, task_id, run_id, idempotency_key,"
                 " effect_digest, preparation_digest, created_at,"
                 " expires_at, record_blob, row_sha256"
                 " FROM host_prepared_effects ORDER BY created_at")


def _assert_no_effect_rows(db_path):
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 0


def _assert_clean_chain(excinfo, marker):
    """The fixed error must carry neither the offending input nor any
    input-holding original exception: not in its message or repr, not
    anywhere in the chain, and not in the rendered traceback."""
    assert marker not in str(excinfo.value)
    assert marker not in repr(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = "".join(traceback.format_exception(
        type(excinfo.value), excinfo.value, excinfo.value.__traceback__))
    assert marker not in rendered


# -- forged stored rows (legal digests, illegal content) ------------------------


def _forged_envelope(ctx, idempotency_key, operation_id, **overrides):
    """Build a self-consistently hashed envelope the way an attacker
    with storage write access would: legal digests over illegal
    content. Read-back must still refuse these rows; recomputing the
    hashes must never make an out-of-contract row acceptable."""
    envelope = {
        "schema_version": 1,
        "preparation_state": "prepared",
        "operation_id": operation_id,
        "installation_id": ctx.installation_id,
        "company_id": ctx.company_id,
        "principal_id": ctx.principal_id,
        "member_revision": ctx.member_revision,
        "task_id": ctx.task_id,
        "run_id": ctx.run_id,
        "policy_id": ctx.policy_id,
        "policy_revision": ctx.policy_revision,
        "idempotency_key": idempotency_key,
        "action": "read",
        "target": "uploads-inbox",
        "objects": ["sam://original/a.txt"],
        "effect_manifest": {"source_batch": "batch-9"},
        "object_format": "sha1",
        "base_commit": BASE40,
        "dependency_versions": {"plan": "plan-v1"},
        "created_at": _time_text(at(50)),
        "expires_at": _time_text(at(600)),
    }
    envelope.update(overrides)
    effect_payload = {name: envelope[name]
                      for name in _EFFECT_PAYLOAD_KEYS}
    envelope["effect_digest"] = hashlib.sha256(
        _EFFECT_DOMAIN
        + git_format.dumps_record(effect_payload)).hexdigest()
    preparation_payload = {name: envelope[name]
                           for name in _PREPARATION_PAYLOAD_KEYS}
    envelope["preparation_digest"] = hashlib.sha256(
        _PREPARATION_DOMAIN
        + git_format.dumps_record(preparation_payload)).hexdigest()
    return envelope


def _insert_forged_row(db_path, envelope, *, canonical=True,
                       blob_override=None, row_sha256=None,
                       index_overrides=None):
    if blob_override is not None:
        blob = blob_override
    elif canonical:
        blob = git_format.dumps_record(envelope)
    else:
        blob = (json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
                + "\n").encode("utf-8")
    if row_sha256 is None:
        row_sha256 = hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()
    index = {
        "operation_id": envelope["operation_id"],
        "task_id": envelope["task_id"],
        "run_id": envelope["run_id"],
        "idempotency_key": envelope["idempotency_key"],
        "effect_digest": envelope["effect_digest"],
        "preparation_digest": envelope["preparation_digest"],
        "created_at": envelope["created_at"],
        "expires_at": envelope["expires_at"],
    }
    if index_overrides:
        index.update(index_overrides)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO host_prepared_effects (operation_id, task_id,"
            " run_id, idempotency_key, effect_digest,"
            " preparation_digest, created_at, expires_at, record_blob,"
            " row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (index["operation_id"], index["task_id"], index["run_id"],
             index["idempotency_key"], index["effect_digest"],
             index["preparation_digest"], index["created_at"],
             index["expires_at"], blob, row_sha256))
        conn.commit()
    finally:
        conn.close()


def _build_stack(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    service = HostPreparedEffectService(conn, identity, authorization,
                                        credentials)
    return conn, service


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "host-prepared-effects.db"


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
def service(identity, authorization, credentials):
    return HostPreparedEffectService(identity.db, identity, authorization,
                                     credentials)


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


# -- constructor binding contract -------------------------------------------------


def test_constructor_requires_connection_and_bound_service_types(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    with pytest.raises(TypeError):
        HostPreparedEffectService("host-prepared-effects.db", identity,
                                  authorization, credentials)
    with pytest.raises(TypeError):
        HostPreparedEffectService(object(), identity, authorization,
                                  credentials)
    with pytest.raises(TypeError):
        HostPreparedEffectService(conn, object(), authorization,
                                  credentials)
    with pytest.raises(TypeError):
        HostPreparedEffectService(conn, identity, object(), credentials)
    with pytest.raises(TypeError):
        HostPreparedEffectService(conn, identity, authorization,
                                  object())
    conn.close()


def test_constructor_refuses_second_connection_to_same_file(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    second = _connect(db_path)
    with pytest.raises(ValueError):
        HostPreparedEffectService(second, identity, authorization,
                                  credentials)
    # The refusal happens before any DDL: no prepared-effect table or
    # trigger may appear through the rejected connection.
    assert {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'host_prepared_effects%'")} == set()
    second.close()
    conn.close()


def test_constructor_refuses_divergent_service_instances(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    other_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    other_authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    foreign_credentials = HostTaskCredentialService(conn, identity,
                                                    other_authorization)
    with pytest.raises(ValueError):
        HostPreparedEffectService(conn, other_identity, authorization,
                                  credentials)
    with pytest.raises(ValueError):
        HostPreparedEffectService(conn, identity, other_authorization,
                                  credentials)
    with pytest.raises(ValueError):
        HostPreparedEffectService(conn, identity, authorization,
                                  foreign_credentials)
    conn.close()


def test_constructor_refuses_active_caller_transaction(db_path):
    conn = _connect(db_path)
    identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        HostPreparedEffectService(conn, identity, authorization,
                                  credentials)
    assert conn.in_transaction
    conn.rollback()
    conn.close()


def test_constructor_ddl_is_idempotent_across_reconnect(db_path):
    conn, _ = _build_stack(db_path)
    conn.close()
    conn, _ = _build_stack(db_path)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "host_prepared_effects" in names
    conn.close()


# -- happy path --------------------------------------------------------------------


def test_prepare_persists_single_verifiable_envelope(service, cred,
                                                     policy, db_path):
    prepared = _prepare(service, cred)
    assert isinstance(prepared, PreparedEffect)
    assert prepared.operation_id.startswith("hop_")
    assert at(50) <= prepared.created_at < at(51)
    assert prepared.expires_at == at(600)
    expected_effect = _expected_effect_digest(
        action="read", target="uploads-inbox",
        sorted_objects=["sam://original/a.txt", "sam://original/b.txt"],
        manifest={"source_batch": "batch-7", "notes": "synthetic"},
        fmt="sha1", base=BASE40,
        deps={"taxonomy_pack": "generic-v1", "plan": "plan-v1"})
    assert prepared.effect_digest == expected_effect
    assert prepared.preparation_digest == _expected_preparation_digest(
        cred.context, "upload-batch-7", at(600), expected_effect)
    rows = _effect_rows(db_path)
    assert len(rows) == 1
    (operation_id, task_id, run_id, key, effect_digest,
     preparation_digest, created_at, expires_at, blob,
     row_sha256) = rows[0]
    assert operation_id == prepared.operation_id
    assert (task_id, run_id, key) == (cred.task_id, cred.run_id,
                                      "upload-batch-7")
    assert effect_digest == prepared.effect_digest
    assert preparation_digest == prepared.preparation_digest
    assert created_at == _time_text(prepared.created_at)
    assert expires_at == _time_text(at(600))
    assert blob == prepared.envelope_bytes
    assert row_sha256 == hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()
    # Persistence only: no approval or operation/execution state exists.
    names = {row[0] for row in _rows(
        db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "host_prepared_effects" in names
    assert not any("approval" in name or "operation" in name
                   for name in names)


def test_envelope_exact_fields_and_fixed_time_format(service, cred):
    prepared = _prepare(service, cred)
    data = prepared.to_dict()
    assert frozenset(data) == EXPECTED_ENVELOPE_KEYS
    assert type(data["schema_version"]) is int
    assert data["schema_version"] == 1
    assert data["preparation_state"] == "prepared"
    assert data["installation_id"] == "inst-syn-1"
    assert data["company_id"] == "co-syn-1"
    assert data["principal_id"] == "user-syn-owner"
    assert data["member_revision"] == 1
    assert data["task_id"] == cred.task_id
    assert data["run_id"] == cred.run_id
    assert data["policy_id"] == cred.context.policy_id
    assert data["policy_revision"] == 1
    assert data["idempotency_key"] == "upload-batch-7"
    assert data["action"] == "read"
    assert data["target"] == "uploads-inbox"
    assert data["objects"] == ["sam://original/a.txt",
                               "sam://original/b.txt"]
    assert data["effect_manifest"] == {"source_batch": "batch-7",
                                       "notes": "synthetic"}
    assert data["object_format"] == "sha1"
    assert data["base_commit"] == BASE40
    assert data["dependency_versions"] == {"taxonomy_pack": "generic-v1",
                                           "plan": "plan-v1"}
    for name in ("created_at", "expires_at"):
        text = data[name]
        assert text.endswith("+00:00")
        assert _time_text(datetime.fromisoformat(text)) == text
    assert data["created_at"] < data["expires_at"]


def test_prepare_get_and_reopen_preserves_history(service, cred,
                                                  db_path):
    prepared = _prepare(service, cred)
    loaded = service.get_prepared_effect(
        cred.secret, now=at(55), operation_id=prepared.operation_id)
    assert loaded.to_dict() == prepared.to_dict()
    assert loaded.envelope_bytes == prepared.envelope_bytes
    assert loaded.created_at == prepared.created_at
    service.db.close()
    conn, reopened = _build_stack(db_path)
    try:
        again = reopened.get_prepared_effect(
            cred.secret, now=at(56), operation_id=prepared.operation_id)
        assert again.to_dict() == prepared.to_dict()
        # A replay after reconnect returns the original identifiers and
        # adds no row: reopening a connection never resets history.
        replayed = reopened.prepare_effect(cred.secret, now=at(57),
                                           **effect_kwargs())
        assert replayed.operation_id == prepared.operation_id
        assert replayed.created_at == prepared.created_at
        assert _scalar(db_path,
                       "SELECT COUNT(*) FROM host_prepared_effects") == 1
    finally:
        conn.close()


def test_distinct_keys_produce_distinct_host_generated_operations(
        service, cred, db_path):
    first = _prepare(service, cred)
    second = _prepare(service, cred, idempotency_key="upload-batch-8")
    assert first.operation_id != second.operation_id
    assert first.operation_id.startswith("hop_")
    assert second.operation_id.startswith("hop_")
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 2


# -- idempotent replay and conflict -------------------------------------------------


def test_idempotent_replay_returns_original_without_new_row(
        service, cred, db_path):
    first = _prepare(service, cred)
    second = _prepare(service, cred, now=at(55))
    assert second.operation_id == first.operation_id
    assert second.created_at == first.created_at
    assert second.to_dict() == first.to_dict()
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_replay_stable_under_objects_and_dict_key_order(
        service, cred, db_path):
    first = _prepare(service, cred)
    second = service.prepare_effect(
        cred.secret, now=at(52),
        **effect_kwargs(
            objects=["sam://original/a.txt", "sam://original/b.txt"],
            effect_manifest={"notes": "synthetic",
                             "source_batch": "batch-7"},
            dependency_versions={"plan": "plan-v1",
                                 "taxonomy_pack": "generic-v1"}))
    assert second.operation_id == first.operation_id
    assert second.created_at == first.created_at
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_same_key_changed_request_conflicts_and_never_overwrites(
        service, cred, db_path):
    prepared = _prepare(service, cred)
    original_blob = prepared.envelope_bytes
    variants = [
        {"action": "extract"},
        {"objects": ["sam://original/c.txt"]},
        {"effect_manifest": {"source_batch": "batch-8"}},
        {"object_format": "sha256", "base_commit": BASE64},
        {"base_commit": "f" * 40},
        {"dependency_versions": {"plan": "plan-v2"}},
        {"expires_at": at(700)},
    ]
    for overrides in variants:
        with pytest.raises(PreparedEffectConflict) as excinfo:
            _prepare(service, cred, **overrides)
        assert excinfo.value.__context__ is None
    rows = _effect_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == prepared.operation_id
    assert rows[0][8] == original_blob


def test_same_key_changed_target_is_scope_denied_not_overwrite(
        service, cred, db_path):
    _prepare(service, cred)
    with pytest.raises(PreparedScopeDenied):
        _prepare(service, cred, target="other-inbox")
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


# -- credential / membership / policy failure: zero writes -------------------------


def test_prepare_unknown_secret_zero_write(service, db_path):
    with pytest.raises(TaskCredentialInvalid):
        service.prepare_effect("unknown-secret", now=at(50),
                               **effect_kwargs())
    _assert_no_effect_rows(db_path)


def test_prepare_revoked_credential_no_replay_revival(
        service, credentials, grant, cred, db_path):
    prepared = _prepare(service, cred)
    assert credentials.revoke_task_credential(
        grant.secret, now=at(60), task_id=cred.task_id) is True
    with pytest.raises(TaskCredentialRevoked):
        _prepare(service, cred, idempotency_key="another-key")
    # Even an exact replay of the stored preparation cannot revive it.
    with pytest.raises(TaskCredentialRevoked):
        _prepare(service, cred)
    with pytest.raises(TaskCredentialRevoked):
        service.get_prepared_effect(cred.secret, now=at(61),
                                    operation_id=prepared.operation_id)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_prepare_expired_credential_zero_write(service, credentials,
                                               grant, policy, db_path):
    # A real credential issued with an independently shorter validity
    # cap: at prepare time the policy is still valid and the credential
    # itself has expired, so the error is deterministically the
    # credential's own expiry rather than the shared policy boundary.
    short_cred = credentials.issue_task_credential(
        grant.secret, now=at(40), policy_id=policy.policy_id,
        expected_revision=1, not_after=at(100))
    with pytest.raises(TaskCredentialExpired):
        service.prepare_effect(short_cred.secret, now=at(101),
                               **effect_kwargs())
    _assert_no_effect_rows(db_path)


def test_prepare_member_revoked_zero_write(service, identity, cred,
                                           db_path):
    assert identity.revoke_member("user-syn-owner", now=at(60)) is True
    with pytest.raises(TaskMembershipRevoked):
        _prepare(service, cred)
    _assert_no_effect_rows(db_path)


def test_prepare_and_get_refuse_paused_or_reversioned_policy(
        service, authorization, grant, policy, cred, db_path):
    prepared = _prepare(service, cred)
    authorization.pause_policy(grant.secret, now=at(60),
                               policy_id=policy.policy_id,
                               expected_revision=1)
    with pytest.raises(TaskPolicyStale):
        _prepare(service, cred, idempotency_key="another-key")
    with pytest.raises(TaskPolicyStale):
        service.get_prepared_effect(cred.secret, now=at(61),
                                    operation_id=prepared.operation_id)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_prepare_scope_widening_refused_zero_write(service, cred,
                                                   db_path):
    for overrides in (
            {"action": "delete"},
            {"target": "other-inbox"},
            {"objects": ["sam://original/z.txt"]},
            {"objects": ["sam://original/a.txt",
                         "sam://original/z.txt"]},
            {"expires_at": at(3601)},
    ):
        with pytest.raises(PreparedScopeDenied):
            _prepare(service, cred, **overrides)
    _assert_no_effect_rows(db_path)


def test_expires_may_equal_scope_not_after(service, cred):
    prepared = _prepare(service, cred, expires_at=at(3600))
    assert prepared.expires_at == at(3600)


# -- caller input validation --------------------------------------------------------


def test_manifest_must_be_canonically_encodable_dict(service, cred,
                                                     db_path):
    with pytest.raises(TypeError):
        _prepare(service, cred, effect_manifest=["not", "a", "dict"])
    with pytest.raises(TypeError):
        _prepare(service, cred, effect_manifest=True)
    with pytest.raises(TypeError):
        _prepare(service, cred, effect_manifest={1: "numeric key"})
    with pytest.raises(TypeError):
        _prepare(service, cred, effect_manifest={"text": "ok",
                                                 2: "mixed"})
    with pytest.raises(TypeError):
        _prepare(service, cred, effect_manifest={"bad": 1.5})
    with pytest.raises(ValueError):
        _prepare(service, cred,
                 effect_manifest={"blob": "x" * (1024 * 1024)})
    _assert_no_effect_rows(db_path)
    prepared = _prepare(
        service, cred,
        effect_manifest={"items": [1, "two", None, True],
                         "nested": {"a": 1}})
    assert prepared.to_dict()["effect_manifest"] == {
        "items": [1, "two", None, True], "nested": {"a": 1}}


def test_dependency_versions_must_be_dict_of_str(service, cred,
                                                 db_path):
    with pytest.raises(TypeError):
        _prepare(service, cred, dependency_versions=None)
    with pytest.raises(TypeError):
        _prepare(service, cred, dependency_versions="plan-v1")
    for bad_value in (None, 1, True, ["plan-v1"], {"nested": "x"}):
        with pytest.raises(TypeError):
            _prepare(service, cred,
                     dependency_versions={"plan": bad_value})
    with pytest.raises(ValueError):
        _prepare(service, cred, dependency_versions={"": "plan-v1"})
    with pytest.raises(ValueError):
        _prepare(service, cred, dependency_versions={"plan": ""})
    with pytest.raises(ValueError):
        _prepare(service, cred,
                 dependency_versions={"k" * 201: "plan-v1"})
    with pytest.raises(ValueError):
        _prepare(service, cred,
                 dependency_versions={"plan": "v" * 201})
    _assert_no_effect_rows(db_path)
    # An empty dict is the legal nullable form and stays literal.
    prepared = _prepare(service, cred, dependency_versions={})
    assert prepared.to_dict()["dependency_versions"] == {}


def test_object_format_and_base_commit_validation(service, cred,
                                                  db_path):
    with pytest.raises(ValueError):
        _prepare(service, cred, object_format="md5")
    with pytest.raises(ValueError):
        _prepare(service, cred, object_format="SHA1")
    with pytest.raises(TypeError):
        _prepare(service, cred, object_format=None)
    with pytest.raises(TypeError):
        _prepare(service, cred, base_commit=123)
    with pytest.raises(ValueError):
        _prepare(service, cred, base_commit="A" * 40)
    with pytest.raises(ValueError):
        _prepare(service, cred, base_commit="g" * 40)
    with pytest.raises(ValueError):
        _prepare(service, cred, base_commit="a" * 39)
    with pytest.raises(ValueError):
        _prepare(service, cred, base_commit="a" * 41)
    with pytest.raises(ValueError):
        _prepare(service, cred, object_format="sha256")
    _assert_no_effect_rows(db_path)
    prepared = _prepare(service, cred, object_format="sha256",
                        base_commit=BASE64)
    assert prepared.to_dict()["base_commit"] == BASE64


def test_objects_shape_validation(service, cred, db_path):
    with pytest.raises(ValueError):
        _prepare(service, cred, objects=[])
    with pytest.raises(TypeError):
        _prepare(service, cred, objects="sam://original/a.txt")
    with pytest.raises(ValueError):
        _prepare(service, cred, objects=[""])
    with pytest.raises(ValueError):
        _prepare(service, cred, objects=["sam://original/a.txt",
                                         "sam://original/a.txt"])
    with pytest.raises(TypeError):
        _prepare(service, cred, objects=[1])
    with pytest.raises(TypeError):
        _prepare(service, cred, objects=True)
    _assert_no_effect_rows(db_path)
    # A tuple is accepted and stored string-sorted.
    prepared = _prepare(service, cred,
                        objects=("sam://original/b.txt",
                                 "sam://original/a.txt"))
    assert prepared.to_dict()["objects"] == ["sam://original/a.txt",
                                             "sam://original/b.txt"]


def test_idempotency_key_validation(service, cred, db_path):
    with pytest.raises(ValueError):
        _prepare(service, cred, idempotency_key="")
    with pytest.raises(ValueError):
        _prepare(service, cred, idempotency_key=123)
    with pytest.raises(ValueError):
        _prepare(service, cred, idempotency_key="k" * 129)
    with pytest.raises(ValueError):
        _prepare(service, cred, idempotency_key=True)
    _assert_no_effect_rows(db_path)


def test_time_inputs_must_be_timezone_aware_and_future(service, cred,
                                                       db_path):
    with pytest.raises(ValueError):
        _prepare(service, cred, now=datetime(2026, 10, 24, 12, 0, 50))
    with pytest.raises(ValueError):
        _prepare(service, cred,
                 expires_at=datetime(2026, 10, 24, 12, 10))
    with pytest.raises(TypeError):
        _prepare(service, cred, now="2026-10-24T12:00:50+00:00")
    with pytest.raises(PreparedEffectExpired):
        _prepare(service, cred, expires_at=at(50))
    with pytest.raises(PreparedEffectExpired):
        _prepare(service, cred, expires_at=at(40))
    _assert_no_effect_rows(db_path)


# -- error chains and message safety -------------------------------------------------


def test_lone_surrogate_manifest_rejected_with_fixed_error(
        service, cred, db_path):
    visible = "MARKER-surrogate-51af"
    with pytest.raises(ValueError) as excinfo:
        _prepare(service, cred,
                 effect_manifest={"notes": visible + chr(0xD800)})
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    _assert_no_effect_rows(db_path)


def test_lone_surrogate_secret_refused_before_resolution(
        service, cred, db_path):
    visible = "task-secret-MARKER-7c09"
    bad_secret = visible + chr(0xD800)
    with pytest.raises(ValueError) as excinfo:
        service.prepare_effect(bad_secret, now=at(50),
                               **effect_kwargs())
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    with pytest.raises(ValueError) as excinfo:
        service.get_prepared_effect(bad_secret, now=at(50),
                                    operation_id="hop_0000000000000000")
    assert not isinstance(excinfo.value, UnicodeEncodeError)
    _assert_clean_chain(excinfo, visible)
    _assert_no_effect_rows(db_path)


def test_errors_never_echo_caller_input(service, cred, db_path):
    marker = "MARKER-9f27e6-private"
    cases = [
        {"action": marker},  # literal scope denial, fixed message
        {"idempotency_key": marker + chr(0xD800)},  # encoding failure
        {"dependency_versions": {marker * 20: "v"}},  # key > 200 chars
        {"base_commit": marker},  # hex/length failure
        {"effect_manifest": {marker: "x", 1: 2}},  # mixed key types
    ]
    for overrides in cases:
        with pytest.raises(Exception) as excinfo:
            _prepare(service, cred, **overrides)
        _assert_clean_chain(excinfo, marker)
    _assert_no_effect_rows(db_path)


def test_conflict_error_carries_no_request_content(service, cred,
                                                   db_path):
    marker = "MARKER-conflict-2b66"
    _prepare(service, cred)
    with pytest.raises(PreparedEffectConflict) as excinfo:
        _prepare(service, cred, effect_manifest={"source_batch": marker})
    _assert_clean_chain(excinfo, marker)
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


# -- read-back behavior -------------------------------------------------------------


def test_get_missing_and_cross_task_share_one_unavailable_error(
        service, credentials, grant, policy, cred):
    prepared = _prepare(service, cred)
    other = credentials.issue_task_credential(
        grant.secret, now=at(45), policy_id=policy.policy_id,
        expected_revision=1)
    with pytest.raises(PreparedEffectUnavailable) as missing:
        service.get_prepared_effect(cred.secret, now=at(55),
                                    operation_id="hop_0000000000000000")
    with pytest.raises(PreparedEffectUnavailable) as foreign:
        service.get_prepared_effect(other.secret, now=at(55),
                                    operation_id=prepared.operation_id)
    assert str(missing.value) == str(foreign.value)
    # The owning task still reads it.
    loaded = service.get_prepared_effect(
        cred.secret, now=at(55), operation_id=prepared.operation_id)
    assert loaded.operation_id == prepared.operation_id


def test_get_expired_effect_fails_closed(service, cred, db_path):
    prepared = _prepare(service, cred, expires_at=at(100))
    with pytest.raises(PreparedEffectExpired):
        service.get_prepared_effect(cred.secret, now=at(100),
                                    operation_id=prepared.operation_id)
    # A replay whose validity has already passed cannot restore it.
    with pytest.raises(PreparedEffectExpired):
        _prepare(service, cred, now=at(150), expires_at=at(100))
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_get_input_validation(service, cred):
    prepared = _prepare(service, cred)
    with pytest.raises(ValueError):
        service.get_prepared_effect(
            cred.secret, now=datetime(2026, 10, 24, 12, 0, 55),
            operation_id=prepared.operation_id)
    with pytest.raises(ValueError):
        service.get_prepared_effect("", now=at(55),
                                    operation_id=prepared.operation_id)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(55),
                                    operation_id="")


def test_get_refuses_caller_transaction_and_preserves_it(
        service, cred, db_path):
    prepared = _prepare(service, cred)
    conn = service.db
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_prepared_effects").fetchone()
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(55),
                                    operation_id=prepared.operation_id)
    # The refused caller transaction is never closed, committed or
    # rolled back by the module.
    assert conn.in_transaction
    conn.rollback()
    loaded = service.get_prepared_effect(
        cred.secret, now=at(55), operation_id=prepared.operation_id)
    assert loaded.operation_id == prepared.operation_id


def test_get_refuses_stale_caller_snapshot_hiding_revocation(
        service, identity, grant, cred, db_path):
    prepared = _prepare(service, cred)
    conn = service.db
    # WAL lets the revocation below commit while this connection keeps
    # its old snapshot, exactly the situation the refusal protects.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_prepared_effects").fetchone()
    other = _connect(db_path)
    try:
        other_identity = HostIdentityService(other)
        other_authorization = HostAuthorizationService(other,
                                                       other_identity)
        other_credentials = HostTaskCredentialService(
            other, other_identity, other_authorization)
        assert other_credentials.revoke_task_credential(
            grant.secret, now=at(70), task_id=cred.task_id) is True
    finally:
        other.close()
    # get must NOT read through the stale snapshot (which still shows
    # the credential as valid): it refuses the caller transaction.
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(71),
                                    operation_id=prepared.operation_id)
    assert conn.in_transaction
    conn.rollback()
    # On a fresh snapshot the revocation is visible and the read fails
    # closed instead of returning content.
    with pytest.raises(TaskCredentialRevoked):
        service.get_prepared_effect(cred.secret, now=at(71),
                                    operation_id=prepared.operation_id)


# -- transaction ownership and atomicity ---------------------------------------------


def test_prepare_refuses_caller_transaction_and_preserves_it(
        service, cred, db_path):
    conn = service.db
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM host_prepared_effects").fetchone()
    with pytest.raises(ValueError):
        _prepare(service, cred)
    assert conn.in_transaction
    conn.rollback()
    _assert_no_effect_rows(db_path)
    prepared = _prepare(service, cred)
    assert prepared.operation_id.startswith("hop_")


def test_failed_insert_rolls_back_completely(identity, grant, policy,
                                             cred, db_path):
    identity.db.close()
    conn = sqlite3.connect(db_path, factory=_FailOn)
    conn.execute("PRAGMA busy_timeout=30000")
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    failing = HostPreparedEffectService(conn, new_identity,
                                        authorization, credentials)
    conn.needle = "INSERT INTO host_prepared_effects"
    with pytest.raises(RuntimeError):
        failing.prepare_effect(cred.secret, now=at(50),
                               **effect_kwargs())
    conn.close()
    _assert_no_effect_rows(db_path)
    # The credential ledger is untouched by the rolled-back attempt.
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
    monkeypatch.setattr("app.host_prepared_effects._monotonic_ns", clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    return HostPreparedEffectService(conn, new_identity, authorization,
                                     credentials)


def test_prepare_rejected_when_lock_wait_crosses_expires(
        identity, grant, policy, cred, db_path, monkeypatch):
    service = _reopen_with_simulated_lock_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before expires_at; the simulated 2us
    # write-lock wait moves the in-transaction effective time one
    # microsecond past expires_at, so nothing may be written on the
    # stale pre-lock time.
    with pytest.raises(PreparedEffectExpired):
        service.prepare_effect(
            cred.secret, now=at(600) - timedelta(microseconds=1),
            **effect_kwargs())
    service.db.close()
    _assert_no_effect_rows(db_path)


def test_prepare_counts_pre_lock_encoding_time_against_expires(
        service, cred, db_path, monkeypatch):
    # The monotonic origin is captured at call entry, so wall time
    # spent on validation, canonical encoding and digest work before
    # the write lock must also count against expires_at.
    clock = _LockWaitClock(2)
    real_canonical = prepared_module._canonical_bytes
    advanced = {"done": False}

    def advancing_canonical(payload):
        if not advanced["done"]:
            clock.advance()  # 2us spent before the write lock
            advanced["done"] = True
        return real_canonical(payload)

    monkeypatch.setattr("app.host_prepared_effects._monotonic_ns", clock)
    monkeypatch.setattr("app.host_prepared_effects._canonical_bytes",
                        advancing_canonical)
    with pytest.raises(PreparedEffectExpired):
        service.prepare_effect(
            cred.secret, now=at(600) - timedelta(microseconds=1),
            **effect_kwargs())
    _assert_no_effect_rows(db_path)


class _WaitOnFirstRead(sqlite3.Connection):
    """Connection advancing the fake clock when the fresh read snapshot
    is actually established by the first real SELECT."""

    clock = None

    def execute(self, sql, parameters=()):
        clock = self.clock
        if clock is not None and isinstance(sql, str) \
                and "FROM host_prepared_effects" in sql:
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
    monkeypatch.setattr("app.host_prepared_effects._monotonic_ns", clock)
    new_identity = HostIdentityService(conn)
    authorization = HostAuthorizationService(conn, new_identity)
    credentials = HostTaskCredentialService(conn, new_identity,
                                            authorization)
    return HostPreparedEffectService(conn, new_identity, authorization,
                                     credentials)


def test_get_rejected_when_read_wait_crosses_effect_expiry(
        service, identity, cred, db_path, monkeypatch):
    prepared = _prepare(service, cred, expires_at=at(100))
    service = _reopen_with_simulated_read_wait(identity, db_path, 2,
                                               monkeypatch)
    # Entry one microsecond before expires_at; the simulated 2us wait
    # establishing the read snapshot moves the effective time one
    # microsecond past expires_at, so the stale entry time must not
    # pass.
    with pytest.raises(PreparedEffectExpired):
        service.get_prepared_effect(
            cred.secret, now=at(100) - timedelta(microseconds=1),
            operation_id=prepared.operation_id)
    # The refused read closed its own snapshot and wrote nothing.
    assert not service.db.in_transaction
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1
    # A normal read at a valid time still returns the effect.
    loaded = service.get_prepared_effect(
        cred.secret, now=at(50), operation_id=prepared.operation_id)
    assert loaded.operation_id == prepared.operation_id


def test_get_rejected_when_read_wait_crosses_credential_expiry(
        service, credentials, identity, grant, policy, db_path,
        monkeypatch):
    short_cred = credentials.issue_task_credential(
        grant.secret, now=at(45), policy_id=policy.policy_id,
        expected_revision=1, not_after=at(100))
    prepared = service.prepare_effect(
        short_cred.secret, now=at(50),
        **effect_kwargs(expires_at=at(100)))
    service = _reopen_with_simulated_read_wait(identity, db_path, 2,
                                               monkeypatch)
    # The policy is still valid; the task credential's own shorter cap
    # lapses during the simulated 2us snapshot-establishment wait, so
    # resolution must fail instead of passing on the entry time.
    with pytest.raises(TaskCredentialExpired):
        service.get_prepared_effect(
            short_cred.secret, now=at(100) - timedelta(microseconds=1),
            operation_id=prepared.operation_id)
    assert not service.db.in_transaction
    loaded = service.get_prepared_effect(
        short_cred.secret, now=at(50),
        operation_id=prepared.operation_id)
    assert loaded.operation_id == prepared.operation_id


# -- stored-row immutability guards -----------------------------------------------------


def test_rows_reject_update_delete_and_both_replace_classes(
        service, cred, db_path):
    prepared = _prepare(service, cred)
    ctx = cred.context
    columns = ("operation_id, task_id, run_id, idempotency_key, "
               "effect_digest, preparation_digest, created_at, "
               "expires_at, record_blob, row_sha256")
    forged_params = ("hop_guard0000000001", ctx.task_id, ctx.run_id,
                     "guard-other-key", "0" * 64, "1" * 64,
                     _time_text(at(50)), _time_text(at(600)), b"{}",
                     "2" * 64)
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE host_prepared_effects "
                         "SET effect_digest = ?", ("0" * 64,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM host_prepared_effects")
        # INSERT OR REPLACE over the primary key (operation_id).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_prepared_effects ("
                + columns + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (prepared.operation_id,) + forged_params[1:])
        # INSERT OR REPLACE over the idempotency unique key with a
        # fresh operation_id.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO host_prepared_effects ("
                + columns + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                forged_params[:3] + ("upload-batch-7",)
                + forged_params[4:])
        # A plain duplicate-key insert is refused by the same guard.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO host_prepared_effects (" + columns
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("hop_guard0000000002",) + forged_params[1:3]
                + ("upload-batch-7",) + forged_params[4:])
    finally:
        conn.close()
    rows = _effect_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == prepared.operation_id
    assert rows[0][8] == prepared.envelope_bytes
    again = service.get_prepared_effect(
        cred.secret, now=at(55), operation_id=prepared.operation_id)
    assert again.preparation_digest == prepared.preparation_digest


# -- forged stored rows: legal digests never excuse illegal content ------------------


def test_forged_row_wrong_row_digest_refused(service, cred, db_path):
    envelope = _forged_envelope(cred.context, "forge-row-digest",
                                "hop_forged00000aa1")
    _insert_forged_row(db_path, envelope, row_sha256="0" * 64)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa1")


def test_forged_row_index_blob_mismatch_refused(service, cred, db_path):
    envelope = _forged_envelope(cred.context, "forge-index-drift",
                                "hop_forged00000aa2")
    _insert_forged_row(db_path, envelope,
                       index_overrides={"effect_digest": "0" * 64})
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa2")


def test_forged_noncanonical_blob_refused(service, cred, db_path):
    envelope = _forged_envelope(cred.context, "forge-noncanonical",
                                "hop_forged00000aa3")
    _insert_forged_row(db_path, envelope, canonical=False)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa3")


def test_forged_unsorted_objects_refused(service, cred, db_path):
    envelope = _forged_envelope(
        cred.context, "forge-unsorted", "hop_forged00000aa4",
        objects=["sam://original/b.txt", "sam://original/a.txt"])
    _insert_forged_row(db_path, envelope)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa4")


def test_forged_bool_schema_version_refused(service, cred, db_path):
    envelope = _forged_envelope(cred.context, "forge-bool-schema",
                                "hop_forged00000aa5", schema_version=True)
    _insert_forged_row(db_path, envelope)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa5")


def test_forged_null_dependency_versions_refused(service, cred, db_path):
    # null is refused on read-back exactly as at write time; the
    # self-consistent digests must not make the row acceptable.
    envelope = _forged_envelope(cred.context, "forge-null-deps",
                                "hop_forged00000aa6",
                                dependency_versions=None)
    _insert_forged_row(db_path, envelope)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa6")


def test_forged_created_at_not_before_expires_refused(service, cred,
                                                      db_path):
    for index, (created, expires) in enumerate((
            (_time_text(at(700)), _time_text(at(600))),
            (_time_text(at(600)), _time_text(at(600))))):
        operation_id = "hop_forged00000bb%d" % index
        envelope = _forged_envelope(cred.context,
                                    "forge-bad-times-%d" % index,
                                    operation_id,
                                    created_at=created,
                                    expires_at=expires)
        _insert_forged_row(db_path, envelope)
        with pytest.raises(ValueError):
            service.get_prepared_effect(cred.secret, now=at(60),
                                        operation_id=operation_id)


def test_forged_expires_beyond_scope_not_after_refused(service, cred,
                                                       db_path):
    envelope = _forged_envelope(cred.context, "forge-long-validity",
                                "hop_forged00000aa8",
                                expires_at=_time_text(at(7000)))
    _insert_forged_row(db_path, envelope)
    with pytest.raises(ValueError):
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000aa8")


def test_forged_bad_date_error_carries_no_stored_content(
        service, cred, db_path):
    visible = "MARKER-date-77bd"
    envelope = _forged_envelope(
        cred.context, "forge-bad-date", "hop_forged00000cc1",
        created_at="2026-10-24T12:00:50 " + visible)
    _insert_forged_row(db_path, envelope)
    with pytest.raises(ValueError) as excinfo:
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000cc1")
    _assert_clean_chain(excinfo, visible)


def test_forged_unparseable_blob_errors_carry_no_stored_content(
        service, cred, db_path):
    visible_json = "MARKER-json-3e52"
    envelope = _forged_envelope(cred.context, "forge-bad-json",
                                "hop_forged00000cc2")
    _insert_forged_row(
        db_path, envelope,
        blob_override=b'{"schema_version": 1, "x": '
        + visible_json.encode("utf-8") + b'}')
    with pytest.raises(ValueError) as excinfo:
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000cc2")
    _assert_clean_chain(excinfo, visible_json)

    visible_utf8 = "MARKER-utf8-90ce"
    envelope = _forged_envelope(cred.context, "forge-bad-utf8",
                                "hop_forged00000cc3")
    _insert_forged_row(
        db_path, envelope,
        blob_override=visible_utf8.encode("utf-8") + b"\xff\xfe")
    with pytest.raises(ValueError) as excinfo:
        service.get_prepared_effect(cred.secret, now=at(60),
                                    operation_id="hop_forged00000cc3")
    _assert_clean_chain(excinfo, visible_utf8)


# -- cross-connection concurrency -----------------------------------------------------


def _thread_prepare(db_path, barrier, results, index, secret, overrides):
    conn = _connect(db_path)
    try:
        identity = HostIdentityService(conn)
        authorization = HostAuthorizationService(conn, identity)
        credentials = HostTaskCredentialService(conn, identity,
                                                authorization)
        service = HostPreparedEffectService(conn, identity,
                                            authorization, credentials)
        barrier.wait(timeout=30)
        results[index] = ("ok", service.prepare_effect(
            secret, now=at(60), **effect_kwargs(**overrides)))
    except BaseException as exc:
        results[index] = ("error", exc)
    finally:
        conn.close()


def _run_concurrent_pair(db_path, secret, overrides_a, overrides_b):
    barrier = threading.Barrier(2)
    results = [None, None]
    threads = [
        threading.Thread(
            target=_thread_prepare,
            args=(db_path, barrier, results, index, secret, overrides))
        for index, overrides in enumerate((overrides_a, overrides_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    for thread in threads:
        assert not thread.is_alive(), \
            "concurrent prepare thread did not finish"
    assert all(entry is not None for entry in results)
    return results


def test_concurrent_same_key_same_content_single_row(service, cred,
                                                     db_path):
    results = _run_concurrent_pair(db_path, cred.secret, {}, {})
    for status, value in results:
        if status != "ok":
            raise value
    first, second = results[0][1], results[1][1]
    assert first.operation_id == second.operation_id
    assert first.created_at == second.created_at
    assert _scalar(db_path,
                   "SELECT COUNT(*) FROM host_prepared_effects") == 1


def test_concurrent_same_key_different_content_single_winner(
        service, cred, db_path):
    results = _run_concurrent_pair(
        db_path, cred.secret, {},
        {"effect_manifest": {"source_batch": "batch-8"}})
    statuses = sorted(status for status, _ in results)
    assert statuses == ["error", "ok"]
    error = next(value for status, value in results
                 if status == "error")
    if not isinstance(error, PreparedEffectConflict):
        raise error
    # The insert race failure never chains the database exception.
    assert error.__context__ is None
    rows = _effect_rows(db_path)
    assert len(rows) == 1


# -- result immutability and secrecy ----------------------------------------------------


def test_result_is_deeply_immutable_and_exports_copies(service, cred):
    prepared = _prepare(service, cred)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.operation_id = "hop_changed0000000"
    with pytest.raises(TypeError):
        prepared.envelope["action"] = "extract"
    with pytest.raises(TypeError):
        prepared.envelope["effect_manifest"]["notes"] = "mutated"
    assert isinstance(prepared.envelope["objects"], tuple)
    assert isinstance(prepared.envelope["dependency_versions"],
                      MappingProxyType)
    exported = prepared.to_dict()
    exported["effect_manifest"]["notes"] = "mutated"
    exported["objects"].append("sam://original/c.txt")
    exported["dependency_versions"]["plan"] = "plan-v2"
    fresh = prepared.to_dict()
    assert fresh["effect_manifest"]["notes"] == "synthetic"
    assert fresh["objects"] == ["sam://original/a.txt",
                                "sam://original/b.txt"]
    assert fresh["dependency_versions"]["plan"] == "plan-v1"


def test_result_carries_no_secret_or_credential_digest(service, cred,
                                                       grant):
    prepared = _prepare(service, cred)
    digest = hashlib.sha256(
        b"sam-host-task-credentials credential-v1\x00"
        + cred.secret.encode("utf-8")).hexdigest()
    blob_text = prepared.envelope_bytes.decode("utf-8")
    rendered = repr(prepared)
    serialized = json.dumps(prepared.to_dict())
    for marker in (cred.secret, grant.secret, digest):
        assert marker not in blob_text
        assert marker not in rendered
        assert marker not in serialized


# -- default-database isolation ----------------------------------------------------------


def test_module_never_opens_the_default_real_database(tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("SAM_DATA_ROOT", str(tmp_path / "elsewhere"))
    conn = _connect(tmp_path / "explicit.db")
    identity = HostIdentityService(conn)
    identity.initialize_installation(
        installation_id="inst-syn-9", company_id="co-syn-9",
        owner_principal_id="user-syn-owner-9", owner_role="owner",
        now=T0)
    authorization = HostAuthorizationService(conn, identity)
    credentials = HostTaskCredentialService(conn, identity, authorization)
    service = HostPreparedEffectService(conn, identity, authorization,
                                        credentials)
    ticket = identity.issue_pairing_credential(now=at(10),
                                               lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20),
                          session_lifetime_seconds=3600)
    policy = authorization.create_policy(grant.secret, now=at(30),
                                         **scope_kwargs())
    cred = credentials.issue_task_credential(
        grant.secret, now=at(40), policy_id=policy.policy_id,
        expected_revision=1)
    prepared = _prepare(service, cred)
    assert prepared.to_dict()["company_id"] == "co-syn-9"
    conn.close()
    assert not (tmp_path / "elsewhere").exists()
