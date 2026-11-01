"""Append-only mapping-version index ledger for F-001 R-closure slice R2.

The immutable byte cabinet (:class:`app.storage.versioned_mapping_objects.
VersionedMappingObjectStore`) already keeps every historical mapping byte
content-addressed and create-only, but nothing records which *logical*
mapping source's Nth version points at which cabinet digest, so an old
version can be proven to have existed but not to have been *that source's*
version.  This module is the missing append-only registration book.  It is
a pure, separately-sited primitive: this slice wires it into no live write
or read path (that is the later R3 slice), no existing production module
imports it, and it never modifies an existing database table — the ledger
lives in its own small SQLite database under a caller-provided controlled
root.

Invariants (freeze appendix R2 §4):

- V1 append-only: a committed registration can never be UPDATE/DELETEd or
  silently replaced.  BEFORE UPDATE / BEFORE DELETE triggers and a BEFORE
  INSERT key-exists guard enforce this inside SQLite itself, mirroring the
  established in-repo hardness of ``candidate_work_store``; the ledger
  object exposes no rewrite or removal method at all.
- V2 monotone ledger-issued numbers: per (installation, company, logical
  source) the 1-based version number is issued inside a ``BEGIN
  IMMEDIATE`` transaction as max+1; callers can never name a version
  number.  Registering the same (source, digest) again is idempotent and
  returns the existing row without inserting; out-of-order, concurrent and
  replayed registrations can neither duplicate nor skip numbers.
- V3 no ghost indexes (core): before a registration is committed the
  candidate digest is read through the real byte cabinet's ``get`` (open
  plus read-time SHA-256 revalidation), and the measured byte size must
  equal the declared size.  A missing object, a digest mismatch, a size
  mismatch or an unsafe cabinet occupant all reject the registration
  before any ledger row is written.
- V4 old versions are never substituted: registering version N+1 never
  touches earlier rows (V1 makes that impossible at the storage layer);
  the read surface reports a later-deleted or corrupted object as an
  explicit gap and never returns another version's bytes.
- V6 single-company scope: the ledger database is created for one
  (installation_id, company_id) pair, recorded in a singleton meta row
  and re-checked every time the database is reopened; every call carries
  its declared scope and a foreign installation/company is refused before
  any read or write.  The ledger directory is opened O_NOFOLLOW/O_DIRECTORY,
  must be owned by the effective user and must not be group- or
  world-writable; the database file is 0600 in that 0700 directory.
- V8 stable, secret-free digest: the ledger's total digest uses the exact
  manifest rule (:func:`backup_manifest.compute_core_digest`, canonical
  record serialization plus SHA-256) over a payload of stable coordinates
  only — no absolute path, no wall-clock timestamp (``registered_at`` is
  non-digest metadata), no object bytes and therefore no content secret.
  Replaying the same registrations against two ledger locations yields
  the same digest.
- V9 fail closed: every structural check, cabinet verification, scope
  refusal and trigger refusal happens before/inside one transaction; any
  failure rolls back and raises, never leaving a success-shaped partial
  registration.
- V10 no external side effects: no network, model, medium, GC or object
  deletion; the cabinet is only read.

Object vocabulary aligns with the R1 closure (``OBJECT_MAPPING_VERSION``/
``STORAGE_MAPPING_OBJECTS`` there); a contract test pins the string
equality so the two cannot drift.  This module imports no backup_* module
beyond the pure digest helper, and nothing in app.storage, app.harness,
app.employees, broker or webapp imports it.

Threat model, like the candidate work store: the triggers and the
per-row ``row_sha256`` drift check defeat accidental rewriting and
unsophisticated SQLite-level tampering; a hostile database owner who can
recompute checksums and drop triggers is not in scope.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.facts_tree.git_format import dumps_record
from app.storage.versioned_mapping_objects import (
    ObjectMismatchError,
    ObjectMissingError,
    UnsafeObjectPathError,
    VersionedMappingObjectStore,
)

from .backup_manifest import DIGEST_ALGORITHM, compute_core_digest

SCHEMA = "sam.f001.mapping_version_index"
SCHEMA_VERSION = 1
_DB_SCHEMA_VERSION = 1

# Vocabulary kept character-identical to the R1 closure on purpose; a test
# asserts equality with backup_object_closure.OBJECT_MAPPING_VERSION and
# .STORAGE_MAPPING_OBJECTS so future consumers see one vocabulary.
OBJECT_TYPE_MAPPING_VERSION = "mapping_version"
STORAGE_CLASS_MAPPING_OBJECTS = "mapping_objects"

SOURCE_KIND_ONLINE = "online"
SOURCE_KIND_BACKFILL = "backfill"
_SOURCE_KINDS = frozenset((SOURCE_KIND_ONLINE, SOURCE_KIND_BACKFILL))

# Byte verdicts reuse the R1 closure's status words.
BYTE_PRESENT = "present"
BYTE_MISSING = "missing"
BYTE_CORRUPT = "corrupt"
BYTE_UNSAFE_PATH = "unsafe_path"

STATUS_REGISTERED = "registered"
STATUS_REPLAYED = "replayed"

_DB_FILENAME = "mapping-version-index.sqlite"
_DIR_MODE = 0o700
_FILE_MODE = 0o600

_DIGEST_HEX_RE = re.compile(r"\A[0-9a-f]{64}\Z")
# sha1 (40) or sha256 (64) git object ids, structural shape only.
_COMMIT_OID_RE = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")
_MAX_ID_CHARS = 256
_MAX_COORDINATE_CHARS = 512

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_DB_OPEN_FLAGS = os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC
# Winner of the create race is born private; losers follow the wait path.
_DB_CREATE_FLAGS = _DB_OPEN_FLAGS | os.O_CREAT | os.O_EXCL
# Bounded readiness wait for a concurrent first-constructor finishing the
# initialization of a fresh database file; never a busy-spin.
_INIT_WAIT_SECONDS = 10.0
_INIT_POLL_INTERVAL = 0.02
_PROC_FD = "/proc/self/fd"

# mkdir/lstat/chmod through dir_fd plus O_NOFOLLOW/O_DIRECTORY are what pin
# the no-symlink boundary; refuse to run where they are unavailable.
_DIR_FD_PRIMITIVES = (os.open, os.mkdir, os.lstat)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fact_mapping_version_index_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_mapping_version_registrations (
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    logical_source_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    object_type TEXT NOT NULL
        CHECK (object_type = 'mapping_version'),
    storage_class TEXT NOT NULL
        CHECK (storage_class = 'mapping_objects'),
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('online', 'backfill')),
    source_coordinate TEXT,
    h_commit_oid TEXT,
    registered_at TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (installation_id, company_id, logical_source_id, version_no),
    UNIQUE (installation_id, company_id, logical_source_id, digest)
);

-- V1: a committed registration is never rewritten or removed.
CREATE TRIGGER IF NOT EXISTS fact_mapping_version_index_reject_update
BEFORE UPDATE ON fact_mapping_version_registrations
BEGIN
    SELECT RAISE(ABORT,
        'fact_mapping_version_registrations is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS fact_mapping_version_index_reject_delete
BEFORE DELETE ON fact_mapping_version_registrations
BEGIN
    SELECT RAISE(ABORT,
        'fact_mapping_version_registrations is append-only: DELETE rejected');
END;

-- INSERT OR REPLACE would delete-then-insert (the REPLACE conflict path
-- does not fire the DELETE trigger unless recursive triggers are on); the
-- BEFORE INSERT guard runs while the old row still exists and refuses it.
CREATE TRIGGER IF NOT EXISTS fact_mapping_version_index_reject_replace
BEFORE INSERT ON fact_mapping_version_registrations
WHEN EXISTS (
    SELECT 1 FROM fact_mapping_version_registrations
    WHERE installation_id = NEW.installation_id
      AND company_id = NEW.company_id
      AND logical_source_id = NEW.logical_source_id
      AND version_no = NEW.version_no
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_mapping_version_registrations is append-only: version already issued; refusing to overwrite it');
END;

-- The binding of one ledger database to one authorized scope is itself
-- immutable.
CREATE TRIGGER IF NOT EXISTS fact_mapping_version_meta_reject_update
BEFORE UPDATE ON fact_mapping_version_index_meta
BEGIN
    SELECT RAISE(ABORT,
        'fact_mapping_version_index_meta is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS fact_mapping_version_meta_reject_delete
BEFORE DELETE ON fact_mapping_version_index_meta
BEGIN
    SELECT RAISE(ABORT,
        'fact_mapping_version_index_meta is append-only: DELETE rejected');
END;
"""

_COLUMNS = (
    "installation_id", "company_id", "logical_source_id", "version_no",
    "object_type", "storage_class", "digest", "size_bytes", "source_kind",
    "source_coordinate", "h_commit_oid", "registered_at", "row_sha256",
)


class MappingVersionIndexError(ValueError):
    """The ledger call itself is malformed (bad structural input)."""


class UnsafeLedgerRootError(RuntimeError):
    """The ledger location is a symlink, wrong-typed, or over-permissive."""


class LedgerIntegrityError(RuntimeError):
    """Stored ledger rows fail their append-only integrity re-checks."""


class UnknownVersionError(KeyError):
    """That source has never had a version with the requested number."""


class MappingVersionScopeError(PermissionError):
    """The call's declared scope is not the ledger's authorized scope."""


class GhostObjectError(MappingVersionIndexError):
    """A registration candidate fails cabinet verification (V3).

    ``reason`` is one of ``object_missing``, ``sha256_mismatch``,
    ``size_mismatch`` or ``unsafe_object_path``.  No ledger row is written
    when this is raised; a not-yet-proven version can only be reported as a
    gap, never indexed to bytes that are not there.
    """

    def __init__(self, digest: str, reason: str, detail: str) -> None:
        super().__init__(
            f"refusing to register digest {digest}: {reason} ({detail})")
        self.digest = digest
        self.reason = reason


def _pinned(parent_fd: int, name: str) -> str:
    """Absolute /proc/self/fd path for a name inside a pinned directory."""
    return f"{_PROC_FD}/{parent_fd}/{name}"


def _open_pinned(dir_fd: int, name: str, flags: int,
                 mode: int = 0o600) -> int:
    """Open *name* inside a held directory without a relative audit name.

    os.open's audit event carries no dir_fd, so a bare relative name cannot
    be attributed to the pinned directory by path-attributing audit hooks
    and looks like a write outside the sandbox. On Linux the
    /proc/self/fd magic link names the same held directory with an absolute
    path; resolution stays pinned to *dir_fd* and O_EXCL/O_NOFOLLOW still
    apply to the trailing component. The dir_fd-relative form remains only
    as a fallback where the magic link is unavailable.
    """
    if os.path.isdir(f"{_PROC_FD}/{dir_fd}"):
        return os.open(_pinned(dir_fd, name), flags, mode)
    return os.open(name, flags, mode, dir_fd=dir_fd)


def _require_identity(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MappingVersionIndexError(f"{label} must be a non-empty str")
    if len(value) > _MAX_ID_CHARS or "\x00" in value:
        raise MappingVersionIndexError(
            f"{label} must be at most {_MAX_ID_CHARS} chars and contain no NUL")
    return value


def _require_digest_hex(value) -> str:
    if not isinstance(value, str) or not _DIGEST_HEX_RE.fullmatch(value):
        raise MappingVersionIndexError(
            "digest must be 64 lowercase hex characters (the cabinet's "
            "bare SHA-256 form)")
    return value


def _require_size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingVersionIndexError(
            "size_bytes must be a non-negative built-in int")
    return value


def _row_digest(*, installation_id, company_id, logical_source_id,
                version_no, object_type, storage_class, digest, size_bytes,
                source_kind, source_coordinate, h_commit_oid,
                registered_at) -> str:
    """Unkeyed drift-detection checksum over one row (not authentication).

    Like the candidate work store's row_sha256: canonical record bytes of
    a fixed dict binding every column.  ``registered_at`` participates
    here (post-insert metadata drift must be detectable) even though it is
    deliberately excluded from the stable ledger total digest (V8).
    """
    return hashlib.sha256(dumps_record({
        "installation_id": installation_id,
        "company_id": company_id,
        "logical_source_id": logical_source_id,
        "version_no": version_no,
        "object_type": object_type,
        "storage_class": storage_class,
        "digest": digest,
        "size_bytes": size_bytes,
        "source_kind": source_kind,
        "source_coordinate": source_coordinate,
        "h_commit_oid": h_commit_oid,
        "registered_at": registered_at,
    })).hexdigest()


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of the single write entry: newly issued or idempotent hit."""

    installation_id: str
    company_id: str
    logical_source_id: str
    version_no: int
    object_type: str
    storage_class: str
    digest: str
    size_bytes: int
    source_kind: str
    source_coordinate: str | None
    h_commit_oid: str | None
    status: str  # STATUS_REGISTERED / STATUS_REPLAYED


@dataclass(frozen=True)
class VersionRecord:
    """One registration plus the current read-time cabinet byte verdict."""

    installation_id: str
    company_id: str
    logical_source_id: str
    version_no: int
    object_type: str
    storage_class: str
    digest: str
    size_bytes: int
    source_kind: str
    source_coordinate: str | None
    h_commit_oid: str | None
    registered_at: str
    byte_status: str
    issue: str | None
    content: bytes | None = None

    @property
    def bytes_present(self) -> bool:
        return self.byte_status == BYTE_PRESENT


class MappingVersionLedger:
    """Append-only per-source version ledger over a real mapping byte cabinet.

    Construction pins and trust-checks a private ledger directory (0700,
    owned by the effective user, never a symlink, never group/world
    writable) and opens a dedicated 0600 SQLite database inside it.  The
    database is bound to exactly one (installation_id, company_id); the
    same file reopened under a foreign scope refuses.  ``object_store``
    must be a real :class:`VersionedMappingObjectStore` — the ledger only
    reads it and never writes, moves or deletes cabinet objects.
    """

    def __init__(self, index_root: Path | str, *,
                 installation_id: str, company_id: str,
                 object_store: VersionedMappingObjectStore) -> None:
        if _O_NOFOLLOW == 0 or _O_DIRECTORY == 0 or not all(
                fn in os.supports_dir_fd for fn in _DIR_FD_PRIMITIVES):
            raise RuntimeError(
                "the mapping-version ledger requires POSIX dir_fd, "
                "O_NOFOLLOW and O_DIRECTORY support to enforce its "
                "no-symlink private-root boundary; refusing to run here "
                "instead of silently degrading")
        if not isinstance(index_root, (str, Path)) or not str(index_root):
            raise MappingVersionIndexError(
                "an explicit ledger root is required")
        if not isinstance(object_store, VersionedMappingObjectStore):
            raise MappingVersionIndexError(
                "object_store must be a real VersionedMappingObjectStore")
        self.installation_id = _require_identity(
            installation_id, "installation_id")
        self.company_id = _require_identity(company_id, "company_id")
        self._object_store = object_store
        self._lock = threading.RLock()
        # Additive batch seam (R2 backfiller, V5/V9): when an open
        # registration_batch() context is active on this thread, register
        # calls join its transaction instead of opening their own.
        self._batch_depth = 0

        root_fd, is_creator, database_path = self._prepare_root(index_root)
        try:
            self.database_path = database_path
            # The database name was born 0600 on the O_EXCL create race,
            # so mode=rw (never rwc) only attaches to an existing name.
            # check_same_thread=False: the parsing main link is
            # multi-threaded and shares one ledger (hence one connection)
            # through the R3 online seam. Every post-construction use of
            # this connection runs inside self._lock (register_version,
            # registration_batch, list/get/shape/payload, close), so the
            # connection is still serialized; only construction-time
            # initialization runs before the object is published.
            self._db = sqlite3.connect(
                Path(_pinned(root_fd, _DB_FILENAME)).as_uri()
                + "?mode=rw",
                uri=True,
                check_same_thread=False,
            )
        finally:
            os.close(root_fd)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            if is_creator:
                self._initialize_new_database()
            else:
                self._await_initialized_database()
        except BaseException:
            self._db.close()
            raise

    # -- construction / root hardening (V6) ----------------------------------

    def _prepare_root(self, index_root) -> tuple[int, bool, str]:
        """Create-if-absent the 0700 leaf and pin it on a parent descriptor.

        Only the leaf is created; its parent must already exist (same
        contract as the byte cabinet).  Every open is O_NOFOLLOW/
        O_DIRECTORY relative to a held descriptor, the leaf must be owned
        by the effective user and must not be group/world writable, and an
        existing database file must be a regular 0600 non-symlink.
        """
        root = Path(os.path.abspath(index_root))
        try:
            parent_fd = os.open(str(root.parent), _DIR_OPEN_FLAGS)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"ledger root parent {root.parent} does not exist; the "
                "ledger creates only its root leaf directory") from None
        except OSError as exc:
            raise UnsafeLedgerRootError(
                f"ledger root parent cannot be opened pinned: "
                f"{root.parent}: {exc}") from exc
        try:
            try:
                os.mkdir(root.name, _DIR_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass
            # The leaf stays open on this returned descriptor: the
            # database-name claim and the /proc/self/fd SQLite connect
            # both resolve through it, so a replacement planted after the
            # check cannot redirect the connection.
            try:
                root_fd = os.open(root.name, _DIR_OPEN_FLAGS,
                                  dir_fd=parent_fd)
            except OSError as exc:
                raise UnsafeLedgerRootError(
                    f"ledger root {root} is a symlink or not a directory: "
                    f"{exc}") from exc
            try:
                st = os.fstat(root_fd)
                if not stat.S_ISDIR(st.st_mode):
                    raise UnsafeLedgerRootError(
                        f"ledger root {root} is not a directory")
                if st.st_uid != os.geteuid():
                    raise UnsafeLedgerRootError(
                        "ledger root must be owned by the current "
                        "effective user")
                if st.st_mode & 0o022:
                    raise UnsafeLedgerRootError(
                        "ledger root must not be group- or world-writable")
                # The leaf's directory entry is persisted before the
                # database file is created inside it, on both fresh and
                # pre-existing roots, matching the byte cabinet's
                # sync-on-every-construction rule.
                os.fsync(parent_fd)

                # Atomic claim of the database name: the winner is born a
                # private 0600 regular file (O_EXCL refuses a planted
                # symlink), the loser attaches to the existing name and
                # later waits for the winner's initialization. There is no
                # 0644-before-chmod window for a second opener to observe.
                try:
                    db_fd = _open_pinned(root_fd, _DB_FILENAME,
                                         _DB_CREATE_FLAGS, _FILE_MODE)
                    is_creator = True
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise UnsafeLedgerRootError(
                            f"ledger database {_DB_FILENAME} is a symlink; "
                            "refusing to create or follow it"
                        ) from exc
                    if exc.errno != errno.EEXIST:
                        raise UnsafeLedgerRootError(
                            f"ledger database {_DB_FILENAME} cannot be "
                            f"created pinned: {exc}") from exc
                    try:
                        db_fd = _open_pinned(root_fd, _DB_FILENAME,
                                             _DB_OPEN_FLAGS)
                    except OSError as exc:
                        if exc.errno == errno.ELOOP:
                            raise UnsafeLedgerRootError(
                                f"ledger database {_DB_FILENAME} is a "
                                "symlink; refusing to follow it"
                            ) from exc
                        raise UnsafeLedgerRootError(
                            f"ledger database {_DB_FILENAME} cannot be "
                            f"opened pinned: {exc}") from exc
                    is_creator = False
                try:
                    db_st = os.fstat(db_fd)
                    if stat.S_ISLNK(db_st.st_mode):
                        raise UnsafeLedgerRootError(
                            f"ledger database {_DB_FILENAME} is a symlink; "
                            "refusing to follow it")
                    if not stat.S_ISREG(db_st.st_mode):
                        raise UnsafeLedgerRootError(
                            f"ledger database {_DB_FILENAME} is not a "
                            "regular file")
                    if db_st.st_uid != os.geteuid():
                        raise UnsafeLedgerRootError(
                            "ledger database must be owned by the current "
                            "effective user")
                    if db_st.st_mode & 0o077:
                        raise UnsafeLedgerRootError(
                            "ledger database must be a private 0600 file; "
                            "actual mode %s"
                            % oct(db_st.st_mode & 0o777))
                finally:
                    os.close(db_fd)
                if is_creator:
                    # Persist the private name before SQLite initializes it.
                    os.fsync(root_fd)
            except BaseException:
                os.close(root_fd)
                raise
            return root_fd, is_creator, str(root / _DB_FILENAME)
        finally:
            os.close(parent_fd)

    def _initialize_new_database(self) -> None:
        """Winner of the O_EXCL name race: create schema and bind scope."""
        self._db.executescript(_SCHEMA_SQL)
        self._db.execute(
            "INSERT INTO fact_mapping_version_index_meta "
            "(singleton, installation_id, company_id) VALUES (1, ?, ?)",
            (self.installation_id, self.company_id))
        # user_version is the completion marker awaited by concurrent
        # openers that lost the creation race; set it last in one commit.
        self._db.execute(
            "PRAGMA user_version = %d" % _DB_SCHEMA_VERSION)
        self._db.commit()

    def _await_initialized_database(self) -> None:
        """Loser path: wait read-only for the creator's commit, then verify."""
        deadline = time.monotonic() + _INIT_WAIT_SECONDS
        while True:
            try:
                version = self._db.execute(
                    "PRAGMA user_version").fetchone()[0]
                row = self._db.execute(
                    "SELECT installation_id, company_id "
                    "FROM fact_mapping_version_index_meta "
                    "WHERE singleton = 1").fetchone()
            except sqlite3.OperationalError:
                row = None
                version = 0
            if version == _DB_SCHEMA_VERSION and row is not None:
                break
            if version > _DB_SCHEMA_VERSION:
                raise LedgerIntegrityError(
                    "ledger database was created by a newer schema "
                    f"version {version}; this build speaks "
                    f"{_DB_SCHEMA_VERSION}")
            if time.monotonic() >= deadline:
                raise LedgerIntegrityError(
                    "ledger database exists but its initialization never "
                    "completed (a concurrent first constructor may have "
                    "crashed); refusing to use the half-built ledger")
            time.sleep(_INIT_POLL_INTERVAL)
        if row["installation_id"] != self.installation_id \
                or row["company_id"] != self.company_id:
            raise MappingVersionScopeError(
                "ledger database is bound to installation "
                f"{row['installation_id']!r}/company "
                f"{row['company_id']!r}; reopening it as "
                f"{self.installation_id!r}/{self.company_id!r} is refused")
        # The triggers/shape are part of the same committed completion
        # marker as user_version; a missing table is a half-built ledger.
        try:
            self._db.execute(
                "SELECT 1 FROM fact_mapping_version_registrations LIMIT 0")
        except sqlite3.OperationalError as exc:
            raise LedgerIntegrityError(
                "initialized ledger is missing its registration table"
            ) from exc

    # -- scope plumbing (V6) --------------------------------------------------

    def _check_scope(self, installation_id: str, company_id: str) -> None:
        if (installation_id != self.installation_id
                or company_id != self.company_id):
            raise MappingVersionScopeError(
                "the call's declared scope "
                f"({installation_id!r}, {company_id!r}) is not this "
                "ledger's authorized scope "
                f"({self.installation_id!r}, {self.company_id!r})")

    # -- V3: the only cabinet authority is the cabinet itself ----------------

    def _verify_cabinet_bytes(self, digest: str,
                              size_bytes: int) -> None:
        try:
            data = self._object_store.get(digest)
        except ObjectMissingError as exc:
            raise GhostObjectError(
                digest, "object_missing",
                "digest is not present in the mapping-object cabinet"
            ) from exc
        except ObjectMismatchError as exc:
            raise GhostObjectError(
                digest, "sha256_mismatch",
                "stored cabinet bytes fail read-time SHA-256 revalidation"
            ) from exc
        except UnsafeObjectPathError as exc:
            raise GhostObjectError(
                digest, "unsafe_object_path",
                f"cabinet occupant is unsafe to read: {exc}"
            ) from exc
        if len(data) != size_bytes:
            raise GhostObjectError(
                digest, "size_mismatch",
                f"declared size {size_bytes} does not match verified "
                f"byte length {len(data)}")

    # -- the single write entry (V1/V2/V3) -----------------------------------

    def register_version(self, *, logical_source_id: str, digest: str,
                         size_bytes: int,
                         installation_id: str, company_id: str,
                         source_kind: str = SOURCE_KIND_ONLINE,
                         source_coordinate: str | None = None,
                         h_commit_oid: str | None = None
                         ) -> RegistrationResult:
        """Register one cabinet-backed mapping version, or idempotently replay.

        The caller declares the logical source identity within the scope
        (in production this is the original's ``file_hash``; the ledger
        invents no business key), the candidate digest and size, an
        optional provenance label/coordinate (traceability only, never
        version truth) and an optional fixed-H commit oid.  Verification
        order: structural validation, scope refusal (V6), cabinet
        get+sha256+size verification (V3), then one ``BEGIN IMMEDIATE``
        transaction that either returns the existing row for a repeated
        (source, digest) or issues max+1 and inserts exactly one row
        (V1/V2).  Any failure rolls back and writes nothing.
        """
        source = _require_identity(logical_source_id, "logical_source_id")
        digest_hex = _require_digest_hex(digest)
        size = _require_size(size_bytes)
        self._check_scope(
            _require_identity(installation_id, "installation_id"),
            _require_identity(company_id, "company_id"))
        if source_kind not in _SOURCE_KINDS:
            raise MappingVersionIndexError(
                "source_kind must be 'online' or 'backfill'")
        if source_coordinate is not None:
            if (not isinstance(source_coordinate, str)
                    or not source_coordinate
                    or len(source_coordinate) > _MAX_COORDINATE_CHARS
                    or "\x00" in source_coordinate):
                raise MappingVersionIndexError(
                    "source_coordinate must be a non-empty str of at most "
                    f"{_MAX_COORDINATE_CHARS} chars without NUL, or None")
        if h_commit_oid is not None:
            if not isinstance(h_commit_oid, str) \
                    or not _COMMIT_OID_RE.fullmatch(h_commit_oid):
                raise MappingVersionIndexError(
                    "h_commit_oid must be 40/64 lowercase hex characters "
                    "or None")

        # Read-time cabinet verification happens before any ledger write
        # transaction; failure here leaves the database untouched.
        self._verify_cabinet_bytes(digest_hex, size)

        registered_at = datetime.now(timezone.utc).isoformat()
        joined_batch = self._batch_depth > 0
        with self._lock:
            if not joined_batch:
                self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT " + ", ".join(_COLUMNS) + " "
                    "FROM fact_mapping_version_registrations "
                    "WHERE installation_id=? AND company_id=? "
                    "AND logical_source_id=? AND digest=?",
                    (self.installation_id, self.company_id, source,
                     digest_hex),
                ).fetchone()
                if existing is not None:
                    row = self._load_row(existing)
                    # Same digest fixes the bytes, hence their size; a
                    # differing stored size means row drift, not a replay.
                    if row["size_bytes"] != size:
                        raise LedgerIntegrityError(
                            "stored registration size disagrees with the "
                            "cabinet-verified bytes for the same digest")
                    if not joined_batch:
                        self._db.commit()
                    return self._result(row, STATUS_REPLAYED)

                next_version = self._db.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 "
                    "FROM fact_mapping_version_registrations "
                    "WHERE installation_id=? AND company_id=? "
                    "AND logical_source_id=?",
                    (self.installation_id, self.company_id, source),
                ).fetchone()[0]
                row_sha = _row_digest(
                    installation_id=self.installation_id,
                    company_id=self.company_id,
                    logical_source_id=source,
                    version_no=next_version,
                    object_type=OBJECT_TYPE_MAPPING_VERSION,
                    storage_class=STORAGE_CLASS_MAPPING_OBJECTS,
                    digest=digest_hex,
                    size_bytes=size,
                    source_kind=source_kind,
                    source_coordinate=source_coordinate,
                    h_commit_oid=h_commit_oid,
                    registered_at=registered_at)
                try:
                    self._db.execute(
                        "INSERT INTO fact_mapping_version_registrations ("
                        + ", ".join(_COLUMNS) + ") VALUES ("
                        + ", ".join("?" for _ in _COLUMNS) + ")",
                        (self.installation_id, self.company_id, source,
                         next_version, OBJECT_TYPE_MAPPING_VERSION,
                         STORAGE_CLASS_MAPPING_OBJECTS, digest_hex, size,
                         source_kind, source_coordinate, h_commit_oid,
                         registered_at, row_sha))
                except sqlite3.IntegrityError as exc:
                    # Lost a race despite BEGIN IMMEDIATE/busy_timeout, or
                    # a trigger fired: fail closed, never number-and-lose.
                    raise LedgerIntegrityError(
                        f"registration insert refused by the append-only "
                        f"schema: {exc}") from exc
                if not joined_batch:
                    self._db.commit()
            except BaseException:
                if not joined_batch:
                    self._db.rollback()
                # Joined batch: leave the failed transaction for the
                # registration_batch() context, which rolls back every
                # registration in the batch together.
                raise
        return RegistrationResult(
            installation_id=self.installation_id,
            company_id=self.company_id,
            logical_source_id=source,
            version_no=next_version,
            object_type=OBJECT_TYPE_MAPPING_VERSION,
            storage_class=STORAGE_CLASS_MAPPING_OBJECTS,
            digest=digest_hex,
            size_bytes=size,
            source_kind=source_kind,
            source_coordinate=source_coordinate,
            h_commit_oid=h_commit_oid,
            status=STATUS_REGISTERED)

    @contextlib.contextmanager
    def registration_batch(self):
        """Bind several :meth:`register_version` calls to one transaction.

        Additive seam for the offline, repeatable backfill reconciler
        (R2 slice 2, V5/V9): every register call made inside this context
        joins one ``BEGIN IMMEDIATE`` transaction instead of opening its
        own, so a batch is all-or-nothing — either every version in it is
        committed together or none of them is.  Register calls outside
        this context keep their original one-call-one-transaction behavior
        unchanged.  Each register call still performs its own structural
        validation, scope check and cabinet verification, and numbering
        still runs max+1 inside the shared transaction; batches cannot be
        nested.  A register failure leaves the batch transaction to this
        context, which rolls it back in full.
        """
        with self._lock:
            if self._batch_depth != 0:
                raise MappingVersionIndexError(
                    "registration batches cannot be nested")
            self._batch_depth = 1
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()
            finally:
                self._batch_depth = 0

    # -- read surface ---------------------------------------------------------

    def list_versions(self, logical_source_id: str, *,
                      installation_id: str, company_id: str,
                      verify: bool = True) -> tuple[VersionRecord, ...]:
        """All versions of one source, ordered by the issued version number."""
        source = _require_identity(logical_source_id, "logical_source_id")
        self._check_scope(
            _require_identity(installation_id, "installation_id"),
            _require_identity(company_id, "company_id"))
        with self._lock:
            rows = self._db.execute(
                "SELECT " + ", ".join(_COLUMNS) + " "
                "FROM fact_mapping_version_registrations "
                "WHERE installation_id=? AND company_id=? "
                "AND logical_source_id=? ORDER BY version_no ASC",
                (self.installation_id, self.company_id, source),
            ).fetchall()
            records = []
            for row in rows:
                loaded = self._load_row(row)
                records.append(
                    self._record(loaded) if not verify
                    else self._verify_record(loaded))
        return tuple(records)

    def get_version(self, logical_source_id: str, version_no: int, *,
                    installation_id: str, company_id: str,
                    include_bytes: bool = False) -> VersionRecord:
        """Return one issued version with current cabinet byte evidence.

        A never-issued number raises :class:`UnknownVersionError`.  A
        version whose object has since been deleted or corrupted is
        returned as a record with ``byte_status`` missing/corrupt and the
        original digest intact — never another version's bytes.
        """
        source = _require_identity(logical_source_id, "logical_source_id")
        if isinstance(version_no, bool) or not isinstance(version_no, int) \
                or version_no < 1:
            raise MappingVersionIndexError(
                "version_no must be a positive built-in int")
        self._check_scope(
            _require_identity(installation_id, "installation_id"),
            _require_identity(company_id, "company_id"))
        with self._lock:
            row = self._db.execute(
                "SELECT " + ", ".join(_COLUMNS) + " "
                "FROM fact_mapping_version_registrations "
                "WHERE installation_id=? AND company_id=? "
                "AND logical_source_id=? AND version_no=?",
                (self.installation_id, self.company_id, source,
                 version_no),
            ).fetchone()
            if row is None:
                raise UnknownVersionError(
                    f"{source} has no version {version_no}")
            loaded = self._load_row(row)
            return self._verify_record(loaded,
                                       include_bytes=include_bytes)

    def ledger_shape(self, *, verify: bool = True) -> dict:
        """Per-source version counts plus current byte-gap counts."""
        with self._lock:
            sources = [r[0] for r in self._db.execute(
                "SELECT DISTINCT logical_source_id "
                "FROM fact_mapping_version_registrations "
                "WHERE installation_id=? AND company_id=? "
                "ORDER BY logical_source_id ASC",
                (self.installation_id, self.company_id))]
            shapes = []
            total_versions = 0
            total_gaps = 0
            for source in sources:
                records = [
                    self._load_row(row) for row in self._db.execute(
                        "SELECT " + ", ".join(_COLUMNS) + " "
                        "FROM fact_mapping_version_registrations "
                        "WHERE installation_id=? AND company_id=? "
                        "AND logical_source_id=? ORDER BY version_no ASC",
                        (self.installation_id, self.company_id, source))
                ]
                gap_versions: list[int] = []
                if verify:
                    for loaded in records:
                        verdict = self._byte_verdict(
                            loaded["digest"], loaded["size_bytes"])
                        if verdict[0] != BYTE_PRESENT:
                            gap_versions.append(loaded["version_no"])
                shapes.append({
                    "logical_source_id": source,
                    "versions": len(records),
                    "gaps": len(gap_versions),
                    "gap_versions": gap_versions,
                })
                total_versions += len(records)
                total_gaps += len(gap_versions)
        return {
            "installation_id": self.installation_id,
            "company_id": self.company_id,
            "object_type": OBJECT_TYPE_MAPPING_VERSION,
            "sources": shapes,
            "total_sources": len(shapes),
            "total_versions": total_versions,
            "total_gaps": total_gaps,
        }

    def canonical_payload(self) -> dict:
        """Stable, secret-free ledger payload (V8), sorted deterministically."""
        with self._lock:
            rows = self._db.execute(
                "SELECT " + ", ".join(_COLUMNS) + " "
                "FROM fact_mapping_version_registrations "
                "WHERE installation_id=? AND company_id=? "
                "ORDER BY logical_source_id ASC, version_no ASC",
                (self.installation_id, self.company_id),
            ).fetchall()
            registrations = []
            for row in rows:
                loaded = self._load_row(row)
                registrations.append({
                    "logical_source_id": loaded["logical_source_id"],
                    "version_no": loaded["version_no"],
                    "sha256_digest":
                        f"{DIGEST_ALGORITHM}:{loaded['digest']}",
                    "size_bytes": loaded["size_bytes"],
                    "source_kind": loaded["source_kind"],
                    "source_coordinate": loaded["source_coordinate"],
                    "h_commit_oid": loaded["h_commit_oid"],
                })
        return {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "scope": {
                "kind": "company",
                "installation_id": self.installation_id,
                "company_id": self.company_id,
            },
            "object_type": OBJECT_TYPE_MAPPING_VERSION,
            "storage_class": STORAGE_CLASS_MAPPING_OBJECTS,
            "digest_algorithm": DIGEST_ALGORITHM,
            "registrations": registrations,
        }

    def ledger_digest(self) -> str:
        """Canonical total digest; identical registration sequences agree."""
        return compute_core_digest(self.canonical_payload())

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "MappingVersionLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- row loading / verdicts ----------------------------------------------

    @staticmethod
    def _load_row(row: sqlite3.Row) -> dict:
        """Re-validate every stored column and its drift checksum."""
        values = {name: row[name] for name in _COLUMNS}
        digest = values["digest"]
        if not isinstance(digest, str) or not _DIGEST_HEX_RE.fullmatch(digest):
            raise LedgerIntegrityError(
                "stored digest is not 64 lowercase hex characters")
        if values["object_type"] != OBJECT_TYPE_MAPPING_VERSION:
            raise LedgerIntegrityError("stored object_type drifted")
        if values["storage_class"] != STORAGE_CLASS_MAPPING_OBJECTS:
            raise LedgerIntegrityError("stored storage_class drifted")
        if values["source_kind"] not in _SOURCE_KINDS:
            raise LedgerIntegrityError("stored source_kind drifted")
        version_no = values["version_no"]
        if isinstance(version_no, bool) or not isinstance(version_no, int) \
                or version_no < 1:
            raise LedgerIntegrityError("stored version_no is invalid")
        size_bytes = values["size_bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) \
                or size_bytes < 0:
            raise LedgerIntegrityError("stored size_bytes is invalid")
        expected = _row_digest(
            installation_id=values["installation_id"],
            company_id=values["company_id"],
            logical_source_id=values["logical_source_id"],
            version_no=version_no,
            object_type=values["object_type"],
            storage_class=values["storage_class"],
            digest=digest,
            size_bytes=size_bytes,
            source_kind=values["source_kind"],
            source_coordinate=values["source_coordinate"],
            h_commit_oid=values["h_commit_oid"],
            registered_at=values["registered_at"])
        if expected != values["row_sha256"]:
            raise LedgerIntegrityError(
                "stored row-integrity digest does not match the stored "
                "columns; refusing to read possibly tampered data")
        return values

    def _byte_verdict(self, digest: str,
                      size_bytes: int) -> tuple[str, str | None, bytes | None]:
        """Read-time cabinet evidence; gaps are verdicts, never exceptions."""
        try:
            data = self._object_store.get(digest)
        except ObjectMissingError:
            return BYTE_MISSING, "object_missing", None
        except ObjectMismatchError:
            return BYTE_CORRUPT, "sha256_mismatch", None
        except UnsafeObjectPathError as exc:
            return BYTE_UNSAFE_PATH, f"unsafe_object_path: {exc}", None
        if len(data) != size_bytes:
            return BYTE_CORRUPT, "size_mismatch", None
        return BYTE_PRESENT, None, data

    def _verify_record(self, loaded: dict, *,
                       include_bytes: bool = False) -> VersionRecord:
        status, issue, data = self._byte_verdict(
            loaded["digest"], loaded["size_bytes"])
        return VersionRecord(
            installation_id=loaded["installation_id"],
            company_id=loaded["company_id"],
            logical_source_id=loaded["logical_source_id"],
            version_no=loaded["version_no"],
            object_type=loaded["object_type"],
            storage_class=loaded["storage_class"],
            digest=loaded["digest"],
            size_bytes=loaded["size_bytes"],
            source_kind=loaded["source_kind"],
            source_coordinate=loaded["source_coordinate"],
            h_commit_oid=loaded["h_commit_oid"],
            registered_at=loaded["registered_at"],
            byte_status=status,
            issue=issue,
            content=data if include_bytes else None)

    @staticmethod
    def _record(loaded: dict) -> VersionRecord:
        return VersionRecord(
            installation_id=loaded["installation_id"],
            company_id=loaded["company_id"],
            logical_source_id=loaded["logical_source_id"],
            version_no=loaded["version_no"],
            object_type=loaded["object_type"],
            storage_class=loaded["storage_class"],
            digest=loaded["digest"],
            size_bytes=loaded["size_bytes"],
            source_kind=loaded["source_kind"],
            source_coordinate=loaded["source_coordinate"],
            h_commit_oid=loaded["h_commit_oid"],
            registered_at=loaded["registered_at"],
            byte_status="unverified",
            issue=None)

    @staticmethod
    def _result(row: dict, status: str) -> RegistrationResult:
        return RegistrationResult(
            installation_id=row["installation_id"],
            company_id=row["company_id"],
            logical_source_id=row["logical_source_id"],
            version_no=row["version_no"],
            object_type=row["object_type"],
            storage_class=row["storage_class"],
            digest=row["digest"],
            size_bytes=row["size_bytes"],
            source_kind=row["source_kind"],
            source_coordinate=row["source_coordinate"],
            h_commit_oid=row["h_commit_oid"],
            status=status)
