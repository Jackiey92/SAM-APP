"""E1: real host chain, independent facts, corruption and W checkpoint drills."""

import ast
import hashlib
import json
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from app import host_approval_consumptions as consumption_module
from app import host_approval_reservations as reservations_module
from app.facts_tree import git_format
from app.host_approval_consumptions import (
    ApprovalConsumptionError,
    HostApprovalConsumptionService,
    verify_consumption_row,
)
from tests.test_host_approval_reservations import (
    _build_stack,
    _connect,
    _reserve,
    at,
    authorization,
    cred,
    credentials,
    db_path,
    decision,
    decisions,
    effect,
    grant,
    identity,
    policy,
    prepared,
    reservation,
    service,
)

SCOPE = {"installation_id": "inst-syn-1", "company_id": "co-syn-1"}
TABLE = "host_approval_consumptions"
DOMAIN = b"sam-host-approval-consumption row-v1\x00"
FIELDS = ("reservation_id", "approval_id", "operation_id", "task_id", "run_id",
          "effect_digest", "preparation_digest", "created_at", "expires_at")
COLUMNS = ", ".join(FIELDS + ("record_blob", "row_sha256"))
KEYS = {"schema_version", "consumption_state", "reservation_id", "approval_id",
        "operation_id", "task_id", "run_id", "effect_digest", "preparation_digest",
        "created_at", "expires_at", "reservation_created_at", "installation_id",
        "company_id", "principal_id", "member_revision", "policy_id",
        "policy_revision", "action", "target"}


@pytest.fixture
def consumptions(service):
    return HostApprovalConsumptionService(service.db, service, initialize=True)


def request(reservation, **overrides):
    result = {key: getattr(reservation, key) for key in
              ("operation_id", "reservation_id", "approval_id")}
    result.update(expected_effect_digest=reservation.effect_digest,
                  expected_preparation_digest=reservation.preparation_digest,
                  now=at(80))
    result.update(overrides)
    return result


def consume(consumptions, cred, reservation, **overrides):
    return consumptions.record_admission(cred.secret, **request(reservation, **overrides))


def rows(db):
    return db.execute("SELECT " + COLUMNS + " FROM " + TABLE).fetchall()


def assert_error(call, code=None, secrets=()):
    with pytest.raises(ApprovalConsumptionError) as caught:
        call()
    error = caught.value
    assert str(error) == error.code
    assert error.code.startswith("approval_consumption_")
    if code is not None:
        assert error.code == code
    assert error.__context__ is None
    assert error.__cause__ is None
    for secret in secrets:
        assert secret not in str(error) + repr(error) + repr(error.args)
    return error.code


def test_admission_fact_roundtrip_digest_idempotency_and_immutable(
        consumptions, cred, reservation, grant):
    db = consumptions.db
    assert rows(db) == []  # reserving alone does not consume
    current = consumptions.get_current_status(
        cred.secret, now=at(75), operation_id=reservation.operation_id)
    assert current.reservation == reservation and current.consumption is None
    fact = consume(consumptions, cred, reservation)
    assert set(fact.record) == KEYS
    assert fact.record["consumption_state"] == "consumed"
    assert fact.record["reservation_created_at"] == reservation.record["created_at"]
    assert fact.created_at >= reservation.created_at
    assert fact.expires_at == reservation.expires_at
    assert rows(db)[0][-1] == hashlib.sha256(DOMAIN + fact.record_bytes).hexdigest()
    assert fact.record_bytes == git_format.dumps_record(fact.to_dict())
    assert verify_consumption_row(rows(db)[0], **SCOPE) == fact
    assert consume(consumptions, cred, reservation, now=at(90)) == fact
    assert len(rows(db)) == 1
    assert consumptions.get_current_status(
        cred.secret, now=at(90), operation_id=fact.operation_id).consumption == fact
    assert consumptions.read_consumptions(**SCOPE) == (fact,)
    with pytest.raises(TypeError):
        fact.record["consumption_state"] = "accepted"
    with pytest.raises(AttributeError):
        fact.operation_id = "changed"
    copy = fact.to_dict()
    copy["consumption_state"] = "accepted"
    assert fact.record["consumption_state"] == "consumed"
    assert not hasattr(fact, "execute") and not hasattr(fact, "accepted")
    for secret in (cred.secret, grant.secret):
        assert secret.encode() not in fact.record_bytes
        assert secret not in repr(fact)


def test_reopen_preserves_fact_without_ddl_repair(consumptions, cred, reservation, db_path):
    fact = consume(consumptions, cred, reservation)
    conn, stack = _build_stack(db_path)
    try:
        reopened = HostApprovalConsumptionService(conn, stack)
        assert reopened.read_consumptions(**SCOPE) == (fact,)
        assert consume(reopened, cred, reservation) == fact
    finally:
        conn.close()


@pytest.mark.parametrize("field,value", [
    ("operation_id", "different-operation"), ("reservation_id", "different-reservation"),
    ("approval_id", "different-approval"), ("expected_effect_digest", "a" * 64),
    ("expected_preparation_digest", "b" * 64),
])
def test_different_binding_never_consumes(consumptions, cred, reservation, field, value):
    assert_error(lambda: consume(consumptions, cred, reservation, **{field: value}))
    assert rows(consumptions.db) == []
    assert consumptions.db.execute("SELECT COUNT(*) FROM host_approval_reservations").fetchone() == (1,)


@pytest.mark.parametrize("same", [True, False])
def test_true_two_connection_concurrent_admission(
        consumptions, cred, reservation, db_path, same):
    barrier = threading.Barrier(2)
    outcomes, errors, connection_ids = [], [], []

    def worker(index):
        conn, stack = _build_stack(db_path)
        try:
            svc = HostApprovalConsumptionService(conn, stack)
            connection_ids.append(id(conn))
            barrier.wait(timeout=10)
            overrides = {} if same or index == 0 else {"operation_id": "other-operation"}
            try:
                outcomes.append(consume(svc, cred, reservation, **overrides))
            except ApprovalConsumptionError as exc:
                errors.append(exc.code)
            assert not conn.in_transaction
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
        assert not thread.is_alive()
    assert len(set(connection_ids)) == 2
    assert len(rows(consumptions.db)) == 1
    assert consumptions.db.execute("SELECT COUNT(*) FROM host_approval_reservations").fetchone() == (1,)
    if same:
        assert len(outcomes) == 2 and outcomes[0] == outcomes[1] and not errors
    else:
        assert len(outcomes) == 1 and errors == ["approval_consumption_unavailable"]


@pytest.mark.parametrize("recursive", [0, 1])
@pytest.mark.parametrize("mutation", ["update", "delete", "reservation_id", "approval_id",
                                      "operation_id", "all_keys", "upsert"])
def test_database_append_only_every_conflict_path(
        consumptions, cred, reservation, recursive, mutation):
    consume(consumptions, cred, reservation)
    db = consumptions.db
    before = rows(db)
    db.execute("PRAGMA recursive_triggers=" + str(recursive))
    with pytest.raises(sqlite3.IntegrityError, match="approval_consumption_append_only"):
        if mutation == "update":
            db.execute("UPDATE " + TABLE + " SET row_sha256='changed'")
        elif mutation == "delete":
            db.execute("DELETE FROM " + TABLE)
        else:
            row = list(before[0])
            for index, field in enumerate(FIELDS[:3]):
                if mutation != "all_keys" and field != mutation:
                    row[index] = "new-" + field
            suffix = ""
            prefix = "INSERT OR REPLACE"
            if mutation == "upsert":
                row = list(before[0])
                prefix = "INSERT"
                suffix = " ON CONFLICT(operation_id) DO UPDATE SET row_sha256='changed'"
            db.execute(prefix + " INTO " + TABLE + " (" + COLUMNS + ") VALUES ("
                       + ",".join("?" for _ in row) + ")" + suffix, row)
    db.rollback()
    assert rows(db) == before


@pytest.mark.parametrize("which", ["read", "write", "history"])
@pytest.mark.parametrize("damage", ["drop_guard", "replace_guard", "extra_guard",
                                    "drop_table", "view", "prepared_guard",
                                    "decision_guard", "reservation_guard"])
def test_schema_and_guard_damage_fail_closed(consumptions, cred, reservation, which, damage):
    db = consumptions.db
    if damage in {"drop_table", "view"}:
        db.execute("DROP TABLE " + TABLE)
        if damage == "view":
            db.execute("CREATE VIEW " + TABLE + " AS SELECT 1 AS reservation_id")
    elif damage.endswith("guard") and damage in {
            "prepared_guard", "decision_guard", "reservation_guard"}:
        table = {"prepared_guard": "host_prepared_effects",
                 "decision_guard": "host_approval_decisions",
                 "reservation_guard": "host_approval_reservations"}[damage]
        db.execute("DROP TRIGGER " + table + "_no_update")
    elif damage == "extra_guard":
        db.execute("CREATE TRIGGER unexpected AFTER INSERT ON " + TABLE
                   + " BEGIN SELECT 1; END")
    else:
        db.execute("DROP TRIGGER " + TABLE + "_no_update")
        if damage == "replace_guard":
            db.execute("CREATE TRIGGER " + TABLE + "_no_update BEFORE UPDATE ON "
                       + TABLE + " BEGIN SELECT 1; END")
    db.commit()
    call = {"read": lambda: consumptions.get_current_status(
        cred.secret, now=at(80), operation_id=reservation.operation_id),
        "write": lambda: consume(consumptions, cred, reservation),
        "history": lambda: consumptions.read_consumptions(**SCOPE)}[which]
    assert_error(call, "approval_consumption_storage_invalid")
    assert not db.in_transaction
    assert_error(lambda: HostApprovalConsumptionService(db, consumptions.reservations),
                 "approval_consumption_storage_invalid")


@pytest.mark.parametrize("damage", ["hash", "byte", "noncanonical", "index",
                                    "company_id", "installation_id", "task_id",
                                    "reservation_id", "approval_id", "operation_id",
                                    "effect_digest", "preparation_digest", "state",
                                    "created_at", "reservation_created_at", "expires_at",
                                    "revision_bool", "extra_secret", "missing_field"])
def test_row_corruption_self_consistent_forgeries_and_scope_rejected(
        consumptions, cred, reservation, damage):
    fact = consume(consumptions, cred, reservation)
    db = consumptions.db
    row = list(rows(db)[0])
    record = fact.to_dict()
    if damage == "hash":
        row[-1] = "f" * 64
    elif damage == "byte":
        row[-2] = row[-2] + b" "
    elif damage == "index":
        row[3] = "other-task"
    else:
        if damage == "noncanonical":
            blob = json.dumps(record, indent=2).encode()
        else:
            changes = {"company_id": "other-company", "installation_id": "other-installation",
                       "task_id": "other-task", "reservation_id": "other-reservation",
                       "approval_id": "other-approval", "operation_id": "other-operation",
                       "effect_digest": "e" * 64, "preparation_digest": "d" * 64,
                       "state": "accepted", "created_at": at(10).isoformat(),
                       "reservation_created_at": at(90).isoformat(),
                       "expires_at": at(301).isoformat(), "revision_bool": True}
            if damage == "extra_secret":
                record["token"] = "private-secret-value"
            elif damage == "missing_field":
                del record["target"]
            else:
                key = {"state": "consumption_state", "revision_bool": "member_revision"}.get(damage, damage)
                record[key] = changes[damage]
            blob = git_format.dumps_record(record)
        row = [record[key] for key in FIELDS] + [blob, hashlib.sha256(DOMAIN + blob).hexdigest()]
    # Simulate storage tampering, then restore the exact guard so the row
    # itself (not just guard absence) must be detected.
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?",
                     (TABLE + "_no_update",)).fetchone()[0]
    db.execute("DROP TRIGGER " + TABLE + "_no_update")
    db.execute("UPDATE " + TABLE + " SET " + ",".join(key + "=?" for key in FIELDS
               + ("record_blob", "row_sha256")), row)
    db.execute(sql)
    db.commit()
    assert_error(lambda: consumptions.read_consumptions(**SCOPE), secrets=("private-secret-value",))
    assert_error(lambda: consume(consumptions, cred, reservation))
    assert len(rows(db)) == 1


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("invalidation", ["expired", "revoked", "member", "policy", "revision"])
def test_current_invalidity_explicit_history_permanent(
        consumptions, cred, reservation, credentials, grant, identity,
        authorization, policy, when, invalidation):
    fact = consume(consumptions, cred, reservation) if when == "after" else None
    if invalidation == "revoked":
        credentials.revoke_task_credential(grant.secret, now=at(90), task_id=cred.task_id)
    elif invalidation == "member":
        identity.revoke_member("user-syn-owner", now=at(90))
    elif invalidation == "policy":
        authorization.pause_policy(grant.secret, now=at(90), policy_id=policy.policy_id,
                                   expected_revision=1)
    elif invalidation == "revision":
        consumptions.db.execute("UPDATE host_identity_members SET revision=2")
        consumptions.db.commit()
    now = at(301) if invalidation == "expired" else at(100)
    code = assert_error(lambda: consume(consumptions, cred, reservation, now=now))
    assert_error(lambda: consumptions.get_current_status(
        cred.secret, now=now, operation_id=reservation.operation_id), code)
    assert consumptions.read_consumptions(**SCOPE) == (() if fact is None else (fact,))
    assert consumptions.db.execute("SELECT COUNT(*) FROM host_approval_reservations").fetchone() == (1,)
    assert len(rows(consumptions.db)) == (0 if fact is None else 1)


def test_denied_and_unreserved_are_explicit_without_new_reservations(
        consumptions, cred, decisions, grant, effect):
    denied = decisions.approve_effect(
        grant.secret, now=at(60), operation_id=effect.operation_id,
        expected_effect_digest=effect.effect_digest,
        expected_preparation_digest=effect.preparation_digest,
        decision="denied", expires_at=at(300))
    assert denied.decision == "denied"
    for call in [lambda: consumptions.get_current_status(
            cred.secret, now=at(80), operation_id=effect.operation_id),
            lambda: consumptions.record_admission(cred.secret, now=at(80),
                operation_id=effect.operation_id, reservation_id="does-not-exist",
                approval_id=denied.approval_id, expected_effect_digest=effect.effect_digest,
                expected_preparation_digest=effect.preparation_digest)]:
        assert_error(call, "approval_consumption_denied")
    assert consumptions.db.execute("SELECT COUNT(*) FROM host_approval_decisions").fetchone() == (1,)
    assert consumptions.db.execute("SELECT COUNT(*) FROM host_approval_reservations").fetchone() == (0,)
    assert rows(consumptions.db) == []


@pytest.mark.parametrize("method", ["write", "read", "history", "construct"])
def test_caller_transaction_never_committed_or_rolled_back(
        consumptions, cred, reservation, method):
    db = consumptions.db
    db.execute("CREATE TABLE caller_data (value TEXT)")
    db.execute("INSERT INTO caller_data VALUES ('pending')")
    call = {"write": lambda: consume(consumptions, cred, reservation),
            "read": lambda: consumptions.get_current_status(
                cred.secret, now=at(80), operation_id=reservation.operation_id),
            "history": lambda: consumptions.read_consumptions(**SCOPE),
            "construct": lambda: HostApprovalConsumptionService(db, consumptions.reservations)}[method]
    assert_error(call)
    assert db.in_transaction
    assert db.execute("SELECT * FROM caller_data").fetchall() == [("pending",)]
    db.rollback()
    assert db.execute("SELECT * FROM caller_data").fetchall() == []


def test_constructor_binding_before_ddl_and_explicit_initialization(service, tmp_path):
    db = service.db
    assert_error(lambda: HostApprovalConsumptionService(db, service),
                 "approval_consumption_storage_invalid")
    with sqlite3.connect(tmp_path / "different.db") as other:
        assert_error(lambda: HostApprovalConsumptionService(other, service, initialize=True))
        assert other.execute("SELECT name FROM sqlite_master").fetchall() == []
    assert_error(lambda: HostApprovalConsumptionService(db, object(), initialize=True))
    assert db.execute("SELECT name FROM sqlite_master WHERE name=?", (TABLE,)).fetchall() == []
    HostApprovalConsumptionService(db, service, initialize=True)
    HostApprovalConsumptionService(db, service, initialize=True)


@pytest.mark.parametrize("secret", ["unknown-private-secret", "secret-\ud800", "", None, {}, b"secret"])
def test_invalid_secret_has_fixed_clean_error(consumptions, reservation, secret):
    assert_error(lambda: consumptions.record_admission(secret, **request(reservation)),
                 secrets=("unknown-private-secret", "secret-\ud800"))
    assert rows(consumptions.db) == []


def test_offline_runtime_and_no_new_command_or_db_open_surface(
        consumptions, cred, reservation, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected IO")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    consume(consumptions, cred, reservation)
    consumptions.read_consumptions(**SCOPE)
    consumptions.get_current_status(cred.secret, now=at(90), operation_id=reservation.operation_id)
    tree = ast.parse(Path(consumption_module.__file__).read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert not any(name and name.split(".")[0] in
                   {"socket", "subprocess", "requests", "httpx", "urllib", "openai", "os"}
                   for name in imports)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Name) and node.func.id in
                        {"open", "eval", "exec", "__import__"})
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr in
                        {"connect", "system", "popen", "Popen", "run"})


def test_scope_mismatch_even_with_empty_ledger(consumptions):
    assert_error(lambda: consumptions.read_consumptions(
        installation_id=SCOPE["installation_id"], company_id="foreign"),
        "approval_consumption_scope_mismatch")


@pytest.mark.parametrize("table", ["host_prepared_effects", "host_approval_decisions",
                                   "host_approval_reservations"])
@pytest.mark.parametrize("damage", ["row_hash", "missing_row"])
def test_full_chain_corruption_refused_even_with_intact_consumption(
        consumptions, cred, reservation, table, damage):
    consume(consumptions, cred, reservation)
    db = consumptions.db
    guard = table + ("_no_update" if damage == "row_hash" else "_no_delete")
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (guard,)).fetchone()[0]
    db.execute("DROP TRIGGER " + guard)
    if damage == "row_hash":
        db.execute("UPDATE " + table + " SET row_sha256=?", ("0" * 64,))
    else:
        db.execute("DELETE FROM " + table)
    db.execute(sql)
    db.commit()
    assert_error(lambda: consumptions.read_consumptions(**SCOPE))
    assert_error(lambda: consume(consumptions, cred, reservation))
    assert len(rows(db)) == 1


def test_current_missing_and_foreign_operation_indistinguishable(
        consumptions, cred, reservation, credentials, grant, policy):
    # A second genuine root task cannot see or consume the first one's row.
    other = credentials.issue_task_credential(
        grant.secret, now=at(71), policy_id=policy.policy_id, expected_revision=1)
    foreign = assert_error(lambda: consumptions.get_current_status(
        other.secret, now=at(80), operation_id=reservation.operation_id))
    missing = assert_error(lambda: consumptions.get_current_status(
        other.secret, now=at(80), operation_id="missing-operation"))
    assert foreign == missing == "approval_consumption_unavailable"
    assert_error(lambda: consumptions.record_admission(other.secret, **request(reservation)), foreign)
    assert rows(consumptions.db) == []


@pytest.mark.parametrize("write", [True, False])
def test_real_lock_wait_crossing_expiry_refused(
        consumptions, cred, reservation, db_path, monkeypatch, write):
    from datetime import timedelta
    # The actual BEGIN/first schema read meets an EXCLUSIVE lock owned by
    # a second connection. Advance a controlled host clock after the worker
    # attempts that SQL, while the other connection still holds its lock.
    entered = threading.Event()
    release = threading.Event()
    clock = [10**12]
    monkeypatch.setattr(consumption_module, "_monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(reservations_module, "_monotonic_ns", lambda: clock[0])
    result = []

    class WatchingConnection(sqlite3.Connection):
        watching = False

        def execute(self, sql, parameters=()):
            if self.watching and (sql == "BEGIN IMMEDIATE" if write else sql.startswith(
                    "SELECT name, type, sql FROM sqlite_master WHERE tbl_name IN")):
                waiting.set()
            return super().execute(sql, parameters)

    def worker():
        conn = sqlite3.connect(db_path, factory=WatchingConnection, timeout=10)
        try:
            stack = clone_stack(consumptions.reservations, conn)
            svc = HostApprovalConsumptionService(conn, stack)
            entered.set()
            assert release.wait(10)
            call = (lambda: consume(svc, cred, reservation,
                                    now=reservation.expires_at - timedelta(microseconds=1))) if write else (
                lambda: svc.get_current_status(cred.secret,
                    now=reservation.expires_at - timedelta(microseconds=1),
                    operation_id=reservation.operation_id))
            conn.watching = True
            result.append(assert_error(call, "approval_consumption_expired"))
        finally:
            conn.close()

    waiting = threading.Event()
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    consumptions.db.execute("BEGIN EXCLUSIVE")
    release.set()
    try:
        assert waiting.wait(10)
        clock[0] += 2_000
    finally:
        consumptions.db.rollback()
    thread.join(15)
    assert not thread.is_alive()
    assert result == ["approval_consumption_expired"]
    assert rows(consumptions.db) == []


@pytest.mark.parametrize("failure", ["before_insert", "after_insert", "commit"])
def test_storage_failure_rolls_back_without_secret_in_errors(
        consumptions, cred, reservation, db_path, failure):
    class FaultConnection(sqlite3.Connection):
        armed = False

        def execute(self, sql, parameters=()):
            target = self.armed and sql.startswith("INSERT INTO " + TABLE)
            if target and failure == "before_insert":
                raise sqlite3.OperationalError("private-storage-secret")
            result = super().execute(sql, parameters)
            if target and failure == "after_insert":
                raise sqlite3.OperationalError("private-storage-secret")
            return result

        def commit(self):
            if self.armed and failure == "commit":
                raise sqlite3.OperationalError("private-storage-secret")
            return super().commit()

    # Bind the real existing service graph to a fault-injection connection;
    # no service methods or verification are mocked.
    conn = sqlite3.connect(db_path, factory=FaultConnection)
    try:
        stack = clone_stack(consumptions.reservations, conn)
        svc = HostApprovalConsumptionService(conn, stack)
        conn.armed = True
        assert_error(lambda: consume(svc, cred, reservation),
                     "approval_consumption_storage_invalid", secrets=("private-storage-secret",))
        assert not conn.in_transaction
        assert rows(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM host_approval_reservations").fetchone() == (1,)
    finally:
        conn.close()


def clone_stack(source, db):
    """Bind a test-only graph to an existing DB without invoking any DDL.

    All real service implementations and exact instance relationships stay
    in use. This also lets the W drill verify mode=ro copied databases.
    """
    memo = {}

    def clone(service):
        if id(service) in memo:
            return memo[id(service)]
        result = object.__new__(type(service))
        memo[id(service)] = result
        for key, value in vars(service).items():
            if key == "db":
                value = db
            elif key in {"identity", "authorization", "task_credentials",
                         "prepared_effects", "approval_decisions"}:
                value = clone(value)
            setattr(result, key, value)
        return result
    return clone(source)


@pytest.mark.parametrize("method", ["write", "read", "history"])
def test_closed_authority_connection_fails_closed(consumptions, cred, reservation, method):
    consumptions.db.close()
    call = {"write": lambda: consume(consumptions, cred, reservation),
            "read": lambda: consumptions.get_current_status(cred.secret, now=at(80),
                operation_id=reservation.operation_id),
            "history": lambda: consumptions.read_consumptions(**SCOPE)}[method]
    assert_error(call, "approval_consumption_storage_invalid")


@pytest.mark.parametrize("method", ["write", "read", "history"])
def test_corrupt_database_image_fails_closed(consumptions, cred, reservation, db_path, method):
    consume(consumptions, cred, reservation)
    # SQLite's real image loader replaces this connection's database with
    # a physically invalid image. The original on-disk history stays intact.
    consumptions.db.deserialize(b"corrupt-private-secret" + b"\0" * 4096)
    call = {"write": lambda: consume(consumptions, cred, reservation),
            "read": lambda: consumptions.get_current_status(cred.secret, now=at(90),
                operation_id=reservation.operation_id),
            "history": lambda: consumptions.read_consumptions(**SCOPE)}[method]
    assert_error(call, "approval_consumption_storage_invalid", secrets=("corrupt-private-secret",))
    with sqlite3.connect(db_path) as original:
        assert len(rows(original)) == 1


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_real_work_database_spec_freeze_contains_all_facts_and_guards(
        consumptions, cred, reservation, tmp_path, object_format):
    from app.facts_tree.backup_coordinator import (
        AssemblyIdentity, BarrierBudget, FreezeAssembly, WorkDatabaseSpec,
        freeze_consistent_cut,
    )
    from app.facts_tree.broker_coordination import BrokerCoordination
    from app.facts_tree.git_repository import FactsGitRepository
    fact = consume(consumptions, cred, reservation)
    ids = dict(SCOPE, repo_id="repo-consumption-e1")
    repo = FactsGitRepository.initialize(tmp_path / "facts.git", object_format=object_format, **ids)
    retention = tmp_path / "retention"
    retention.mkdir(mode=0o700)
    broker_db = sqlite3.connect(":memory:")
    coordination = BrokerCoordination(tmp_path / "domain", ids["repo_id"], broker_db)

    def scope_probe(conn):
        assert conn is consumptions.db
        return tuple(sorted({json.loads(row[0])["company_id"] for row in conn.execute(
            "SELECT record_blob FROM " + TABLE)}))

    spec = WorkDatabaseSpec(
        role="w_host", connection=consumptions.db, scope_probe=scope_probe,
        schema={"approval_consumptions": 1}, scope_watermark={"fact_count": 1})
    try:
        cut = freeze_consistent_cut(FreezeAssembly(
            backup_id="bkp-e1-consumptions", identity=AssemblyIdentity(**ids),
            coordination=coordination, repository=repo, databases=(spec,),
            retention_root=retention, budget=BarrierBudget(5.0, 30.0, 0.01)))
        assert cut.frozen, cut.reason
        checkpoint = cut.checkpoints[0]
        path = retention / checkpoint.relative_path
        assert path.exists(), (path, cut)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as restored:
            consumption_module._check_storage(restored)
            replayed = tuple(verify_consumption_row(row, **SCOPE) for row in rows(restored))
            assert replayed == (fact,)
            # Real mode=ro, no DDL constructors and no source writes.
            stack = clone_stack(consumptions.reservations, restored)
            assert HostApprovalConsumptionService(restored, stack).read_consumptions(**SCOPE) == (fact,)
        assert consumptions.read_consumptions(**SCOPE) == (fact,)
    finally:
        broker_db.close()
