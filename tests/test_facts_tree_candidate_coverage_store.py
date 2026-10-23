"""Behavior tests for the F-001 candidate coverage manifest store.

Covers only this slice's guarantees: first persistence, exact
content-addressed replay, conflict on digest-to-different-bytes reuse,
coexistence of multiple digests under one scope, company/job scope
isolation, strict build-result input validation, tamper hard-failures,
database-level immutability guards (UPDATE / DELETE / conflicting-key
INSERT rejected, original snapshot untouched) and the caller-owned
transaction contract (SAVEPOINT-scoped writes, no
COMMIT/ROLLBACK of the caller transaction, store-owned read snapshots
closed with ROLLBACK only). All tests use in-memory SQLite; nothing
touches disk, Git, models or any service.
"""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.facts_tree import git_format
from app.facts_tree.candidate_coverage import build_coverage_manifest
from app.facts_tree.candidate_coverage_store import (
    CandidateCoverageManifestStore,
    CoverageManifestConflict,
)

_TABLE = "fact_candidate_coverage_manifests"

_GUARD_TRIGGERS = (
    "fact_candidate_coverage_manifests_no_update",
    "fact_candidate_coverage_manifests_no_delete",
    "fact_candidate_coverage_manifests_no_replace",
)


def _drop_immutability_guards(conn):
    """Remove the database-level immutability guards explicitly.

    Tests below issue direct UPDATE statements to simulate out-of-band
    damage (a database administrator bypassing the store API); the guards
    must be dropped first to stage that damage. The read-side fail-closed
    assertions afterwards are unchanged.
    """
    for trigger in _GUARD_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS " + trigger)


def _connect():
    return sqlite3.connect(":memory:")


def _begin(conn):
    conn.execute("BEGIN")


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM " + _TABLE).fetchone()[0]


def _windows():
    return [
        {"window_id": "w1", "source_loc": "loc-1", "file_hash": "fh-1",
         "mapped_fingerprint": "mf-1"},
        {"window_id": "w2", "source_loc": "loc-2", "file_hash": "fh-1",
         "mapped_fingerprint": "mf-1"},
    ]


def _window_records(revisions=None):
    if revisions is None:
        revisions = [{"candidate_uid": "cand-1", "revision": 1}]
    records = []
    for window in _windows():
        records.append({
            "window_id": window["window_id"],
            "source_loc": window["source_loc"],
            "file_hash": window["file_hash"],
            "mapped_fingerprint": window["mapped_fingerprint"],
            "status": "reviewed",
            "candidate_revisions": list(revisions),
            "pending_rejections": [],
            "exclusions": [],
            "unresolved": [],
        })
    return records


def _manifest(**kwargs):
    """A coverage-complete manifest build result."""
    return build_coverage_manifest(_windows(), _window_records(**kwargs))


def _gapped_manifest():
    """A manifest with one window record missing: coverage has a gap."""
    return build_coverage_manifest(_windows(), _window_records()[:1])


def _record(store, manifest=None, **overrides):
    args = {
        "company_id": "co-1",
        "job_id": "job-1",
        "recorded_at": "2026-10-23T10:00:00+08:00",
        "manifest": manifest if manifest is not None else _manifest(),
    }
    args.update(overrides)
    return store.record_manifest(**args)


def test_first_persist_stores_snapshot():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    result = _record(store, manifest)
    assert result["replayed"] is False
    assert result["manifest_digest"] == manifest["digest"]
    assert result["manifest_sha256"] == manifest["digest"]
    assert result["manifest"] == manifest["record"]
    assert result["manifest"]["coverage_complete"] is True
    assert result["recorded_at"] == "2026-10-23T10:00:00+08:00"
    conn.commit()
    assert _count(conn) == 1


def test_gapped_snapshot_persists_without_claiming_completion():
    # Persisting a coverage_complete=false snapshot only means a gapped
    # state was durably recorded, never that the review finished.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    gapped = _gapped_manifest()
    assert gapped["record"]["coverage_complete"] is False
    _begin(conn)
    result = _record(store, gapped)
    conn.commit()
    assert result["replayed"] is False
    stored = store.get_manifest("co-1", "job-1", gapped["digest"])
    assert stored["manifest"]["coverage_complete"] is False
    gap_codes = [gap["code"]
                 for window in stored["manifest"]["windows"]
                 for gap in window["gaps"]]
    assert gap_codes == ["missing_window_record"]


def test_exact_replay_returns_original_recorded_at_without_new_rows():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    first = _record(store, manifest)
    # A retry with the same manifest but a different host-issued
    # recorded_at must return the original durable values.
    second = _record(store, manifest,
                     recorded_at="2026-10-23T11:00:00+08:00")
    conn.commit()
    assert second["replayed"] is True
    assert second["recorded_at"] == "2026-10-23T10:00:00+08:00"
    assert second["manifest"] == first["manifest"]
    assert second["manifest_digest"] == first["manifest_digest"]
    assert _count(conn) == 1


def test_different_digests_coexist_under_one_scope():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    complete = _manifest()
    gapped = _gapped_manifest()
    assert complete["digest"] != gapped["digest"]
    _begin(conn)
    _record(store, complete)
    _record(store, gapped)
    conn.commit()
    assert _count(conn) == 2
    assert store.get_manifest(
        "co-1", "job-1", complete["digest"])["manifest"]["coverage_complete"] \
        is True
    assert store.get_manifest(
        "co-1", "job-1", gapped["digest"])["manifest"]["coverage_complete"] \
        is False


def test_scope_isolation_for_lookup_and_storage():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    conn.commit()
    # Same digest under another company or job is a different scope.
    assert store.get_manifest("co-2", "job-1", manifest["digest"]) is None
    assert store.get_manifest("co-1", "job-2", manifest["digest"]) is None
    _begin(conn)
    other = _record(store, manifest, company_id="co-2")
    conn.commit()
    assert other["replayed"] is False
    assert _count(conn) == 2
    assert store.get_manifest(
        "co-2", "job-1", manifest["digest"])["company_id"] == "co-2"


def test_result_object_must_have_exactly_record_and_digest_keys():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    with pytest.raises(ValueError, match="exactly"):
        _record(store, manifest={"record": manifest["record"],
                                 "digest": manifest["digest"], "extra": 1})
    with pytest.raises(ValueError, match="exactly"):
        _record(store, manifest={"record": manifest["record"]})
    with pytest.raises(TypeError):
        _record(store, manifest=["not-a-result"])
    with pytest.raises(TypeError):
        _record(store, manifest={"record": manifest["record"],
                                 "digest": manifest["digest"], 1: "x"})
    with pytest.raises(TypeError):
        _record(store, manifest={"record": ["not-an-object"],
                                 "digest": manifest["digest"]})
    conn.rollback()
    assert _count(conn) == 0


def test_mismatched_digest_rejected_and_nothing_stored():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    other = _gapped_manifest()
    _begin(conn)
    # A well-formed but wrong digest is rejected the same as a bogus one.
    with pytest.raises(ValueError, match="digest"):
        _record(store, manifest={"record": manifest["record"],
                                 "digest": other["digest"]})
    with pytest.raises(ValueError, match="digest"):
        _record(store, manifest={"record": manifest["record"],
                                 "digest": "0" * 64})
    conn.rollback()
    assert _count(conn) == 0


def test_empty_windows_manifest_rejected_before_any_write():
    # A hand-forged, digest-correct record with windows=[] can never
    # prove file-level full-text coverage (P01 section 4 item 5 /
    # section 14.1). The store independently re-checks the windows list
    # and must fail before any SAVEPOINT or database write.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    forged_record = {
        "record_type": "candidate_coverage_manifest",
        "format_version": 1,
        "coverage_complete": True,
        "windows": [],
    }
    forged = {
        "record": forged_record,
        "digest": hashlib.sha256(
            git_format.dumps_record(forged_record)).hexdigest(),
    }
    _begin(conn)
    with pytest.raises(ValueError, match="windows"):
        _record(store, manifest=forged)
    assert conn.in_transaction  # caller transaction stays open
    conn.rollback()
    assert _count(conn) == 0  # no row was written


def test_legacy_empty_windows_snapshot_rejected_on_read():
    # Simulate a snapshot persisted by an earlier store version whose
    # write path accepted digest-correct records with windows=[]. The
    # canonical blob, content digest, primary-key digest and row
    # integrity digest are all correct, so every existing tamper check
    # passes; the read path must still refuse the hollow
    # coverage_complete=true record instead of returning it.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    legacy_record = {
        "record_type": "candidate_coverage_manifest",
        "format_version": 1,
        "coverage_complete": True,
        "windows": [],
    }
    blob = git_format.dumps_record(legacy_record)
    digest = hashlib.sha256(blob).hexdigest()
    recorded_at = "2026-10-23T09:00:00+08:00"
    row_digest = hashlib.sha256(git_format.dumps_record({
        "company_id": "co-1",
        "job_id": "job-1",
        "manifest_digest": digest,
        "recorded_at": recorded_at,
        "manifest_sha256": digest,
    })).hexdigest()
    conn.execute(
        "INSERT INTO " + _TABLE + " (company_id, job_id, manifest_digest, "
        "recorded_at, manifest_blob, manifest_sha256, row_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("co-1", "job-1", digest, recorded_at, blob, digest, row_digest))
    conn.commit()
    with pytest.raises(ValueError, match="windows"):
        store.get_manifest("co-1", "job-1", digest)
    # The store-owned read snapshot is closed with ROLLBACK on failure
    # exactly as on success.
    assert not conn.in_transaction


def test_conflict_on_same_digest_different_bytes():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    complete = _manifest()
    gapped = _gapped_manifest()
    _begin(conn)
    _record(store, complete)
    conn.commit()
    # Simulate drift/collision: the row addressed by complete's digest
    # now holds gapped's canonical bytes. Direct UPDATE simulates
    # out-of-band damage, so the guards are dropped explicitly first.
    gapped_blob = git_format.dumps_record(gapped["record"])
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET manifest_blob=? WHERE company_id='co-1'",
        (gapped_blob,))
    conn.commit()
    _begin(conn)
    with pytest.raises(CoverageManifestConflict):
        _record(store, complete)
    assert conn.in_transaction  # caller transaction still open
    assert _count(conn) == 1  # nothing overwritten or appended
    conn.rollback()
    # The read path hard-fails on the digest mismatch as well.
    with pytest.raises(ValueError, match="tampered"):
        store.get_manifest("co-1", "job-1", complete["digest"])


def test_failed_write_rolls_back_only_its_own_savepoint():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    complete = _manifest()
    gapped = _gapped_manifest()
    _begin(conn)
    _record(store, complete)
    conn.commit()
    gapped_blob = git_format.dumps_record(gapped["record"])
    # Direct UPDATE simulates out-of-band damage; drop the guards first.
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET manifest_blob=? WHERE company_id='co-1'",
        (gapped_blob,))
    conn.commit()
    _begin(conn)
    _record(store, gapped)  # different digest: succeeds in the same txn
    with pytest.raises(CoverageManifestConflict):
        _record(store, complete)
    assert conn.in_transaction
    # The earlier write in this caller transaction survives the failure.
    assert _count(conn) == 2
    conn.rollback()
    assert _count(conn) == 1


def test_read_revalidates_blob_and_column_digests():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    _record(store, manifest)
    conn.commit()
    # Direct UPDATE simulates out-of-band damage; drop the guards first.
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET manifest_sha256=? "
        "WHERE company_id='co-1'",
        (hashlib.sha256(b"other").hexdigest(),))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_manifest("co-1", "job-1", manifest["digest"])


def test_noncanonical_blob_with_matched_digests_fails_closed():
    # A blob that parses fine and whose digest column and primary key were
    # updated to match, but whose bytes are not the exact canonical
    # serialization, must still hard-fail on read.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    _record(store, manifest)
    conn.commit()
    noncanonical = json.dumps(manifest["record"],
                              sort_keys=True).encode("utf-8")
    assert noncanonical != git_format.dumps_record(manifest["record"])
    digest = hashlib.sha256(noncanonical).hexdigest()
    # Direct UPDATE simulates out-of-band damage; drop the guards first.
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET manifest_blob=?, manifest_sha256=?, "
        "manifest_digest=? WHERE company_id='co-1'",
        (noncanonical, digest, digest))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_manifest("co-1", "job-1", digest)


def test_mutated_identity_columns_fail_closed_even_under_changed_scope():
    # Row-integrity digest: changing company_id, job_id or recorded_at
    # after insert must fail closed on read, even when the lookup itself
    # uses the mutated scope/key values.
    for column, mutate in (
            ("company_id", lambda d: ("co-mutated", ("co-mutated", "job-1", d))),
            ("job_id", lambda d: ("job-mutated", ("co-1", "job-mutated", d))),
            ("recorded_at", lambda d: ("2026-10-23T12:00:00+08:00",
                                       ("co-1", "job-1", d)))):
        conn = _connect()
        store = CandidateCoverageManifestStore(conn)
        manifest = _manifest()
        _begin(conn)
        _record(store, manifest)
        conn.commit()
        mutated, scope = mutate(manifest["digest"])
        # Direct UPDATE simulates out-of-band damage; drop the guards.
        _drop_immutability_guards(conn)
        conn.execute(
            "UPDATE " + _TABLE + " SET %s=? WHERE company_id='co-1'" % column,
            (mutated,))
        conn.commit()
        with pytest.raises(ValueError, match="tampered"):
            store.get_manifest(*scope)
        if column in ("company_id", "job_id"):
            continue  # key no longer matches; write path sees no row
        # Replay through the write path must fail closed the same way.
        _begin(conn)
        with pytest.raises(ValueError, match="tampered"):
            _record(store, manifest)
        conn.rollback()


def test_mutated_digest_key_column_fails_closed():
    # Changing the content-addressing primary key after insert breaks the
    # binding between the key and the stored content digest.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    conn.commit()
    mutated = hashlib.sha256(b"other").hexdigest()
    # Direct UPDATE simulates out-of-band damage; drop the guards first.
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET manifest_digest=? "
        "WHERE company_id='co-1'", (mutated,))
    conn.commit()
    with pytest.raises(ValueError, match="tampered"):
        store.get_manifest("co-1", "job-1", mutated)
    # The original key no longer resolves to a row.
    assert store.get_manifest("co-1", "job-1", manifest["digest"]) is None


def test_mutated_row_digest_column_fails_closed():
    # Tampering with the stored digest value itself also fails closed.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    conn.commit()
    # Direct UPDATE simulates out-of-band damage; drop the guards first.
    _drop_immutability_guards(conn)
    conn.execute(
        "UPDATE " + _TABLE + " SET row_sha256='deadbeef' "
        "WHERE company_id='co-1'")
    conn.commit()
    with pytest.raises(ValueError, match="row-integrity digest"):
        store.get_manifest("co-1", "job-1", manifest["digest"])


def test_store_owned_read_snapshot_never_emits_commit():
    # On an idle connection get_manifest opens its own read snapshot;
    # the trace callback must never see a COMMIT on that path.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        stored = store.get_manifest("co-1", "job-1", manifest["digest"])
        assert stored["manifest"] == manifest["record"]
        assert stored["replayed"] is False
        missing = store.get_manifest("co-1", "job-1", "0" * 64)
        assert missing is None
    finally:
        conn.set_trace_callback(None)
    commits = [s for s in statements
               if s.lstrip().upper().startswith("COMMIT")]
    assert commits == []
    assert not conn.in_transaction  # snapshot fully closed afterwards


def test_read_inside_caller_transaction_leaves_it_open():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        stored = store.get_manifest("co-1", "job-1", manifest["digest"])
    finally:
        conn.set_trace_callback(None)
    assert stored["recorded_at"] == "2026-10-23T10:00:00+08:00"
    assert conn.in_transaction  # caller transaction untouched
    forbidden = [s for s in statements
                 if s.lstrip().upper().startswith(("COMMIT", "ROLLBACK"))]
    assert forbidden == []
    conn.commit()
    assert store.get_manifest("co-1", "job-1", manifest["digest"]) \
        is not None


def test_caller_transaction_stays_open_and_can_roll_back():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    _record(store, manifest)
    assert conn.in_transaction
    conn.rollback()
    assert store.get_manifest("co-1", "job-1", manifest["digest"]) is None
    # Failure path likewise leaves the caller transaction open.
    gapped = _gapped_manifest()
    _begin(conn)
    _record(store, manifest)
    with pytest.raises(ValueError, match="digest"):
        _record(store, manifest={"record": manifest["record"],
                                 "digest": gapped["digest"]})
    assert conn.in_transaction
    conn.rollback()
    assert store.get_manifest("co-1", "job-1", manifest["digest"]) is None


def test_write_outside_caller_transaction_fails_before_mutation():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    with pytest.raises(ValueError, match="caller transaction"):
        _record(store)
    assert _count(conn) == 0


def test_constructor_rejects_active_caller_transaction():
    conn = _connect()
    _begin(conn)
    with pytest.raises(ValueError, match="idle connection"):
        CandidateCoverageManifestStore(conn)
    conn.rollback()


def test_constructor_rejects_non_sqlite_connection():
    with pytest.raises(TypeError, match="sqlite3.Connection"):
        CandidateCoverageManifestStore(object())


def test_inputs_are_not_mutated():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    snapshot = deepcopy(manifest)
    _begin(conn)
    _record(store, manifest)
    conn.commit()
    assert manifest == snapshot


def test_identity_values_must_be_host_shaped():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    with pytest.raises(ValueError):
        _record(store, company_id="")
    with pytest.raises(ValueError):
        _record(store, job_id=123)
    with pytest.raises(ValueError):
        _record(store, recorded_at="")
    conn.rollback()
    assert _count(conn) == 0
    # Malformed digests are rejected at the read boundary as well.
    with pytest.raises(ValueError, match="digest"):
        store.get_manifest("co-1", "job-1", "not-a-digest")
    with pytest.raises(ValueError, match="digest"):
        store.get_manifest("co-1", "job-1", "A" * 64)  # uppercase rejected
    with pytest.raises(ValueError, match="digest"):
        store.get_manifest("co-1", "job-1", "0" * 63)


def test_get_missing_returns_none():
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    assert store.get_manifest("co-1", "job-1", "0" * 64) is None


def test_database_guard_rejects_update_and_delete():
    # P01 section 4 item 5 immutability floor: even SQL issued outside
    # the store API cannot rewrite or remove a persisted snapshot.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    _record(store, manifest)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE " + _TABLE + " SET recorded_at=? "
            "WHERE company_id='co-1'",
            ("2099-01-01T00:00:00+08:00",))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM " + _TABLE + " WHERE company_id='co-1'")
    conn.rollback()
    stored = store.get_manifest("co-1", "job-1", manifest["digest"])
    assert stored["recorded_at"] == "2026-10-23T10:00:00+08:00"
    assert stored["manifest"] == manifest["record"]
    assert _count(conn) == 1


def test_existing_database_installs_guards_without_touching_history(tmp_path):
    # Regression for the existing-table initialization path: a database
    # whose rows predate the immutability triggers must get the guards on
    # the next construction without any change to the stored history.
    db_path = tmp_path / "coverage.db"
    conn = sqlite3.connect(str(db_path))
    store = CandidateCoverageManifestStore(conn)
    manifest = _manifest()
    _begin(conn)
    first = _record(store, manifest)
    conn.commit()
    conn.close()

    # Simulate the prior schema: the populated table exists but none of
    # the three immutability guards do.
    conn = sqlite3.connect(str(db_path))
    _drop_immutability_guards(conn)
    conn.commit()
    triggers = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'")
    assert triggers.fetchone()[0] == 0
    conn.close()

    # Reopening the same file and reconstructing the store must install
    # the guards and leave the snapshot readable and unchanged.
    conn = sqlite3.connect(str(db_path))
    store = CandidateCoverageManifestStore(conn)
    stored = store.get_manifest("co-1", "job-1", manifest["digest"])
    assert stored["manifest_digest"] == first["manifest_digest"]
    assert stored["recorded_at"] == first["recorded_at"]
    assert stored["manifest"] == first["manifest"]
    assert stored["manifest_sha256"] == first["manifest_sha256"]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE " + _TABLE + " SET recorded_at=? "
            "WHERE company_id='co-1'",
            ("2099-01-01T00:00:00+08:00",))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM " + _TABLE + " WHERE company_id='co-1'")
    conn.rollback()
    blob = git_format.dumps_record(manifest["record"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT OR REPLACE INTO " + _TABLE + " (company_id, job_id, "
            "manifest_digest, recorded_at, manifest_blob, manifest_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("co-1", "job-1", manifest["digest"],
             "2099-01-01T00:00:00+08:00", blob, manifest["digest"],
             "deadbeef"))
    conn.rollback()

    stored = store.get_manifest("co-1", "job-1", manifest["digest"])
    assert stored["recorded_at"] == first["recorded_at"]
    assert stored["manifest"] == first["manifest"]
    assert _count(conn) == 1
    conn.close()


def test_database_guard_rejects_insert_or_replace_overwrite():
    # INSERT OR REPLACE on an existing content-addressed key would
    # silently delete and replace the original snapshot; the BEFORE
    # INSERT guard rejects it and the original row stays untouched.
    conn = _connect()
    store = CandidateCoverageManifestStore(conn)
    _begin(conn)
    manifest = _manifest()
    _record(store, manifest)
    conn.commit()
    blob = git_format.dumps_record(manifest["record"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT OR REPLACE INTO " + _TABLE + " (company_id, job_id, "
            "manifest_digest, recorded_at, manifest_blob, manifest_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("co-1", "job-1", manifest["digest"],
             "2099-01-01T00:00:00+08:00", blob, manifest["digest"],
             "deadbeef"))
    conn.rollback()
    stored = store.get_manifest("co-1", "job-1", manifest["digest"])
    assert stored["recorded_at"] == "2026-10-23T10:00:00+08:00"
    assert _count(conn) == 1
    # A genuinely new digest still inserts normally.
    gapped = _gapped_manifest()
    assert gapped["digest"] != manifest["digest"]
    _begin(conn)
    _record(store, gapped)
    conn.commit()
    assert _count(conn) == 2
