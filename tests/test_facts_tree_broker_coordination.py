"""Isolated coordination/SQLite/process fault tests, never real Git acceptance.

Child executors below are inert Python processes with no ref-write ability.
They demonstrate surviving-parent fences, not OS permission provisioning or
the later Broker acceptance/reconciliation/approval-consumption protocol.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from app.facts_tree.broker_coordination import (
    APPROVAL_BIND,
    APPROVAL_VALIDATE,
    AUTHORIZATION_CHANGE,
    AUTHORIZATION_REVOKE,
    BACKUP_BARRIER,
    BrokerCoordination,
    BrokerOccupied,
    CANDIDATE_REVISE,
    CANDIDATE_WITHDRAW,
    CoordinationStateError,
    COVERAGE_INVALIDATE,
    EXTERNAL_OBJECT_GC,
    FINAL_ACCEPT,
    GIT_GC,
    LockOrderError,
    MEMBERSHIP_CHANGE,
    MEMBERSHIP_REVOKE,
    PARTICIPANTS,
    TASK_CANCEL,
    TOKEN_TRANSFER,
    UnregisteredParticipant,
)
from app.facts_tree.operation_work_store import OperationWorkStore


@pytest.fixture
def domain(tmp_path):
    db = sqlite3.connect(":memory:")
    coordination = BrokerCoordination(tmp_path / "repo-domain", "repo-acc-b2", db)
    yield coordination
    db.close()


class Handle:
    """Trusted-launcher test double; no actual process or CAS execution."""

    pid = 424242

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def _launch(domain, lease, handle=None, executor="cas-acc-1"):
    handle = Handle() if handle is None else handle
    with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
        with gate.work_transaction():
            pass  # Later Broker writes the verified execution intent here.
        record = gate.register_executor(executor, lambda: handle)
    return handle, record


def _reopen(domain):
    return BrokerCoordination(domain.directory, domain.repo_id, domain.db)


def test_registry_covers_every_design_participant():
    assert PARTICIPANTS == {
        FINAL_ACCEPT, AUTHORIZATION_CHANGE, AUTHORIZATION_REVOKE,
        MEMBERSHIP_CHANGE, MEMBERSHIP_REVOKE, TASK_CANCEL, TOKEN_TRANSFER,
        CANDIDATE_REVISE, CANDIDATE_WITHDRAW, COVERAGE_INVALIDATE,
        APPROVAL_VALIDATE, APPROVAL_BIND, BACKUP_BARRIER, GIT_GC,
        EXTERNAL_OBJECT_GC,
    }


@pytest.mark.parametrize("operation", ["unknown", "", None, [], "FINAL_ACCEPT"])
def test_unregistered_operation_denied_without_callback_or_audit(domain, operation):
    with pytest.raises(UnregisteredParticipant):
        with domain.admit(operation):
            pytest.fail("unregistered participant was admitted")
    assert domain.audit_records() == ()


@pytest.mark.parametrize("operation", sorted(PARTICIPANTS - {FINAL_ACCEPT}))
def test_registered_participants_share_ordered_entry(domain, operation):
    with domain.admit(operation) as gate:
        with gate.work_transaction() as db:
            assert db.in_transaction
    assert [event["action"] for event in domain.audit_records()] == [
        "admitted", "completed"]
    assert {event["operation"] for event in domain.audit_records()} == {operation}
    assert not domain.db.in_transaction


def test_final_accept_requires_live_singleton_lease(domain):
    with pytest.raises(BrokerOccupied):
        with domain.admit(FINAL_ACCEPT):
            pytest.fail("missing singleton")
    with domain.claim_broker() as lease:
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with gate.work_transaction():
                pass
    with pytest.raises(BrokerOccupied):
        with domain.admit(FINAL_ACCEPT, lease=lease):
            pytest.fail("closed singleton")


def test_correct_order_joins_real_existing_operation_store(domain):
    store = OperationWorkStore(domain.db)
    with domain.admit(TASK_CANCEL) as gate:
        with gate.work_transaction() as db:
            store.record_operation("repo-acc-b2", "acc-b2", "op-acc-b2",
                                   "task-acc-b2", "idem-acc-b2",
                                   "2026-10-30T12:00:00+08:00", {"opaque": 1})
            assert db.in_transaction
    assert store.get_operation("repo-acc-b2", "acc-b2", "op-acc-b2") is not None


def test_caller_transaction_denied_before_lock_and_left_untouched(domain):
    domain.db.execute("CREATE TABLE synthetic (value)")
    domain.db.execute("BEGIN IMMEDIATE")
    domain.db.execute("INSERT INTO synthetic VALUES (1)")
    try:
        for entrance in (domain.maintenance_admission, domain.repo_acceptance_gate,
                         lambda: domain.admit(CANDIDATE_REVISE)):
            with pytest.raises(LockOrderError):
                with entrance():
                    pytest.fail("reverse order admitted")
            assert domain.db.in_transaction
            assert domain.db.execute("SELECT value FROM synthetic").fetchall() == [(1,)]
        with pytest.raises(LockOrderError):
            domain.claim_broker()
        with pytest.raises(LockOrderError):
            _reopen(domain)
    finally:
        domain.db.rollback()
    assert domain.audit_records() == ()


def test_missing_stage_and_nested_reverse_order_fail_closed(domain, tmp_path):
    other_db = sqlite3.connect(":memory:")
    other = BrokerCoordination(tmp_path / "other-domain", "repo-acc-other", other_db)
    try:
        with pytest.raises(LockOrderError):
            with domain.repo_acceptance_gate():
                pass
        with pytest.raises(LockOrderError):
            with domain.work_transaction():
                pass
        with domain.maintenance_admission():
            with pytest.raises(LockOrderError):
                with domain.work_transaction():
                    pass
            with domain.repo_acceptance_gate():
                with domain.work_transaction():
                    for target in (domain, other):
                        with pytest.raises(LockOrderError):
                            with target.maintenance_admission():
                                pass
                        with pytest.raises(LockOrderError):
                            with target.repo_acceptance_gate():
                                pass
                        with pytest.raises(LockOrderError):
                            with target.admit(TASK_CANCEL):
                                pass
    finally:
        other_db.close()


def test_admission_cannot_escape_thread_or_context(domain):
    with domain.admit(TASK_CANCEL) as gate:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(lambda: gate._check())
            with pytest.raises(LockOrderError):
                future.result(timeout=5)
    with pytest.raises(LockOrderError):
        with gate.work_transaction():
            pass


def test_same_domain_multithread_and_multi_instance_mutual_exclusion(domain):
    active = 0
    peak = 0
    entered = []
    guard = threading.Lock()
    barrier = threading.Barrier(6)

    def worker(number):
        nonlocal active, peak
        db = sqlite3.connect(":memory:")
        try:
            local = BrokerCoordination(domain.directory, domain.repo_id, db)
            barrier.wait(timeout=10)
            for index in range(3):
                with local.admit(CANDIDATE_REVISE, detail="%d/%d" % (number, index)) as gate:
                    with gate.work_transaction():
                        with guard:
                            active += 1
                            peak = max(peak, active)
                        time.sleep(0.003)
                        entered.append((number, index))
                        with guard:
                            active -= 1
        finally:
            db.close()

    with ThreadPoolExecutor(6) as pool:
        futures = [pool.submit(worker, number) for number in range(6)]
        for future in futures:
            future.result(timeout=20)
    assert peak == 1
    assert len(set(entered)) == 18
    audit = domain.audit_records()
    assert [event["seq"] for event in audit] == list(range(1, 37))
    for index in range(0, len(audit), 2):
        assert [audit[index]["action"], audit[index + 1]["action"]] == ["admitted", "completed"]
        assert audit[index]["detail"] == audit[index + 1]["detail"]


def test_singleton_held_across_instances_and_cannot_close_in_reverse_order(domain):
    lease = domain.claim_broker()
    try:
        with pytest.raises(BrokerOccupied):
            _reopen(domain).claim_broker(takeover=True)
        with domain.admit(FINAL_ACCEPT, lease=lease):
            with pytest.raises(LockOrderError):
                lease.close()
        with pytest.raises(BrokerOccupied):
            _reopen(domain).claim_broker(takeover=True)
    finally:
        lease.close()


def test_alive_executor_fences_all_participants_even_after_owner_close(domain):
    lease = domain.claim_broker()
    handle, record = _launch(domain, lease)
    assert record.state == "running" and handle.poll() is None
    with pytest.raises(BrokerOccupied):
        domain.confirm_executor_terminated(lease.owner_id, record.executor_id)
    lease.close()
    recovered = _reopen(domain)
    for operation in PARTICIPANTS:
        with pytest.raises(BrokerOccupied):
            with recovered.admit(operation):
                pytest.fail("unresolved executor allowed a participant")
    with pytest.raises(BrokerOccupied):
        recovered.claim_broker(takeover=True)
    assert any(event["action"] == "broker_abandoned" for event in recovered.audit_records())


def test_timeout_never_proves_takeover(domain, monkeypatch):
    lease = domain.claim_broker()
    _launch(domain, lease)
    lease.close()
    monkeypatch.setattr(time, "time", lambda: 10**30)
    monkeypatch.setattr(time, "monotonic", lambda: 10**30)
    with pytest.raises(BrokerOccupied):
        _reopen(domain).claim_broker(takeover=True)
    with pytest.raises(TypeError):
        domain.claim_broker(takeover=True, timeout=0)


def test_observed_actual_exit_and_explicit_takeover(domain):
    lease = domain.claim_broker()
    handle, record = _launch(domain, lease)
    lease.close()
    handle.code = 0
    assert domain.confirm_executor_terminated(lease.owner_id, record.executor_id).state == "terminated"
    recovered = _reopen(domain)
    with pytest.raises(BrokerOccupied):
        recovered.claim_broker()  # Taking over is an explicit action.
    with recovered.claim_broker(takeover=True) as replacement:
        assert replacement.owner_id != lease.owner_id
        assert replacement.generation == lease.generation + 1
        with recovered.admit(FINAL_ACCEPT, lease=replacement):
            pass
        with pytest.raises(BrokerOccupied):
            with domain.admit(FINAL_ACCEPT, lease=lease):
                pass


def test_host_proven_isolation_allows_takeover_of_still_live_executor(domain):
    lease = domain.claim_broker()
    handle, record = _launch(domain, lease)
    lease.close()
    recovered = _reopen(domain)
    seen = []

    def isolate(actual):
        seen.append(actual)
        # Synthetic host attestation, not a real permission change.
        return "acc-host proved writer and descendants cannot write the ref"

    assert recovered.isolate_executor(lease.owner_id, record.executor_id,
                                     prove=isolate).state == "isolated"
    assert seen == [record]
    assert handle.poll() is None
    with recovered.claim_broker(takeover=True):
        pass


@pytest.mark.parametrize("proof", [None, False, True, "", 1])
def test_no_boolean_or_missing_isolation_proof(domain, proof):
    lease = domain.claim_broker()
    _, record = _launch(domain, lease)
    lease.close()
    with pytest.raises((BrokerOccupied, ValueError)):
        domain.isolate_executor(lease.owner_id, record.executor_id, prove=lambda row: proof)
    with pytest.raises(BrokerOccupied):
        domain.claim_broker(takeover=True)


def test_wrong_owner_or_executor_cannot_clear_fence(domain):
    lease = domain.claim_broker()
    _, record = _launch(domain, lease)
    lease.close()
    for owner, executor in [("wrong-owner", record.executor_id), (lease.owner_id, "unknown")]:
        with pytest.raises(BrokerOccupied):
            domain.isolate_executor(owner, executor, prove=lambda row: "acc-isolation-proof")
    with pytest.raises(BrokerOccupied):
        domain.claim_broker(takeover=True)


def test_launcher_failure_after_launch_keeps_durable_registration(domain):
    lease = domain.claim_broker()
    called = []

    def launch():
        called.append("spawn may already have occurred")
        raise RuntimeError("synthetic spawn-result failure")

    with pytest.raises(RuntimeError, match="spawn-result failure"):
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with gate.work_transaction():
                pass
            gate.register_executor("cas-acc-unknown", launch)
    lease.close()
    assert len(called) == 1
    with pytest.raises(BrokerOccupied):
        _reopen(domain).claim_broker(takeover=True)
    assert [event["action"] for event in domain.audit_records()][-3:] == [
        "executor_registered", "failed", "broker_abandoned"]


def test_executor_requires_final_gate_committed_transaction_and_live_handle(domain):
    with domain.claim_broker() as lease:
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with pytest.raises(LockOrderError):
                gate.register_executor("cas-before-intent", lambda: Handle())
            with gate.work_transaction():
                with pytest.raises(LockOrderError):
                    gate.register_executor("cas-during-intent", lambda: Handle())
        with domain.admit(TASK_CANCEL) as gate:
            with gate.work_transaction():
                pass
            with pytest.raises(LockOrderError):
                gate.register_executor("cas-wrong-participant", lambda: Handle())
    lease = domain.claim_broker()
    with pytest.raises(CoordinationStateError):
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with gate.work_transaction():
                pass
            gate.register_executor("cas-no-handle", lambda: object())
    lease.close()
    with pytest.raises(BrokerOccupied):
        domain.claim_broker(takeover=True)


def test_executor_exit_inside_gate_allows_next_participant(domain):
    with domain.claim_broker() as lease:
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with gate.work_transaction():
                pass
            gate.register_executor("cas-acc-done", lambda: Handle(code=0))
            assert gate.confirm_executor_terminated("cas-acc-done").state == "terminated"
        domain.revise_candidate(lambda db: "callback ran")


def _corrupt(domain, target):
    with sqlite3.connect(domain.database_path) as ledger:
        if target == "checksum":
            ledger.execute("UPDATE state SET sha256='damaged'")
        elif target == "payload":
            ledger.execute("UPDATE state SET payload='not-json'")
        elif target == "missing-state":
            ledger.execute("DELETE FROM state")
        elif target == "audit":
            ledger.execute("UPDATE audit SET payload='{}'")
        elif target == "missing-audit":
            ledger.execute("DELETE FROM audit")
        elif target == "unknown-executor-state":
            state = json.loads(ledger.execute("SELECT payload FROM state").fetchone()[0])
            state["executors"]["cas-acc-1"]["state"] = "unknown-invented-state"
            payload = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            ledger.execute("UPDATE state SET payload=?, sha256=?",
                           (payload, hashlib.sha256(payload.encode()).hexdigest()))


@pytest.mark.parametrize("target", ["checksum", "payload", "missing-state", "audit",
                                    "missing-audit", "unknown-executor-state"])
def test_damaged_occupation_or_audit_fails_closed(domain, target):
    lease = domain.claim_broker()
    _launch(domain, lease)
    lease.close()
    _corrupt(domain, target)
    with pytest.raises(CoordinationStateError):
        with domain.admit(FINAL_ACCEPT, lease=lease):
            pass
    with pytest.raises(CoordinationStateError):
        domain.claim_broker(takeover=True)
    with pytest.raises(CoordinationStateError):
        _reopen(domain)


@pytest.mark.parametrize("missing", ["database", "anchor"])
def test_missing_ledger_is_not_reinitialized(domain, missing):
    target = domain.database_path if missing == "database" else domain.directory / "initialized"
    target.unlink()
    with pytest.raises(CoordinationStateError):
        _reopen(domain)
    assert not target.exists()


def test_repo_identity_mismatch_rejected(domain):
    with pytest.raises(CoordinationStateError):
        BrokerCoordination(domain.directory, "different-repo", domain.db)


def test_candidate_seams_order_callbacks_and_audit_in_same_domain(domain):
    domain.db.execute("CREATE TABLE synthetic_changes (seq INTEGER PRIMARY KEY, kind TEXT)")
    calls = []

    def change(kind):
        def callback(db):
            assert db is domain.db and db.in_transaction
            with pytest.raises(LockOrderError):
                with domain.maintenance_admission():
                    pass
            db.execute("INSERT INTO synthetic_changes(kind) VALUES (?)", (kind,))
            calls.append(kind)
            return "opaque-" + kind
        return callback

    for method, kind in [(domain.revise_candidate, CANDIDATE_REVISE),
                         (domain.withdraw_candidate, CANDIDATE_WITHDRAW),
                         (domain.invalidate_coverage, COVERAGE_INVALIDATE)]:
        assert method(change(kind), detail="opaque host request") == "opaque-" + kind
    assert calls == [CANDIDATE_REVISE, CANDIDATE_WITHDRAW, COVERAGE_INVALIDATE]
    assert domain.db.execute("SELECT kind FROM synthetic_changes ORDER BY seq").fetchall() == [
        (kind,) for kind in calls]
    assert [(event["seq"], event["operation"], event["action"])
            for event in _reopen(domain).audit_records()] == [
        (1, CANDIDATE_REVISE, "admitted"), (2, CANDIDATE_REVISE, "completed"),
        (3, CANDIDATE_WITHDRAW, "admitted"), (4, CANDIDATE_WITHDRAW, "completed"),
        (5, COVERAGE_INVALIDATE, "admitted"), (6, COVERAGE_INVALIDATE, "completed")]


def test_candidate_callback_failure_rolls_back_and_audits_failure(domain):
    domain.db.execute("CREATE TABLE synthetic_changes (value)")

    def fail(db):
        db.execute("INSERT INTO synthetic_changes VALUES ('uncommitted')")
        raise RuntimeError("synthetic storage failure")

    with pytest.raises(RuntimeError, match="synthetic storage failure"):
        domain.withdraw_candidate(fail)
    assert domain.db.execute("SELECT * FROM synthetic_changes").fetchall() == []
    assert not domain.db.in_transaction
    assert [event["action"] for event in domain.audit_records()] == ["admitted", "failed"]
    assert domain.invalidate_coverage(lambda db: "next") == "next"


@pytest.mark.parametrize("options", [{"isolation_level": None}, {"autocommit": True}])
def test_autocommit_connection_still_commits_and_rolls_back_explicit_transactions(tmp_path, options):
    db = sqlite3.connect(":memory:", **options)
    try:
        domain = BrokerCoordination(tmp_path / "autocommit", "repo-acc-auto", db)
        db.execute("CREATE TABLE synthetic_changes (value)")
        domain.revise_candidate(lambda db: db.execute("INSERT INTO synthetic_changes VALUES (1)"))

        def fail(db):
            db.execute("INSERT INTO synthetic_changes VALUES (2)")
            raise RuntimeError("synthetic rollback")

        with pytest.raises(RuntimeError):
            domain.withdraw_candidate(fail)
        assert not db.in_transaction
        assert db.execute("SELECT * FROM synthetic_changes").fetchall() == [(1,)]
    finally:
        db.close()


def test_raw_transaction_escape_is_rolled_back_and_audited_as_failure(domain):
    domain.db.execute("CREATE TABLE synthetic_changes (value)")
    with pytest.raises(LockOrderError):
        with domain.admit(TASK_CANCEL):
            domain.db.execute("BEGIN IMMEDIATE")
            domain.db.execute("INSERT INTO synthetic_changes VALUES (1)")
    assert not domain.db.in_transaction
    assert domain.db.execute("SELECT * FROM synthetic_changes").fetchall() == []
    assert [event["action"] for event in domain.audit_records()] == ["admitted", "failed"]


def test_failed_latest_transaction_does_not_authorize_executor_start(domain):
    with domain.claim_broker() as lease:
        with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
            with gate.work_transaction():
                pass
            with pytest.raises(RuntimeError):
                with gate.work_transaction():
                    raise RuntimeError("latest intent failed")
            with pytest.raises(LockOrderError):
                gate.register_executor("cas-after-rollback", lambda: Handle())


def test_handle_attachment_write_failure_keeps_prelaunch_fence(domain, monkeypatch):
    lease = domain.claim_broker()
    save = domain._save
    launched = []

    def fail_attachment(state, action, **kwargs):
        if action == "executor_attached":
            raise CoordinationStateError("synthetic attachment write failure")
        return save(state, action, **kwargs)

    def launch():
        launched.append(Handle())
        return launched[-1]

    with monkeypatch.context() as patch:
        patch.setattr(domain, "_save", fail_attachment)
        with pytest.raises(CoordinationStateError, match="attachment write failure"):
            with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
                with gate.work_transaction():
                    pass
                gate.register_executor("cas-attachment-failed", launch)
    lease.close()
    assert len(launched) == 1 and launched[0].poll() is None
    recovered = _reopen(domain)
    with pytest.raises(BrokerOccupied):
        recovered.claim_broker(takeover=True)
    with pytest.raises(BrokerOccupied):
        recovered.revise_candidate(lambda db: pytest.fail("fence was erased"))


@pytest.mark.parametrize("kind", ["poll", "proof"])
def test_failing_executor_evidence_never_clears_occupation(domain, kind):
    lease = domain.claim_broker()
    handle, record = _launch(domain, lease)
    lease.close()

    def fail(*args):
        raise RuntimeError("synthetic evidence unavailable")

    if kind == "poll":
        handle.poll = fail
        resolve = lambda: domain.confirm_executor_terminated(lease.owner_id, record.executor_id)
    else:
        resolve = lambda: _reopen(domain).isolate_executor(
            lease.owner_id, record.executor_id, prove=fail)
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        resolve()
    with pytest.raises(BrokerOccupied):
        _reopen(domain).claim_broker(takeover=True)


def _wait_file(path, process=None):
    deadline = time.monotonic() + 10
    while not path.exists():
        if process is not None and process.poll() is not None:
            raise AssertionError("child exited before handshake")
        if time.monotonic() >= deadline:
            raise AssertionError("child handshake timed out")
        time.sleep(0.01)


def test_file_lock_serializes_separate_process(domain, tmp_path):
    ready = tmp_path / "process-ready"
    attempt = tmp_path / "process-attempt"
    start = tmp_path / "process-start"
    entered = tmp_path / "process-entered"
    # The child proves it reached the entrance while the parent holds the gate.
    script = """
import sqlite3, sys, time
from pathlib import Path
from app.facts_tree.broker_coordination import BrokerCoordination, TASK_CANCEL
domain = BrokerCoordination(sys.argv[1], 'repo-acc-b2', sqlite3.connect(':memory:'))
Path(sys.argv[2]).write_text('ready')
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('start handshake timed out')
    time.sleep(.01)
Path(sys.argv[4]).write_text('attempting admission')
with domain.admit(TASK_CANCEL):
    Path(sys.argv[5]).write_text('entered')
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(domain.directory),
                                str(ready), str(start), str(attempt), str(entered)])
    try:
        _wait_file(ready, process)
        with domain.admit(CANDIDATE_REVISE):
            start.touch()
            _wait_file(attempt, process)
            time.sleep(0.05)
            assert not entered.exists()
            assert process.poll() is None
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert entered.read_text() == "entered"
    assert [event["operation"] for event in domain.audit_records()
            if event["action"] == "admitted"] == [CANDIDATE_REVISE, TASK_CANCEL]


def test_parent_exit_does_not_clear_surviving_actual_executor(domain, tmp_path):
    ready = tmp_path / "orphan-ready"
    release = tmp_path / "orphan-release"
    done = tmp_path / "orphan-done"
    # Parent exits abruptly, skipping lease.close. Its actual inert executor
    # survives, holding no Python domain object and writing no Git or company DB.
    child_script = """
import sys, time
from pathlib import Path
ready, release, done = map(Path, sys.argv[1:])
ready.write_text('actual executor alive')
deadline = time.monotonic() + 20
while not release.exists() and time.monotonic() < deadline:
    time.sleep(.01)
done.write_text('actual executor has stopped its synthetic work')
"""
    parent_script = """
import os, sqlite3, subprocess, sys
from app.facts_tree.broker_coordination import BrokerCoordination, FINAL_ACCEPT
domain = BrokerCoordination(sys.argv[1], 'repo-acc-b2', sqlite3.connect(':memory:'))
lease = domain.claim_broker()
with domain.admit(FINAL_ACCEPT, lease=lease) as gate:
    with gate.work_transaction():
        pass
    gate.register_executor('cas-orphan', lambda: subprocess.Popen(
        [sys.executable, '-c', sys.argv[2], *sys.argv[3:]]))
os._exit(0)
"""
    parent = subprocess.Popen([sys.executable, "-c", parent_script, str(domain.directory),
                               child_script, str(ready), str(release), str(done)])
    try:
        assert parent.wait(timeout=10) == 0
        _wait_file(ready)
        assert not done.exists()
        with pytest.raises(BrokerOccupied):
            _reopen(domain).claim_broker(takeover=True)
        with pytest.raises(BrokerOccupied):
            with _reopen(domain).admit(FINAL_ACCEPT):
                pass
    finally:
        release.touch()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        _wait_file(done)
    # A completion file or parent death alone still does not release a fence.
    with pytest.raises(BrokerOccupied):
        domain.claim_broker(takeover=True)
