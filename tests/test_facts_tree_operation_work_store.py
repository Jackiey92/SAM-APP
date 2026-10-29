"""Dedicated tests for the F-001/CHG-064 operation/attempt work record.

Scope: the runtime SQLite work-record primitive
``app.facts_tree.operation_work_store`` ONLY, per CHG-064 (DEC-030)
``96-Commit-Broker提交与恢复设计.md`` sections 4, 7 and 9 and F-001 P01
section 10. These are storage tests of one injected in-memory SQLite
connection: the operation request snapshot, its append-only state
history, immutable attempt snapshots, their append-only state history,
the paired section-7 transition write entries and fail-closed
tamper detection.

They are NOT Broker tests: nothing here verifies Git refs/CAS/legal
chains, authorization, approval bindings, candidate revisions, process
isolation or re-admission proofs. The module records host mirrors only
and ``accepted`` here is never acceptance; the tests pin that boundary.

All fixtures are synthetic (acc-* fake company, fake OIDs of '0'/'a'
hex); nothing reads real company data, files, models or repositories.
Stores are exercised through their public methods; direct SELECT is
used solely for row counts, and the private insert helpers are used
ONLY to plant out-of-band histories with otherwise-valid checksums so
that the joint stable-state checks (not merely checksum checks) are
what rejects them.
"""
import hashlib
import sqlite3

import pytest

from app.facts_tree import git_format
from app.facts_tree.operation_work_store import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_ACCEPTING,
    ATTEMPT_PREPARED,
    ATTEMPT_STALE,
    ATTEMPT_UNKNOWN,
    OPERATION_ACCEPTED,
    OPERATION_APPROVAL_REQUIRED,
    OPERATION_IN_FLIGHT,
    OPERATION_PREPARED,
    OPERATION_REJECTED,
    OPERATION_RESERVED,
    OPERATION_UNKNOWN,
    AttemptConflict,
    AttemptEventConflict,
    IllegalStateTransition,
    OperationConflict,
    OperationEventConflict,
    OperationWorkStore,
    UnknownAttempt,
    UnknownOperation,
)

_REPO = "repo-ws-op-1"
_COMPANY = "acc-ws-op"
_OTHER_COMPANY = "acc-ws-op-other"
_OP = "op-ws-1"
_OP2 = "op-ws-2"
_TASK = "task-ws-1"
_KEY = "idem-ws-1"
_ATT = "att-ws-1"
_ATT2 = "att-ws-2"
_T0 = "2026-10-25T10:00:00+08:00"
_T1 = "2026-10-25T10:01:00+08:00"
_T2 = "2026-10-25T10:02:00+08:00"
_T3 = "2026-10-25T10:03:00+08:00"
_T4 = "2026-10-25T10:04:00+08:00"
_T5 = "2026-10-25T10:05:00+08:00"
_T6 = "2026-10-25T10:06:00+08:00"

_SHA1_BASE = "0" * 40
_SHA1_C = "a" * 40
_SHA256_BASE = "0" * 64
_SHA256_C = "a" * 64

_RECORDS_TABLE = "fact_operation_work_records"
_OP_EVENTS_TABLE = "fact_operation_work_events"
_ATTEMPTS_TABLE = "fact_operation_attempts"
_ATT_EVENTS_TABLE = "fact_operation_attempt_events"


def _request(n=1):
    # The payload carries identity-looking keys on purpose: stored
    # identity must come from the explicit host arguments, never here.
    return {
        "task": "synthetic operation request %d" % n,
        "company_id": "payload-scope-must-not-win",
        "repo_id": "payload-repo-must-not-win",
        "operation_id": "payload-op-must-not-win",
        "n": n,
    }


def _plan(n=1):
    return {"plan": "synthetic plan %d" % n, "round": n}


def _effect(n=1):
    return {"effect": "synthetic accurate effect %d" % n}


def _deps(n=1):
    return {"dependency_versions": {"dep-x": "v%d" % n}}


def _open():
    conn = sqlite3.connect(":memory:")
    assert conn.in_transaction is False
    store = OperationWorkStore(conn)
    return conn, store


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def _record_operation(conn, store, *, operation_id=_OP, idempotency_key=_KEY,
                      request=None, company=_COMPANY, task=_TASK,
                      created_at=_T0):
    return store.record_operation(
        _REPO, company, operation_id, task, idempotency_key, created_at,
        _request() if request is None else request)


def _prepare(conn, store, *, operation_id=_OP, company=_COMPANY,
             event_id="evt-op-prepared", recorded_at=_T1, detail=None):
    return store.mark_operation_prepared(
        _REPO, company, operation_id, event_id, recorded_at, detail)


def _seed_prepared(conn, store, **kwargs):
    _record_operation(conn, store, **kwargs)
    return _prepare(conn, store,
                    operation_id=kwargs.get("operation_id", _OP),
                    company=kwargs.get("company", _COMPANY))


def _record_attempt(conn, store, *, operation_id=_OP, attempt_id=_ATT,
                    company=_COMPANY, created_at=_T2, base=_SHA1_BASE,
                    object_format="sha1", n=1,
                    prepared_event_id="evt-att-prepared",
                    prepared_at=_T2, prepared_detail=None):
    return store.record_attempt(
        _REPO, company, operation_id, attempt_id, created_at, base,
        _plan(n), _effect(n), _deps(n),
        object_format=object_format,
        prepared_event_id=prepared_event_id,
        prepared_at=prepared_at,
        prepared_detail=prepared_detail)


def _seed_prepared_attempt(conn, store, **kwargs):
    _seed_prepared(conn, store, operation_id=kwargs.get("operation_id", _OP),
                   company=kwargs.get("company", _COMPANY))
    return _record_attempt(conn, store, **kwargs)


def _admit(conn, store, *, operation_id=_OP, attempt_id=_ATT,
           company=_COMPANY, op_event="evt-op-inflight",
           att_event="evt-att-accepting", recorded_at=_T3,
           operation_detail=None, attempt_detail=None):
    return store.admit_execution(
        _REPO, company, operation_id, attempt_id, op_event, att_event,
        recorded_at, operation_detail, attempt_detail)


def _unknown(conn, store, *, operation_id=_OP, attempt_id=_ATT,
             company=_COMPANY, op_event="evt-op-unknown",
             att_event="evt-att-unknown", recorded_at=_T4):
    return store.mark_unknown(
        _REPO, company, operation_id, attempt_id, op_event, att_event,
        recorded_at)


def _accept(conn, store, commit=_SHA1_C, *, operation_id=_OP,
            attempt_id=_ATT, company=_COMPANY, op_event="evt-op-accepted",
            att_event="evt-att-accepted", recorded_at=_T5):
    return store.mark_accepted(
        _REPO, company, operation_id, attempt_id, commit, op_event,
        att_event, recorded_at)


# -- construction and transaction contract ---------------------------------

def test_constructor_requires_sqlite_connection():
    with pytest.raises(TypeError):
        OperationWorkStore(":memory:")
    with pytest.raises(TypeError):
        OperationWorkStore(None)


def test_constructor_refuses_active_caller_transaction():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE preexisting (x INTEGER)")
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            OperationWorkStore(conn)
        # The refused construction must not have touched the caller
        # transaction: it stays open and still rolls back cleanly.
        assert conn.in_transaction is True
    finally:
        conn.rollback()
        conn.close()


def test_constructor_is_idempotent_and_idle():
    conn, store = _open()
    assert conn.in_transaction is False
    # A second construction on the same shared connection must be a
    # no-op schema setup and must stay idle.
    OperationWorkStore(conn)
    assert conn.in_transaction is False


def test_writes_require_active_caller_transaction():
    conn, store = _open()
    assert conn.in_transaction is False
    with pytest.raises(ValueError, match="active caller transaction"):
        store.record_operation(
            _REPO, _COMPANY, _OP, _TASK, _KEY, _T0, _request())


def test_no_public_single_side_event_append_exists():
    # CHG-064 review finding 1: no public single-side write entry may
    # leave a committable in_flight/unknown/accepted intermediate.
    conn, store = _open()
    for name in ("append_operation_event", "append_attempt_event"):
        assert not hasattr(store, name)


def test_caller_commit_persists_full_paired_lifecycle():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    _unknown(conn, store)
    _admit(conn, store, op_event="evt-op-inflight-2",
           att_event="evt-att-accepting-2", recorded_at=_T5)
    _accept(conn, store, recorded_at=_T6)
    conn.commit()
    assert conn.in_transaction is False

    assert store.verify_integrity() is None
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    assert operation["current_state"] == OPERATION_ACCEPTED
    assert operation["accepted_commit"] == _SHA1_C
    attempt = store.get_attempt(_REPO, _COMPANY, _OP, _ATT)
    assert attempt["current_state"] == ATTEMPT_ACCEPTED
    assert attempt["accepted_commit"] == _SHA1_C
    op_events = store.list_operation_events(_REPO, _COMPANY, _OP)
    att_events = store.list_attempt_events(_REPO, _COMPANY, _OP, _ATT)
    assert [event["state"] for event in op_events] == [
        OPERATION_PREPARED, OPERATION_IN_FLIGHT, OPERATION_UNKNOWN,
        OPERATION_IN_FLIGHT, OPERATION_ACCEPTED]
    assert [event["state"] for event in att_events] == [
        ATTEMPT_PREPARED, ATTEMPT_ACCEPTING, ATTEMPT_UNKNOWN,
        ATTEMPT_ACCEPTING, ATTEMPT_ACCEPTED]


def test_caller_rollback_undoes_all_four_tables():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    assert _count(conn, _RECORDS_TABLE) == 1
    assert _count(conn, _OP_EVENTS_TABLE) == 2
    assert _count(conn, _ATTEMPTS_TABLE) == 1
    assert _count(conn, _ATT_EVENTS_TABLE) == 2
    conn.rollback()
    assert conn.in_transaction is False
    assert _count(conn, _RECORDS_TABLE) == 0
    assert _count(conn, _OP_EVENTS_TABLE) == 0
    assert _count(conn, _ATTEMPTS_TABLE) == 0
    assert _count(conn, _ATT_EVENTS_TABLE) == 0
    assert store.get_operation(_REPO, _COMPANY, _OP) is None


def test_failed_paired_call_rolls_back_both_halves_and_keeps_caller_txn():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)

    # Inject a failure AFTER the attempt accepting half is inserted but
    # before the SAVEPOINT releases; the store must roll back to its
    # savepoint, leaving no accepting half and an open, still-usable
    # caller transaction.
    original = store._insert_op_event

    def failing_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected post-insert failure")

    store._insert_op_event = failing_insert
    with pytest.raises(RuntimeError, match="injected"):
        _admit(conn, store)
    store._insert_op_event = original

    assert conn.in_transaction is True
    assert store.get_operation(_REPO, _COMPANY, _OP)["current_state"] \
        == OPERATION_PREPARED
    assert store.get_attempt(_REPO, _COMPANY, _OP, _ATT)["current_state"] \
        == ATTEMPT_PREPARED
    assert _count(conn, _OP_EVENTS_TABLE) == 1
    assert _count(conn, _ATT_EVENTS_TABLE) == 1
    # The same caller transaction remains usable for the real pair.
    pair = _admit(conn, store)
    assert pair["operation_event"]["state"] == OPERATION_IN_FLIGHT
    assert pair["attempt_event"]["state"] == ATTEMPT_ACCEPTING
    assert store.verify_integrity() is None
    conn.rollback()


# -- operation snapshot identity (CHG-064 section 4) -----------------------

def test_request_digest_is_sha256_of_canonical_bytes():
    conn, store = _open()
    conn.execute("BEGIN")
    record = _record_operation(conn, store, request=_request(3))
    expected = hashlib.sha256(
        git_format.dumps_record(_request(3))).hexdigest()
    assert record["request_sha256"] == expected
    assert record["replayed"] is False
    assert record["created_at"] == _T0
    conn.rollback()


def test_identity_is_never_taken_from_request_payload():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    record = store.get_operation(_REPO, _COMPANY, _OP)
    assert record["company_id"] == _COMPANY
    assert record["repo_id"] == _REPO
    assert record["operation_id"] == _OP
    assert record["request"]["company_id"] == "payload-scope-must-not-win"
    assert store.get_operation(_REPO, "payload-scope-must-not-win",
                               _OP) is None
    conn.rollback()


def test_same_operation_replays_identical_request_keeping_original_meta():
    conn, store = _open()
    conn.execute("BEGIN")
    first = _record_operation(conn, store)
    replay = store.record_operation(
        _REPO, _COMPANY, _OP, _TASK, _KEY, _T6, _request())
    assert replay["replayed"] is True
    assert replay["created_at"] == _T0
    assert replay["request_sha256"] == first["request_sha256"]
    assert _count(conn, _RECORDS_TABLE) == 1
    conn.rollback()


def test_same_operation_different_request_content_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store, request=_request(1))
    with pytest.raises(OperationConflict, match="different task"):
        _record_operation(conn, store, request=_request(2))
    with pytest.raises(OperationConflict):
        store.record_operation(
            _REPO, _COMPANY, _OP, _TASK, _KEY, _T0,
            {"task": "different canonical content"})
    conn.rollback()


def test_same_operation_different_task_or_key_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    with pytest.raises(OperationConflict):
        store.record_operation(
            _REPO, _COMPANY, _OP, "task-ws-other", _KEY, _T0, _request())
    with pytest.raises(OperationConflict):
        store.record_operation(
            _REPO, _COMPANY, _OP, _TASK, "idem-ws-other", _T0, _request())
    conn.rollback()


def test_idempotency_key_cannot_be_reused_under_another_operation():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    with pytest.raises(OperationConflict, match="idempotency key"):
        _record_operation(conn, store, operation_id=_OP2,
                          idempotency_key=_KEY)
    conn.rollback()


def test_same_canonical_request_allowed_under_different_operations():
    # CHG-064 review finding 2: section 4 freezes the request digest
    # only WITHIN one operation. Identical canonical content under a
    # different operation id and a different idempotency key is legal.
    conn, store = _open()
    conn.execute("BEGIN")
    first = _record_operation(conn, store)
    second = _record_operation(
        conn, store, operation_id=_OP2, idempotency_key="idem-ws-2",
        request=_request())
    assert second["replayed"] is False
    assert second["request_sha256"] == first["request_sha256"]
    assert second["operation_id"] == _OP2
    by_key = store.get_operation_by_idempotency_key(
        _REPO, _COMPANY, "idem-ws-2")
    assert by_key["operation_id"] == _OP2
    assert store.verify_integrity() is None
    conn.rollback()


def test_snapshot_before_prepared_has_no_current_state():
    # CHG-064 section 5 step 1: only the task input may be registered.
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    assert operation["current_state"] is None
    assert operation["current_seq"] is None
    assert operation["accepted_commit"] is None
    assert store.list_operation_events(_REPO, _COMPANY, _OP) == []
    conn.rollback()


# -- operation-only state machine ------------------------------------------

def test_operation_legal_pre_flight_path_and_replay():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    prepared = _prepare(conn, store, detail={"why": "first"})
    assert prepared["seq"] == 1
    assert prepared["detail"] == {"why": "first"}
    approval = store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T2)
    assert approval["state"] == OPERATION_APPROVAL_REQUIRED
    reserved = store.mark_reserved(
        _REPO, _COMPANY, _OP, "evt-op-reserved", _T3)
    assert reserved["state"] == OPERATION_RESERVED
    assert reserved["replayed"] is False
    # Identical replay appends nothing and returns the stored event.
    replay = store.mark_reserved(
        _REPO, _COMPANY, _OP, "evt-op-reserved", _T3)
    assert replay["replayed"] is True
    assert replay["seq"] == 3
    assert _count(conn, _OP_EVENTS_TABLE) == 3
    assert store.verify_integrity() is None
    conn.rollback()


def test_default_event_detail_is_canonical_empty_object():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    event = _prepare(conn, store)
    assert event["detail"] == {}
    assert event["detail_sha256"] == hashlib.sha256(
        git_format.dumps_record({})).hexdigest()
    conn.rollback()


def test_prepared_must_be_first_operation_event():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    with pytest.raises(IllegalStateTransition, match="first operation"):
        store.mark_approval_required(
            _REPO, _COMPANY, _OP, "evt-bad", _T1)
    conn.rollback()


def test_operation_event_unknown_operation_raises():
    conn, store = _open()
    conn.execute("BEGIN")
    with pytest.raises(UnknownOperation):
        _prepare(conn, store)
    conn.rollback()


def test_illegal_operation_edges_are_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    # approval_required admits nothing but reserve: a second approval
    # event is an illegal self-loop.
    store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T2)
    with pytest.raises(IllegalStateTransition):
        store.mark_approval_required(
            _REPO, _COMPANY, _OP, "evt-op-approval-2", _T3)
    conn.rollback()

    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    with pytest.raises(IllegalStateTransition):
        store.mark_reserved(
            _REPO, _COMPANY, _OP, "evt-op-reserved", _T2)
    # Repeated prepared is illegal.
    with pytest.raises(IllegalStateTransition):
        _prepare(conn, store, event_id="evt-op-prepared-2")
    conn.rollback()


def test_replayed_event_id_with_different_content_conflicts():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T2)
    with pytest.raises(OperationEventConflict):
        # Same event id, different state/detail.
        store.mark_reserved(
            _REPO, _COMPANY, _OP, "evt-op-approval", _T3,
            {"different": True})
    conn.rollback()


def test_reject_from_prepared_and_reserved():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    rejected = store.reject_operation(
        _REPO, _COMPANY, _OP, "evt-op-rejected", _T2,
        {"reason": "illegal request"})
    assert rejected["state"] == OPERATION_REJECTED
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    assert operation["current_state"] == OPERATION_REJECTED
    with pytest.raises(IllegalStateTransition):
        store.mark_approval_required(
            _REPO, _COMPANY, _OP, "evt-after-reject", _T3)
    assert store.verify_integrity() is None
    conn.rollback()


def test_reject_is_blocked_from_gate_and_gate_states():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T2)
    with pytest.raises(IllegalStateTransition):
        store.reject_operation(
            _REPO, _COMPANY, _OP, "evt-bad-reject", _T3)
    conn.rollback()

    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(IllegalStateTransition, match="reconciled"):
        store.reject_operation(
            _REPO, _COMPANY, _OP, "evt-bad-reject", _T4)
    conn.rollback()


# -- attempt snapshot and pre-flight scheduling ----------------------------

def test_record_attempt_inserts_snapshot_and_prepared_event_together():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    attempt = _record_attempt(conn, store)
    assert attempt["replayed"] is False
    assert attempt["base_commit"] == _SHA1_BASE
    assert attempt["object_format"] == "sha1"
    assert attempt["plan"] == _plan()
    assert attempt["plan_sha256"] == hashlib.sha256(
        git_format.dumps_record(_plan())).hexdigest()
    assert attempt["prepared_event"]["state"] == ATTEMPT_PREPARED
    assert attempt["prepared_event"]["seq"] == 1
    assert attempt["prepared_event"]["replayed"] is False
    assert _count(conn, _ATTEMPTS_TABLE) == 1
    assert _count(conn, _ATT_EVENTS_TABLE) == 1
    assert store.verify_integrity() is None
    conn.rollback()


def test_record_attempt_requires_prepared_operation_and_pre_flight_state():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    with pytest.raises(IllegalStateTransition, match="prepared"):
        _record_attempt(conn, store)
    conn.rollback()

    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(IllegalStateTransition,
                       match="reconciled on its existing attempt"):
        _record_attempt(conn, store, attempt_id=_ATT2)
    _unknown(conn, store)
    with pytest.raises(IllegalStateTransition,
                       match="reconciled on its existing attempt"):
        _record_attempt(conn, store, attempt_id=_ATT2)
    conn.rollback()


def test_attempt_requires_known_operation():
    conn, store = _open()
    conn.execute("BEGIN")
    with pytest.raises(UnknownOperation):
        _record_attempt(conn, store)
    conn.rollback()


def test_attempt_replay_and_immutable_content_conflict():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _record_attempt(conn, store)
    replay = _record_attempt(conn, store)
    assert replay["replayed"] is True
    assert replay["prepared_event"]["replayed"] is True
    assert _count(conn, _ATTEMPTS_TABLE) == 1
    assert _count(conn, _ATT_EVENTS_TABLE) == 1
    with pytest.raises(AttemptConflict):
        _record_attempt(conn, store, n=2)
    with pytest.raises(AttemptConflict):
        _record_attempt(conn, store,
                        prepared_event_id="evt-att-prepared-other")
    conn.rollback()


def test_live_sibling_attempt_blocks_new_attempt():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _record_attempt(conn, store)
    with pytest.raises(IllegalStateTransition, match="live attempt"):
        _record_attempt(conn, store, attempt_id=_ATT2)
    conn.rollback()


def test_stale_attempt_allows_follow_up_attempt_pre_flight():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _record_attempt(conn, store)
    stale = store.mark_attempt_stale(
        _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T3,
        {"reason": "candidate revised before gate"})
    assert stale["state"] == ATTEMPT_STALE
    assert stale["replayed"] is False
    follow_up = _record_attempt(
        conn, store, attempt_id=_ATT2, created_at=_T4, n=2,
        prepared_event_id="evt-att2-prepared", prepared_at=_T4)
    assert follow_up["replayed"] is False
    attempts = store.list_attempts(_REPO, _COMPANY, _OP)
    assert [a["attempt_id"] for a in attempts] == [_ATT, _ATT2]
    assert store.verify_integrity() is None
    conn.rollback()


def test_stale_is_reachable_only_from_prepared_pre_flight():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    # Double stale is terminal.
    store.mark_attempt_stale(
        _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T3)
    with pytest.raises(IllegalStateTransition, match="prepared"):
        store.mark_attempt_stale(
            _REPO, _COMPANY, _OP, _ATT, "evt-att-stale-2", _T4)
    conn.rollback()

    # No accepting -> stale edge.
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(IllegalStateTransition, match="unknown -> stale"):
        store.mark_attempt_stale(
            _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T4)
    conn.rollback()

    # No unknown -> stale edge: scheduling can never clear unknown.
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    _unknown(conn, store)
    with pytest.raises(IllegalStateTransition, match="unknown -> stale"):
        store.mark_attempt_stale(
            _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T5)
    conn.rollback()


def test_stale_requires_known_attempt():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    with pytest.raises(UnknownAttempt):
        store.mark_attempt_stale(
            _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T3)
    conn.rollback()


# -- paired section-7 transitions ------------------------------------------

def test_admit_pairs_in_flight_and_accepting_in_one_call():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    pair = _admit(conn, store)
    assert pair["operation_event"]["state"] == OPERATION_IN_FLIGHT
    assert pair["operation_event"]["accepted_commit"] is None
    assert pair["attempt_event"]["state"] == ATTEMPT_ACCEPTING
    assert pair["operation_event"]["replayed"] is False
    assert pair["attempt_event"]["replayed"] is False
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    attempt = store.get_attempt(_REPO, _COMPANY, _OP, _ATT)
    assert operation["current_state"] == OPERATION_IN_FLIGHT
    assert attempt["current_state"] == ATTEMPT_ACCEPTING
    assert store.verify_integrity() is None
    conn.rollback()


def test_admit_from_reserved_and_not_from_approval_required():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T2)
    # An attempt may be prepared during approval_required, but the gate
    # cannot be opened from that state: reserve first.
    _record_attempt(conn, store)
    with pytest.raises(IllegalStateTransition):
        _admit(conn, store)
    store.mark_reserved(
        _REPO, _COMPANY, _OP, "evt-op-reserved", _T3)
    pair = _admit(conn, store, recorded_at=_T5)
    assert pair["operation_event"]["state"] == OPERATION_IN_FLIGHT
    conn.rollback()


def test_admit_requires_matching_attempt_state():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    # A second admit while already accepting is not an edge pair.
    with pytest.raises(IllegalStateTransition):
        _admit(conn, store, op_event="evt-op-inflight-2",
              att_event="evt-att-accepting-2")
    conn.rollback()


def test_unknown_retains_occupation_and_readmission_pairs_again():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    pair = _unknown(conn, store)
    assert pair["operation_event"]["state"] == OPERATION_UNKNOWN
    assert pair["attempt_event"]["state"] == ATTEMPT_UNKNOWN
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    attempt = store.get_attempt(_REPO, _COMPANY, _OP, _ATT)
    assert operation["current_state"] == OPERATION_UNKNOWN
    assert attempt["current_state"] == ATTEMPT_UNKNOWN
    # A fresh attempt id can never dodge an unknown result.
    with pytest.raises(IllegalStateTransition, match="existing attempt"):
        _record_attempt(conn, store, attempt_id=_ATT2)
    readmit = _admit(
        conn, store, op_event="evt-op-readmit",
        att_event="evt-att-readmit-accepting", recorded_at=_T5)
    assert readmit["operation_event"]["state"] == OPERATION_IN_FLIGHT
    assert readmit["attempt_event"]["state"] == ATTEMPT_ACCEPTING
    assert store.verify_integrity() is None
    conn.rollback()


def test_mark_unknown_requires_the_in_flight_pair():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    with pytest.raises(IllegalStateTransition):
        _unknown(conn, store)
    conn.rollback()


def test_accepted_pair_from_in_flight_couples_one_commit():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    pair = _accept(conn, store)
    assert pair["operation_event"]["state"] == OPERATION_ACCEPTED
    assert pair["attempt_event"]["state"] == ATTEMPT_ACCEPTED
    assert pair["operation_event"]["accepted_commit"] == _SHA1_C
    assert pair["attempt_event"]["accepted_commit"] == _SHA1_C
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    assert operation["accepted_commit"] == _SHA1_C
    # Terminal.
    with pytest.raises(IllegalStateTransition):
        _admit(conn, store, op_event="evt-op-again",
               att_event="evt-att-again")
    assert store.verify_integrity() is None
    conn.rollback()


def test_accepted_pair_from_unknown_after_readmission_cycle():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    _unknown(conn, store)
    pair = _accept(conn, store, recorded_at=_T5)
    assert pair["operation_event"]["state"] == OPERATION_ACCEPTED
    assert pair["attempt_event"]["state"] == ATTEMPT_ACCEPTED
    assert store.verify_integrity() is None
    conn.rollback()


def test_accepted_requires_commit_and_oid_shape():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(ValueError, match="accepted_commit"):
        store.mark_accepted(
            _REPO, _COMPANY, _OP, _ATT, None, "evt-op-accepted",
            "evt-att-accepted", _T5)
    with pytest.raises(ValueError, match="40 hex"):
        _accept(conn, store, commit=_SHA256_C)  # sha1 repo, 64 chars
    with pytest.raises(ValueError, match="lowercase hex"):
        _accept(conn, store, commit="A" * 40)
    with pytest.raises(ValueError, match="40 hex"):
        _accept(conn, store, commit="a" * 39)
    conn.rollback()


def test_accepted_commit_validated_per_attempt_object_format():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _record_attempt(conn, store, base=_SHA256_BASE,
                    object_format="sha256",
                    prepared_event_id="evt-att-prepared")
    _admit(conn, store)
    with pytest.raises(ValueError, match="64 hex"):
        _accept(conn, store, commit=_SHA1_C)
    pair = _accept(conn, store, commit=_SHA256_C)
    assert pair["attempt_event"]["accepted_commit"] == _SHA256_C
    assert store.verify_integrity() is None
    conn.rollback()


def test_genesis_all_zero_base_commit_accepted():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    attempt = _record_attempt(conn, store, base=_SHA1_BASE)
    assert attempt["base_commit"] == _SHA1_BASE
    conn.rollback()


def test_reject_blocked_while_attempt_accepting_or_unknown():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(IllegalStateTransition, match="unknown result"):
        store.reject_operation(
            _REPO, _COMPANY, _OP, "evt-op-reject", _T4)
    conn.rollback()

    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    _unknown(conn, store)
    with pytest.raises(IllegalStateTransition):
        store.reject_operation(
            _REPO, _COMPANY, _OP, "evt-op-reject", _T5)
    conn.rollback()


def test_paired_call_replays_when_both_event_ids_match():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    pair = _admit(conn, store)
    replay = _admit(conn, store)
    assert replay["operation_event"]["replayed"] is True
    assert replay["attempt_event"]["replayed"] is True
    assert replay["operation_event"]["seq"] == \
        pair["operation_event"]["seq"]
    assert _count(conn, _OP_EVENTS_TABLE) == 2
    assert _count(conn, _ATT_EVENTS_TABLE) == 2
    conn.rollback()


def test_paired_replay_with_different_content_conflicts():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    with pytest.raises(OperationEventConflict):
        _admit(conn, store, operation_detail={"different": True})
    with pytest.raises(AttemptEventConflict):
        _admit(conn, store, attempt_detail={"x": 1})
    conn.rollback()


def test_half_recorded_pair_fails_closed():
    # Exactly one of the two paired event ids already on disk (planted
    # out of band) must never be silently completed or trusted.
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    store._insert_op_event(
        _REPO, _COMPANY, _OP, 2, "evt-orphan-pair",
        OPERATION_IN_FLIGHT, None, _T3,
        git_format.dumps_record({}),
        hashlib.sha256(git_format.dumps_record({})).hexdigest(),
        replayed=False)
    with pytest.raises(ValueError, match="half recorded"):
        _admit(conn, store, op_event="evt-orphan-pair",
               att_event="evt-att-accepting")
    conn.rollback()


# -- scope isolation --------------------------------------------------------

def test_scopes_are_isolated_and_same_ids_coexist():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _admit(conn, store)
    # Same operation/attempt/event ids in a different company scope.
    _seed_prepared_attempt(conn, store, company=_OTHER_COMPANY)
    _admit(conn, store, company=_OTHER_COMPANY)
    assert store.verify_integrity() is None
    first = store.get_operation(_REPO, _COMPANY, _OP)
    other = store.get_operation(_REPO, _OTHER_COMPANY, _OP)
    assert first["company_id"] == _COMPANY
    assert other["company_id"] == _OTHER_COMPANY
    assert store.get_operation(_REPO, _OTHER_COMPANY, _OP)[
        "current_state"] == OPERATION_IN_FLIGHT
    with pytest.raises(UnknownAttempt):
        store.list_attempt_events(_REPO, _OTHER_COMPANY, _OP, _ATT2)
    conn.rollback()


# -- append-only database guards -------------------------------------------

def test_direct_update_and_delete_are_blocked_on_all_tables():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    cases = [
        ("UPDATE " + _RECORDS_TABLE + " SET task_id='x'", ()),
        ("DELETE FROM " + _RECORDS_TABLE, ()),
        ("UPDATE " + _OP_EVENTS_TABLE + " SET state='rejected'", ()),
        ("DELETE FROM " + _OP_EVENTS_TABLE, ()),
        ("UPDATE " + _ATTEMPTS_TABLE + " SET base_commit=?", (_SHA1_C,)),
        ("DELETE FROM " + _ATTEMPTS_TABLE, ()),
        ("UPDATE " + _ATT_EVENTS_TABLE + " SET state='stale'", ()),
        ("DELETE FROM " + _ATT_EVENTS_TABLE, ()),
    ]
    for sql, params in cases:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql, params)
    conn.rollback()


def test_insert_or_replace_is_blocked_for_existing_identity():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    blob = git_format.dumps_record(_request())
    digest = hashlib.sha256(blob).hexdigest()
    row_digest = store._operation_row_digest(
        _REPO, _COMPANY, _TASK, _OP, _KEY, _T0, digest)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO " + _RECORDS_TABLE
            + " (repo_id, company_id, task_id, operation_id, "
            "idempotency_key, created_at, request_blob, request_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_REPO, _COMPANY, _TASK, _OP, _KEY, _T0, blob, digest,
             row_digest))
    conn.rollback()


def test_events_require_their_parent_rows():
    conn, store = _open()
    conn.execute("BEGIN")
    blob = git_format.dumps_record({})
    digest = hashlib.sha256(blob).hexdigest()
    with pytest.raises(sqlite3.DatabaseError, match="requires a recorded"):
        conn.execute(
            "INSERT INTO " + _OP_EVENTS_TABLE
            + " (repo_id, company_id, operation_id, seq, event_id, "
            "state, state_commit, recorded_at, detail_blob, "
            "detail_sha256, row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (_REPO, _COMPANY, "op-orphan", 1, "evt-orphan", "prepared",
             None, _T1, blob, digest, digest))
    conn.rollback()


def test_tampered_request_digest_fails_closed_on_every_read():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    conn.execute("DROP TRIGGER fact_operation_work_records_no_update")
    conn.execute(
        "UPDATE " + _RECORDS_TABLE + " SET request_sha256=?",
        ("f" * 64,))
    with pytest.raises(ValueError, match="tampered"):
        store.get_operation(_REPO, _COMPANY, _OP)
    with pytest.raises(ValueError, match="tampered"):
        store.verify_integrity()
    conn.rollback()


def test_tampered_event_detail_fails_closed():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    conn.execute("DROP TRIGGER fact_operation_work_events_no_update")
    conn.execute(
        "UPDATE " + _OP_EVENTS_TABLE
        + " SET detail_sha256=? WHERE event_id='evt-op-prepared'",
        ("f" * 64,))
    with pytest.raises(ValueError, match="tampered"):
        store.list_operation_events(_REPO, _COMPANY, _OP)
    conn.rollback()


# -- joint stable-state enforcement (review finding 1) ---------------------

def _plant_op_event(store, seq, state, *, event_id=None, commit=None,
                    recorded_at=_T3, detail=None):
    detail = {} if detail is None else detail
    blob = git_format.dumps_record(detail)
    return store._insert_op_event(
        _REPO, _COMPANY, _OP, seq,
        event_id or ("evt-plant-op-%d" % seq), state, commit, recorded_at,
        blob, hashlib.sha256(blob).hexdigest(), replayed=False)


def _plant_att_event(store, seq, state, *, attempt_id=_ATT, event_id=None,
                     commit=None, recorded_at=_T3, detail=None):
    detail = {} if detail is None else detail
    blob = git_format.dumps_record(detail)
    return store._insert_att_event(
        _REPO, _COMPANY, _OP, attempt_id, seq,
        event_id or ("evt-plant-att-%d" % seq), state, commit, recorded_at,
        blob, hashlib.sha256(blob).hexdigest(), replayed=False)


def _plant_attempt_row(conn, store, attempt_id, *, object_format="sha1",
                       base=_SHA1_BASE, n=1):
    plan_blob = git_format.dumps_record(_plan(n))
    effect_blob = git_format.dumps_record(_effect(n))
    deps_blob = git_format.dumps_record(_deps(n))
    plan_sha = hashlib.sha256(plan_blob).hexdigest()
    effect_sha = hashlib.sha256(effect_blob).hexdigest()
    deps_sha = hashlib.sha256(deps_blob).hexdigest()
    row_digest = store._attempt_row_digest(
        _REPO, _COMPANY, _OP, attempt_id, _T2, object_format, base,
        plan_sha, effect_sha, deps_sha)
    conn.execute(
        "INSERT INTO " + _ATTEMPTS_TABLE
        + " (repo_id, company_id, operation_id, attempt_id, created_at, "
        "object_format, base_commit, plan_blob, plan_sha256, "
        "effect_blob, effect_sha256, deps_blob, deps_sha256, "
        "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_REPO, _COMPANY, _OP, attempt_id, _T2, object_format, base,
         plan_blob, plan_sha, effect_blob, effect_sha, deps_blob,
         deps_sha, row_digest))


def _assert_joint_history_rejected(store):
    # Every read that reconstructs the operation must fail closed, not
    # just the explicit integrity scan.
    with pytest.raises(ValueError, match="corrupted state history"):
        store.verify_integrity()
    with pytest.raises(ValueError, match="corrupted state history"):
        store.get_operation(_REPO, _COMPANY, _OP)
    with pytest.raises(ValueError, match="corrupted state history"):
        store.list_operation_events(_REPO, _COMPANY, _OP)
    with pytest.raises(ValueError, match="corrupted state history"):
        store.list_attempt_events(_REPO, _COMPANY, _OP, _ATT)


def test_in_flight_operation_with_prepared_attempt_is_rejected():
    # The exact draft defect from review finding 1: operation in_flight
    # committed while its attempt is still prepared.
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_in_flight_without_any_attempt_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    with pytest.raises(ValueError, match="corrupted state history"):
        store.verify_integrity()
    with pytest.raises(ValueError, match="corrupted state history"):
        store.get_operation(_REPO, _COMPANY, _OP)
    with pytest.raises(ValueError, match="corrupted state history"):
        store.list_operation_events(_REPO, _COMPANY, _OP)
    conn.rollback()


def test_in_flight_with_only_stale_attempt_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _record_attempt(conn, store)
    store.mark_attempt_stale(
        _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T3)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT, recorded_at=_T4)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_unknown_without_unknown_attempt_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    _plant_att_event(store, 2, ATTEMPT_ACCEPTING)
    _plant_op_event(store, 3, OPERATION_UNKNOWN, recorded_at=_T4)
    # Attempt stays accepting: paired subsequence lengths disagree.
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_attempt_accepting_without_in_flight_operation_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _plant_att_event(store, 2, ATTEMPT_ACCEPTING)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_accepted_pair_commit_mismatch_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    _plant_att_event(store, 2, ATTEMPT_ACCEPTING)
    _plant_att_event(
        store, 3, ATTEMPT_ACCEPTED, commit=_SHA1_C, recorded_at=_T5)
    _plant_op_event(
        store, 3, OPERATION_ACCEPTED, commit="b" * 40, recorded_at=_T5)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_accepted_operation_without_accepted_attempt_is_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared_attempt(conn, store)
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    _plant_att_event(store, 2, ATTEMPT_ACCEPTING)
    _plant_op_event(
        store, 3, OPERATION_ACCEPTED, commit=_SHA1_C, recorded_at=_T5)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_two_live_prepared_attempts_are_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _plant_attempt_row(conn, store, _ATT, n=1)
    _plant_att_event(store, 1, ATTEMPT_PREPARED, attempt_id=_ATT)
    _plant_attempt_row(conn, store, _ATT2, n=2)
    _plant_att_event(store, 1, ATTEMPT_PREPARED, attempt_id=_ATT2,
                     event_id="evt-att2-prepared-plant")
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_live_attempt_following_a_waiting_prepared_attempt_rejected():
    conn, store = _open()
    conn.execute("BEGIN")
    _seed_prepared(conn, store)
    _plant_attempt_row(conn, store, _ATT, n=1)
    _plant_att_event(store, 1, ATTEMPT_PREPARED, attempt_id=_ATT)
    _plant_attempt_row(conn, store, _ATT2, n=2)
    _plant_att_event(store, 1, ATTEMPT_PREPARED, attempt_id=_ATT2,
                     event_id="evt-att2-prepared-plant")
    _plant_att_event(store, 2, ATTEMPT_ACCEPTING, attempt_id=_ATT2,
                     event_id="evt-att2-accepting-plant")
    _plant_op_event(store, 2, OPERATION_IN_FLIGHT)
    _assert_joint_history_rejected(store)
    conn.rollback()


def test_attempt_events_before_operation_prepared_are_rejected():
    # Snapshot exists (the trigger is satisfied) but the operation has
    # no prepared event while an attempt history is planted.
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    _plant_attempt_row(conn, store, _ATT)
    _plant_att_event(store, 1, ATTEMPT_PREPARED, attempt_id=_ATT)
    with pytest.raises(ValueError, match="corrupted state history"):
        store.verify_integrity()
    conn.rollback()


def test_complex_legal_history_with_stale_attempt_and_unknown_cycle():
    conn, store = _open()
    conn.execute("BEGIN")
    _record_operation(conn, store)
    _prepare(conn, store)
    _record_attempt(conn, store)
    store.mark_attempt_stale(
        _REPO, _COMPANY, _OP, _ATT, "evt-att-stale", _T3)
    store.mark_approval_required(
        _REPO, _COMPANY, _OP, "evt-op-approval", _T3)
    store.mark_reserved(
        _REPO, _COMPANY, _OP, "evt-op-reserved", _T4)
    _record_attempt(conn, store, attempt_id=_ATT2, created_at=_T4, n=2,
                    prepared_event_id="evt-att2-prepared",
                    prepared_at=_T4)
    _admit(conn, store, attempt_id=_ATT2, recorded_at=_T5)
    _unknown(conn, store, attempt_id=_ATT2, recorded_at=_T6,
             op_event="evt-op-unknown", att_event="evt-att-unknown")
    _admit(conn, store, attempt_id=_ATT2, recorded_at=_T6,
           op_event="evt-op-readmit",
           att_event="evt-att-readmit")
    assert store.verify_integrity() is None
    operation = store.get_operation(_REPO, _COMPANY, _OP)
    assert operation["current_state"] == OPERATION_IN_FLIGHT
    assert store.get_attempt(
        _REPO, _COMPANY, _OP, _ATT)["current_state"] == ATTEMPT_STALE
    conn.rollback()
