"""Cross-connection restart persistence tests for the F-001 work stores.

Covers exactly one guarantee of F-001 P01 sections 9.1 and 10.8
("uncommitted candidates and work-store state must be recoverable"):
after one caller transaction COMMITs a submission receipt, both
candidate revisions and the coverage manifest into a file-backed SQLite
database and the connection is closed, a brand-new connection with
brand-new store instances reads back the original receipt, the complete
revision history, the coverage manifest and every digest; an exact
replay after reopening returns the original durable receipts and adds
no rows. A caller ROLLBACK before close leaves all three stores empty
after reopening.

This only verifies cross-connection SQLite persistence of the existing
storage primitives. It claims nothing about task-scheduler restart
recovery, task-failure retry or cross-process mutual exclusion.

All fixtures are synthetic strings over one fake company (acc-*) and one
fake job; the database file lives only under pytest tmp_path. Stores
are written and read only through their public methods; direct SELECT
is used solely for row counts.
"""
import sqlite3

from app.facts_tree.candidate_coverage import build_coverage_manifest
from app.facts_tree.candidate_coverage_store import (
    CandidateCoverageManifestStore,
)
from app.facts_tree.candidate_revision_store import CandidateRevisionStore
from app.facts_tree.candidate_work_store import CandidateSubmissionStore

_COMPANY = "acc-ws-restart"
_JOB = "job-ws-restart-1"
_SUBMISSION_REQUEST = "req-ws-submission-1"
_SUBMISSION = "sub-ws-restart-1"
_CANDIDATE = "cand-ws-restart-1"
_CREATE_REQUEST = "req-ws-create-1"
_REVISE_REQUEST = "req-ws-revise-1"
_RECEIVED_AT = "2026-10-24T09:00:00+08:00"
_CREATED_AT = "2026-10-24T09:01:00+08:00"
_REVISED_AT = "2026-10-24T09:02:00+08:00"
_MANIFEST_RECORDED_AT = "2026-10-24T09:03:00+08:00"

_SUBMISSIONS_TABLE = "fact_candidate_submissions"
_REVISIONS_TABLE = "fact_candidate_revisions"
_MANIFESTS_TABLE = "fact_candidate_coverage_manifests"


def _batch_payload():
    return {
        "batch_note": "synthetic restart batch",
        "candidates": [{"local_id": "c1", "statement": "synthetic"}],
    }


def _receipt():
    return {
        "outcome": "received",
        "items": [{"local_id": "c1", "result": "accepted"}],
    }


def _candidate_payload_v1():
    return {"statement": "synthetic candidate payload v1",
            "revision_of": "c1"}


def _candidate_payload_v2():
    return {"statement": "synthetic candidate payload v2",
            "revision_of": "c1"}


def _window():
    return {
        "window_id": "win-1",
        "source_loc": "loc-1",
        "file_hash": "fh-1",
        "mapped_fingerprint": "mf-1",
    }


def _manifest():
    """A manifest referencing the synthetic candidate revision 1 and
    fully covering one single host window."""
    window = _window()
    record = dict(window)
    record.update({
        "status": "reviewed",
        "candidate_revisions": [{"candidate_uid": _CANDIDATE,
                                 "revision": 1}],
        "pending_rejections": [],
        "exclusions": [],
        "unresolved": [],
    })
    return build_coverage_manifest([window], [record])


def _stores(db_path):
    """A fresh file-backed connection plus all three stores, constructed
    while the connection is idle."""
    conn = sqlite3.connect(str(db_path))
    assert conn.in_transaction is False
    submissions = CandidateSubmissionStore(conn)
    revisions = CandidateRevisionStore(conn)
    manifests = CandidateCoverageManifestStore(conn)
    return conn, submissions, revisions, manifests


def _write_all(submissions, revisions, manifests, manifest):
    """The caller-transaction writes, in intake order: one submission
    receipt, candidate revisions 1 and 2, one coverage manifest."""
    submission = submissions.record_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST, _SUBMISSION, _RECEIVED_AT,
        _batch_payload(), _receipt())
    created = revisions.create_candidate(
        _COMPANY, _JOB, _CANDIDATE, _CREATE_REQUEST, _CREATED_AT,
        _candidate_payload_v1())
    revised = revisions.revise_candidate(
        _COMPANY, _JOB, _CANDIDATE, _REVISE_REQUEST, 1, _REVISED_AT,
        _candidate_payload_v2())
    coverage = manifests.record_manifest(
        _COMPANY, _JOB, _MANIFEST_RECORDED_AT, manifest)
    return submission, created, revised, coverage


def _count(conn, table):
    # Direct SQL is used only to count rows; stores are exercised
    # exclusively through their public methods.
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_commit_close_reopen_restores_all_three_stores(tmp_path):
    db_path = tmp_path / "work_store_restart.db"
    manifest = _manifest()
    assert manifest["record"]["coverage_complete"] is True

    conn, submissions, revisions, manifests = _stores(db_path)
    conn.execute("BEGIN")
    submission, created, revised, coverage = _write_all(
        submissions, revisions, manifests, manifest)
    assert submission["replayed"] is False
    assert created["replayed"] is False and created["revision"] == 1
    assert revised["replayed"] is False and revised["revision"] == 2
    assert coverage["replayed"] is False
    conn.commit()
    conn.close()

    # Restart: a brand-new connection and brand-new store instances.
    conn, submissions, revisions, manifests = _stores(db_path)

    stored_submission = submissions.get_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST)
    assert stored_submission["company_id"] == _COMPANY
    assert stored_submission["job_id"] == _JOB
    assert stored_submission["request_id"] == _SUBMISSION_REQUEST
    assert stored_submission["submission_id"] == _SUBMISSION
    assert stored_submission["received_at"] == _RECEIVED_AT
    assert stored_submission["payload"] == _batch_payload()
    assert stored_submission["receipt"] == _receipt()
    assert stored_submission["payload_sha256"] == submission["payload_sha256"]
    assert stored_submission["receipt_sha256"] == submission["receipt_sha256"]

    history = revisions.list_revisions(_COMPANY, _JOB, _CANDIDATE)
    assert len(history) == 2
    assert history[0]["revision"] == 1
    assert history[0]["request_id"] == _CREATE_REQUEST
    assert history[0]["recorded_at"] == _CREATED_AT
    assert history[0]["payload"] == _candidate_payload_v1()
    assert history[0]["payload_sha256"] == created["payload_sha256"]
    assert history[1]["revision"] == 2
    assert history[1]["request_id"] == _REVISE_REQUEST
    assert history[1]["recorded_at"] == _REVISED_AT
    assert history[1]["payload"] == _candidate_payload_v2()
    assert history[1]["payload_sha256"] == revised["payload_sha256"]

    current = revisions.get_current(_COMPANY, _JOB, _CANDIDATE)
    assert current["revision"] == 2
    assert current["request_id"] == _REVISE_REQUEST
    assert current["payload"] == _candidate_payload_v2()
    assert current["payload_sha256"] == revised["payload_sha256"]

    stored_manifest = manifests.get_manifest(
        _COMPANY, _JOB, manifest["digest"])
    assert stored_manifest["company_id"] == _COMPANY
    assert stored_manifest["job_id"] == _JOB
    assert stored_manifest["recorded_at"] == _MANIFEST_RECORDED_AT
    assert stored_manifest["manifest_digest"] == manifest["digest"]
    assert stored_manifest["manifest_sha256"] == manifest["digest"]
    assert stored_manifest["manifest"] == manifest["record"]
    assert stored_manifest["manifest"]["windows"][0][
        "candidate_revisions"] == [{"candidate_uid": _CANDIDATE,
                                    "revision": 1}]

    assert _count(conn, _SUBMISSIONS_TABLE) == 1
    assert _count(conn, _REVISIONS_TABLE) == 2
    assert _count(conn, _MANIFESTS_TABLE) == 1

    # Exact replay after the restart returns the original durable
    # receipts and writes nothing.
    conn.execute("BEGIN")
    replayed_submission = submissions.record_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST, _SUBMISSION, _RECEIVED_AT,
        _batch_payload(), _receipt())
    assert replayed_submission["replayed"] is True
    assert replayed_submission["submission_id"] == _SUBMISSION
    assert replayed_submission["received_at"] == _RECEIVED_AT
    assert replayed_submission["receipt"] == _receipt()
    assert replayed_submission["receipt_sha256"] \
        == submission["receipt_sha256"]

    replayed_create = revisions.create_candidate(
        _COMPANY, _JOB, _CANDIDATE, _CREATE_REQUEST, _CREATED_AT,
        _candidate_payload_v1())
    assert replayed_create["replayed"] is True
    assert replayed_create["revision"] == 1
    assert replayed_create["recorded_at"] == _CREATED_AT
    assert replayed_create["payload_sha256"] == created["payload_sha256"]

    replayed_revise = revisions.revise_candidate(
        _COMPANY, _JOB, _CANDIDATE, _REVISE_REQUEST, 1, _REVISED_AT,
        _candidate_payload_v2())
    assert replayed_revise["replayed"] is True
    assert replayed_revise["revision"] == 2
    assert replayed_revise["recorded_at"] == _REVISED_AT
    assert replayed_revise["payload_sha256"] == revised["payload_sha256"]

    replayed_coverage = manifests.record_manifest(
        _COMPANY, _JOB, "2026-10-24T09:04:00+08:00", manifest)
    assert replayed_coverage["replayed"] is True
    # The original recorded_at is returned as first durably recorded,
    # never replaced by the replay's argument.
    assert replayed_coverage["recorded_at"] == _MANIFEST_RECORDED_AT
    assert replayed_coverage["manifest_digest"] == manifest["digest"]
    conn.commit()

    assert _count(conn, _SUBMISSIONS_TABLE) == 1
    assert _count(conn, _REVISIONS_TABLE) == 2
    assert _count(conn, _MANIFESTS_TABLE) == 1
    conn.close()


def test_rollback_close_reopen_leaves_all_three_stores_empty(tmp_path):
    db_path = tmp_path / "work_store_restart_rollback.db"
    manifest = _manifest()

    conn, submissions, revisions, manifests = _stores(db_path)
    conn.execute("BEGIN")
    _write_all(submissions, revisions, manifests, manifest)
    assert conn.in_transaction is True
    conn.rollback()
    conn.close()

    # Restart: a brand-new connection and brand-new store instances.
    conn, submissions, revisions, manifests = _stores(db_path)

    assert submissions.get_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST) is None
    assert revisions.get_current(_COMPANY, _JOB, _CANDIDATE) is None
    assert revisions.list_revisions(_COMPANY, _JOB, _CANDIDATE) == []
    assert manifests.get_manifest(
        _COMPANY, _JOB, manifest["digest"]) is None
    assert _count(conn, _SUBMISSIONS_TABLE) == 0
    assert _count(conn, _REVISIONS_TABLE) == 0
    assert _count(conn, _MANIFESTS_TABLE) == 0
    conn.close()
