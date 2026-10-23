"""Runtime work-store primitive for F-001 candidate submission receipts.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 sections 3.2-4 and 10.6. This module is a small standalone
SQLite persistence primitive for the *runtime work store* only: it
persists one exact candidate submission snapshot plus the host-produced
per-item receipt, with request-idempotent replay. Per P01 section 10 the
runtime work store keeps uncommitted candidate state reliably, but it is
NOT the formal fact Git repository and NOT the rebuildable query
projection; a row here only means "this request was durably received",
never that any candidate entered the formal tree.

Scope of this slice (explicitly not implemented here): candidate
uid/revision issuance, host envelope construction, read-grant /
evidence / schema validation, full-text coverage manifests, HTTP/API
entry points, formal Git operations, query projections, and any
business-acceptance state.

Caller obligations (this store does NOT do these):

- The caller must build the trusted host envelope itself and must have
  already run the schema, source-grant and evidence checks of P01
  sections 3 and 7 before recording. company_id / job_id / request_id /
  submission_id are host-owned identity values passed as separate
  arguments; they are never read out of the candidate payload, so a
  payload field named "company_id" (or similar) can never set stored
  identity.
- Nothing here authenticates company_id or job_id, and nothing here
  proves actor identity, policy or delegation-chain validity, grant
  validity or current revocation state. There is no current SAM-APP
  source for P01 actor_id / policy revision / delegation chain /
  authorization credential references; this module does not fabricate
  or default them.
- Rejected per-item outcomes are persisted only as caller-provided
  receipt content. This store never marks a window complete and has no
  "accepted into the formal tree" state to return.

Transaction contract (documented choice):

- The constructor takes only a caller-provided sqlite3.Connection. It
  never opens a database, never chooses a data path and never migrates
  app.db. While the connection is idle it creates its own table;
  construction with an already-active caller transaction is refused.
- record_submission REQUIRES an active caller transaction and joins it:
  all writes run inside one SAVEPOINT. On success the savepoint is
  released; on any exception the store rolls back only to that
  savepoint, releases it and re-raises, so a failed call leaves no
  partial rows and the caller transaction stays open. This store never
  issues BEGIN/COMMIT/ROLLBACK against the caller transaction.
- get_submission is read-only and never issues COMMIT; when the
  connection is idle it opens its own short read snapshot and always
  closes it with ROLLBACK, on success and on failure alike. When the
  caller already holds a transaction this store reads through it and
  never closes, commits or rolls it back.

Idempotency: the key is (company_id, job_id, request_id). The first
call stores the snapshot and original receipt. A later call with the
same key and byte-identical canonical payload returns the original
durable receipt with replayed=True and writes nothing; the original
submission_id, received_at and receipt are never replaced. The same key
with a different canonical payload fails closed with SubmissionConflict
and all prior rows stay unchanged.

Tamper resistance: the receipt rows are append-only at the database
level. BEFORE UPDATE / BEFORE DELETE triggers reject any rewrite or
removal of a recorded receipt, and a BEFORE INSERT trigger rejects an
insert (including INSERT OR REPLACE, which would otherwise delete and
replace the old row) whose (company_id, job_id, request_id) key is
already recorded, so the original receipt can never be overwritten
through SQLite itself. A first-time insert on a fresh key is
unaffected. On top of that, payload and receipt are stored as canonical
git_format bytes plus their SHA-256 digest. Reads re-parse the blobs
through git_format.loads_record, require the stored bytes to equal the
exact canonical git_format.dumps_record serialization of the parsed
value (parseable-with-matching-digest is not enough), and re-verify
both digests and the stored scope/identity columns; any drift is a hard
failure, never a silent repair. Each row also carries an internal
row-integrity digest (row_sha256) that binds all host envelope
identity/metadata columns (company_id, job_id, request_id,
submission_id, received_at) together with the validated payload/receipt
SHA-256 values; it is stored at insert time and recomputed and compared
on every read before returning, so a row whose company_id,
submission_id, received_at or any other bound column was changed after
insert fails closed even when the lookup key itself was changed to
match. This is an unkeyed checksum over canonical git_format.dumps_record
bytes, NOT authentication: it detects accidental or unsophisticated
row drift, and it cannot protect against a malicious database
administrator who can recompute the checksum.
"""
import hashlib
import sqlite3
from threading import RLock

from app.facts_tree import git_format

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_candidate_submissions (
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_blob BLOB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    receipt_blob BLOB NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (company_id, job_id, request_id)
);

-- Append-only enforcement at the database level. P01 section 4 items
-- 1-4: once a request is durably received its receipt is never
-- rewritten; only an exact replay of the same request identity with a
-- byte-identical payload returns the original receipt. UPDATE and
-- DELETE of a recorded receipt row are rejected outright.
CREATE TRIGGER IF NOT EXISTS fact_candidate_submissions_reject_update
BEFORE UPDATE ON fact_candidate_submissions
BEGIN
    SELECT RAISE(ABORT,
        'fact_candidate_submissions is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS fact_candidate_submissions_reject_delete
BEFORE DELETE ON fact_candidate_submissions
BEGIN
    SELECT RAISE(ABORT,
        'fact_candidate_submissions is append-only: DELETE rejected');
END;

-- INSERT OR REPLACE on an already-recorded request identity would
-- delete the original receipt row and insert a replacement (the REPLACE
-- conflict resolution does not fire the DELETE trigger unless recursive
-- triggers are enabled), silently overwriting the durable receipt. The
-- BEFORE INSERT guard runs before conflict resolution while the old row
-- still exists, so it rejects the statement and the original row stays
-- untouched. A first-time insert on a fresh key is unaffected.
CREATE TRIGGER IF NOT EXISTS fact_candidate_submissions_reject_replace
BEFORE INSERT ON fact_candidate_submissions
WHEN EXISTS (
    SELECT 1 FROM fact_candidate_submissions
    WHERE company_id = NEW.company_id
      AND job_id = NEW.job_id
      AND request_id = NEW.request_id
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_candidate_submissions is append-only: request identity already recorded; refusing to overwrite the original receipt');
END;
"""

_SAVEPOINT = "sam_candidate_submission_write"


class SubmissionConflict(Exception):
    """Same (company_id, job_id, request_id) reused with a different payload."""


class CandidateSubmissionStore:
    """Append-only receipt store for candidate submissions.

    See the module docstring for the transaction contract, caller
    obligations and the explicit list of what this slice does not do.
    """

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "candidate submission store requires a caller-provided "
                "sqlite3.Connection")
        # Checked before any DDL: a caller transaction is left untouched,
        # never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "candidate submission store requires an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _require_id(name, value):
        # git_format.encode_id enforces the host-issued ID bounds: str,
        # non-empty, length-capped. Its result is not needed for storage;
        # this is pure validation of the boundary values.
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s is not a valid host-issued id: %s"
                             % (name, exc)) from exc
        return value

    @staticmethod
    def _require_received_at(value):
        if not isinstance(value, str) or not value:
            raise ValueError("received_at must be a non-empty host string")
        return value

    @staticmethod
    def _canonical(name, value):
        if not isinstance(value, dict):
            raise TypeError(
                "%s must be a JSON object (dict); this store keeps payload "
                "and receipt as opaque host objects" % name)
        blob = git_format.dumps_record(value)
        return blob, hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _row_digest(company_id, job_id, request_id, submission_id,
                    received_at, payload_sha256, receipt_sha256):
        """Internal row-integrity digest over the host envelope identity.

        The checksum input is the canonical git_format.dumps_record bytes
        of a fixed dict binding every host-owned identity/metadata column
        and both validated content digests, so changing any one of them
        after insert changes the expected digest. This is an unkeyed
        checksum for drift detection only; it is not authentication and
        does not protect against a malicious DB administrator.
        """
        return hashlib.sha256(git_format.dumps_record({
            "company_id": company_id,
            "job_id": job_id,
            "request_id": request_id,
            "submission_id": submission_id,
            "received_at": received_at,
            "payload_sha256": payload_sha256,
            "receipt_sha256": receipt_sha256,
        })).hexdigest()

    # -- write path -----------------------------------------------------------

    def record_submission(self, company_id, job_id, request_id, submission_id,
                          received_at, payload, receipt):
        """Persist one submission snapshot and receipt, or replay it.

        All identity values come from the host as separate arguments; the
        candidate payload is never consulted for them. Returns a dict with
        the durable receipt and replayed=False on first receipt. The exact
        same (company_id, job_id, request_id) with a byte-identical
        canonical payload returns the original durable receipt with
        replayed=True and writes nothing. The same key with a different
        canonical payload raises SubmissionConflict and leaves every prior
        row unchanged.

        Requires an active caller transaction; see the module docstring.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        request_id = self._require_id("request_id", request_id)
        submission_id = self._require_id("submission_id", submission_id)
        received_at = self._require_received_at(received_at)
        payload_blob, payload_sha256 = self._canonical("payload", payload)
        receipt_blob, receipt_sha256 = self._canonical("receipt", receipt)

        if not self.db.in_transaction:
            raise ValueError(
                "record_submission requires an active caller transaction; "
                "this store never begins, commits or rolls back the caller "
                "transaction itself")

        with self.lock:
            self.db.execute("SAVEPOINT " + _SAVEPOINT)
            try:
                result = self._record_core(
                    company_id, job_id, request_id, submission_id,
                    received_at, payload_blob, payload_sha256, receipt_blob,
                    receipt_sha256)
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
                self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
                raise
            self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            return result

    def _record_core(self, company_id, job_id, request_id, submission_id,
                     received_at, payload_blob, payload_sha256, receipt_blob,
                     receipt_sha256):
        existing = self._fetch_row(company_id, job_id, request_id)
        if existing is not None:
            stored = self._load_row(existing)
            if stored["payload_sha256"] != payload_sha256 \
                    or stored["payload"] != git_format.loads_record(payload_blob):
                raise SubmissionConflict(
                    "request identity (%r, %r, %r) was already recorded with "
                    "different payload content; refusing to overwrite the "
                    "original durable receipt"
                    % (company_id, job_id, request_id))
            # Exact replay: the original submission_id, received_at and
            # receipt are returned as first durably recorded, never replaced.
            stored["replayed"] = True
            return stored
        self.db.execute(
            "INSERT INTO fact_candidate_submissions "
            "(company_id, job_id, request_id, submission_id, received_at, "
            "payload_blob, payload_sha256, receipt_blob, receipt_sha256, "
            "row_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, job_id, request_id, submission_id, received_at,
             payload_blob, payload_sha256, receipt_blob, receipt_sha256,
             self._row_digest(company_id, job_id, request_id, submission_id,
                              received_at, payload_sha256, receipt_sha256)))
        return {
            "company_id": company_id,
            "job_id": job_id,
            "request_id": request_id,
            "submission_id": submission_id,
            "received_at": received_at,
            "payload_sha256": payload_sha256,
            "payload": git_format.loads_record(payload_blob),
            "receipt_sha256": receipt_sha256,
            "receipt": git_format.loads_record(receipt_blob),
            "replayed": False,
        }

    # -- read path ------------------------------------------------------------

    def get_submission(self, company_id, job_id, request_id):
        """Return the durable receipt for one exact scope, or None.

        Exact-scope only: no prefix or cross-company lookup exists. When a
        row is present both blobs are re-parsed from their canonical bytes,
        both SHA-256 digests are re-verified and the stored scope/identity
        columns are checked against the queried key; any inconsistency is a
        hard failure (ValueError), never a silent repair.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        request_id = self._require_id("request_id", request_id)
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                row = self._fetch_row(company_id, job_id, request_id)
                result = self._load_row(row) if row is not None else None
            finally:
                if owns_snapshot:
                    # Read-only snapshot owned by this store: close it
                    # with ROLLBACK on success and failure alike. A
                    # caller-owned transaction is never touched.
                    self.db.rollback()
            if result is not None:
                result["replayed"] = False
            return result

    def _fetch_row(self, company_id, job_id, request_id):
        return self.db.execute(
            "SELECT company_id, job_id, request_id, submission_id, "
            "received_at, payload_blob, payload_sha256, receipt_blob, "
            "receipt_sha256, row_sha256 FROM fact_candidate_submissions "
            "WHERE company_id=? AND job_id=? AND request_id=?",
            (company_id, job_id, request_id)).fetchone()

    @staticmethod
    def _load_row(row):
        (company_id, job_id, request_id, submission_id, received_at,
         payload_blob, payload_sha256, receipt_blob, receipt_sha256,
         row_sha256) = row
        try:
            payload = git_format.loads_record(payload_blob)
            receipt = git_format.loads_record(receipt_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored candidate submission blobs failed canonical "
                "re-validation; refusing to read possibly tampered data") \
                from exc
        if hashlib.sha256(payload_blob).hexdigest() != payload_sha256:
            raise ValueError(
                "stored payload digest does not match the stored payload; "
                "refusing to read possibly tampered data")
        if hashlib.sha256(receipt_blob).hexdigest() != receipt_sha256:
            raise ValueError(
                "stored receipt digest does not match the stored receipt; "
                "refusing to read possibly tampered data")
        # A matching digest over parseable bytes is not enough: the stored
        # bytes must be exactly the canonical serialization of the parsed
        # value, so any non-canonical-but-valid rewrite fails closed.
        if git_format.dumps_record(payload) != bytes(payload_blob):
            raise ValueError(
                "stored payload bytes are not the exact canonical record "
                "format; refusing to read possibly tampered data")
        if git_format.dumps_record(receipt) != bytes(receipt_blob):
            raise ValueError(
                "stored receipt bytes are not the exact canonical record "
                "format; refusing to read possibly tampered data")
        for name, value in (("company_id", company_id), ("job_id", job_id),
                            ("request_id", request_id),
                            ("submission_id", submission_id)):
            git_format.encode_id(value)  # stored identity must stay in bounds
        if not isinstance(received_at, str) or not received_at:
            raise ValueError(
                "stored received_at is not a non-empty host string; "
                "refusing to read possibly tampered data")
        # Recompute the row-integrity digest over every host envelope
        # identity/metadata column plus both validated content digests and
        # compare it with the stored value. Any post-insert change to one
        # of these columns fails closed even when the lookup key itself
        # was changed to the mutated scope. Unkeyed checksum only; not
        # authentication against a malicious DB administrator.
        if CandidateSubmissionStore._row_digest(
                company_id, job_id, request_id, submission_id, received_at,
                payload_sha256, receipt_sha256) != row_sha256:
            raise ValueError(
                "stored row-integrity digest does not match the stored "
                "identity/metadata columns; refusing to read possibly "
                "tampered data")
        return {
            "company_id": company_id,
            "job_id": job_id,
            "request_id": request_id,
            "submission_id": submission_id,
            "received_at": received_at,
            "payload_sha256": payload_sha256,
            "payload": payload,
            "receipt_sha256": receipt_sha256,
            "receipt": receipt,
        }
