"""Atomicity tests: the three F-001 candidate work stores in one caller transaction.

Covers exactly one guarantee: CandidateSubmissionStore (submission
receipts), CandidateRevisionStore (candidate revisions) and
CandidateCoverageManifestStore (full-text coverage snapshots) can all
join the same caller-owned transaction on one in-memory SQLite
connection, so one explicit caller COMMIT persists all three records
and one explicit caller ROLLBACK undoes all three together. This only
verifies the transaction composition of the existing storage
primitives; it is not a candidate intake service and not a host
authorization implementation. No claim is made about cross-connection,
multi-process or power-loss durability; only the single-connection
caller transaction boundary is exercised.

All fixtures are synthetic strings over one fake company (acc-*) and
one fake job; nothing reads real company data, files or models. Stores
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

_COMPANY = "acc-ws-atomicity"
_JOB = "job-ws-atomicity-1"
_SUBMISSION_REQUEST = "req-ws-submission-1"
_SUBMISSION = "sub-ws-atomicity-1"
_CANDIDATE = "cand-ws-atomicity-1"
_CREATE_REQUEST = "req-ws-create-1"
_RECEIVED_AT = "2026-10-23T23:30:00+08:00"
_RECORDED_AT = "2026-10-23T23:31:00+08:00"

_SUBMISSIONS_TABLE = "fact_candidate_submissions"
_REVISIONS_TABLE = "fact_candidate_revisions"
_MANIFESTS_TABLE = "fact_candidate_coverage_manifests"


def _batch_payload():
    return {
        "batch_note": "synthetic atomicity batch",
        "candidates": [{"local_id": "c1", "statement": "synthetic"}],
    }


def _receipt():
    return {
        "outcome": "received",
        "items": [{"local_id": "c1", "result": "accepted"}],
    }


def _candidate_payload():
    return {"statement": "synthetic candidate payload", "revision_of": "c1"}


def _window():
    return {
        "window_id": "win-1",
        "source_loc": "loc-1",
        "file_hash": "fh-1",
        "mapped_fingerprint": "mf-1",
    }


def _manifest():
    """A manifest referencing the synthetic candidate/revision and fully
    covering one single host window."""
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


def _stores():
    """All three stores constructed while the connection is idle."""
    conn = sqlite3.connect(":memory:")
    assert conn.in_transaction is False
    submissions = CandidateSubmissionStore(conn)
    revisions = CandidateRevisionStore(conn)
    manifests = CandidateCoverageManifestStore(conn)
    return conn, submissions, revisions, manifests


def _write_all(submissions, revisions, manifests, manifest):
    """The three caller-transaction writes, in intake order."""
    submission = submissions.record_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST, _SUBMISSION, _RECEIVED_AT,
        _batch_payload(), _receipt())
    revision = revisions.create_candidate(
        _COMPANY, _JOB, _CANDIDATE, _CREATE_REQUEST, _RECORDED_AT,
        _candidate_payload())
    coverage = manifests.record_manifest(
        _COMPANY, _JOB, _RECORDED_AT, manifest)
    return submission, revision, coverage


def _count(conn, table):
    # Direct SQL is used only to count rows; stores are exercised
    # exclusively through their public methods.
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_caller_commit_persists_all_three_stores():
    conn, submissions, revisions, manifests = _stores()
    manifest = _manifest()
    assert manifest["record"]["coverage_complete"] is True

    conn.execute("BEGIN")
    assert conn.in_transaction is True

    # Each store write joins the caller transaction and leaves it open.
    submission = submissions.record_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST, _SUBMISSION, _RECEIVED_AT,
        _batch_payload(), _receipt())
    assert conn.in_transaction is True
    revision = revisions.create_candidate(
        _COMPANY, _JOB, _CANDIDATE, _CREATE_REQUEST, _RECORDED_AT,
        _candidate_payload())
    assert conn.in_transaction is True
    coverage = manifests.record_manifest(
        _COMPANY, _JOB, _RECORDED_AT, manifest)
    assert conn.in_transaction is True

    assert submission["replayed"] is False
    assert revision["replayed"] is False
    assert revision["revision"] == 1
    assert coverage["replayed"] is False

    conn.commit()
    assert conn.in_transaction is False

    stored_submission = submissions.get_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST)
    assert stored_submission["company_id"] == _COMPANY
    assert stored_submission["job_id"] == _JOB
    assert stored_submission["request_id"] == _SUBMISSION_REQUEST
    assert stored_submission["submission_id"] == _SUBMISSION
    assert stored_submission["received_at"] == _RECEIVED_AT
    assert stored_submission["payload"] == _batch_payload()
    assert stored_submission["receipt"] == _receipt()
    assert stored_submission["payload_sha256"] \
        == submission["payload_sha256"]
    assert stored_submission["receipt_sha256"] \
        == submission["receipt_sha256"]

    stored_revision = revisions.get_current(_COMPANY, _JOB, _CANDIDATE)
    assert stored_revision["company_id"] == _COMPANY
    assert stored_revision["job_id"] == _JOB
    assert stored_revision["candidate_uid"] == _CANDIDATE
    assert stored_revision["revision"] == 1
    assert stored_revision["request_id"] == _CREATE_REQUEST
    assert stored_revision["payload"] == _candidate_payload()
    assert stored_revision["payload_sha256"] == revision["payload_sha256"]

    stored_manifest = manifests.get_manifest(
        _COMPANY, _JOB, manifest["digest"])
    assert stored_manifest["company_id"] == _COMPANY
    assert stored_manifest["job_id"] == _JOB
    assert stored_manifest["manifest_digest"] == manifest["digest"]
    assert stored_manifest["manifest_sha256"] == manifest["digest"]
    assert stored_manifest["manifest"] == manifest["record"]
    assert stored_manifest["manifest"]["windows"][0][
        "candidate_revisions"] == [{"candidate_uid": _CANDIDATE,
                                    "revision": 1}]


def test_caller_rollback_undoes_all_three_stores():
    conn, submissions, revisions, manifests = _stores()
    manifest = _manifest()

    conn.execute("BEGIN")
    _write_all(submissions, revisions, manifests, manifest)
    assert conn.in_transaction is True

    conn.rollback()
    assert conn.in_transaction is False

    # None of the three keys is readable after the outer rollback...
    assert submissions.get_submission(
        _COMPANY, _JOB, _SUBMISSION_REQUEST) is None
    assert revisions.get_current(_COMPANY, _JOB, _CANDIDATE) is None
    assert manifests.get_manifest(
        _COMPANY, _JOB, manifest["digest"]) is None
    # ...and all three tables are empty again: the three writes committed
    # or rolled back as one atomic unit under the caller transaction.
    assert _count(conn, _SUBMISSIONS_TABLE) == 0
    assert _count(conn, _REVISIONS_TABLE) == 0
    assert _count(conn, _MANIFESTS_TABLE) == 0
