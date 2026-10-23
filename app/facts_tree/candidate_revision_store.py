"""Runtime work-store primitive for F-001 candidate revision CAS.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 4 (candidate.revise) and section 10 (runtime work
store). This module is a small standalone SQLite persistence primitive
for the *runtime work store* only: it appends complete replacement
payloads for one host-issued candidate identity as numbered revisions,
keeps every old revision readable, enforces compare-and-swap on
expected_revision and provides request-idempotent replay. Per P01
section 10 the runtime work store keeps uncommitted candidate state
reliably, but it is NOT the formal fact Git repository and NOT the
rebuildable query projection; a row here only means "this payload was
durably received", never that any candidate entered the formal tree.
The CAS enforced here is only the work-store revision counter: per P01
sections 9.2 and 10.5.2 the Commit Broker must re-check the current
candidate revision and digest in its final gate before accepting a
plan, and this store cannot prove that a revision has not already been
formally consumed.

Scope of this slice (explicitly not implemented here): task-permission
checks, host envelope construction, schema / source-grant / evidence
validation of the payload, full-text coverage manifests, candidate
consumption records, HTTP/API entry points, formal Git operations,
query projections and any business-acceptance state.

Caller obligations (this store does NOT do these):

- The caller must have verified the current task permission required
  for candidate.revise (P01 section 4) before calling revise_candidate,
  and must have validated the replacement payload per P01 sections 3
  and 7. company_id / job_id / candidate_uid / request_id are
  host-owned identity values passed as separate arguments; they are
  never read out of the payload, so a payload field named "company_id"
  (or similar) can never set stored identity.
- Nothing here authenticates the actor, company_id or job_id, and
  nothing here proves policy or delegation-chain validity, grant
  validity, current revocation state or authorization credentials.
  There is no current SAM-APP source for P01 actor_id / policy
  revision / delegation chain / authorization credential references;
  this module does not fabricate or default them.

Transaction contract (same documented choice as the sibling
candidate_work_store module):

- The constructor takes only a caller-provided sqlite3.Connection. It
  never opens a database, never chooses a data path and never migrates
  app.db. While the connection is idle it creates its own table and
  index; construction with an already-active caller transaction is
  refused.
- create_candidate and revise_candidate REQUIRE an active caller
  transaction and join it: all writes run inside one SAVEPOINT. On
  success the savepoint is released; on any exception the store rolls
  back only to that savepoint, releases it and re-raises, so a failed
  call leaves no partial rows and the caller transaction stays open.
  This store never issues BEGIN/COMMIT/ROLLBACK against the caller
  transaction.
- get_current, get_revision and list_revisions are read-only and never
  issue COMMIT; when the connection is idle they open their own short
  read snapshot and always close it with ROLLBACK, on success and on
  failure alike. When the caller already holds a transaction this store
  reads through it and never closes, commits or rolls it back.

Compare-and-swap: revise_candidate succeeds only when expected_revision
equals the candidate's current stored revision; on success it appends
exactly one row with revision expected_revision + 1 and never
overwrites or deletes an old revision. A stale expected_revision fails
closed with StaleCandidate and leaves every row unchanged; an unknown
candidate fails closed with UnknownCandidate. create_candidate creates
exactly revision 1 and fails closed with CandidateExists when the
candidate identity already has revisions under a different request
identity.

Append-only durability: the table itself enforces the same rule with
database-level guards (P01 section 10.2.2: historical revisions are
append-only, never rewritten in place or deleted). BEFORE UPDATE and
BEFORE DELETE triggers reject any rewrite or removal of stored
revisions, and a BEFORE INSERT trigger rejects an insert whose primary
key or (company_id, job_id, request_id) request key already exists, so
an INSERT OR REPLACE can never make SQLite silently delete the old row
before writing the new one (the implicit REPLACE delete does not fire
the BEFORE DELETE trigger while recursive_triggers is off, hence the
insert-side check). The Python-level checks stay in place; the triggers
are the last line of defence even for SQL that bypasses this module.

Idempotency: the key is (company_id, job_id, request_id), one request
identity per operation. A replayed call with the same key and
byte-identical canonical input (candidate_uid and payload for create;
candidate_uid, expected_revision and payload for revise) returns the
originally appended revision with replayed=True and writes nothing,
even when newer revisions have since been appended. The same key with
a different candidate_uid, revision position or canonical payload
fails closed with RevisionConflict and all prior rows stay unchanged.

Tamper resistance: each revision payload is stored as canonical
git_format bytes plus its SHA-256 digest. Reads re-parse the blob
through git_format.loads_record, require the stored bytes to equal the
exact canonical git_format.dumps_record serialization of the parsed
value (parseable-with-matching-digest is not enough), and re-verify
the digest and the stored scope/identity columns; any drift is a hard
failure, never a silent repair. Each row also carries an internal
row-integrity digest (row_sha256) that binds all host-owned
identity/metadata columns (company_id, job_id, candidate_uid,
revision, request_id, recorded_at) together with the validated payload
digest; it is stored at insert time and recomputed and compared on
every read before returning, so a row whose identity columns were
changed after insert fails closed even when the lookup key itself was
changed to match. This is an unkeyed checksum over canonical
git_format.dumps_record bytes, NOT authentication: it detects
accidental or unsophisticated row drift, and it cannot protect against
a malicious database administrator who can recompute the checksum.
"""
import hashlib
import sqlite3
from threading import RLock

from app.facts_tree import git_format

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_candidate_revisions (
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    candidate_uid TEXT NOT NULL,
    revision INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_blob BLOB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (company_id, job_id, candidate_uid, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS fact_candidate_revisions_request
    ON fact_candidate_revisions (company_id, job_id, request_id);
CREATE TRIGGER IF NOT EXISTS fact_candidate_revisions_no_update
BEFORE UPDATE ON fact_candidate_revisions
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_revisions is append-only: stored revisions cannot be updated');
END;
CREATE TRIGGER IF NOT EXISTS fact_candidate_revisions_no_delete
BEFORE DELETE ON fact_candidate_revisions
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_revisions is append-only: stored revisions cannot be deleted');
END;
-- INSERT OR REPLACE on a conflicting key would make SQLite delete the
-- old row before inserting the new one, and that implicit delete does
-- not fire the BEFORE DELETE trigger while recursive_triggers is off
-- (the SQLite default). BEFORE INSERT runs before any conflict
-- resolution, so checking for an existing primary key or request key
-- here makes REPLACE unable to overwrite stored history.
CREATE TRIGGER IF NOT EXISTS fact_candidate_revisions_no_replace
BEFORE INSERT ON fact_candidate_revisions
WHEN EXISTS (
    SELECT 1 FROM fact_candidate_revisions
    WHERE (company_id = NEW.company_id AND job_id = NEW.job_id
           AND candidate_uid = NEW.candidate_uid
           AND revision = NEW.revision)
       OR (company_id = NEW.company_id AND job_id = NEW.job_id
           AND request_id = NEW.request_id)
)
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_revisions is append-only: INSERT OR REPLACE cannot overwrite an existing primary or request key');
END;
"""

_SAVEPOINT = "sam_candidate_revision_write"

_COLUMNS = ("company_id, job_id, candidate_uid, revision, request_id, "
            "recorded_at, payload_blob, payload_sha256, row_sha256")


class RevisionConflict(Exception):
    """Same (company_id, job_id, request_id) reused with different input."""


class StaleCandidate(Exception):
    """expected_revision does not match the candidate's current revision."""


class UnknownCandidate(Exception):
    """revise_candidate target has no stored revision in this scope."""


class CandidateExists(Exception):
    """create_candidate target already has revisions in this scope."""


class CandidateRevisionStore:
    """Append-only revision store for host-issued candidate identities.

    See the module docstring for the transaction contract, caller
    obligations and the explicit list of what this slice does not do.
    """

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "candidate revision store requires a caller-provided "
                "sqlite3.Connection")
        # Checked before any DDL: a caller transaction is left untouched,
        # never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "candidate revision store requires an idle connection at "
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
    def _require_revision(name, value):
        # bool is a subclass of int, so an explicit isinstance check for
        # bool must run first: True can never be a revision number at
        # this boundary.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                "%s must be an int revision number; bool is never a "
                "valid revision" % name)
        if value < 1:
            raise ValueError("%s must be a positive revision number" % name)
        return value

    @staticmethod
    def _require_recorded_at(value):
        if not isinstance(value, str) or not value:
            raise ValueError("recorded_at must be a non-empty host string")
        return value

    @staticmethod
    def _canonical(name, value):
        if not isinstance(value, dict):
            raise TypeError(
                "%s must be a JSON object (dict); this store keeps the "
                "replacement payload as an opaque host object" % name)
        blob = git_format.dumps_record(value)
        return blob, hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _row_digest(company_id, job_id, candidate_uid, revision, request_id,
                    recorded_at, payload_sha256):
        """Internal row-integrity digest over the host-owned identity.

        The checksum input is the canonical git_format.dumps_record bytes
        of a fixed dict binding every host-owned identity/metadata column
        and the validated content digest, so changing any one of them
        after insert changes the expected digest. This is an unkeyed
        checksum for drift detection only; it is not authentication and
        does not protect against a malicious DB administrator.
        """
        return hashlib.sha256(git_format.dumps_record({
            "company_id": company_id,
            "job_id": job_id,
            "candidate_uid": candidate_uid,
            "revision": revision,
            "request_id": request_id,
            "recorded_at": recorded_at,
            "payload_sha256": payload_sha256,
        })).hexdigest()

    # -- write path ---------------------------------------------------------

    def create_candidate(self, company_id, job_id, candidate_uid,
                         request_id, recorded_at, payload):
        """Persist the initial revision 1 for a new candidate, or replay.

        All identity values come from the host as separate arguments; the
        payload is never consulted for them. Returns a dict with the
        durable revision record and replayed=False on first receipt. The
        exact same (company_id, job_id, request_id) with the same
        candidate_uid and a byte-identical canonical payload returns the
        original durable revision with replayed=True and writes nothing.
        The same key with a different candidate_uid or canonical payload
        raises RevisionConflict and leaves every prior row unchanged. A
        candidate_uid that already has revisions under a different
        request identity raises CandidateExists.

        Requires an active caller transaction; see the module docstring.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        candidate_uid = self._require_id("candidate_uid", candidate_uid)
        request_id = self._require_id("request_id", request_id)
        recorded_at = self._require_recorded_at(recorded_at)
        payload_blob, payload_sha256 = self._canonical("payload", payload)

        def core():
            return self._create_core(company_id, job_id, candidate_uid,
                                     request_id, recorded_at, payload_blob,
                                     payload_sha256)
        return self._write(core)

    def revise_candidate(self, company_id, job_id, candidate_uid,
                         request_id, expected_revision, recorded_at,
                         payload):
        """Append the next revision under expected_revision CAS, or replay.

        Succeeds only when expected_revision equals the candidate's
        current stored revision; on success exactly one new row with
        revision expected_revision + 1 is appended and no old revision is
        overwritten or deleted. A stale expected_revision raises
        StaleCandidate and an unknown candidate raises UnknownCandidate;
        both leave every row unchanged. The exact same (company_id,
        job_id, request_id) with the same candidate_uid, expected_revision
        and a byte-identical canonical payload returns the originally
        appended revision with replayed=True and writes nothing, even
        when newer revisions have since been appended; the same key with
        different input raises RevisionConflict.

        Requires an active caller transaction; see the module docstring.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        candidate_uid = self._require_id("candidate_uid", candidate_uid)
        request_id = self._require_id("request_id", request_id)
        expected_revision = self._require_revision("expected_revision",
                                                   expected_revision)
        recorded_at = self._require_recorded_at(recorded_at)
        payload_blob, payload_sha256 = self._canonical("payload", payload)

        def core():
            return self._revise_core(company_id, job_id, candidate_uid,
                                     request_id, expected_revision,
                                     recorded_at, payload_blob,
                                     payload_sha256)
        return self._write(core)

    def _write(self, core):
        if not self.db.in_transaction:
            raise ValueError(
                "candidate revision writes require an active caller "
                "transaction; this store never begins, commits or rolls "
                "back the caller transaction itself")
        with self.lock:
            self.db.execute("SAVEPOINT " + _SAVEPOINT)
            try:
                result = core()
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
                self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
                raise
            self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            return result

    def _create_core(self, company_id, job_id, candidate_uid, request_id,
                     recorded_at, payload_blob, payload_sha256):
        existing = self._fetch_by_request(company_id, job_id, request_id)
        if existing is not None:
            stored = self._load_row(existing)
            if stored["candidate_uid"] != candidate_uid \
                    or stored["revision"] != 1 \
                    or stored["payload_sha256"] != payload_sha256 \
                    or stored["payload"] != git_format.loads_record(
                        payload_blob):
                raise RevisionConflict(
                    "request identity (%r, %r, %r) was already recorded "
                    "with different candidate or payload content; "
                    "refusing to overwrite the original durable revision"
                    % (company_id, job_id, request_id))
            # Exact replay: the original revision record is returned as
            # first durably recorded, never replaced.
            stored["replayed"] = True
            return stored
        current = self._fetch_current_row(company_id, job_id, candidate_uid)
        if current is not None:
            raise CandidateExists(
                "candidate %r already has stored revisions in scope "
                "(%r, %r); refusing to create revision 1 again under a "
                "new request identity" % (candidate_uid, company_id, job_id))
        self._insert_row(company_id, job_id, candidate_uid, 1, request_id,
                         recorded_at, payload_blob, payload_sha256)
        return self._result(company_id, job_id, candidate_uid, 1,
                            request_id, recorded_at, payload_blob,
                            payload_sha256)

    def _revise_core(self, company_id, job_id, candidate_uid, request_id,
                     expected_revision, recorded_at, payload_blob,
                     payload_sha256):
        # Idempotent replay is checked before the CAS: a retry of an
        # already-recorded request returns its original revision even
        # when newer revisions have since made expected_revision stale.
        existing = self._fetch_by_request(company_id, job_id, request_id)
        if existing is not None:
            stored = self._load_row(existing)
            if stored["candidate_uid"] != candidate_uid \
                    or stored["revision"] != expected_revision + 1 \
                    or stored["payload_sha256"] != payload_sha256 \
                    or stored["payload"] != git_format.loads_record(
                        payload_blob):
                raise RevisionConflict(
                    "request identity (%r, %r, %r) was already recorded "
                    "with different candidate, revision or payload "
                    "content; refusing to overwrite the original durable "
                    "revision" % (company_id, job_id, request_id))
            stored["replayed"] = True
            return stored
        current_row = self._fetch_current_row(company_id, job_id,
                                              candidate_uid)
        if current_row is None:
            raise UnknownCandidate(
                "candidate %r has no stored revision in scope (%r, %r); "
                "refusing to revise an unknown candidate"
                % (candidate_uid, company_id, job_id))
        current = self._load_row(current_row)
        if current["revision"] != expected_revision:
            raise StaleCandidate(
                "expected_revision %r does not match the current stored "
                "revision %r for candidate %r; refusing to append on a "
                "stale base" % (expected_revision, current["revision"],
                                candidate_uid))
        new_revision = expected_revision + 1
        self._insert_row(company_id, job_id, candidate_uid, new_revision,
                         request_id, recorded_at, payload_blob,
                         payload_sha256)
        return self._result(company_id, job_id, candidate_uid, new_revision,
                            request_id, recorded_at, payload_blob,
                            payload_sha256)

    def _insert_row(self, company_id, job_id, candidate_uid, revision,
                    request_id, recorded_at, payload_blob, payload_sha256):
        self.db.execute(
            "INSERT INTO fact_candidate_revisions "
            "(company_id, job_id, candidate_uid, revision, request_id, "
            "recorded_at, payload_blob, payload_sha256, row_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, job_id, candidate_uid, revision, request_id,
             recorded_at, payload_blob, payload_sha256,
             self._row_digest(company_id, job_id, candidate_uid, revision,
                              request_id, recorded_at, payload_sha256)))

    @staticmethod
    def _result(company_id, job_id, candidate_uid, revision, request_id,
                recorded_at, payload_blob, payload_sha256):
        return {
            "company_id": company_id,
            "job_id": job_id,
            "candidate_uid": candidate_uid,
            "revision": revision,
            "request_id": request_id,
            "recorded_at": recorded_at,
            "payload_sha256": payload_sha256,
            "payload": git_format.loads_record(payload_blob),
            "replayed": False,
        }

    # -- read path ----------------------------------------------------------

    def get_current(self, company_id, job_id, candidate_uid):
        """Return the candidate's current revision record, or None.

        Exact-scope only: no prefix or cross-company lookup exists. The
        stored blob is re-parsed from its canonical bytes, its SHA-256
        digest is re-verified and the stored identity columns are checked
        through the row-integrity digest; any inconsistency is a hard
        failure (ValueError), never a silent repair.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        candidate_uid = self._require_id("candidate_uid", candidate_uid)

        def fetch():
            row = self._fetch_current_row(company_id, job_id, candidate_uid)
            return self._load_row(row) if row is not None else None
        result = self._read(fetch)
        if result is not None:
            result["replayed"] = False
        return result

    def get_revision(self, company_id, job_id, candidate_uid, revision):
        """Return one exact historical revision record, or None.

        Old revisions stay readable forever; this store never rewrites
        or deletes them. The same re-validation as get_current applies.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        candidate_uid = self._require_id("candidate_uid", candidate_uid)
        revision = self._require_revision("revision", revision)

        def fetch():
            row = self.db.execute(
                "SELECT " + _COLUMNS + " FROM fact_candidate_revisions "
                "WHERE company_id=? AND job_id=? AND candidate_uid=? "
                "AND revision=?",
                (company_id, job_id, candidate_uid, revision)).fetchone()
            return self._load_row(row) if row is not None else None
        result = self._read(fetch)
        if result is not None:
            result["replayed"] = False
        return result

    def list_revisions(self, company_id, job_id, candidate_uid):
        """Return all stored revisions of one candidate, oldest first.

        Returns an empty list when the candidate has no stored revision
        in this exact scope. Every row passes the same re-validation as
        get_current before being returned.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        candidate_uid = self._require_id("candidate_uid", candidate_uid)

        def fetch():
            rows = self.db.execute(
                "SELECT " + _COLUMNS + " FROM fact_candidate_revisions "
                "WHERE company_id=? AND job_id=? AND candidate_uid=? "
                "ORDER BY revision ASC",
                (company_id, job_id, candidate_uid)).fetchall()
            return [self._load_row(row) for row in rows]
        results = self._read(fetch)
        for result in results:
            result["replayed"] = False
        return results

    def _read(self, fetch):
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                return fetch()
            finally:
                if owns_snapshot:
                    # Read-only snapshot owned by this store: close it
                    # with ROLLBACK on success and failure alike. A
                    # caller-owned transaction is never touched.
                    self.db.rollback()

    def _fetch_by_request(self, company_id, job_id, request_id):
        return self.db.execute(
            "SELECT " + _COLUMNS + " FROM fact_candidate_revisions "
            "WHERE company_id=? AND job_id=? AND request_id=?",
            (company_id, job_id, request_id)).fetchone()

    def _fetch_current_row(self, company_id, job_id, candidate_uid):
        return self.db.execute(
            "SELECT " + _COLUMNS + " FROM fact_candidate_revisions "
            "WHERE company_id=? AND job_id=? AND candidate_uid=? "
            "ORDER BY revision DESC LIMIT 1",
            (company_id, job_id, candidate_uid)).fetchone()

    @staticmethod
    def _load_row(row):
        (company_id, job_id, candidate_uid, revision, request_id,
         recorded_at, payload_blob, payload_sha256, row_sha256) = row
        try:
            payload = git_format.loads_record(payload_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored candidate revision blob failed canonical "
                "re-validation; refusing to read possibly tampered data") \
                from exc
        if hashlib.sha256(payload_blob).hexdigest() != payload_sha256:
            raise ValueError(
                "stored payload digest does not match the stored payload; "
                "refusing to read possibly tampered data")
        # A matching digest over parseable bytes is not enough: the stored
        # bytes must be exactly the canonical serialization of the parsed
        # value, so any non-canonical-but-valid rewrite fails closed.
        if git_format.dumps_record(payload) != bytes(payload_blob):
            raise ValueError(
                "stored payload bytes are not the exact canonical record "
                "format; refusing to read possibly tampered data")
        for value in (company_id, job_id, candidate_uid, request_id):
            git_format.encode_id(value)  # stored identity must stay in bounds
        if isinstance(revision, bool) or not isinstance(revision, int) \
                or revision < 1:
            raise ValueError(
                "stored revision is not a positive int; refusing to read "
                "possibly tampered data")
        if not isinstance(recorded_at, str) or not recorded_at:
            raise ValueError(
                "stored recorded_at is not a non-empty host string; "
                "refusing to read possibly tampered data")
        # Recompute the row-integrity digest over every host-owned
        # identity/metadata column plus the validated content digest and
        # compare it with the stored value. Any post-insert change to one
        # of these columns fails closed even when the lookup key itself
        # was changed to the mutated scope. Unkeyed checksum only; not
        # authentication against a malicious DB administrator.
        if CandidateRevisionStore._row_digest(
                company_id, job_id, candidate_uid, revision, request_id,
                recorded_at, payload_sha256) != row_sha256:
            raise ValueError(
                "stored row-integrity digest does not match the stored "
                "identity/metadata columns; refusing to read possibly "
                "tampered data")
        return {
            "company_id": company_id,
            "job_id": job_id,
            "candidate_uid": candidate_uid,
            "revision": revision,
            "request_id": request_id,
            "recorded_at": recorded_at,
            "payload_sha256": payload_sha256,
            "payload": payload,
        }
