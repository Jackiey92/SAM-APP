"""In-barrier consistent-cut freeze coordinator for F-001 full backups.

First assembly slice of the approved F-001 backup design
(``98-完整备份与隔离恢复设计.md`` sections 3-5 and appendix C1). This
module is the assembly spine only: it holds ONE maintenance barrier,
and inside that single barrier hold it

1. captures the exact authoritative Git head H and pins that exact
   commit with the existing ``git_repository`` maintenance-pin
   primitive (never a second authority ref, never REF_FACTS moved),
2. produces one immutable SQLite checkpoint per declared work database
   with the existing ``backup_sqlite_checkpoint`` primitive,
3. re-verifies every published component with the existing
   ``backup_component_validation`` primitive,
4. fixes the cut evidence digest with the existing
   ``backup_manifest.compute_core_digest`` kernel, and
5. durably records the freeze outcome before releasing the barrier.

It performs NO Git bundle export, NO R object closure, NO final
manifest, NO verification report, NO restore, NO job state machine, NO
GC and NO generic multi-participant quiesce protocol; those belong to
later slices.

Trust model and caller preconditions
------------------------------------

The assembly manifest is declarative. The host is responsible for the
truth of what it wires (which repository, which live connections,
which roles, which scope); the coordinator validates the manifest's
*structure* and the *single-company scope* it can physically observe.
It contains zero business judgement: no regular expressions over
data, no keywords, no enums of table names, no model calls. Which
databases belong to W and what roles they play comes exclusively from
the assembly. Scope evidence per work database is a host-supplied
probe that returns the company ids physically observed over that
database's live connection; the coordinator only compares that set to
the singleton declared company id. A database whose physical scope
cannot be proven is refused.

Barrier reuse
-------------

The barrier is the existing rank-1 maintenance flock of the
caller-supplied ``BrokerCoordination``: the canonical
``<coordination.directory>/maintenance.lock`` file. This module never
creates its own lock file or a parallel protocol; the flock it holds
is the same flock ``maintenance_admission()`` takes, so it excludes
and is excluded by every broker ``_gates()`` entrant across threads,
instances and processes. ``maintenance_admission()`` itself is a
blocking context manager with no deadline, while the design requires
a bounded, cancellable wait; the acquire below therefore polls the
same canonical file with ``LOCK_EX | LOCK_NB``. The ordering rules are
still the broker module's rules: a host transaction open on the
coordination connection, or any rank frame already held by this
thread, raises the broker module's own ``LockOrderError`` unchanged.

Failure direction
-----------------

Every operational failure inside the barrier returns a terminal
``FreezeEvidence`` whose status is ``failed`` or ``incomplete``; no
successful cut is ever reported, no completed cut directory is
published and no later slice's high watermark is advanced by this
module. Partly published checkpoint files stay in the cut's private
staging directory for inspection (a CONFIRMED durable file is never
deleted to make a failure look clean); an UNCERTAIN publication always
classifies the cut as incomplete and explicitly asks for inspection.
Only structural caller errors and barrier/cancellation problems raise.
"""

from contextlib import suppress
from dataclasses import dataclass, field
import datetime as _datetime
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat as stat_module
import threading
import time

from app.facts_tree.backup_component_validation import (
    BackupComponentValidationError,
    verify_backup_component,
)
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.backup_sqlite_checkpoint import (
    PublicationState,
    SqliteCheckpointError,
    create_sqlite_checkpoint,
)
from app.facts_tree.broker_coordination import (
    BrokerCoordination,
    LockOrderError,
    _LOCAL as _COORDINATION_LOCAL,
)
from app.facts_tree.git_format import loads_record, record_path
from app.facts_tree.git_repository import (
    REF_FACTS,
    FactsGitRepository,
    GitRepositoryError,
    IntegrityError,
    MaintenancePinError,
    SnapshotReadError,
    maintenance_pin_ref,
)

# Journal/cut states. PREPARING lives only in the journal; a row left in
# it after a crash means "a freeze once started here", recognizable on
# restart (resume/cleanup is a later slice).
PREPARING = "preparing"
FROZEN = "frozen"
FAILED = "failed"
INCOMPLETE = "incomplete"
_TERMINAL_STATES = frozenset({FROZEN, FAILED, INCOMPLETE})
_JOURNAL_STATES = frozenset({PREPARING, FROZEN, FAILED, INCOMPLETE})

CORE_SCHEMA = "sam.f001.backup_core"
CORE_VERSION = 1
_JOURNAL_SCHEMA_VERSION = 1

_COORDINATOR_DIRNAME = ".coordinator"
_STAGING_DIRNAME = ".staging"
_CUTS_DIRNAME = "cuts"
_JOURNAL_FILENAME = "freezes.sqlite"
_CHECKPOINT_SUFFIX = ".sqlite"
_META_RECORD_PATH = record_path("repository_meta")

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DEFAULT_POLL_INTERVAL = 0.02
# Bounded, but never busy-spinning: floor the poll interval.
_MIN_POLL_INTERVAL = 0.002

_IDENTITY_KEYS = ("repo_id", "installation_id", "company_id")
_PROC_FD = "/proc/self/fd"
_TIMING_DECIMAL_PLACES = 6
_DECIMAL_TAG = "__decimal_exact__"
_DECIMAL_PLACEHOLDER = re.compile(
    r'\{"' + re.escape(_DECIMAL_TAG)
    + r'":\s*"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"\}'
)


def _exact_seconds(duration):
    """Monotonic timing as a fixed-point Decimal (canonical JSON number)."""
    return Decimal(format(round(duration, _TIMING_DECIMAL_PLACES),
                          ".%df" % _TIMING_DECIMAL_PLACES))


def _journal_dumps(detail):
    """JSON for the host journal, preserving Decimal as exact JSON numbers.

    git_format's canonical record format (which fixes the core digest)
    rejects binary floats and accepts Decimal; the journal round-trip
    must not rewrite those numbers through float either, or a digest
    recomputed from the journal would not reproduce.
    """

    def convert(value):
        if isinstance(value, Decimal):
            return {_DECIMAL_TAG: format(value, "f")}
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    text = json.dumps(convert(detail), sort_keys=True, ensure_ascii=True,
                      allow_nan=False)
    return _DECIMAL_PLACEHOLDER.sub(r"\1", text)


def _pinned(parent_fd, name):
    """Absolute /proc/self/fd path for a name inside a pinned directory."""
    return "%s/%d/%s" % (_PROC_FD, parent_fd, name)


class BackupCoordinatorError(Exception):
    """Base class for coordinator failures that cannot return evidence."""


class AssemblyValidationError(BackupCoordinatorError, ValueError):
    """The declarative freeze assembly has an invalid structure.

    Raised before any barrier entry and before any journal or backup
    artifact is written.
    """


class ScopeMismatchError(AssemblyValidationError):
    """The assembly's declared single-company scope is inconsistent.

    Raised for declaration-level mismatches found before the barrier
    (for example a work database declared for another company than the
    installation). Physically observed scope mismatches inside the
    barrier are instead returned as terminal ``failed`` evidence that
    lists every mismatch, because the observation itself is cut work.
    """

    def __init__(self, message, mismatches=()):
        super().__init__(message)
        self.mismatches = tuple(mismatches)


class FreezeAlreadyRecordedError(BackupCoordinatorError):
    """A freeze record for this backup_id already exists.

    This slice identifies and rejects duplicate freezes (D2); it never
    produces a second cut, overwrites the first evidence or re-runs a
    stale preparation. ``prior_state`` is the journal state found.
    """

    def __init__(self, backup_id, prior_state):
        super().__init__(
            "backup_id %r already has a %s freeze record; refusing a second "
            "freeze for the same identity" % (backup_id, prior_state)
        )
        self.backup_id = backup_id
        self.prior_state = prior_state


class BarrierUnavailableError(BackupCoordinatorError):
    """The rank-1 maintenance barrier could not be acquired in budget.

    The coordinator never bypasses the barrier or degrades to a
    barrier-free backup.
    """


class FreezeCancelled(BackupCoordinatorError):
    """The cancellation token fired or the hold budget expired.

    ``reason`` is "cancelled" or "hold-timeout". Always raised *before*
    any success marker exists and after (or without ever taking) the
    barrier; the journal row is left ``incomplete``.
    """

    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class AssemblyIdentity:
    """The single installation/company/repo identity one cut covers."""

    repo_id: str
    installation_id: str
    company_id: str


@dataclass(frozen=True)
class WorkDatabaseSpec:
    """One declarative W database entry of the freeze assembly.

    ``role`` is the logical role label used as the checkpoint file stem;
    it carries no semantics here. ``connection`` is the host-owned live
    SQLite connection the coordinator never closes, commits, rolls back
    or reconfigures. ``scope_probe`` is host code run over that
    connection inside the barrier; it must return every company id
    physically observed in the database. ``schema`` and
    ``scope_watermark`` are opaque host evidence fixed verbatim into the
    core payload.
    """

    role: str
    connection: sqlite3.Connection
    scope_probe: object
    schema: dict = field(default_factory=dict)
    scope_watermark: dict = field(default_factory=dict)
    company_id: str | None = None


@dataclass(frozen=True)
class BarrierBudget:
    """Bounded wait and hold for one freeze."""

    wait_timeout: float
    hold_timeout: float
    poll_interval: float = _DEFAULT_POLL_INTERVAL


@dataclass(frozen=True)
class FreezeAssembly:
    """The complete declarative input for one consistent-cut freeze."""

    backup_id: str
    identity: AssemblyIdentity
    coordination: BrokerCoordination
    repository: FactsGitRepository
    databases: tuple
    retention_root: Path
    budget: BarrierBudget
    cancel_event: threading.Event | None = None


@dataclass(frozen=True)
class AuthorityEvidence:
    """Evidence fixing H and its maintenance pin for one cut."""

    ref: str
    commit_oid: str
    object_format: str
    genesis_oid: str
    commit_count: int
    pin_ref: str
    pin_created: bool


@dataclass(frozen=True)
class WorkCheckpointEvidence:
    """One W checkpoint as fixed in the cut evidence."""

    role: str
    relative_path: str
    size_bytes: int
    sha256_digest: str
    schema: dict
    scope_watermark: dict
    publication: str = PublicationState.CONFIRMED


@dataclass(frozen=True)
class ScopeObservation:
    """One precise scope discrepancy (structural or physical)."""

    where: str
    role: str
    expected: str
    observed: tuple


@dataclass(frozen=True)
class FreezeEvidence:
    """Result of one freeze: one frozen cut or a terminal failure.

    ``status`` is one of FROZEN/FAILED/INCOMPLETE. A non-frozen result
    never carries a core digest or a completed cut directory; its
    checkpoints describe at most private-staging debris that must be
    inspected, never a successful cut.
    """

    backup_id: str
    barrier_id: str
    status: str
    reason: str | None
    company_id: str
    core_digest: str | None
    authority: AuthorityEvidence | None
    checkpoints: tuple
    scope_mismatches: tuple
    cut_dir: Path | None
    staging_dir: Path | None
    detail: str | None
    started_at: str
    finished_at: str
    wait_seconds: float
    held_seconds: float

    @property
    def frozen(self):
        return self.status == FROZEN


@dataclass(frozen=True)
class FreezeRecord:
    """One durable journal row: a freeze that happened on this host."""

    backup_id: str
    state: str
    barrier_id: str | None
    created_at: str
    updated_at: str
    detail: dict


def _utc_now():
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _host_text(value, label):
    if not isinstance(value, str) or not value:
        raise AssemblyValidationError("%s must be a non-empty str" % label)
    if len(value) > 128:
        raise AssemblyValidationError("%s is longer than 128 characters" % label)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AssemblyValidationError("%s must be UTF-8 encodable" % label) \
            from None
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise AssemblyValidationError("%s must be a dict" % label)
    return value


def _validate_assembly(assembly):
    if not isinstance(assembly, FreezeAssembly):
        raise AssemblyValidationError(
            "freeze input must be a FreezeAssembly, got %s"
            % type(assembly).__name__
        )
    backup_id = _host_text(assembly.backup_id, "backup_id")
    # Reuse the Git primitive's own fail-closed identity validation
    # instead of duplicating its charset rule.
    try:
        maintenance_pin_ref(backup_id, "b0")
    except ValueError as exc:
        raise AssemblyValidationError(str(exc)) from exc

    identity = assembly.identity
    if not isinstance(identity, AssemblyIdentity):
        raise AssemblyValidationError("identity must be an AssemblyIdentity")
    declared = {
        key: _host_text(getattr(identity, key), key) for key in _IDENTITY_KEYS
    }

    if not isinstance(assembly.coordination, BrokerCoordination):
        raise AssemblyValidationError(
            "coordination must be a constructed BrokerCoordination"
        )
    if not isinstance(assembly.repository, FactsGitRepository):
        raise AssemblyValidationError(
            "repository must be a bound FactsGitRepository"
        )

    databases = assembly.databases
    if not isinstance(databases, (tuple, list)) or not databases:
        raise AssemblyValidationError(
            "databases must be a non-empty tuple of WorkDatabaseSpec"
        )
    seen_roles = set()
    normalized = []
    for index, spec in enumerate(databases):
        label = "databases[%d]" % index
        if not isinstance(spec, WorkDatabaseSpec):
            raise AssemblyValidationError(
                "%s must be a WorkDatabaseSpec" % label
            )
        role = _host_text(spec.role, label + ".role")
        if len(role) > 64 or not all(
                character.isascii() and
                (character.isalnum() or character in "_-")
                for character in role):
            raise AssemblyValidationError(
                "%s.role must be 1-64 characters of [A-Za-z0-9_-]" % label
            )
        if role in seen_roles:
            raise AssemblyValidationError("duplicate W role: %r" % role)
        seen_roles.add(role)
        if not isinstance(spec.connection, sqlite3.Connection):
            raise AssemblyValidationError(
                "%s.connection must be an open sqlite3.Connection" % label
            )
        if not callable(spec.scope_probe):
            raise AssemblyValidationError(
                "%s.scope_probe must be callable" % label
            )
        _mapping(spec.schema, label + ".schema")
        _mapping(spec.scope_watermark, label + ".scope_watermark")
        spec_company = (
            declared["company_id"] if spec.company_id is None
            else _host_text(spec.company_id, label + ".company_id")
        )
        if spec_company != declared["company_id"]:
            raise ScopeMismatchError(
                "W database %r is declared for company %r but the "
                "installation covers %r"
                % (role, spec_company, declared["company_id"]),
                [ScopeObservation("database-declared", role,
                                  declared["company_id"], (spec_company,))],
            )
        normalized.append(spec)

    budget = assembly.budget
    if not isinstance(budget, BarrierBudget):
        raise AssemblyValidationError("budget must be a BarrierBudget")
    for name in ("wait_timeout", "hold_timeout"):
        value = getattr(budget, name)
        if type(value) is not float or not (value > 0.0) or value != value:
            raise AssemblyValidationError(
                "%s must be a positive finite float" % name
            )
    poll = budget.poll_interval
    if type(poll) is not float or not (poll >= _MIN_POLL_INTERVAL):
        raise AssemblyValidationError(
            "poll_interval must be a float >= %g" % _MIN_POLL_INTERVAL
        )

    cancel_event = assembly.cancel_event
    if cancel_event is not None and (
            not isinstance(cancel_event, threading.Event)
            or not callable(getattr(cancel_event, "is_set", None))):
        raise AssemblyValidationError(
            "cancel_event must be a threading.Event or None"
        )

    root = _validated_root(assembly.retention_root)
    return (
        backup_id,
        AssemblyIdentity(**declared),
        tuple(normalized),
        root,
    )


def _validated_root(retention_root):
    """Pin and trust-check the retention root before anything is created."""
    if not isinstance(retention_root, (str, Path)):
        raise AssemblyValidationError(
            "retention_root must be a str or Path"
        )
    root = Path(retention_root)
    if root.is_symlink():
        raise AssemblyValidationError(
            "retention root must not itself be a symlink: %s" % root
        )
    try:
        root_fd = os.open(str(root), _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise AssemblyValidationError(
            "retention root cannot be opened as a real directory: %s: %s"
            % (root, exc)
        ) from exc
    try:
        result = os.fstat(root_fd)
        if not stat_module.S_ISDIR(result.st_mode):
            raise AssemblyValidationError("retention root is not a directory")
        if result.st_uid != os.geteuid():
            raise AssemblyValidationError(
                "retention root must be owned by the current effective user"
            )
        if result.st_mode & 0o022:
            raise AssemblyValidationError(
                "retention root must not be group- or world-writable"
            )
    finally:
        os.close(root_fd)
    return root


def _ensure_private_dir(parent_fd, name):
    """Create-if-absent a 0700 non-symlink child directory, pinned open."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise BackupCoordinatorError(
            "cannot create private directory %r under the retention root: %s"
            % (name, exc)
        ) from exc
    fd = os.open(_pinned(parent_fd, name), _DIRECTORY_OPEN_FLAGS)
    try:
        result = os.fstat(fd)
        if (not stat_module.S_ISDIR(result.st_mode)
                or result.st_uid != os.geteuid()
                or result.st_mode & 0o077):
            raise BackupCoordinatorError(
                "private directory %r is not a 0700 directory owned by this "
                "user: mode=%s" % (name, oct(result.st_mode & 0o777))
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _fsync_dir(path):
    fd = os.open(str(path), _DIRECTORY_OPEN_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _journal_path(root):
    return root / _COORDINATOR_DIRNAME / _JOURNAL_FILENAME


def _connect_journal(root):
    coordinator_fd = _ensure_private_dir(_open_root_fd(root),
                                         _COORDINATOR_DIRNAME)
    try:
        journal_path = _journal_path(root)
        fresh = not journal_path.exists()
        journal = sqlite3.connect(
            journal_path.as_uri() + "?mode=rwc", uri=True
        )
        if fresh:
            # The 0700 parent already denies access; tighten the file too.
            with suppress(OSError):
                os.chmod(journal_path, 0o600)
    finally:
        os.close(coordinator_fd)
    journal.row_factory = sqlite3.Row
    journal.execute("PRAGMA synchronous=FULL")
    journal.execute(
        "CREATE TABLE IF NOT EXISTS freeze_journal ("
        "backup_id TEXT PRIMARY KEY, "
        "state TEXT NOT NULL CHECK (state IN "
        "('preparing','frozen','failed','incomplete')), "
        "barrier_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "detail TEXT NOT NULL)"
    )
    journal.execute("PRAGMA user_version = %d" % _JOURNAL_SCHEMA_VERSION)
    journal.commit()
    return journal


def _open_root_fd(root):
    return os.open(str(root), _DIRECTORY_OPEN_FLAGS)


def _journal_insert_preparing(journal, backup_id, barrier_id, created_at):
    try:
        with journal:
            journal.execute(
                "INSERT INTO freeze_journal (backup_id, state, barrier_id, "
                "created_at, updated_at, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (backup_id, PREPARING, barrier_id, created_at, created_at,
                 _journal_dumps({"reason": "preparing"})),
            )
    except sqlite3.IntegrityError:
        row = journal.execute(
            "SELECT state FROM freeze_journal WHERE backup_id = ?",
            (backup_id,),
        ).fetchone()
        raise FreezeAlreadyRecordedError(backup_id, row["state"]) \
            from None


def _journal_finish(journal, backup_id, state, updated_at, detail):
    with journal:
        journal.execute(
            "UPDATE freeze_journal SET state = ?, updated_at = ?, detail = ? "
            "WHERE backup_id = ?",
            (state, updated_at, _journal_dumps(detail), backup_id),
        )


def _read_journal(retention_root, backup_id=None):
    path = _journal_path(Path(retention_root))
    if not path.is_file():
        return () if backup_id is None else None
    journal = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    journal.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM freeze_journal"
        arguments = ()
        if backup_id is not None:
            query += " WHERE backup_id = ?"
            arguments = (backup_id,)
        query += " ORDER BY backup_id"
        rows = journal.execute(query, arguments).fetchall()
    finally:
        journal.close()
    records = tuple(
        FreezeRecord(
            backup_id=row["backup_id"],
            state=row["state"],
            barrier_id=row["barrier_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            detail=json.loads(row["detail"], parse_float=Decimal),
        )
        for row in rows
    )
    if backup_id is not None:
        return records[0] if records else None
    return records


def read_freeze_record(retention_root, backup_id):
    """Return the durable freeze record for one id, or None if absent.

    Host-side read of the D1 minimal journal. A row left in
    ``preparing`` after a crash is reported as-is; this slice performs
    no resume and no cleanup.
    """
    _host_text(backup_id, "backup_id")
    return _read_journal(retention_root, backup_id)


def list_freeze_records(retention_root):
    """Return every durable freeze record under a retention root."""
    return _read_journal(retention_root, None)


def _acquire_barrier(coordination, budget, cancel_event, wait_started):
    """Take the canonical rank-1 maintenance flock, bounded and cancellable.

    Lock-order violations are the broker module's own errors, raised
    before any flock attempt.
    """
    try:
        if coordination.db.in_transaction:
            raise LockOrderError(
                "host transaction precedes the maintenance barrier"
            )
        frames = getattr(_COORDINATION_LOCAL, "frames", [])
        if frames:
            raise LockOrderError(
                "nested or reversed coordination entrance before freeze"
            )
    except LockOrderError:
        raise
    domain_fd = os.open(
        str(coordination.directory), _DIRECTORY_OPEN_FLAGS
    )
    lock_fd = None
    try:
        lock_fd = os.open(
            _pinned(domain_fd, "maintenance.lock"), _LOCK_OPEN_FLAGS, 0o600
        )
        deadline = wait_started + budget.wait_timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Ownership of both fds transfers to the caller on success.
                held_fd, lock_fd = lock_fd, -1
                domain_fd, returned_domain_fd = -1, domain_fd
                return held_fd, returned_domain_fd
            except BlockingIOError:
                pass
            except OSError as exc:
                raise BarrierUnavailableError(
                    "maintenance barrier lock is unusable: %s" % exc
                ) from exc
            if cancel_event is not None and cancel_event.is_set():
                raise FreezeCancelled(
                    "cancelled",
                    "cancellation requested while waiting for the "
                    "maintenance barrier",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise BarrierUnavailableError(
                    "maintenance barrier could not be acquired within the "
                    "%.3fs wait budget" % budget.wait_timeout
                )
            time.sleep(min(budget.poll_interval, remaining))
    finally:
        if lock_fd is not None and lock_fd != -1:
            os.close(lock_fd)
        if domain_fd != -1:
            os.close(domain_fd)


def _release_barrier(held_fd, domain_fd):
    # Releasing the flock and closing the descriptors is the only
    # barrier teardown; the coordination ledger is not touched.
    for descriptor in (held_fd, domain_fd):
        with suppress(OSError):
            os.close(descriptor)


def _new_barrier_id():
    stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S"
    )
    return "b%s-%s" % (stamp, secrets.token_hex(4))


def _observed_companies(spec):
    """Run one host scope probe; normalize to a tuple of strings."""
    try:
        observed = spec.scope_probe(spec.connection)
    except Exception as exc:  # host probe failure is scope evidence, not a crash
        return ("<scope probe error: %s>" % type(exc).__name__,)
    if not isinstance(observed, (tuple, list)) or not observed:
        return ("<scope probe returned no company evidence>",)
    normalized = []
    for value in observed:
        if not isinstance(value, str) or not value:
            return ("<scope probe returned a non-string company id>",)
        normalized.append(value)
    return tuple(normalized)


def _authority_fix(assembly, backup_id, barrier_id, identity,
                   hold_deadline, cancel_event):
    """Capture and pin H. Returns (evidence, mismatches) or raises terminal."""
    repository = assembly.repository
    head = repository.head_oid()
    integrity = repository.verify_integrity(expected_head=head)
    snapshot = repository.read_snapshot_at(head)
    mismatches = []
    meta = None
    for record in snapshot.records:
        if record.path == _META_RECORD_PATH:
            meta = loads_record(record.content)
            break
    if not isinstance(meta, dict):
        mismatches.append(ScopeObservation(
            "authority", "meta", identity.company_id,
            ("<repository identity record unreadable>",),
        ))
    else:
        for key in _IDENTITY_KEYS:
            if meta.get(key) != getattr(identity, key):
                mismatches.append(ScopeObservation(
                    "authority", key, getattr(identity, key),
                    (str(meta.get(key)),),
                ))
    if mismatches:
        return None, tuple(mismatches)

    # H is captured and proven before the pin; a cancel/deadline signal
    # arriving during that work must stop before a named pin exists.
    _check_held_budget(
        hold_deadline, cancel_event, "authority-pin"
    )
    pin_result = repository.create_maintenance_pin(
        backup_id, barrier_id, head
    )
    if not pin_result.created:
        return None, (ScopeObservation(
            "authority", "pin", head,
            ("<pin identity already existed: %s>" % pin_result.pin.commit_oid,),
        ),)
    # Independent readback through the strict read-only primitive.
    readback = repository.read_maintenance_pin(backup_id, barrier_id)
    if readback is None or readback.commit_oid != head:
        return None, (ScopeObservation(
            "authority", "pin-readback", head,
            ("<pin target could not be read back as H>",),
        ),)
    evidence = AuthorityEvidence(
        ref=REF_FACTS,
        commit_oid=head,
        object_format=integrity.object_format,
        genesis_oid=integrity.genesis_oid,
        commit_count=integrity.commit_count,
        pin_ref=pin_result.pin.ref,
        pin_created=True,
    )
    return evidence, ()


def _check_held_budget(hold_deadline, cancel_event, phase):
    if cancel_event is not None and cancel_event.is_set():
        raise FreezeCancelled(
            "cancelled",
            "cancellation requested during freeze: %s" % phase,
        )
    if time.monotonic() >= hold_deadline:
        raise FreezeCancelled(
            "hold-timeout",
            "maintenance barrier hold budget expired during freeze: %s"
            % phase,
        )


def freeze_consistent_cut(assembly):
    """Freeze one consistent H+W cut inside one maintenance barrier hold.

    See module docstring for the full contract. Structural assembly
    problems raise :class:`AssemblyValidationError` before anything is
    written; a physical single-company scope mismatch observed over a
    live W connection raises :class:`ScopeMismatchError` listing every
    offending database, likewise before the barrier; a duplicate
    backup_id raises :class:`FreezeAlreadyRecordedError`; a barrier that
    cannot be taken in budget raises :class:`BarrierUnavailableError`;
    cancellation or hold-budget expiry raises :class:`FreezeCancelled`.
    Every other failure is returned as terminal ``FreezeEvidence``
    (status ``failed`` or ``incomplete``).
    """
    backup_id, identity, databases, root = _validate_assembly(assembly)
    budget = assembly.budget
    cancel_event = assembly.cancel_event

    # Physical single-company scope evidence is part of the pre-barrier
    # gate (design appendix C1 section 4): a database that cannot prove
    # the declared company is refused before any preparing row, barrier,
    # pin or checkpoint exists. The in-barrier phase re-probes every W
    # right before its checkpoint so the evidence is cut evidence.
    pre_barrier_mismatches = []
    for spec in databases:
        observed = _observed_companies(spec)
        if set(observed) != {identity.company_id}:
            pre_barrier_mismatches.append(ScopeObservation(
                "database", spec.role, identity.company_id, observed
            ))
    if pre_barrier_mismatches:
        raise ScopeMismatchError(
            "one or more W databases are outside the declared "
            "single-company scope; no freeze was started",
            pre_barrier_mismatches,
        )

    barrier_id = _new_barrier_id()
    # The generated id must satisfy the pin charset by construction.
    maintenance_pin_ref(backup_id, barrier_id)

    started_at = _utc_now()
    started_monotonic = time.monotonic()
    journal = _connect_journal(root)
    try:
        _journal_insert_preparing(journal, backup_id, barrier_id, started_at)

        held_fd = domain_fd = None
        entered_monotonic = None
        try:
            held_fd, domain_fd = _acquire_barrier(
                assembly.coordination, budget, cancel_event,
                started_monotonic
            )
            entered_monotonic = time.monotonic()
            hold_deadline = entered_monotonic + budget.hold_timeout
            return _freeze_inside_barrier(
                assembly=assembly,
                backup_id=backup_id,
                barrier_id=barrier_id,
                identity=identity,
                databases=databases,
                root=root,
                journal=journal,
                budget=budget,
                cancel_event=cancel_event,
                hold_deadline=hold_deadline,
                started_at=started_at,
                started_monotonic=started_monotonic,
                entered_monotonic=entered_monotonic,
            )
        except BarrierUnavailableError:
            # Bounded wait-budget failure: no cut products; the
            # preparing row becomes a recognizable aborted attempt.
            _finish_if_preparing(
                journal, backup_id, INCOMPLETE, barrier_id,
                reason="barrier-timeout", staging_dir=None,
            )
            raise
        except FreezeCancelled as exc:
            # Wait-phase cancellation (the inner phase records hold-phase
            # cancellation itself and leaves the row non-preparing).
            _finish_if_preparing(
                journal, backup_id, INCOMPLETE, barrier_id,
                reason=exc.reason, staging_dir=None,
            )
            raise
        except LockOrderError:
            _finish_if_preparing(
                journal, backup_id, FAILED, barrier_id,
                reason="lock-order", staging_dir=None,
            )
            raise
        except BaseException:
            # Control-flow exceptions (KeyboardInterrupt/SystemExit) are
            # never disguised: mark what we can, then re-raise.
            _finish_if_preparing(
                journal, backup_id, INCOMPLETE, barrier_id,
                reason="interrupted", staging_dir=None,
            )
            raise
        finally:
            if held_fd is not None:
                _release_barrier(held_fd, domain_fd)
    finally:
        journal.close()


def _finish_if_preparing(journal, backup_id, state, barrier_id, *, reason,
                         staging_dir, **extra):
    """Finish the journal row only while it still reads ``preparing``.

    Used by outer abort paths so an inner phase's precise terminal
    reason is never overwritten.
    """
    detail = {"reason": reason}
    if staging_dir is not None:
        detail["staging_dir"] = str(staging_dir)
    detail.update(extra)
    try:
        with journal:
            journal.execute(
                "UPDATE freeze_journal SET state = ?, updated_at = ?, "
                "detail = ? WHERE backup_id = ? AND state = 'preparing'",
                (state, _utc_now(), _journal_dumps(detail), backup_id),
            )
    except sqlite3.Error:
        # The preparing row staying visible is the honest outcome; a
        # later host must inspect rather than assume completion.
        pass


def _freeze_inside_barrier(*, assembly, backup_id, barrier_id, identity,
                           databases, root, journal, budget, cancel_event,
                           hold_deadline, started_at, started_monotonic,
                           entered_monotonic):
    repository = assembly.repository
    # The hold budget is checked before any in-barrier artifact, so a
    # deadline already crossed while waiting on the lock still aborts
    # with nothing created.
    _check_held_budget(hold_deadline, cancel_event, "barrier-entry")
    root_fd = _open_root_fd(root)
    staging_dir = None
    staging_fd = None
    published_cut = None
    try:
        staging_root_fd = _ensure_private_dir(root_fd, _STAGING_DIRNAME)
        try:
            staging_name = "%s-%s-%s" % (
                backup_id, barrier_id, secrets.token_hex(4))
            os.mkdir(staging_name, 0o700, dir_fd=staging_root_fd)
            staging_fd = os.open(
                _pinned(staging_root_fd, staging_name), _DIRECTORY_OPEN_FLAGS
            )
            staging_dir = root / _STAGING_DIRNAME / staging_name
        finally:
            os.close(staging_root_fd)

        def terminal(status, reason, *, authority=None, checkpoints=(),
                     mismatches=(), detail_text=None, core_digest=None,
                     cut_dir=None, extra=None):
            # A cut that reached the completed position must not stay
            # there on a non-frozen outcome: move it back into staging as
            # retained debris so the completed position never advertises
            # an all-or-nothing cut that did not complete.
            if cut_dir is not None:
                cut_dir = _quarantine_published_cut(
                    root, root_fd, backup_id, barrier_id, cut_dir
                )
            if staging_fd is not None:
                _remove_empty_staging(staging_fd, staging_dir)
            held_seconds = time.monotonic() - entered_monotonic
            record_detail = {
                "reason": reason,
                "staging_dir": str(staging_dir) if staging_dir else None,
                "cut_dir": str(cut_dir) if cut_dir else None,
            }
            if extra:
                record_detail.update(extra)
            _journal_finish(journal, backup_id, status, _utc_now(),
                            record_detail)
            return FreezeEvidence(
                backup_id=backup_id,
                barrier_id=barrier_id,
                status=status,
                reason=reason,
                company_id=identity.company_id,
                core_digest=core_digest,
                authority=authority,
                checkpoints=tuple(checkpoints),
                scope_mismatches=tuple(mismatches),
                cut_dir=cut_dir,
                staging_dir=staging_dir,
                detail=detail_text,
                started_at=started_at,
                finished_at=_utc_now(),
                wait_seconds=entered_monotonic - started_monotonic,
                held_seconds=held_seconds,
            )

        _check_held_budget(hold_deadline, cancel_event, "authority-fix")
        try:
            authority, mismatches = _authority_fix(
                assembly, backup_id, barrier_id, identity,
                hold_deadline, cancel_event,
            )
        except FreezeCancelled:
            raise
        except (GitRepositoryError, MaintenancePinError, IntegrityError,
                SnapshotReadError) as exc:
            return terminal(
                FAILED, "authority-fix-failed",
                detail_text="H could not be captured or pinned: %s" % exc,
            )
        if mismatches:
            return terminal(
                FAILED, "scope-mismatch", mismatches=mismatches,
                detail_text="authoritative repository scope does not match "
                            "the declared installation",
            )

        # Physical scope evidence for every W before any checkpoint is
        # produced, so a foreign-company database never yields a file.
        scope_mismatches = []
        observed_scopes = {}
        for spec in databases:
            _check_held_budget(hold_deadline, cancel_event,
                               "scope-probe:%s" % spec.role)
            observed = _observed_companies(spec)
            observed_scopes[spec.role] = observed
            if set(observed) != {identity.company_id}:
                scope_mismatches.append(ScopeObservation(
                    "database", spec.role, identity.company_id, observed
                ))
        if scope_mismatches:
            return terminal(
                FAILED, "scope-mismatch", authority=authority,
                mismatches=scope_mismatches,
                detail_text="one or more W databases are outside the "
                            "declared single-company scope",
                extra={"observed_scopes": {
                    role: list(values)
                    for role, values in observed_scopes.items()}},
            )

        # W checkpoints, strictly in assembly order, each published while
        # this barrier is still held (the checkpoint primitive's only
        # durable-success condition).
        outcomes = []
        for spec in databases:
            _check_held_budget(hold_deadline, cancel_event,
                               "checkpoint:%s" % spec.role)
            destination = staging_dir / (spec.role + _CHECKPOINT_SUFFIX)
            try:
                result = create_sqlite_checkpoint(spec.connection, destination)
            except SqliteCheckpointError as exc:
                if exc.publication == PublicationState.UNCERTAIN:
                    status, reason = (
                        INCOMPLETE,
                        "checkpoint-publication-uncertain",
                    )
                    detail_text = (
                        "W %r publication is UNCERTAIN; inspect the staging "
                        "directory before any retry: %s" % (spec.role, exc)
                    )
                elif exc.publication == PublicationState.CONFIRMED:
                    status, reason = FAILED, "checkpoint-staging-cleanup-failed"
                    detail_text = (
                        "W %r is durably published but its staging cleanup "
                        "failed; the cut is not accepted: %s" % (spec.role, exc)
                    )
                else:
                    status, reason = FAILED, "checkpoint-failed"
                    detail_text = "W %r checkpoint failed: %s" % (spec.role, exc)
                return terminal(
                    status, reason, authority=authority, checkpoints=outcomes,
                    detail_text=detail_text,
                    extra={"failing_role": spec.role,
                           "publication": exc.publication},
                )
            outcomes.append(WorkCheckpointEvidence(
                role=spec.role,
                relative_path=str(Path(
                    _CUTS_DIRNAME, backup_id, barrier_id,
                    spec.role + _CHECKPOINT_SUFFIX)),
                size_bytes=result.size_bytes,
                sha256_digest=result.digest,
                schema=dict(spec.schema),
                scope_watermark=dict(spec.scope_watermark),
            ))

        # All checkpoints are durable in the private staging directory;
        # publish the whole cut atomically (same filesystem rename) into
        # the completed position, still inside the barrier.
        _check_held_budget(hold_deadline, cancel_event, "cut-publish")
        published_cut = _publish_cut_directory(
            root_fd, root, backup_id, barrier_id, staging_dir
        )
        cut_dir = published_cut
        if cut_dir is None:
            return terminal(
                INCOMPLETE, "cut-publish-uncertain", authority=authority,
                checkpoints=outcomes,
                detail_text="staging cut could not be proven published or "
                            "refused because the target position existed; "
                            "inspect the staging directory",
            )

        cut_fd = os.open(str(cut_dir), _DIRECTORY_OPEN_FLAGS)
        try:
            verified = []
            try:
                for evidence in outcomes:
                    _check_held_budget(hold_deadline, cancel_event,
                                       "verify:%s" % evidence.role)
                    verify_backup_component(
                        cut_fd,
                        relative_path=evidence.role + _CHECKPOINT_SUFFIX,
                        size_bytes=evidence.size_bytes,
                        sha256_digest=evidence.sha256_digest,
                    )
                    verified.append(evidence)
            except BackupComponentValidationError as exc:
                return terminal(
                    FAILED, "component-verification-failed",
                    authority=authority, checkpoints=outcomes, cut_dir=cut_dir,
                    detail_text="published component failed independent "
                                "verification: %s" % exc,
                )
        finally:
            os.close(cut_fd)

        entered_at = _utc_now()
        fixed_at = _utc_now()
        frozen_wait_seconds = _exact_seconds(
            entered_monotonic - started_monotonic
        )
        frozen_held_seconds = _exact_seconds(
            time.monotonic() - entered_monotonic
        )
        core_payload = {
            "schema": CORE_SCHEMA,
            "version": CORE_VERSION,
            "backup_id": backup_id,
            "barrier_id": barrier_id,
            "installation_id": identity.installation_id,
            "company_id": identity.company_id,
            "repo_id": identity.repo_id,
            "authority": {
                "ref": authority.ref,
                "commit_oid": authority.commit_oid,
                "object_format": authority.object_format,
                "genesis_oid": authority.genesis_oid,
                "commit_count": authority.commit_count,
                "pin_ref": authority.pin_ref,
            },
            "databases": [
                {
                    "role": item.role,
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "sha256_digest": item.sha256_digest,
                    "schema": item.schema,
                    "scope_watermark": item.scope_watermark,
                }
                for item in verified
            ],
            "barrier": {
                "ref": "maintenance.lock",
                "wait_timeout": _exact_seconds(budget.wait_timeout),
                "hold_timeout": _exact_seconds(budget.hold_timeout),
                "wait_seconds": frozen_wait_seconds,
                "held_seconds": frozen_held_seconds,
                "entered_at": entered_at,
                "fixed_at": fixed_at,
            },
        }
        try:
            core_digest = compute_core_digest(core_payload)
        except (TypeError, ValueError) as exc:
            return terminal(
                FAILED, "core-digest-failed", authority=authority,
                checkpoints=verified, cut_dir=cut_dir,
                detail_text="core payload could not be digested: %s" % exc,
            )

        finished_at = _utc_now()
        held_seconds = time.monotonic() - entered_monotonic
        record_detail = {
            "reason": "frozen",
            "core_digest": core_digest,
            "core_payload": core_payload,
            "cut_dir": str(cut_dir),
        }
        try:
            _journal_finish(journal, backup_id, FROZEN, finished_at,
                            record_detail)
        except sqlite3.Error as exc:
            # The completion marker could not be durably recorded: the
            # cut must not remain at the completed position pretending
            # success. Move it back into staging best-effort and report
            # incomplete for host inspection; never retry over it.
            quarantine = _quarantine_published_cut(root, root_fd, backup_id,
                                                   barrier_id, cut_dir)
            return FreezeEvidence(
                backup_id=backup_id,
                barrier_id=barrier_id,
                status=INCOMPLETE,
                reason="freeze-record-undurable",
                company_id=identity.company_id,
                core_digest=None,
                authority=authority,
                checkpoints=tuple(verified),
                scope_mismatches=(),
                cut_dir=quarantine,
                staging_dir=staging_dir,
                detail="frozen evidence could not be durably recorded: %s"
                       % exc,
                started_at=started_at,
                finished_at=_utc_now(),
                wait_seconds=entered_monotonic - started_monotonic,
                held_seconds=held_seconds,
            )

        return FreezeEvidence(
            backup_id=backup_id,
            barrier_id=barrier_id,
            status=FROZEN,
            reason=None,
            company_id=identity.company_id,
            core_digest=core_digest,
            authority=authority,
            checkpoints=tuple(verified),
            scope_mismatches=(),
            cut_dir=cut_dir,
            staging_dir=staging_dir,
            detail=None,
            started_at=started_at,
            finished_at=finished_at,
            wait_seconds=entered_monotonic - started_monotonic,
            held_seconds=held_seconds,
        )
    except FreezeCancelled as exc:
        final_position = published_cut
        if final_position is not None:
            final_position = _quarantine_published_cut(
                root, root_fd, backup_id, barrier_id, final_position
            )
        elif staging_fd is not None:
            _remove_empty_staging(staging_fd, staging_dir)
        cancel_detail = {
            "reason": exc.reason,
            "staging_dir": str(staging_dir) if staging_dir else None,
            "cut_dir": str(final_position) if final_position else None,
        }
        try:
            _journal_finish(journal, backup_id, INCOMPLETE, _utc_now(),
                            cancel_detail)
        except sqlite3.Error:
            pass
        raise
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(root_fd)


def _publish_cut_directory(root_fd, root, backup_id, barrier_id, staging_dir):
    """Atomically rename the staging cut into cuts/<backup_id>/<barrier_id>.

    Every directory operation is performed relative to pinned
    descriptors of the retention tree; the leaf itself is never opened
    through a path that could have become a symlink. Returns the
    completed-position directory, or None when the target position
    already exists (no-overwrite refusal) or the rename could not be
    proven. The staging directory is left intact on every failure.
    """
    cuts_fd = _ensure_private_dir(root_fd, _CUTS_DIRNAME)
    backup_fd = None
    try:
        backup_fd = _ensure_private_dir(cuts_fd, backup_id)
        try:
            probe = os.open(
                _pinned(backup_fd, barrier_id),
                getattr(os, "O_PATH", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            # Target position exists: never replace an existing cut.
            os.close(probe)
            return None
        try:
            os.rename(
                staging_dir,
                root / _CUTS_DIRNAME / backup_id / barrier_id,
            )
        except OSError:
            return None
        _fsync_dir(root / _CUTS_DIRNAME / backup_id)
        _fsync_dir(root / _CUTS_DIRNAME)
        _fsync_dir(root / _STAGING_DIRNAME)
        return root / _CUTS_DIRNAME / backup_id / barrier_id
    except OSError:
        return None
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(cuts_fd)


def _quarantine_published_cut(root, root_fd, backup_id, barrier_id, cut_dir):
    """Best-effort move of an un-recordable cut back under .staging."""
    quarantine = (
        root / _STAGING_DIRNAME
        / ("%s-%s-quarantine-%s" % (backup_id, barrier_id, secrets.token_hex(4)))
    )
    try:
        os.rename(cut_dir, quarantine)
        _fsync_dir(root / _CUTS_DIRNAME / backup_id)
        _fsync_dir(root / _STAGING_DIRNAME)
        return quarantine
    except OSError:
        return cut_dir


def _remove_empty_staging(staging_fd, staging_dir):
    """Drop the private staging directory only when it holds no debris."""
    try:
        if os.listdir(staging_fd):
            return
    except OSError:
        return
    try:
        os.rmdir(staging_dir)
    except OSError:
        pass
