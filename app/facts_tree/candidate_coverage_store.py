"""Runtime work-store primitive for F-001 full-text coverage manifests.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 4 (candidate intake and full-text coverage: the
immutable candidate / coverage manifest of item 5) and section 10
(reliable persistence of the runtime work store). This module is a
small standalone SQLite persistence primitive for the *runtime work
store* only: it durably persists host-generated coverage manifest
snapshots built by
app.facts_tree.candidate_coverage.build_coverage_manifest, keyed by
content digest, with exact replay of an already-persisted snapshot.
Per P01 section 10 the runtime work store keeps uncommitted candidate
state reliably, but it is NOT the formal fact Git repository and NOT
the rebuildable query projection; a row here only means "this coverage
snapshot was durably persisted", never that any candidate entered the
formal tree. Persisting a manifest whose coverage_complete flag is
false only means a gapped state was durably recorded; it never means
the full-text review finished.

Scope of this slice (explicitly not implemented here):

- No actor, company or job authentication and no task-permission,
  policy, delegation-chain, revocation-state or authorization-credential
  verification; company_id / job_id / recorded_at are host-owned values
  passed as separate arguments. There is no current SAM-APP source for
  P01 actor_id / policy revision / delegation chain / authorization
  credential references; this module does not fabricate or default
  them.
- No verification that any read window was genuinely granted, genuinely
  read or genuinely reviewed, and no fact-candidate/1 schema, citation,
  quote, attribution or source-grant validation; those belong to
  candidate_schema / candidate_evidence / candidate_coverage before the
  manifest is persisted. The coverage algorithm itself is never
  re-implemented here; this store only re-serializes the supplied
  record to verify its digest.
- No decision about whether the candidate manager may proceed with the
  covered batch, and no HTTP/API entry point, legacy reading entry
  point, formal Git operation, Commit-Broker or query-projection
  integration.

Transaction contract (same documented choice as the sibling
candidate_work_store and candidate_revision_store modules):

- The constructor takes only a caller-provided sqlite3.Connection. It
  never opens a database, never chooses a data path and never migrates
  app.db. While the connection is idle it creates its own table;
  construction with an already-active caller transaction is refused.
- record_manifest REQUIRES an active caller transaction and joins it:
  all writes run inside one SAVEPOINT. On success the savepoint is
  released; on any exception the store rolls back only to that
  savepoint, releases it and re-raises, so a failed call leaves no
  partial rows and the caller transaction stays open. This store never
  issues BEGIN/COMMIT/ROLLBACK against the caller transaction.
- get_manifest is read-only and never issues COMMIT; when the
  connection is idle it opens its own short read snapshot and always
  closes it with ROLLBACK, on success and on failure alike. When the
  caller already holds a transaction this store reads through it and
  never closes, commits or rolls it back.

Content addressing: the key is (company_id, job_id, manifest_digest)
where manifest_digest is the SHA-256 of the canonical
git_format.dumps_record bytes of the manifest record. Multiple
snapshots with different digests coexist under one scope. A later call
with the same key and byte-identical canonical manifest returns the
original recorded_at and the original snapshot with replayed=True and
writes nothing; the same content digest resolving to different bytes
fails closed with CoverageManifestConflict and all prior rows stay
unchanged. The API is immutable: insert and read only; no update or
delete operation exists. Database-level triggers enforce the same floor
outside this API: BEFORE UPDATE / BEFORE DELETE triggers reject any
rewrite of a persisted snapshot, and a BEFORE INSERT trigger rejects an
INSERT over an existing content-addressed key, so INSERT OR REPLACE
cannot silently delete and replace an old snapshot. A genuinely new
digest inserts normally.

Input strictness: the manifest argument must be exactly the result
object of candidate_coverage.build_coverage_manifest — a dict with
exactly the keys "record" and "digest". This store re-computes the
canonical git_format.dumps_record bytes of "record" and their SHA-256
and requires them to equal the supplied "digest"; a non-canonical
record or a mismatched digest is rejected and nothing is stored.

Tamper resistance: the manifest is stored as canonical git_format
bytes plus its SHA-256 digest. Reads re-parse the blob through
git_format.loads_record, require the stored bytes to equal the exact
canonical git_format.dumps_record serialization of the parsed value
(parseable-with-matching-digest is not enough), and re-verify the
blob digest, the content-addressing primary key and the stored scope
columns; any drift is a hard failure, never a silent repair. Each row
also carries an internal row-integrity digest (row_sha256) that binds
all host-owned identity/metadata columns (company_id, job_id,
manifest_digest, recorded_at) together with the validated content
digest; it is stored at insert time and recomputed and compared on
every read before returning, so a row whose identity columns were
changed after insert fails closed even when the lookup key itself was
changed to match. This is an unkeyed checksum over canonical
git_format.dumps_record bytes, NOT authentication: it detects
accidental or unsophisticated row drift, and it cannot protect against
a malicious database administrator who can recompute the checksum.

Reads also re-apply the shared non-empty-windows floor of
_require_non_empty_windows (P01 section 4 item 5 / section 14.1), so a
historical row persisted before the write-side guard existed fails
closed instead of returning a hollow coverage_complete manifest.
"""
import hashlib
import sqlite3
from threading import RLock

from app.facts_tree import git_format

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_candidate_coverage_manifests (
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    manifest_blob BLOB NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (company_id, job_id, manifest_digest)
);

CREATE TRIGGER IF NOT EXISTS fact_candidate_coverage_manifests_no_update
BEFORE UPDATE ON fact_candidate_coverage_manifests
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_coverage_manifests is immutable: UPDATE of a persisted snapshot is rejected');
END;

CREATE TRIGGER IF NOT EXISTS fact_candidate_coverage_manifests_no_delete
BEFORE DELETE ON fact_candidate_coverage_manifests
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_coverage_manifests is immutable: DELETE of a persisted snapshot is rejected');
END;

CREATE TRIGGER IF NOT EXISTS fact_candidate_coverage_manifests_no_replace
BEFORE INSERT ON fact_candidate_coverage_manifests
WHEN EXISTS (
    SELECT 1 FROM fact_candidate_coverage_manifests
    WHERE company_id = NEW.company_id
      AND job_id = NEW.job_id
      AND manifest_digest = NEW.manifest_digest
)
BEGIN
    SELECT RAISE(ABORT, 'fact_candidate_coverage_manifests is immutable: INSERT over an existing content-addressed key would overwrite a persisted snapshot');
END;
"""

_SAVEPOINT = "sam_candidate_coverage_write"

_HEX_DIGEST_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_hex_digest(value):
    return isinstance(value, str) and len(value) == _HEX_DIGEST_LENGTH \
        and all(ch in _HEX_DIGITS for ch in value)


def _require_non_empty_windows(record):
    """Shared semantic floor for one manifest record, on both paths.

    P01 section 4 item 5 / section 14.1: zero windows can never prove
    file-level full-text coverage, so a manifest record whose "windows"
    is missing, not a list or empty is refused even when every digest
    matches. This is deliberately NOT a full manifest schema
    re-validation: it only closes the empty-windows bypass, on the write
    path and again when reading back a stored row, so a historical row
    persisted before the write-side guard existed fails closed instead
    of returning a hollow coverage_complete manifest.
    """
    windows = record.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError(
            "manifest record windows must be a non-empty list: zero "
            "windows can never prove file-level full-text coverage "
            "(P01 section 4 item 5 / section 14.1); refusing a hollow "
            "coverage manifest")


class CoverageManifestConflict(Exception):
    """One content digest resolved to different canonical bytes."""


class CandidateCoverageManifestStore:
    """Immutable content-addressed store for coverage manifest snapshots.

    See the module docstring for the transaction contract, caller
    obligations and the explicit list of what this slice does not do.
    """

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "candidate coverage manifest store requires a "
                "caller-provided sqlite3.Connection")
        # Checked before any DDL: a caller transaction is left untouched,
        # never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "candidate coverage manifest store requires an idle "
                "connection at construction; refusing to join or commit a "
                "caller transaction")
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
    def _require_digest(name, value):
        if not _is_hex_digest(value):
            raise ValueError(
                "%s must be a 64-character lowercase hex SHA-256 digest"
                % name)
        return value

    @staticmethod
    def _require_recorded_at(value):
        if not isinstance(value, str) or not value:
            raise ValueError("recorded_at must be a non-empty host string")
        return value

    @staticmethod
    def _canonical_manifest(manifest):
        if not isinstance(manifest, dict):
            raise TypeError(
                "manifest must be the dict result of "
                "candidate_coverage.build_coverage_manifest, got %s"
                % type(manifest).__name__)
        # A non-string key is a structural type error and must be reported
        # before any key-set comparison or sorting touches it.
        for key in manifest:
            if not isinstance(key, str):
                raise TypeError(
                    "manifest keys must be str, got %s"
                    % type(key).__name__)
        if set(manifest) != {"record", "digest"}:
            raise ValueError(
                "manifest must have exactly the keys ['digest', 'record'] "
                "of candidate_coverage.build_coverage_manifest; got %s"
                % sorted(manifest))
        record = manifest["record"]
        if not isinstance(record, dict):
            raise TypeError(
                "manifest record must be a JSON object (dict); this store "
                "keeps the manifest as an opaque host object")
        blob = git_format.dumps_record(record)
        digest = hashlib.sha256(blob).hexdigest()
        # The supplied digest must match the SHA-256 of the re-serialized
        # canonical record bytes; non-canonical or digest-mismatched input
        # is never stored.
        if manifest["digest"] != digest:
            raise ValueError(
                "supplied manifest digest does not match the SHA-256 of "
                "the re-serialized canonical record; refusing to persist "
                "non-canonical or digest-mismatched data")
        # Shared semantic floor (see _require_non_empty_windows): even a
        # digest-correct record with an empty windows list is refused
        # here, before any SAVEPOINT or database write.
        _require_non_empty_windows(record)
        return blob, digest

    @staticmethod
    def _row_digest(company_id, job_id, manifest_digest, recorded_at,
                    manifest_sha256):
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
            "manifest_digest": manifest_digest,
            "recorded_at": recorded_at,
            "manifest_sha256": manifest_sha256,
        })).hexdigest()

    # -- write path ---------------------------------------------------------

    def record_manifest(self, company_id, job_id, recorded_at, manifest):
        """Persist one coverage manifest snapshot, or replay it.

        All identity values come from the host as separate arguments;
        manifest must be the exact result object of
        candidate_coverage.build_coverage_manifest and is re-validated
        against its own digest before anything is stored. Returns a dict
        with the durable snapshot and replayed=False on first receipt.
        The exact same (company_id, job_id, manifest_digest) with
        byte-identical canonical bytes returns the original recorded_at
        and the original snapshot with replayed=True and writes nothing.
        The same content digest resolving to different bytes raises
        CoverageManifestConflict and leaves every prior row unchanged.

        Requires an active caller transaction; see the module docstring.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        recorded_at = self._require_recorded_at(recorded_at)
        manifest_blob, manifest_digest = self._canonical_manifest(manifest)

        if not self.db.in_transaction:
            raise ValueError(
                "record_manifest requires an active caller transaction; "
                "this store never begins, commits or rolls back the caller "
                "transaction itself")

        with self.lock:
            self.db.execute("SAVEPOINT " + _SAVEPOINT)
            try:
                result = self._record_core(company_id, job_id, recorded_at,
                                           manifest_blob, manifest_digest)
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
                self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
                raise
            self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            return result

    def _record_core(self, company_id, job_id, recorded_at, manifest_blob,
                     manifest_digest):
        existing = self._fetch_row(company_id, job_id, manifest_digest)
        if existing is not None:
            if bytes(existing[4]) != bytes(manifest_blob):
                raise CoverageManifestConflict(
                    "content digest %r in scope (%r, %r) already addresses "
                    "different canonical bytes; refusing to overwrite the "
                    "original snapshot"
                    % (manifest_digest, company_id, job_id))
            stored = self._load_row(existing)
            # Exact replay: the original recorded_at and snapshot are
            # returned as first durably recorded, never replaced.
            stored["replayed"] = True
            return stored
        self.db.execute(
            "INSERT INTO fact_candidate_coverage_manifests "
            "(company_id, job_id, manifest_digest, recorded_at, "
            "manifest_blob, manifest_sha256, row_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, job_id, manifest_digest, recorded_at,
             manifest_blob, manifest_digest,
             self._row_digest(company_id, job_id, manifest_digest,
                              recorded_at, manifest_digest)))
        return self._result(company_id, job_id, recorded_at, manifest_blob,
                            manifest_digest)

    @staticmethod
    def _result(company_id, job_id, recorded_at, manifest_blob,
                manifest_digest):
        return {
            "company_id": company_id,
            "job_id": job_id,
            "manifest_digest": manifest_digest,
            "recorded_at": recorded_at,
            "manifest_sha256": manifest_digest,
            "manifest": git_format.loads_record(manifest_blob),
            "replayed": False,
        }

    # -- read path ----------------------------------------------------------

    def get_manifest(self, company_id, job_id, manifest_digest):
        """Return the durable snapshot for one exact scope, or None.

        Exact-scope only: no prefix or cross-company lookup exists. When
        a row is present the blob is re-parsed from its canonical bytes,
        its SHA-256 digest is re-verified against both the stored digest
        column and the content-addressing primary key, and the stored
        scope/identity columns are checked through the row-integrity
        digest; any inconsistency is a hard failure (ValueError), never
        a silent repair.
        """
        company_id = self._require_id("company_id", company_id)
        job_id = self._require_id("job_id", job_id)
        manifest_digest = self._require_digest("manifest_digest",
                                               manifest_digest)
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                row = self._fetch_row(company_id, job_id, manifest_digest)
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

    def _fetch_row(self, company_id, job_id, manifest_digest):
        return self.db.execute(
            "SELECT company_id, job_id, manifest_digest, recorded_at, "
            "manifest_blob, manifest_sha256, row_sha256 "
            "FROM fact_candidate_coverage_manifests "
            "WHERE company_id=? AND job_id=? AND manifest_digest=?",
            (company_id, job_id, manifest_digest)).fetchone()

    @staticmethod
    def _load_row(row):
        (company_id, job_id, manifest_digest, recorded_at, manifest_blob,
         manifest_sha256, row_sha256) = row
        try:
            manifest = git_format.loads_record(manifest_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored coverage manifest blob failed canonical "
                "re-validation; refusing to read possibly tampered data") \
                from exc
        if hashlib.sha256(manifest_blob).hexdigest() != manifest_sha256:
            raise ValueError(
                "stored manifest digest does not match the stored "
                "manifest; refusing to read possibly tampered data")
        if manifest_sha256 != manifest_digest:
            raise ValueError(
                "stored manifest digest column does not match the "
                "content-addressing primary key; refusing to read "
                "possibly tampered data")
        # A matching digest over parseable bytes is not enough: the stored
        # bytes must be exactly the canonical serialization of the parsed
        # value, so any non-canonical-but-valid rewrite fails closed.
        if git_format.dumps_record(manifest) != bytes(manifest_blob):
            raise ValueError(
                "stored manifest bytes are not the exact canonical record "
                "format; refusing to read possibly tampered data")
        # Same shared floor as the write path: a historical row persisted
        # before that guard existed can be fully digest-correct yet hold
        # an empty windows list; it must fail closed here instead of
        # returning a hollow coverage_complete manifest.
        _require_non_empty_windows(manifest)
        for value in (company_id, job_id):
            git_format.encode_id(value)  # stored identity must stay in bounds
        if not _is_hex_digest(manifest_digest):
            raise ValueError(
                "stored manifest digest is not a well-formed SHA-256 hex "
                "digest; refusing to read possibly tampered data")
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
        if CandidateCoverageManifestStore._row_digest(
                company_id, job_id, manifest_digest, recorded_at,
                manifest_sha256) != row_sha256:
            raise ValueError(
                "stored row-integrity digest does not match the stored "
                "identity/metadata columns; refusing to read possibly "
                "tampered data")
        return {
            "company_id": company_id,
            "job_id": job_id,
            "manifest_digest": manifest_digest,
            "recorded_at": recorded_at,
            "manifest_sha256": manifest_sha256,
            "manifest": manifest,
        }
