"""Behavior tests for the F-001 candidate submission work-store primitive.

Covers only this slice's guarantees: first receipt persistence, exact
request-idempotent replay, conflict on payload reuse, company/job scope
isolation, tamper hard-failures and the caller-owned transaction contract
(SAVEPOINT-scoped writes, no COMMIT/ROLLBACK of the caller transaction,
store-owned read snapshots closed with ROLLBACK only). All tests use
in-memory SQLite; nothing touches disk, Git, models or any service.
"""
import hashlib
import json
import sqlite3

import pytest

from app.facts_tree import git_format
from app.facts_tree.candidate_work_store import (
    CandidateSubmissionStore,
    SubmissionConflict,
)


def _connect():
    return sqlite3.connect(":memory:")


def _begin(conn):
    conn.execute("BEGIN")


def _payload(note="公司2024年营收为100万元。"):
    return {
        "schema_version": "fact-candidate/1",
        "items": [{"local_id": "c1", "statement": note}],
        "complete": False,
        "remaining_source_locs": ["loc-1"],
        "empty_reason": None,
    }


def _receipt():
    return {
        "accepted": [{"local_id": "c1"}],
        "rejected": [{"local_id": "c2", "reason": "EVIDENCE_INVALID"}],
    }


def _record(store, **overrides):
    args = {
        "company_id": "co-1",
        "job_id": "job-1",
        "request_id": "req-1",
        "submission_id": "sub-1",
        "received_at": "2026-10-23T10:00:00+08:00",
        "payload": _payload(),
        "receipt": _receipt(),
    }
    args.update(overrides)
    return store.record_submission(**args)


_APPEND_ONLY_TRIGGERS = (
    "fact_candidate_submissions_reject_update",
    "fact_candidate_submissions_reject_delete",
    "fact_candidate_submissions_reject_replace",
)


def _drop_append_only_guards(conn):
    # Simulates out-of-band damage: a direct database writer bypassing
    # the append-only triggers (e.g. a malicious DB administrator
    # rewriting rows by hand). The tamper tests below drop the guards so
    # they can perform the raw UPDATE, then verify that read-side
    # validation still fails closed on the damaged row.
    for name in _APPEND_ONLY_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)


def test_first_receipt_persists_snapshot_and_receipt():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    result = _record(store)
    assert result["replayed"] is False
    assert result["receipt"] == _receipt()
    assert result["payload"] == _payload()
    assert result["submission_id"] == "sub-1"
    conn.commit()
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1


def test_payload_fields_never_set_stored_identity():
    # A payload carrying identity-looking keys must not influence the
    # stored host envelope identity.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    sneaky = _payload()
    sneaky["company_id"] = "co-evil"
    sneaky["request_id"] = "req-evil"
    _record(store, payload=sneaky)
    conn.commit()
    assert store.get_submission("co-evil", "job-1", "req-evil") is None
    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["company_id"] == "co-1"
    assert stored["request_id"] == "req-1"
    assert stored["payload"]["company_id"] == "co-evil"


def test_exact_replay_returns_original_receipt_without_new_rows():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    # A retry with the same payload but different host-issued
    # submission_id/received_at must return the original durable values.
    second = _record(store, submission_id="sub-retry",
                     received_at="2026-10-23T11:00:00+08:00")
    conn.commit()
    assert second["replayed"] is True
    assert second["submission_id"] == "sub-1"
    assert second["received_at"] == "2026-10-23T10:00:00+08:00"
    assert second["receipt"] == first["receipt"]
    assert second["receipt_sha256"] == first["receipt_sha256"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1


def test_conflicting_request_reuse_fails_closed_and_keeps_rows():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    before = conn.execute(
        "SELECT * FROM fact_candidate_submissions").fetchall()
    with pytest.raises(SubmissionConflict):
        _record(store, payload=_payload(note="完全不同的陈述。"))
    after = conn.execute(
        "SELECT * FROM fact_candidate_submissions").fetchall()
    assert before == after
    assert conn.in_transaction  # caller transaction still open
    conn.commit()


def test_scope_isolation_for_lookup_and_idempotency():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    # Same request_id under another company or job is a different scope.
    assert store.get_submission("co-2", "job-1", "req-1") is None
    assert store.get_submission("co-1", "job-2", "req-1") is None
    _begin(conn)
    other = _record(store, company_id="co-2")
    conn.commit()
    assert other["replayed"] is False
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 2


def test_tampered_blob_or_digest_fails_closed():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    _drop_append_only_guards(conn)  # simulate out-of-band damage
    conn.execute(
        "UPDATE fact_candidate_submissions SET payload_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_submission("co-1", "job-1", "req-1")
    # Repair the digest but corrupt the blob past canonical re-validation:
    # a matching digest over non-canonical bytes must still hard-fail.
    broken = b'"not-an-object"'
    conn.execute(
        "UPDATE fact_candidate_submissions SET payload_blob=?, "
        "payload_sha256=? WHERE company_id='co-1'",
        (broken, hashlib.sha256(broken).hexdigest()))
    conn.commit()
    with pytest.raises(ValueError):
        store.get_submission("co-1", "job-1", "req-1")


def test_store_owned_read_snapshot_never_emits_commit():
    # On an idle connection get_submission opens its own read snapshot;
    # the trace callback must never see a COMMIT on that path.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        stored = store.get_submission("co-1", "job-1", "req-1")
        assert stored["receipt"] == _receipt()
        missing = store.get_submission("co-1", "job-1", "req-missing")
        assert missing is None
    finally:
        conn.set_trace_callback(None)
    commits = [s for s in statements
               if s.lstrip().upper().startswith("COMMIT")]
    assert commits == []
    assert not conn.in_transaction  # snapshot fully closed afterwards


def test_read_inside_caller_transaction_leaves_it_open():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        stored = store.get_submission("co-1", "job-1", "req-1")
    finally:
        conn.set_trace_callback(None)
    assert stored["submission_id"] == "sub-1"
    assert conn.in_transaction  # caller transaction untouched
    forbidden = [s for s in statements
                 if s.lstrip().upper().startswith(("COMMIT", "ROLLBACK"))]
    assert forbidden == []
    conn.commit()
    assert store.get_submission("co-1", "job-1", "req-1") is not None


def test_noncanonical_blob_with_matching_digest_fails_closed():
    # A blob that parses fine and whose stored digest was updated to
    # match, but whose bytes are not the exact canonical serialization,
    # must still hard-fail on read.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    noncanonical = json.dumps(_payload(), sort_keys=True).encode("utf-8")
    assert noncanonical != git_format.dumps_record(_payload())
    _drop_append_only_guards(conn)  # simulate out-of-band damage
    conn.execute(
        "UPDATE fact_candidate_submissions SET payload_blob=?, "
        "payload_sha256=? WHERE company_id='co-1'",
        (noncanonical, hashlib.sha256(noncanonical).hexdigest()))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_submission("co-1", "job-1", "req-1")


def test_receipt_digest_tampering_fails_closed():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    _drop_append_only_guards(conn)  # simulate out-of-band damage
    conn.execute(
        "UPDATE fact_candidate_submissions SET receipt_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_submission("co-1", "job-1", "req-1")
    # Same for a non-canonical receipt blob with a re-matched digest.
    noncanonical = json.dumps(_receipt(), sort_keys=True).encode("utf-8")
    conn.execute(
        "UPDATE fact_candidate_submissions SET receipt_blob=?, "
        "receipt_sha256=? WHERE company_id='co-1'",
        (noncanonical, hashlib.sha256(noncanonical).hexdigest()))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_submission("co-1", "job-1", "req-1")


def test_caller_transaction_stays_open_and_can_roll_back():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    assert conn.in_transaction
    conn.rollback()
    assert store.get_submission("co-1", "job-1", "req-1") is None
    # Failure path likewise leaves the caller transaction open.
    _begin(conn)
    _record(store)
    with pytest.raises(SubmissionConflict):
        _record(store, payload=_payload(note="别的东西。"))
    assert conn.in_transaction
    conn.rollback()
    assert store.get_submission("co-1", "job-1", "req-1") is None


def test_write_outside_caller_transaction_fails_before_mutation():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    with pytest.raises(ValueError, match="caller transaction"):
        _record(store)
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 0


def test_constructor_rejects_active_caller_transaction():
    conn = _connect()
    _begin(conn)
    with pytest.raises(ValueError, match="idle connection"):
        CandidateSubmissionStore(conn)
    conn.rollback()


def test_mutated_identity_columns_fail_closed_even_under_changed_scope():
    # Row-integrity digest: changing company_id, submission_id or
    # received_at after insert must fail closed on read, even when the
    # lookup itself uses the mutated scope/key values.
    for column, mutated, scope in (
            ("company_id", "co-mutated", ("co-mutated", "job-1", "req-1")),
            ("submission_id", "sub-mutated", ("co-1", "job-1", "req-1")),
            ("received_at", "2026-10-23T12:00:00+08:00",
             ("co-1", "job-1", "req-1"))):
        conn = _connect()
        store = CandidateSubmissionStore(conn)
        _begin(conn)
        _record(store)
        conn.commit()
        _drop_append_only_guards(conn)  # simulate out-of-band damage
        conn.execute(
            "UPDATE fact_candidate_submissions SET %s=? "
            "WHERE company_id='co-1'" % column, (mutated,))
        conn.commit()
        with pytest.raises(ValueError, match="row-integrity digest"):
            store.get_submission(*scope)
        # Replay through the write path must fail closed the same way.
        if column == "company_id":
            continue  # key no longer matches; write path sees no row
        _begin(conn)
        with pytest.raises(ValueError, match="row-integrity digest"):
            _record(store)
        conn.rollback()


def test_mutated_row_digest_column_fails_closed():
    # Tampering with the stored digest value itself also fails closed.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    _record(store)
    conn.commit()
    _drop_append_only_guards(conn)  # simulate out-of-band damage
    conn.execute(
        "UPDATE fact_candidate_submissions SET row_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="row-integrity digest"):
        store.get_submission("co-1", "job-1", "req-1")


def test_identity_values_must_be_host_shaped():
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    with pytest.raises(ValueError):
        _record(store, company_id="")
    with pytest.raises(ValueError):
        _record(store, job_id=123)
    with pytest.raises(ValueError):
        _record(store, received_at="")
    with pytest.raises(TypeError):
        _record(store, payload=["not-an-object"])
    conn.rollback()


def test_database_level_update_is_rejected_and_receipt_unchanged():
    # The append-only guard must reject any UPDATE at the database level,
    # leaving the original durable receipt byte-identical.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE fact_candidate_submissions SET receipt_blob=? "
            "WHERE company_id='co-1'", (b"{}",))
    conn.rollback()
    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["receipt"] == first["receipt"]
    assert stored["receipt_sha256"] == first["receipt_sha256"]
    assert stored["submission_id"] == "sub-1"
    assert stored["received_at"] == "2026-10-23T10:00:00+08:00"


def test_database_level_delete_is_rejected_and_receipt_unchanged():
    # The append-only guard must reject any DELETE at the database level.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM fact_candidate_submissions "
            "WHERE company_id='co-1'")
    conn.rollback()
    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["receipt"] == first["receipt"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1


def test_insert_or_replace_on_recorded_key_is_rejected():
    # INSERT OR REPLACE on an already-recorded request identity would
    # otherwise delete the original receipt row and insert a replacement
    # without firing the DELETE trigger; the BEFORE INSERT guard must
    # reject it and leave the original durable receipt untouched.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    conn.commit()
    replacement_payload = git_format.dumps_record(
        _payload(note="试图覆写原回执。"))
    replacement_receipt = git_format.dumps_record({"accepted": []})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO fact_candidate_submissions "
            "(company_id, job_id, request_id, submission_id, received_at, "
            "payload_blob, payload_sha256, receipt_blob, receipt_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("co-1", "job-1", "req-1", "sub-evil",
             "2026-10-24T00:00:00+08:00",
             replacement_payload,
             hashlib.sha256(replacement_payload).hexdigest(),
             replacement_receipt,
             hashlib.sha256(replacement_receipt).hexdigest(),
             "deadbeef"))
    conn.rollback()
    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["submission_id"] == first["submission_id"]
    assert stored["received_at"] == first["received_at"]
    assert stored["payload"] == first["payload"]
    assert stored["receipt"] == first["receipt"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1


def test_existing_database_installs_guards_without_touching_history(tmp_path):
    # Regression for the existing-table initialization path: a database
    # whose rows predate the append-only triggers must get the guards on
    # the next construction without any change to the stored history.
    db_path = tmp_path / "submissions.db"
    conn = sqlite3.connect(str(db_path))
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    conn.commit()
    conn.close()

    # Simulate the prior schema: the populated table exists but none of
    # the three append-only guards do.
    conn = sqlite3.connect(str(db_path))
    for name in _APPEND_ONLY_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.commit()
    triggers = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'")
    assert triggers.fetchone()[0] == 0
    conn.close()

    # Reopening the same file and reconstructing the store must install
    # the guards and leave the recorded row readable and unchanged.
    conn = sqlite3.connect(str(db_path))
    store = CandidateSubmissionStore(conn)
    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["submission_id"] == first["submission_id"]
    assert stored["received_at"] == first["received_at"]
    assert stored["payload"] == first["payload"]
    assert stored["payload_sha256"] == first["payload_sha256"]
    assert stored["receipt"] == first["receipt"]
    assert stored["receipt_sha256"] == first["receipt_sha256"]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE fact_candidate_submissions SET receipt_blob=? "
            "WHERE company_id='co-1'", (b"{}",))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM fact_candidate_submissions "
            "WHERE company_id='co-1'")
    conn.rollback()
    replacement_payload = git_format.dumps_record(
        _payload(note="试图覆写原回执。"))
    replacement_receipt = git_format.dumps_record({"accepted": []})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO fact_candidate_submissions "
            "(company_id, job_id, request_id, submission_id, received_at, "
            "payload_blob, payload_sha256, receipt_blob, receipt_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("co-1", "job-1", "req-1", "sub-evil",
             "2026-10-24T00:00:00+08:00",
             replacement_payload,
             hashlib.sha256(replacement_payload).hexdigest(),
             replacement_receipt,
             hashlib.sha256(replacement_receipt).hexdigest(),
             "deadbeef"))
    conn.rollback()

    stored = store.get_submission("co-1", "job-1", "req-1")
    assert stored["receipt"] == first["receipt"]
    assert stored["receipt_sha256"] == first["receipt_sha256"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1
    conn.close()


def test_first_insert_and_exact_replay_still_work_with_guards():
    # The guards must not interfere with the normal path: first receipt
    # on a fresh key and exact request replay both stay valid.
    conn = _connect()
    store = CandidateSubmissionStore(conn)
    _begin(conn)
    first = _record(store)
    second = _record(store)
    conn.commit()
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["receipt"] == first["receipt"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM fact_candidate_submissions").fetchone()
    assert rows[0] == 1
