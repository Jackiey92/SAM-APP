"""Behavior tests for the F-001 candidate revision CAS work-store primitive.

Covers only this slice's guarantees: initial revision creation,
append-only history, expected_revision compare-and-swap, exact
request-idempotent replay for create and revise, conflict on request
reuse with different input, scope isolation, tamper hard-failures,
database-level append-only guards (BEFORE UPDATE/DELETE rejection and
conflicting INSERT OR REPLACE rejection) and the caller-owned
transaction contract (SAVEPOINT-scoped writes, no
COMMIT/ROLLBACK of the caller transaction, store-owned read snapshots
closed with ROLLBACK only). All tests use in-memory SQLite; nothing
touches disk, Git, models or any service.
"""
import copy
import hashlib
import json
import sqlite3

import pytest

from app.facts_tree import git_format
from app.facts_tree.candidate_revision_store import (
    CandidateExists,
    CandidateRevisionStore,
    RevisionConflict,
    StaleCandidate,
    UnknownCandidate,
)


def _connect():
    return sqlite3.connect(":memory:")


def _begin(conn):
    conn.execute("BEGIN")


def _payload(note="公司2024年营收为100万元。"):
    return {
        "schema_version": "fact-candidate/1",
        "local_id": "c1",
        "statement": note,
        "evidence": [{"read_grant_id": "g1", "quote": note}],
    }


def _create(store, **overrides):
    args = {
        "company_id": "co-1",
        "job_id": "job-1",
        "candidate_uid": "cand-1",
        "request_id": "req-1",
        "recorded_at": "2026-10-23T10:00:00+08:00",
        "payload": _payload(),
    }
    args.update(overrides)
    return store.create_candidate(**args)


def _revise(store, **overrides):
    args = {
        "company_id": "co-1",
        "job_id": "job-1",
        "candidate_uid": "cand-1",
        "request_id": "req-2",
        "expected_revision": 1,
        "recorded_at": "2026-10-23T10:05:00+08:00",
        "payload": _payload(note="修订后的完整陈述。"),
    }
    args.update(overrides)
    return store.revise_candidate(**args)


def _count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_revisions").fetchone()[0]


_APPEND_ONLY_TRIGGERS = (
    "fact_candidate_revisions_no_update",
    "fact_candidate_revisions_no_delete",
    "fact_candidate_revisions_no_replace",
)


def _drop_append_only_guards(conn):
    # Simulate out-of-band damage: a database operator or attacker with
    # direct SQLite access removes the append-only guards before
    # mutating stored rows. The read side must still fail closed on the
    # drift afterwards.
    for name in _APPEND_ONLY_TRIGGERS:
        conn.execute("DROP TRIGGER " + name)


def test_create_returns_revision_one_and_persists():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    result = _create(store)
    assert result["replayed"] is False
    assert result["revision"] == 1
    assert result["payload"] == _payload()
    assert result["candidate_uid"] == "cand-1"
    conn.commit()
    assert _count(conn) == 1
    current = store.get_current("co-1", "job-1", "cand-1")
    assert current["revision"] == 1
    assert current["payload_sha256"] == result["payload_sha256"]
    assert current["replayed"] is False


def test_payload_fields_never_set_stored_identity():
    # A payload carrying identity-looking keys must not influence the
    # stored host-owned identity.
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    sneaky = _payload()
    sneaky["company_id"] = "co-evil"
    sneaky["candidate_uid"] = "cand-evil"
    _create(store, payload=sneaky)
    conn.commit()
    assert store.get_current("co-evil", "job-1", "cand-evil") is None
    stored = store.get_current("co-1", "job-1", "cand-1")
    assert stored["company_id"] == "co-1"
    assert stored["candidate_uid"] == "cand-1"
    assert stored["payload"]["company_id"] == "co-evil"


def test_create_replay_returns_original_revision_without_new_rows():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    # A retry with the same payload but a different host-issued
    # recorded_at must return the original durable values.
    second = _create(store, recorded_at="2026-10-23T11:00:00+08:00")
    conn.commit()
    assert second["replayed"] is True
    assert second["revision"] == 1
    assert second["recorded_at"] == "2026-10-23T10:00:00+08:00"
    assert second["payload_sha256"] == first["payload_sha256"]
    assert _count(conn) == 1


def test_create_same_request_with_different_input_conflicts():
    for overrides in (
            {"candidate_uid": "cand-2"},
            {"payload": _payload(note="完全不同的陈述。")}):
        conn = _connect()
        store = CandidateRevisionStore(conn)
        _begin(conn)
        _create(store)
        with pytest.raises(RevisionConflict):
            _create(store, **overrides)
        assert _count(conn) == 1
        assert conn.in_transaction  # caller transaction still open
        conn.rollback()


def test_create_existing_candidate_under_new_request_fails_closed():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    with pytest.raises(CandidateExists):
        _create(store, request_id="req-new")
    assert _count(conn) == 1
    assert conn.in_transaction
    conn.rollback()
    assert store.get_current("co-1", "job-1", "cand-1") is None


def test_revise_appends_new_revision_and_keeps_history():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    second = _revise(store)
    assert second["replayed"] is False
    assert second["revision"] == 2
    assert second["payload_sha256"] != first["payload_sha256"]
    conn.commit()
    assert _count(conn) == 2
    current = store.get_current("co-1", "job-1", "cand-1")
    assert current["revision"] == 2
    assert current["payload"] == second["payload"]
    # The old revision is preserved byte-for-byte and stays readable.
    old = store.get_revision("co-1", "job-1", "cand-1", 1)
    assert old["payload"] == first["payload"]
    assert old["recorded_at"] == first["recorded_at"]
    assert old["payload_sha256"] == first["payload_sha256"]
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"] == first["payload"]
    assert store.get_revision("co-1", "job-1", "cand-1", 3) is None


def test_revise_replay_returns_original_even_after_newer_revisions():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    second = _revise(store)
    third = _revise(store, request_id="req-3", expected_revision=2,
                   recorded_at="2026-10-23T10:10:00+08:00",
                   payload=_payload(note="第二次修订。"))
    assert third["revision"] == 3
    # Replaying the earlier request must return its original revision 2
    # even though expected_revision=1 is now stale.
    replay = _revise(store, recorded_at="2026-10-23T12:00:00+08:00")
    conn.commit()
    assert replay["replayed"] is True
    assert replay["revision"] == 2
    assert replay["recorded_at"] == second["recorded_at"]
    assert replay["payload_sha256"] == second["payload_sha256"]
    assert _count(conn) == 3
    assert store.get_current("co-1", "job-1", "cand-1")["revision"] == 3


def test_revise_same_request_with_different_input_conflicts():
    for overrides in (
            {"payload": _payload(note="换了一份内容。")},
            {"candidate_uid": "cand-2"},
            {"expected_revision": 7}):
        conn = _connect()
        store = CandidateRevisionStore(conn)
        _begin(conn)
        _create(store)
        _revise(store)
        with pytest.raises(RevisionConflict):
            _revise(store, **overrides)
        assert _count(conn) == 2
        assert conn.in_transaction
        conn.rollback()


def test_stale_expected_revision_fails_closed_and_keeps_history():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    _revise(store)
    # Behind the current revision and ahead of it both fail closed.
    with pytest.raises(StaleCandidate):
        _revise(store, request_id="req-stale", expected_revision=1,
                payload=_payload(note="过期基线上的修订。"))
    with pytest.raises(StaleCandidate):
        _revise(store, request_id="req-ahead", expected_revision=5,
                payload=_payload(note="跳到未来的修订。"))
    assert conn.in_transaction
    conn.commit()
    assert _count(conn) == 2
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"] == first["payload"]


def test_revise_unknown_candidate_fails_closed():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    with pytest.raises(UnknownCandidate):
        _revise(store)
    assert _count(conn) == 0
    assert conn.in_transaction
    conn.rollback()


def test_scope_isolation_for_candidates_and_requests():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    conn.commit()
    # Same candidate_uid under another company or job is invisible here.
    assert store.get_current("co-2", "job-1", "cand-1") is None
    assert store.get_current("co-1", "job-2", "cand-1") is None
    assert store.list_revisions("co-2", "job-1", "cand-1") == []
    _begin(conn)
    with pytest.raises(UnknownCandidate):
        _revise(store, job_id="job-2")
    # The same request_id in another scope is an independent operation.
    other = _create(store, company_id="co-2")
    conn.commit()
    assert other["replayed"] is False
    assert _count(conn) == 2


def test_bool_and_non_int_revisions_rejected():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    with pytest.raises(TypeError):
        _revise(store, expected_revision=True)
    with pytest.raises(TypeError):
        _revise(store, expected_revision="1")
    with pytest.raises(ValueError):
        _revise(store, expected_revision=0)
    with pytest.raises(TypeError):
        store.get_revision("co-1", "job-1", "cand-1", True)
    assert _count(conn) == 1
    conn.rollback()


def test_identity_values_and_payload_shape_enforced():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    with pytest.raises(ValueError):
        _create(store, company_id="")
    with pytest.raises(ValueError):
        _create(store, candidate_uid=123)
    with pytest.raises(ValueError):
        _create(store, request_id="")
    with pytest.raises(ValueError):
        _create(store, recorded_at="")
    with pytest.raises(TypeError):
        _create(store, payload=["not-an-object"])
    assert _count(conn) == 0
    conn.rollback()


def test_constructor_rejects_active_transaction_and_non_connection():
    conn = _connect()
    _begin(conn)
    with pytest.raises(ValueError, match="idle connection"):
        CandidateRevisionStore(conn)
    conn.rollback()
    with pytest.raises(TypeError):
        CandidateRevisionStore("not-a-connection")


def test_writes_require_caller_transaction():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    with pytest.raises(ValueError, match="caller transaction"):
        _create(store)
    _begin(conn)
    _create(store)
    conn.commit()
    with pytest.raises(ValueError, match="caller transaction"):
        _revise(store)
    assert _count(conn) == 1


def test_caller_transaction_stays_open_and_rolls_back():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    _revise(store)
    assert conn.in_transaction
    conn.rollback()
    assert store.get_current("co-1", "job-1", "cand-1") is None
    assert store.list_revisions("co-1", "job-1", "cand-1") == []
    # Failure path likewise leaves the caller transaction open.
    _begin(conn)
    _create(store)
    with pytest.raises(StaleCandidate):
        _revise(store, request_id="req-9", expected_revision=9)
    assert conn.in_transaction
    conn.rollback()
    assert store.get_current("co-1", "job-1", "cand-1") is None


def test_failed_insert_rolls_back_to_savepoint_without_partial_rows():
    # Force the revision-2 INSERT itself to fail mid-write; the store
    # must roll back only to its savepoint, leave revision 1 intact and
    # keep the caller transaction open.
    conn = _connect()
    store = CandidateRevisionStore(conn)
    conn.execute(
        "CREATE TRIGGER fail_second_revision "
        "BEFORE INSERT ON fact_candidate_revisions WHEN NEW.revision = 2 "
        "BEGIN SELECT RAISE(ABORT, 'forced revision-2 failure'); END;")
    _begin(conn)
    _create(store)
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        _revise(store)
    assert conn.in_transaction
    assert _count(conn) == 1
    conn.commit()
    conn.execute("DROP TRIGGER fail_second_revision")
    _begin(conn)
    second = _revise(store)
    conn.commit()
    assert second["revision"] == 2
    assert _count(conn) == 2


def test_reads_on_idle_connection_never_emit_commit():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    _revise(store)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        current = store.get_current("co-1", "job-1", "cand-1")
        assert current["revision"] == 2
        old = store.get_revision("co-1", "job-1", "cand-1", 1)
        assert old["revision"] == 1
        history = store.list_revisions("co-1", "job-1", "cand-1")
        assert len(history) == 2
        missing = store.get_current("co-1", "job-1", "cand-missing")
        assert missing is None
    finally:
        conn.set_trace_callback(None)
    commits = [s for s in statements
               if s.lstrip().upper().startswith("COMMIT")]
    assert commits == []
    assert not conn.in_transaction  # snapshots fully closed afterwards


def test_reads_inside_caller_transaction_leave_it_open():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    _revise(store)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        current = store.get_current("co-1", "job-1", "cand-1")
        history = store.list_revisions("co-1", "job-1", "cand-1")
    finally:
        conn.set_trace_callback(None)
    assert current["revision"] == 2
    assert len(history) == 2
    assert conn.in_transaction  # caller transaction untouched
    forbidden = [s for s in statements
                 if s.lstrip().upper().startswith(("COMMIT", "ROLLBACK"))]
    assert forbidden == []
    conn.commit()
    assert store.get_current("co-1", "job-1", "cand-1") is not None


def test_tampered_payload_digest_fails_closed():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    conn.commit()
    # Guards are removed to simulate out-of-band damage by direct DB
    # access; the read side must still fail closed on the drift.
    _drop_append_only_guards(conn)
    conn.execute(
        "UPDATE fact_candidate_revisions SET payload_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_current("co-1", "job-1", "cand-1")


def test_noncanonical_blob_with_matching_digest_fails_closed():
    # A blob that parses fine and whose stored digest was updated to
    # match, but whose bytes are not the exact canonical serialization,
    # must still hard-fail on read.
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    conn.commit()
    # Guards are removed to simulate out-of-band damage by direct DB
    # access; the read side must still fail closed on the drift.
    _drop_append_only_guards(conn)
    noncanonical = json.dumps(_payload(), sort_keys=True).encode("utf-8")
    assert noncanonical != git_format.dumps_record(_payload())
    conn.execute(
        "UPDATE fact_candidate_revisions SET payload_blob=?, "
        "payload_sha256=? WHERE company_id='co-1'",
        (noncanonical, hashlib.sha256(noncanonical).hexdigest()))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_current("co-1", "job-1", "cand-1")


def test_mutated_identity_columns_fail_closed_even_under_changed_scope():
    # Row-integrity digest: changing candidate_uid, request_id,
    # recorded_at or revision after insert must fail closed on read,
    # even when the lookup itself uses the mutated values.
    def mutate_revision(conn):
        conn.execute(
            "UPDATE fact_candidate_revisions SET revision=7 "
            "WHERE company_id='co-1'")

    cases = (
        ("candidate_uid", "cand-mutated",
         ("co-1", "job-1", "cand-mutated"), None),
        ("request_id", "req-mutated", ("co-1", "job-1", "cand-1"), None),
        ("recorded_at", "2026-10-23T12:00:00+08:00",
         ("co-1", "job-1", "cand-1"), None),
        (None, None, ("co-1", "job-1", "cand-1"), mutate_revision),
    )
    for column, mutated, scope, custom in cases:
        conn = _connect()
        store = CandidateRevisionStore(conn)
        _begin(conn)
        _create(store)
        conn.commit()
        # Guards are removed to simulate out-of-band damage by direct DB
        # access; the read side must still fail closed on the drift.
        _drop_append_only_guards(conn)
        if custom is not None:
            custom(conn)
        else:
            conn.execute(
                "UPDATE fact_candidate_revisions SET %s=? "
                "WHERE company_id='co-1'" % column, (mutated,))
        conn.commit()
        with pytest.raises(ValueError, match="row-integrity digest"):
            store.get_current(*scope)
        # Replay through the write path must fail closed the same way
        # when the request identity still resolves to the mutated row.
        if column != "request_id":
            continue
        _begin(conn)
        with pytest.raises(ValueError, match="row-integrity digest"):
            _create(store, request_id="req-mutated")
        conn.rollback()


def test_mutated_row_digest_column_fails_closed():
    # Tampering with the stored digest value itself also fails closed.
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    _create(store)
    conn.commit()
    # Guards are removed to simulate out-of-band damage by direct DB
    # access; the read side must still fail closed on the drift.
    _drop_append_only_guards(conn)
    conn.execute(
        "UPDATE fact_candidate_revisions SET row_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="row-integrity digest"):
        store.get_current("co-1", "job-1", "cand-1")


def test_input_payloads_are_not_mutated():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    first_payload = _payload()
    first_snapshot = copy.deepcopy(first_payload)
    second_payload = _payload(note="修订后的完整陈述。")
    second_snapshot = copy.deepcopy(second_payload)
    _begin(conn)
    _create(store, payload=first_payload)
    _revise(store, payload=second_payload)
    conn.commit()
    assert first_payload == first_snapshot
    assert second_payload == second_snapshot


_REPLACE_SQL = (
    "INSERT OR REPLACE INTO fact_candidate_revisions "
    "(company_id, job_id, candidate_uid, revision, request_id, "
    "recorded_at, payload_blob, payload_sha256, row_sha256) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _bypass_row(candidate_uid, revision, request_id):
    # A syntactically complete row written by SQL that bypasses the
    # store; the append-only guard must reject it before SQLite can
    # delete the conflicting old row.
    blob = git_format.dumps_record(_payload(note="绕过工作库的覆写内容。"))
    return ("co-1", "job-1", candidate_uid, revision, request_id,
            "2026-10-24T00:00:00+08:00", blob,
            hashlib.sha256(blob).hexdigest(), "f" * 64)


def test_update_of_stored_revision_rejected_and_history_preserved():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    second = _revise(store)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE fact_candidate_revisions SET payload_sha256='deadbeef' "
            "WHERE company_id='co-1' AND revision=1")
    conn.rollback()  # close the transaction left open by the failed write
    assert _count(conn) == 2
    old = store.get_revision("co-1", "job-1", "cand-1", 1)
    assert old["payload_sha256"] == first["payload_sha256"]
    assert old["payload"] == first["payload"]
    current = store.get_current("co-1", "job-1", "cand-1")
    assert current["revision"] == 2
    assert current["payload_sha256"] == second["payload_sha256"]


def test_delete_of_stored_revision_rejected_and_history_preserved():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    second = _revise(store)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM fact_candidate_revisions "
            "WHERE company_id='co-1' AND revision=1")
    conn.rollback()
    assert _count(conn) == 2
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"] == first["payload"]
    assert history[1]["payload_sha256"] == second["payload_sha256"]
    assert store.get_current("co-1", "job-1", "cand-1")["revision"] == 2


def test_insert_or_replace_on_existing_primary_key_cannot_overwrite():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    conn.commit()
    # Same primary key (candidate identity + revision), different
    # request id: without the guard REPLACE would delete revision 1.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(_REPLACE_SQL, _bypass_row("cand-1", 1, "req-evil"))
    conn.rollback()
    assert _count(conn) == 1
    stored = store.get_revision("co-1", "job-1", "cand-1", 1)
    assert stored["request_id"] == "req-1"
    assert stored["payload_sha256"] == first["payload_sha256"]
    assert stored["payload"] == first["payload"]


def test_insert_or_replace_on_existing_request_key_cannot_overwrite():
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    conn.commit()
    # Same (company_id, job_id, request_id) unique key but a different
    # candidate identity: without the guard REPLACE would delete the
    # original revision 1 row.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(_REPLACE_SQL, _bypass_row("cand-other", 1, "req-1"))
    conn.rollback()
    assert _count(conn) == 1
    stored = store.get_revision("co-1", "job-1", "cand-1", 1)
    assert stored["payload_sha256"] == first["payload_sha256"]
    assert stored["payload"] == first["payload"]
    assert store.get_current("co-1", "job-1", "cand-other") is None


def test_existing_database_installs_guards_without_touching_history(tmp_path):
    # Regression for the existing-table initialization path: a database
    # whose rows predate the append-only triggers must get the guards on
    # the next construction without any change to the stored history.
    db_path = tmp_path / "revisions.db"
    conn = sqlite3.connect(str(db_path))
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    second = _revise(store)
    conn.commit()
    conn.close()

    # Simulate the prior schema: the populated table exists but none of
    # the three append-only guards do.
    conn = sqlite3.connect(str(db_path))
    for name in _APPEND_ONLY_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS " + name)
    conn.commit()
    triggers = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'")
    assert triggers.fetchone()[0] == 0
    conn.close()

    # Reopening the same file and reconstructing the store must install
    # the guards and leave both revisions readable and unchanged.
    conn = sqlite3.connect(str(db_path))
    store = CandidateRevisionStore(conn)
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"] == first["payload"]
    assert history[0]["payload_sha256"] == first["payload_sha256"]
    assert history[0]["recorded_at"] == first["recorded_at"]
    assert history[1]["payload_sha256"] == second["payload_sha256"]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE fact_candidate_revisions SET payload_sha256='deadbeef' "
            "WHERE company_id='co-1' AND revision=1")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM fact_candidate_revisions "
            "WHERE company_id='co-1' AND revision=1")
    conn.rollback()
    # Conflicting primary key and conflicting request key both stay
    # unable to overwrite the stored history.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(_REPLACE_SQL, _bypass_row("cand-1", 1, "req-evil"))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(_REPLACE_SQL, _bypass_row("cand-other", 1, "req-1"))
    conn.rollback()

    assert _count(conn) == 2
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"] == first["payload"]
    assert history[1]["payload"] == second["payload"]
    assert store.get_current("co-1", "job-1", "cand-1")["revision"] == 2
    conn.close()


def test_normal_flow_still_works_with_guards_in_place():
    # Guards must not interfere with normal create / CAS revise / exact
    # replay / later revisions appended through the store.
    conn = _connect()
    store = CandidateRevisionStore(conn)
    _begin(conn)
    first = _create(store)
    replay = _create(store, recorded_at="2026-10-23T11:00:00+08:00")
    second = _revise(store)
    third = _revise(store, request_id="req-3", expected_revision=2,
                   recorded_at="2026-10-23T10:10:00+08:00",
                   payload=_payload(note="第三次修订。"))
    replay_second = _revise(store, recorded_at="2026-10-23T12:00:00+08:00")
    conn.commit()
    assert replay["replayed"] is True
    assert replay["revision"] == 1
    assert replay_second["replayed"] is True
    assert replay_second["revision"] == 2
    assert third["revision"] == 3
    assert _count(conn) == 3
    history = store.list_revisions("co-1", "job-1", "cand-1")
    assert [row["revision"] for row in history] == [1, 2, 3]
    assert history[0]["payload"] == first["payload"]
    assert history[1]["payload_sha256"] == second["payload_sha256"]
    assert store.get_current("co-1", "job-1", "cand-1")["revision"] == 3
