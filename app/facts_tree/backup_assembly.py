"""F-001 vault assembly/publish slice (appendix A1).

This module is the *assembly director* for one already-frozen consistent
cut. Holding no barrier and never modifying frozen artifacts, it

1. loads the durable frozen freeze record (``read_freeze_record``) and
   refuses every non-frozen state;
2. re-verifies each evidence family independently:
   - H: the maintenance pin still names the exact frozen commit, and a
     fresh offline bundle of that one pin ref is exported through the
     repository primitive (which runs its own bundle verify / single
     head / empty-repo fetch / fsck suite, never ``--all``); assembly
     additionally hashes the exported bytes itself before and after the
     crash-injection point and independently runs
     ``git bundle list-heads`` to prove exactly one advertised ref;
   - W: every cut checkpoint is byte-verified with
     ``verify_backup_component`` (sha256/size, O_NOFOLLOW, no symlink),
     and the cut directory must contain no undeclared entry;
   - R: ``collect_object_closure`` recomputes the object closure for the
     declared references against the real content-addressed stores;
   - R2: when a version-index root is supplied, the append-only mapping
     ledger is reopened under the cut scope, its bytes re-verified
     (``ledger_shape(verify=True)``) and its stable ledger digest/shape
     folded into the assembly core;
3. builds the single digest chain exclusively through
   ``build_verification_report -> build_final_manifest ->
   verify_final_manifest`` (no second serialization or hash scheme);
4. publishes the package directory atomically (exclusive 0600 files +
   fsync, whole-directory rename, target-exists refusal, every walk step
   dir_fd/O_NOFOLLOW, 0700 directories, no symlink, no group/world
   write), and only then moves the ``current`` pointer with one atomic
   rename (temp file + fsync + rename + directory fsync), so a crash can
   never leave a half or dangling pointer;
5. records failed/incomplete families as outcome evidence; ``current``
   is never moved unless every family is green.

The module is pure library: it imports no SAM webapp/harness/employees
code, nothing imports it from the application graph, it never connects
to the network, invokes a model, sends media, deletes/GCs objects, runs
on a schedule or touches online writes. Failed runs leave their staging
directory in place (never delete, never repair, never substitute);
re-running an unfinished assembly is safe and converges, while a
successfully published backup_id is recognizably rejected.

Linux/WSL only: the hardening requires dir_fd walks, O_NOFOLLOW,
O_PATH/O_DIRECTORY and /proc/self/fd; missing capabilities fail closed.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.facts_tree import git_format
from app.facts_tree.backup_component_validation import (
    BackupComponentValidationError,
    verify_backup_component,
)
from app.facts_tree.backup_coordinator import (
    FROZEN,
    AssemblyIdentity,
    read_freeze_record,
)
from app.facts_tree.backup_manifest import (
    DIGEST_ALGORITHM,
    BackupManifestError,
    build_final_manifest,
    build_verification_report,
    compute_report_digest,
    verify_final_manifest,
)
from app.facts_tree.backup_object_closure import (
    CLOSURE_COMPLETE,
    CLOSURE_INCOMPLETE,
    OBJECT_TYPES,
    ROOT_H,
    ROOT_W,
    SCOPE_COMPANY,
    SCOPE_INSTALLATION,
    STATUS_CORRUPT,
    STATUS_MISSING,
    STATUS_NOT_EXTERNALIZED,
    STATUS_OUT_OF_SCOPE,
    STATUS_PRESENT,
    STATUS_UNSAFE_PATH,
    ClosureScope,
    ClosureStores,
    ObjectClosureError,
    ObjectReference,
    ObjectScope,
    ReferenceRoot,
    collect_object_closure,
)
from app.facts_tree.git_repository import (
    BundleExportError,
    BundleOutputExistsError,
    FactsGitRepository,
    GitCommandError,
    GitRepositoryError,
    MaintenancePinError,
    PinNotFoundError,
    maintenance_pin_ref,
)
from app.facts_tree.mapping_version_index import (
    SCHEMA as MAPPING_LEDGER_SCHEMA,
    MappingVersionIndexError,
    MappingVersionLedger,
    MappingVersionScopeError,
)

# -- schemas / layout ---------------------------------------------------------

ASSEMBLY_CORE_SCHEMA = "sam.f001.backup_assembly_core"
CURRENT_SCHEMA = "sam.f001.backup_current"
OUTCOME_SCHEMA = "sam.f001.backup_assembly_outcome"
SCHEMA_VERSION = 1

ASSEMBLY_DIRNAME = ".assembly"
STAGING_DIRNAME = "staging"
PACKAGES_DIRNAME = "packages"
OUTCOMES_DIRNAME = "outcomes"
CURRENT_NAME = "current"
CURRENT_TMP_PREFIX = "current.tmp-"
PUBLISH_LOG_NAME = "publish.log"

BUNDLE_NAME = "authority.bundle"
MANIFEST_NAME = "manifest.json"
REPORT_NAME = "report.json"
OUTCOME_NAME = "outcome.json"

# Public filesystem layout of MappingVersionLedger; assembly requires an
# already-initialized ledger and never lets the constructor create one
# silently (a missing ledger is an R2 failure, not an empty success).
LEDGER_DB_FILENAME = "mapping-version-index.sqlite"

# -- result vocabulary --------------------------------------------------------

STATUS_PUBLISHED = "published"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"

FAMILY_H = "H"
FAMILY_W = "W"
FAMILY_R = "R"
FAMILY_R2 = "R2"
FAMILY_PUBLISH = "publish"

SEVERITY_FAILED = "failed"
SEVERITY_INCOMPLETE = "incomplete"

# Crash injection phases (storage/process boundary seam for tests).
PHASE_BUNDLE_EXPORTED = "bundle_exported"
PHASE_MANIFEST_STAGED = "manifest_staged"
PHASE_PACKAGE_PUBLISHED = "package_published"
PHASE_CURRENT_SWITCHED = "current_switched"
CRASH_PHASES = frozenset((
    PHASE_BUNDLE_EXPORTED,
    PHASE_MANIFEST_STAGED,
    PHASE_PACKAGE_PUBLISHED,
    PHASE_CURRENT_SWITCHED,
))

# Stable failure reason codes. They are digest-bound report content, so
# they deliberately carry no exception text, absolute path or wall clock.
R_PIN_MISSING = "pin_missing"
R_PIN_TARGET_MOVED = "pin_target_moved"
R_PIN_UNREADABLE = "pin_unreadable"
R_BUNDLE_EXPORT_FAILED = "bundle_export_failed"
R_BUNDLE_BYTES_MISMATCH = "bundle_bytes_mismatch"
R_BUNDLE_CHANGED = "bundle_changed_after_export"
R_BUNDLE_HEADS = "bundle_advertised_refs"
R_COMPONENT_FAILED = "component_verification_failed"
R_UNDECLARED_COMPONENT = "undeclared_cut_component"
R_NON_REGULAR_COMPONENT = "non_regular_cut_component"
R_CLOSURE_STATUS = "closure_not_publishable"
R_MALFORMED_REFERENCE = "malformed_reference_declaration"
R_OBJECT_FORMAT_UNSUPPORTED = "closure_unsupported_object_format"
R_LEDGER_SCOPE = "ledger_foreign_scope"
R_LEDGER_MISSING = "ledger_not_initialized"
R_LEDGER_GAPS = "ledger_byte_gaps"
R_LEDGER_UNREADABLE = "ledger_unreadable"
R_PACKAGE_CONFLICT = "published_package_conflict"
R_FREEZE_RECORD_MALFORMED = "freeze_record_malformed"

_PACKAGE_FILES = (BUNDLE_NAME, MANIFEST_NAME, REPORT_NAME)
_ENTRY_STATUSES = (
    STATUS_PRESENT,
    STATUS_MISSING,
    STATUS_NOT_EXTERNALIZED,
    STATUS_CORRUPT,
    STATUS_UNSAFE_PATH,
    STATUS_OUT_OF_SCOPE,
)
_GAP_OR_WORSE = frozenset(_ENTRY_STATUSES[1:])

_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_SAFE_ROLE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_PACKAGE_COORDINATE_RE = re.compile(
    r"\Apackages/(?P<bid>[A-Za-z0-9_-]{1,64})/"
    r"(?P<bar>[A-Za-z0-9_-]{1,64})\Z"
)
_DIGEST_PREFIX = DIGEST_ALGORITHM + ":"

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | _O_CLOEXEC
_PATH_FLAGS = getattr(os, "O_PATH", 0) | os.O_NOFOLLOW | _O_CLOEXEC
_FILE_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC
)
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_PROC_FD = "/proc/self/fd"
_READ_CHUNK = 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30


# -- errors -------------------------------------------------------------------


class BackupAssemblyError(Exception):
    """Base class: assembly cannot run for structural/storage reasons."""

    def __init__(self, message, *, family=None, reason=None):
        super().__init__(message)
        self.family = family
        self.reason = reason


class CutNotFrozenError(BackupAssemblyError):
    """The freeze record is absent or not in the frozen state."""

    def __init__(self, message, *, state=None, reason=None):
        super().__init__(message, family=FAMILY_H, reason=reason)
        self.state = state


class AlreadyPublishedError(BackupAssemblyError):
    """This backup_id is already published and current points at it."""

    def __init__(self, backup_id, coordinate):
        super().__init__(
            "backup_id %r is already published at %s; refusing a second "
            "publication" % (backup_id, coordinate),
            family=FAMILY_PUBLISH,
            reason="already_published",
        )
        self.backup_id = backup_id
        self.coordinate = coordinate


class AssemblyScopeMismatchError(BackupAssemblyError):
    """The plan identity does not match the frozen cut's single scope."""

    def __init__(self, message, *, reason="scope_mismatch"):
        super().__init__(message, family=FAMILY_PUBLISH, reason=reason)


class AssemblyPackageConflictError(BackupAssemblyError):
    """The package position already holds different, unverifiable bytes."""

    def __init__(self, coordinate, reasons=()):
        super().__init__(
            "package position %s exists and does not verify against the "
            "re-computed frozen evidence; refusing to overwrite: %s"
            % (coordinate, "; ".join(reasons) or "content mismatch"),
            family=FAMILY_PUBLISH,
            reason=R_PACKAGE_CONFLICT,
        )
        self.coordinate = coordinate
        self.reasons = tuple(reasons)


class AssemblyStorageError(BackupAssemblyError):
    """The retention layout violates the private-non-symlink contract."""


# -- plan / result value objects ----------------------------------------------


@dataclass(frozen=True)
class AssemblyCitation:
    """One provenance citation for a declared R reference.

    ``kind`` is "H" or "W". H citations name the frozen commit: leave
    ``root_id`` None and assembly pins it to the exact frozen commit oid
    (an explicitly supplied H root_id must equal it). W citations name
    the checkpoint identifier explicitly. ``coordinate`` is the exact
    stable record path / table-row locator carrying the citation.
    """

    kind: str
    coordinate: str
    root_id: str | None = None


@dataclass(frozen=True)
class AssemblyReference:
    """One caller-declared R object citation for this cut.

    Mirrors :class:`~app.facts_tree.backup_object_closure.ObjectReference`
    input without binding callers to its root/scope constructors:
    ``sha256_digest`` is ``"sha256:"+64hex`` for an externalized object
    (mapping versions live in the mapping-object cabinet, everything
    else in the archive store), or None for an object still inline in W.
    """

    logical_id: str
    object_type: str
    version: str
    scope_kind: str
    citations: tuple[AssemblyCitation, ...]
    sha256_digest: str | None = None
    key_reference: str | None = None
    storage_class: str | None = None
    relative_path: str | None = None


@dataclass(frozen=True)
class AssemblyPlan:
    """Everything assembly needs for one cut; it never mutates these."""

    identity: AssemblyIdentity
    repository: FactsGitRepository
    retention_root: Path
    stores: ClosureStores
    references: tuple[AssemblyReference, ...] = ()
    version_index_root: Path | None = None


@dataclass(frozen=True)
class AssemblyFailure:
    """One stable, digest-safe failure coordinate (family + reason code)."""

    family: str
    check: str
    coordinate: str
    reason: str
    severity: str = SEVERITY_FAILED

    def normalized(self):
        return {
            "family": self.family,
            "check": self.check,
            "coordinate": self.coordinate,
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class CurrentPointer:
    """The parsed, identity-checked contents of the ``current`` pointer."""

    backup_id: str
    barrier_id: str
    identity: AssemblyIdentity
    package: str
    core_digest: str
    report_digest: str


@dataclass(frozen=True)
class PackageState:
    """Classification of one packages/<bid>/<barrier> directory."""

    coordinate: str
    backup_id: str | None
    barrier_id: str | None
    status: str  # "complete" | "damaged"
    core_digest: str | None
    report_digest: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class StagingState:
    """Classification of one leftover staging directory."""

    name: str
    status: str  # "verified" (full chain, crash before rename) | "partial"
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeState:
    """One recorded failed/incomplete assembly outcome."""

    coordinate: str
    backup_id: str | None
    barrier_id: str | None
    status: str | None
    core_digest: str | None
    failures: tuple[AssemblyFailure, ...]


@dataclass(frozen=True)
class AssemblyState:
    """Full re-scan classification used by crash recovery verification."""

    current: CurrentPointer | None
    packages: tuple[PackageState, ...]
    staging: tuple[StagingState, ...]
    outcomes: tuple[OutcomeState, ...]
    current_temp_debris: tuple[str, ...]


@dataclass(frozen=True)
class AssemblyResult:
    """Outcome of one assemble_backup call."""

    status: str
    backup_id: str
    barrier_id: str
    core_digest: str | None
    report_digest: str | None
    failures: tuple[AssemblyFailure, ...]
    current: CurrentPointer | None
    package: str | None
    outcome: str | None
    recovered: bool


# -- low-level hardened filesystem helpers ------------------------------------


def _pinned(parent_fd, name):
    return "%s/%d/%s" % (_PROC_FD, parent_fd, name)


def _fd_real_path(fd):
    """Absolute path of a held directory descriptor (for git subprocesses).

    Descriptor-pinned openat handles every one of our own reads and
    writes; the existing git bundle primitive takes a Path and performs
    its own staging/hard-link publication beside it. The resolved path
    is used only as a subprocess argument: it never enters a digest, a
    report, the manifest or the current pointer.
    """
    return os.readlink("%s/%d" % (_PROC_FD, fd))


def _require_capabilities():
    missing = [
        name
        for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_PATH")
        if not hasattr(os, name)
    ]
    for primitive in (os.open, os.mkdir, os.rename):
        if primitive not in os.supports_dir_fd:
            missing.append(getattr(primitive, "__name__", "dir_fd primitive"))
    if not os.path.isdir(_PROC_FD):
        missing.append(_PROC_FD)
    if missing:
        raise AssemblyStorageError(
            "backup assembly requires Linux dir_fd/O_NOFOLLOW/O_PATH and "
            "%s support that is missing here (%s); refusing path-based or "
            "symlink-following fallbacks" % (_PROC_FD, ", ".join(missing))
        )


def _validated_root(retention_root):
    if not isinstance(retention_root, (str, Path)):
        raise BackupAssemblyError("retention_root must be a str or Path")
    root = Path(retention_root)
    if root.is_symlink():
        raise AssemblyStorageError(
            "retention root must not itself be a symlink: %s" % root
        )
    try:
        root_fd = os.open(str(root), _DIR_FLAGS)
    except OSError as exc:
        raise AssemblyStorageError(
            "retention root cannot be opened as a real directory: %s" % exc
        ) from exc
    try:
        result = os.fstat(root_fd)
        if not stat_module.S_ISDIR(result.st_mode):
            raise AssemblyStorageError("retention root is not a directory")
        if result.st_uid != os.geteuid():
            raise AssemblyStorageError(
                "retention root must be owned by the current effective user"
            )
        if result.st_mode & 0o022:
            raise AssemblyStorageError(
                "retention root must not be group- or world-writable"
            )
    finally:
        os.close(root_fd)
    return root


def _open_child_dir(parent_fd, name):
    """Open an existing child directory pinned, refusing symlinks."""
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def _open_checked_child_dir(parent_fd, name):
    fd = _open_child_dir(parent_fd, name)
    _require_private_dir_stat(fd, name)
    return fd


def _require_private_dir_stat(fd, name):
    result = os.fstat(fd)
    if not stat_module.S_ISDIR(result.st_mode):
        raise AssemblyStorageError(
            "%r is not a directory on the assembly layout" % name
        )
    if result.st_uid != os.geteuid():
        raise AssemblyStorageError(
            "assembly directory %r is not owned by this user" % name
        )
    if result.st_mode & 0o077:
        raise AssemblyStorageError(
            "assembly directory %r is not 0700 (mode=%s)"
            % (name, oct(result.st_mode & 0o777))
        )


def _ensure_private_dir(parent_fd, name):
    """mkdir-if-absent a 0700 non-symlink directory and return its fd."""
    try:
        os.mkdir(name, _DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AssemblyStorageError(
            "cannot create private directory %r: %s" % (name, exc)
        ) from exc
    fd = _open_child_dir(parent_fd, name)
    try:
        _require_private_dir_stat(fd, name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _probe_path(parent_fd, name):
    """O_PATH|O_NOFOLLOW stat of one entry; None iff absent.

    A symlink raises ELOOP from O_NOFOLLOW instead of being observed as
    the target, so callers fail closed on swapped-in symlinks.
    """
    try:
        fd = os.open(name, _PATH_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _require_regular_private(parent_fd, name, label):
    result = _probe_path(parent_fd, name)
    if result is None:
        raise AssemblyStorageError("%s is missing: %r" % (label, name))
    if not stat_module.S_ISREG(result.st_mode):
        raise AssemblyStorageError("%s must be a regular file: %r" % (label, name))
    if result.st_uid != os.geteuid():
        raise AssemblyStorageError("%s must be owned by this user: %r" % (label, name))
    if result.st_mode & 0o022:
        raise AssemblyStorageError(
            "%s must not be group- or world-writable: %r" % (label, name)
        )
    return result


def _write_exclusive_file(parent_fd, name, data):
    """Create a 0600 regular file with O_EXCL|O_NOFOLLOW, fsync it."""
    fd = os.open(_pinned(parent_fd, name), _FILE_WRITE_FLAGS, _FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view[:_READ_CHUNK])
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    result = _probe_path(parent_fd, name)
    if (result is None or not stat_module.S_ISREG(result.st_mode)
            or result.st_mode & 0o022 or result.st_uid != os.geteuid()):
        raise AssemblyStorageError(
            "published file %r is not a 0600 regular file owned by this user"
            % name
        )


def _read_file_at(parent_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC,
                 dir_fd=parent_fd)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha256_file_at(parent_fd, name):
    """Return (hex digest, size) streaming one pinned regular file."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC,
                 dir_fd=parent_fd)
    hasher = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return hasher.hexdigest(), total


def _fsync_dir_fd(fd):
    os.fsync(fd)


def _safe_id(value, label):
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise BackupAssemblyError(
            "%s must be a 1-64 char str of [A-Za-z0-9_-]" % label
        )
    return value


def _nonce():
    return os.urandom(6).hex()


def _walk_private_path(root_fd, segments):
    """Open root/segments pinned step by step; every level is 0700 owned."""
    fd = os.open(".", _DIR_FLAGS, dir_fd=root_fd)
    for segment in segments:
        next_fd = _open_child_dir(fd, segment)
        os.close(fd)
        fd = next_fd
        _require_private_dir_stat(fd, segment)
    return fd


# -- current pointer -----------------------------------------------------------


def _current_payload(backup_id, barrier_id, identity, package,
                     core_digest, report_digest):
    return {
        "schema": CURRENT_SCHEMA,
        "version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "barrier_id": barrier_id,
        "identity": {
            "repo_id": identity.repo_id,
            "installation_id": identity.installation_id,
            "company_id": identity.company_id,
        },
        "package": package,
        "core_digest": core_digest,
        "report_digest": report_digest,
    }


_CURRENT_KEYS = frozenset((
    "schema", "version", "backup_id", "barrier_id", "identity",
    "package", "core_digest", "report_digest",
))


def _parse_current(data, *, identity):
    try:
        payload = git_format.loads_record(data)
    except (TypeError, ValueError) as exc:
        raise BackupAssemblyError(
            "current pointer is not parseable canonical JSON: %s" % exc
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _CURRENT_KEYS:
        raise BackupAssemblyError("current pointer has an unknown shape")
    if payload["schema"] != CURRENT_SCHEMA or payload["version"] != SCHEMA_VERSION:
        raise BackupAssemblyError("current pointer has an unknown schema/version")
    match = _PACKAGE_COORDINATE_RE.fullmatch(payload["package"] or "")
    if not match:
        raise BackupAssemblyError("current pointer carries a bad package path")
    identity_block = payload.get("identity")
    if not isinstance(identity_block, dict) or set(identity_block) != {
        "repo_id", "installation_id", "company_id"
    }:
        raise BackupAssemblyError("current pointer carries a bad identity block")
    pointer_identity = AssemblyIdentity(
        repo_id=identity_block["repo_id"],
        installation_id=identity_block["installation_id"],
        company_id=identity_block["company_id"],
    )
    if identity is not None and pointer_identity != identity:
        raise AssemblyScopeMismatchError(
            "current pointer belongs to installation=%r company=%r repo=%r"
            % (pointer_identity.installation_id, pointer_identity.company_id,
               pointer_identity.repo_id)
        )
    for digest in (payload["core_digest"], payload["report_digest"]):
        if not isinstance(digest, str) or not re.fullmatch(
                r"\Asha256:[0-9a-f]{64}\Z", digest):
            raise BackupAssemblyError("current pointer carries a bad digest")
    _safe_id(payload["backup_id"], "current backup_id")
    _safe_id(payload["barrier_id"], "current barrier_id")
    if match.group("bid") != payload["backup_id"]:
        raise BackupAssemblyError("current pointer backup_id/package disagree")
    return CurrentPointer(
        backup_id=payload["backup_id"],
        barrier_id=payload["barrier_id"],
        identity=pointer_identity,
        package=payload["package"],
        core_digest=payload["core_digest"],
        report_digest=payload["report_digest"],
    )


def _read_current_at(assembly_fd, *, identity):
    result = _probe_path(assembly_fd, CURRENT_NAME)
    if result is None:
        return None
    if not stat_module.S_ISREG(result.st_mode):
        raise AssemblyStorageError("current pointer must be a regular file")
    if result.st_mode & 0o022 or result.st_uid != os.geteuid():
        raise AssemblyStorageError(
            "current pointer must be a private 0600 file owned by this user"
        )
    return _parse_current(_read_file_at(assembly_fd, CURRENT_NAME),
                          identity=identity)


def read_current_backup(retention_root, *, identity=None):
    """Read and validate the current backup pointer; None when absent.

    With ``identity`` supplied, a pointer belonging to another scope
    raises :class:`AssemblyScopeMismatchError`; symlinks, non-regular
    files, group/world-writable modes and malformed contents fail closed.
    """
    _require_capabilities()
    root = _validated_root(retention_root)
    if identity is not None and not isinstance(identity, AssemblyIdentity):
        raise BackupAssemblyError("identity must be an AssemblyIdentity")
    root_fd = os.open(str(root), _DIR_FLAGS)
    try:
        try:
            assembly_fd = _open_child_dir(root_fd, ASSEMBLY_DIRNAME)
        except FileNotFoundError:
            return None
        try:
            return _read_current_at(assembly_fd, identity=identity)
        finally:
            os.close(assembly_fd)
    finally:
        os.close(root_fd)


def _switch_current(assembly_fd, pointer):
    """Atomically install the new current pointer (temp + fsync + rename).

    POSIX rename of one regular file over another leaves no window with
    a missing or dangling pointer; every operation is descriptor-pinned
    with O_NOFOLLOW, and an existing symlink/directory at ``current`` is
    refused before the rename rather than replaced. Callers have already
    scope-checked any existing pointer.
    """
    existing = _probe_path(assembly_fd, CURRENT_NAME)
    if existing is not None:
        _require_regular_private(assembly_fd, CURRENT_NAME, "current pointer")
    temp_name = CURRENT_TMP_PREFIX + _nonce()
    data = git_format.dumps_record(_current_payload(
        pointer.backup_id, pointer.barrier_id, pointer.identity,
        pointer.package, pointer.core_digest, pointer.report_digest,
    ))
    _write_exclusive_file(assembly_fd, temp_name, data)
    os.rename(
        temp_name, CURRENT_NAME,
        src_dir_fd=assembly_fd, dst_dir_fd=assembly_fd,
    )
    _fsync_dir_fd(assembly_fd)


# -- append-only publish log ---------------------------------------------------


def _append_log(assembly_fd, event):
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | _O_CLOEXEC
    )
    fd = os.open(_pinned(assembly_fd, PUBLISH_LOG_NAME), flags, _FILE_MODE)
    try:
        result = os.fstat(fd)
        if not stat_module.S_ISREG(result.st_mode):
            raise AssemblyStorageError("publish log must be a regular file")
        if result.st_mode & 0o022 or result.st_uid != os.geteuid():
            raise AssemblyStorageError(
                "publish log must be a private file owned by this user"
            )
        line = git_format.dumps_record(event) + b"\n"
        view = memoryview(line)
        while view:
            written = os.write(fd, view[:_READ_CHUNK])
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


# -- independent bundle ref check ----------------------------------------------


def _bundle_advertised_refs(bundle_path):
    """Independent `git bundle list-heads` with a scrubbed GIT_* env.

    Returns tuple of (oid, ref) lines; raises on any git failure. Only a
    local bundle file is read (list-heads has no network capability).
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        result = subprocess.run(
            ["git", "bundle", "list-heads", os.fspath(bundle_path)],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupAssemblyError("git bundle list-heads failed: %s" % exc) from exc
    if result.returncode != 0:
        raise BackupAssemblyError("git bundle list-heads returned failure")
    heads = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        oid, ref = line.split(" ", 1)
        heads.append((oid.strip(), ref.strip()))
    return tuple(heads)


# -- plan validation / reference construction ----------------------------------


def _validate_plan(plan):
    if not isinstance(plan, AssemblyPlan):
        raise BackupAssemblyError("plan must be an AssemblyPlan")
    if not isinstance(plan.identity, AssemblyIdentity):
        raise BackupAssemblyError("plan.identity must be an AssemblyIdentity")
    if not isinstance(plan.repository, FactsGitRepository):
        raise BackupAssemblyError(
            "plan.repository must be a real FactsGitRepository"
        )
    if not isinstance(plan.stores, ClosureStores):
        raise BackupAssemblyError("plan.stores must be a real ClosureStores")
    if not isinstance(plan.references, tuple):
        raise BackupAssemblyError("plan.references must be a tuple")
    for reference in plan.references:
        if not isinstance(reference, AssemblyReference):
            raise BackupAssemblyError(
                "every plan reference must be an AssemblyReference"
            )
        if reference.object_type not in OBJECT_TYPES:
            raise BackupAssemblyError(
                "unknown object_type %r in plan reference" % reference.object_type
            )
        if reference.scope_kind not in (SCOPE_INSTALLATION, SCOPE_COMPANY):
            raise BackupAssemblyError(
                "reference scope_kind must be installation/company"
            )
        if not reference.citations or not all(
                isinstance(citation, AssemblyCitation)
                for citation in reference.citations):
            raise BackupAssemblyError(
                "every plan reference needs at least one AssemblyCitation"
            )
    if plan.version_index_root is not None and not isinstance(
            plan.version_index_root, (str, Path)):
        raise BackupAssemblyError("version_index_root must be a path or None")


def _build_object_references(plan, frozen_commit):
    refs = []
    for item in plan.references:
        roots = []
        for citation in item.citations:
            if citation.kind == ROOT_H:
                root_id = frozen_commit if citation.root_id is None \
                    else citation.root_id
                if root_id != frozen_commit:
                    raise BackupAssemblyError(
                        "an H citation must name this cut's frozen commit",
                        family=FAMILY_R,
                        reason=R_MALFORMED_REFERENCE,
                    )
            elif citation.kind == ROOT_W:
                if not citation.root_id:
                    raise BackupAssemblyError(
                        "a W citation needs its checkpoint identifier",
                        family=FAMILY_R,
                        reason=R_MALFORMED_REFERENCE,
                    )
                root_id = citation.root_id
            else:
                raise BackupAssemblyError(
                    "citation kind must be H/W",
                    family=FAMILY_R, reason=R_MALFORMED_REFERENCE,
                )
            roots.append(ReferenceRoot(
                kind=citation.kind, root_id=root_id,
                coordinate=citation.coordinate,
            ))
        company_id = (
            plan.identity.company_id
            if item.scope_kind == SCOPE_COMPANY
            else None
        )
        refs.append(ObjectReference(
            logical_id=item.logical_id,
            object_type=item.object_type,
            version=item.version,
            referenced_by=roots,
            scope=ObjectScope(
                kind=item.scope_kind,
                installation_id=plan.identity.installation_id,
                company_id=company_id,
            ),
            sha256_digest=item.sha256_digest,
            key_reference=item.key_reference,
            storage_class=item.storage_class,
            relative_path=item.relative_path,
        ))
    return refs


# -- family evidence -----------------------------------------------------------


@dataclass(frozen=True)
class _BundleEvidence:
    size_bytes: int
    sha256_digest: str
    advertised_refs: tuple


@dataclass(frozen=True)
class _WEvidence:
    components: tuple  # verified journal entries (dicts), sorted by role


@dataclass(frozen=True)
class _REvidence:
    status: str
    publishable: bool
    r_list_digest: str
    counts: dict


_H_EXPORT_ERRORS = (
    BundleExportError,
    BundleOutputExistsError,
    MaintenancePinError,
    PinNotFoundError,
    GitCommandError,
    GitRepositoryError,
    OSError,
)
_H_PIN_ERRORS = (
    MaintenancePinError,
    GitRepositoryError,
    GitCommandError,
    OSError,
)


def _verify_h(plan, *, backup_id, barrier_id, pin_ref, frozen_commit,
              object_format, staging_root_fd, staging_name, failures, hook):
    """H leg: pin readback, offline bundle export + independent checks."""
    check_pin = "pin_readback"
    check_bundle = "bundle"
    try:
        pin = plan.repository.read_maintenance_pin(backup_id, barrier_id)
    except _H_PIN_ERRORS:
        failures.append(AssemblyFailure(
            FAMILY_H, check_pin, pin_ref, R_PIN_UNREADABLE))
        return None
    if pin is None:
        failures.append(AssemblyFailure(
            FAMILY_H, check_pin, pin_ref, R_PIN_MISSING))
        return None
    if pin.commit_oid != frozen_commit or pin.ref != pin_ref:
        failures.append(AssemblyFailure(
            FAMILY_H, check_pin, pin_ref, R_PIN_TARGET_MOVED))
        return None

    # Resolve our pinned private staging leaf to an absolute path for the
    # existing git primitive; the path never enters a digest or report.
    bundle_output = Path(
        _fd_real_path(staging_root_fd), staging_name, BUNDLE_NAME)
    try:
        bundle = plan.repository.export_maintenance_pin_bundle(
            backup_id, barrier_id, bundle_output)
    except _H_EXPORT_ERRORS:
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_EXPORT_FAILED))
        return None

    stage_fd = _open_child_dir(staging_root_fd, staging_name)
    try:
        digest_hex, size_bytes = _sha256_file_at(stage_fd, BUNDLE_NAME)
    except OSError:
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_BYTES_MISMATCH))
        return None
    finally:
        os.close(stage_fd)
    if (digest_hex != bundle.sha256_digest
            or size_bytes != bundle.size_bytes
            or bundle.object_format != object_format):
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_BYTES_MISMATCH))
        return None

    hook(PHASE_BUNDLE_EXPORTED)

    # Re-hash right before the bytes enter the manifest: a crash hook (or
    # anything with the private staging fd) changing the exported bundle
    # after export must not be digest-bound.
    try:
        stage_fd = _open_child_dir(staging_root_fd, staging_name)
        try:
            digest_now, size_now = _sha256_file_at(stage_fd, BUNDLE_NAME)
        finally:
            os.close(stage_fd)
        heads = _bundle_advertised_refs(bundle_output)
    except (OSError, BackupAssemblyError):
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_HEADS))
        return None
    if (digest_now, size_now) != (digest_hex, size_bytes):
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_CHANGED))
        return None
    if len(heads) != 1 or heads[0] != (frozen_commit, pin_ref):
        failures.append(AssemblyFailure(
            FAMILY_H, check_bundle, pin_ref, R_BUNDLE_HEADS))
        return None
    return _BundleEvidence(
        size_bytes=size_bytes,
        sha256_digest=_DIGEST_PREFIX + digest_hex,
        advertised_refs=tuple(ref for _oid, ref in heads),
    )


def _verify_w(*, freeze_core, cut_fd, failures):
    """W leg: every declared checkpoint byte-verified; no undeclared files."""
    check = "checkpoints"
    declared = {}
    for entry in freeze_core["databases"]:
        role = entry.get("role")
        if (not isinstance(role, str) or not _SAFE_ROLE_RE.fullmatch(role)
                or role in declared):
            failures.append(AssemblyFailure(
                FAMILY_W, check, "freeze:databases",
                R_FREEZE_RECORD_MALFORMED))
            return _WEvidence(())
        expected_coordinate = "cuts/%s/%s/%s.sqlite" % (
            freeze_core["backup_id"], freeze_core["barrier_id"], role)
        if entry.get("relative_path") != expected_coordinate:
            failures.append(AssemblyFailure(
                FAMILY_W, check, expected_coordinate,
                R_FREEZE_RECORD_MALFORMED))
            return _WEvidence(())
        declared[role + ".sqlite"] = entry

    verified = []
    try:
        present_names = set(os.listdir(cut_fd))
    except OSError:
        failures.append(AssemblyFailure(
            FAMILY_W, check, "cut", R_COMPONENT_FAILED))
        return _WEvidence(())

    for name in sorted(present_names):
        result = _probe_path(cut_fd, name)
        if result is None or not stat_module.S_ISREG(result.st_mode):
            failures.append(AssemblyFailure(
                FAMILY_W, check, name, R_NON_REGULAR_COMPONENT))
            continue
        if name not in declared:
            failures.append(AssemblyFailure(
                FAMILY_W, check, name, R_UNDECLARED_COMPONENT))

    for name in sorted(declared):
        entry = declared[name]
        try:
            verify_backup_component(
                cut_fd,
                relative_path=name,
                size_bytes=entry["size_bytes"],
                sha256_digest=entry["sha256_digest"],
            )
        except BackupComponentValidationError:
            failures.append(AssemblyFailure(
                FAMILY_W, check, entry["relative_path"], R_COMPONENT_FAILED))
            continue
        verified.append(entry)
    return _WEvidence(tuple(verified))


def _verify_r(plan, *, frozen_commit, failures):
    """R leg: recompute the object closure for the declared references."""
    check = "closure"
    if plan.repository.object_format != "sha1":
        # The closure primitive's H root contract is the 40-hex sha1 oid;
        # a sha256 cut cannot be closure-verified until that slice gains
        # 64-hex support. Refusing is fail-closed; silently skipping R is
        # not an option.
        failures.append(AssemblyFailure(
            FAMILY_R, check,
            "object_format=" + plan.repository.object_format,
            R_OBJECT_FORMAT_UNSUPPORTED))
        return None
    try:
        references = _build_object_references(plan, frozen_commit)
        result = collect_object_closure(
            commit_oid=frozen_commit,
            authorized_scope=ClosureScope(
                installation_id=plan.identity.installation_id,
                company_id=plan.identity.company_id,
            ),
            references=references,
            stores=plan.stores,
        )
    except BackupAssemblyError as exc:
        failures.append(AssemblyFailure(
            FAMILY_R, check, "references", exc.reason
            if exc.reason is not None else R_MALFORMED_REFERENCE))
        return None
    except ObjectClosureError:
        failures.append(AssemblyFailure(
            FAMILY_R, check, "references", R_MALFORMED_REFERENCE))
        return None

    counts = {
        status: len(result.entries_with(status))
        for status in _ENTRY_STATUSES
    }
    evidence = _REvidence(
        status=result.status,
        publishable=result.publishable,
        r_list_digest=result.r_list_digest,
        counts=counts,
    )
    if result.status != CLOSURE_COMPLETE or not result.publishable:
        severity = (
            SEVERITY_INCOMPLETE
            if result.status == CLOSURE_INCOMPLETE
            else SEVERITY_FAILED
        )
        failures.append(AssemblyFailure(
            FAMILY_R, check, "closure:" + result.status,
            R_CLOSURE_STATUS, severity))
    return evidence, result


def _open_ledger_root(path):
    """Open an existing ledger root pinned; None when absent/symlinked."""
    root = Path(path)
    if root.is_symlink():
        return None
    try:
        return os.open(str(root), _DIR_FLAGS)
    except OSError:
        return None


def _verify_r2(plan, *, failures):
    """R2 leg: reopen the ledger in scope, verify bytes, fold in digest."""
    check = "mapping_ledger"
    if plan.version_index_root is None:
        return None, {"status": "skipped"}

    root_fd = _open_ledger_root(plan.version_index_root)
    if root_fd is None:
        failures.append(AssemblyFailure(
            FAMILY_R2, check, "ledger_root", R_LEDGER_MISSING))
        return None, {"status": "failed", "reason": R_LEDGER_MISSING}
    try:
        probe = _probe_path(root_fd, LEDGER_DB_FILENAME)
        if probe is None or not stat_module.S_ISREG(probe.st_mode):
            failures.append(AssemblyFailure(
                FAMILY_R2, check, LEDGER_DB_FILENAME, R_LEDGER_MISSING))
            return None, {"status": "failed", "reason": R_LEDGER_MISSING}
    finally:
        os.close(root_fd)

    try:
        ledger = MappingVersionLedger(
            plan.version_index_root,
            installation_id=plan.identity.installation_id,
            company_id=plan.identity.company_id,
            object_store=plan.stores.mapping_objects,
        )
    except MappingVersionScopeError:
        failures.append(AssemblyFailure(
            FAMILY_R2, check, "ledger_scope", R_LEDGER_SCOPE))
        return None, {"status": "failed", "reason": R_LEDGER_SCOPE}
    except (MappingVersionIndexError, RuntimeError, OSError, ValueError):
        # UnsafeLedgerRootError/LedgerIntegrityError are RuntimeErrors.
        failures.append(AssemblyFailure(
            FAMILY_R2, check, "ledger", R_LEDGER_UNREADABLE))
        return None, {"status": "failed", "reason": R_LEDGER_UNREADABLE}
    try:
        try:
            shape = ledger.ledger_shape(verify=True)
            digest = ledger.ledger_digest()
        except MappingVersionScopeError:
            failures.append(AssemblyFailure(
                FAMILY_R2, check, "ledger_scope", R_LEDGER_SCOPE))
            return None, {"status": "failed", "reason": R_LEDGER_SCOPE}
        except (MappingVersionIndexError, RuntimeError, OSError, ValueError):
            failures.append(AssemblyFailure(
                FAMILY_R2, check, "ledger", R_LEDGER_UNREADABLE))
            return None, {"status": "failed", "reason": R_LEDGER_UNREADABLE}
    finally:
        ledger.close()

    if shape["total_gaps"]:
        failures.append(AssemblyFailure(
            FAMILY_R2, check, "ledger:gaps", R_LEDGER_GAPS,
            SEVERITY_INCOMPLETE))
    block = {
        "schema": MAPPING_LEDGER_SCHEMA,
        "ledger_digest": digest,
        "total_sources": shape["total_sources"],
        "total_versions": shape["total_versions"],
        "total_gaps": shape["total_gaps"],
        "sources": [
            {
                "logical_source_id": source["logical_source_id"],
                "versions": source["versions"],
                "gaps": source["gaps"],
                "gap_versions": list(source["gap_versions"]),
            }
            for source in shape["sources"]
        ],
    }
    check_block = {
        "status": "ok" if not shape["total_gaps"] else "failed",
        "ledger_digest": digest,
        "total_sources": shape["total_sources"],
        "total_versions": shape["total_versions"],
        "total_gaps": shape["total_gaps"],
    }
    return block, check_block


# -- core / report construction ------------------------------------------------


def _build_core_payload(*, identity, freeze_core, bundle, w_evidence,
                        closure_pair, ledger_block):
    authority = freeze_core["authority"]
    databases = [
        {
            "role": entry["role"],
            "file": "%s.sqlite" % entry["role"],
            "size_bytes": entry["size_bytes"],
            "sha256_digest": entry["sha256_digest"],
            "schema": entry.get("schema", {}),
        }
        for entry in sorted(w_evidence.components, key=lambda item: item["role"])
    ]
    closure_block = None
    if closure_pair is not None:
        evidence, _result = closure_pair
        closure_block = {
            "status": evidence.status,
            "publishable": evidence.publishable,
            "r_list_digest": evidence.r_list_digest,
            "counts": dict(sorted(evidence.counts.items())),
        }
    bundle_block = None
    if bundle is not None:
        bundle_block = {
            "file": BUNDLE_NAME,
            "size_bytes": bundle.size_bytes,
            "sha256_digest": bundle.sha256_digest,
            "single_pin_ref": len(bundle.advertised_refs) == 1,
            "advertised_refs": list(bundle.advertised_refs),
        }
    return {
        "schema": ASSEMBLY_CORE_SCHEMA,
        "version": SCHEMA_VERSION,
        "digest_algorithm": DIGEST_ALGORITHM,
        "backup_id": freeze_core["backup_id"],
        "barrier_id": freeze_core["barrier_id"],
        "identity": {
            "repo_id": identity.repo_id,
            "installation_id": identity.installation_id,
            "company_id": identity.company_id,
        },
        "authority": {
            "ref": authority["ref"],
            "commit_oid": authority["commit_oid"],
            "object_format": authority["object_format"],
            "genesis_oid": authority["genesis_oid"],
            "commit_count": authority["commit_count"],
            "pin_ref": authority["pin_ref"],
        },
        "bundle": bundle_block,
        "databases": databases,
        "closure": closure_block,
        "mapping_ledger": ledger_block,
    }


def _root_coordinates(roots):
    return [
        {"kind": root.kind, "root_id": root.root_id, "coordinate": root.coordinate}
        for root in roots
    ]


def _build_checks(*, pin_ref, frozen_commit, bundle, w_evidence,
                  closure_pair, r2_check):
    checks = {}
    if bundle is not None:
        checks["H.pin_readback"] = {
            "status": "ok", "pin_ref": pin_ref, "commit_oid": frozen_commit,
        }
        checks["H.bundle"] = {
            "status": "ok",
            "file": BUNDLE_NAME,
            "size_bytes": bundle.size_bytes,
            "sha256_digest": bundle.sha256_digest,
            "advertised_refs": list(bundle.advertised_refs),
        }
    checks["W.checkpoints"] = {
        "status": "ok",
        "components": [
            {
                "role": entry["role"],
                "file": "%s.sqlite" % entry["role"],
                "size_bytes": entry["size_bytes"],
                "sha256_digest": entry["sha256_digest"],
            }
            for entry in sorted(
                w_evidence.components, key=lambda item: item["role"])
        ],
    }
    if closure_pair is not None:
        evidence, result = closure_pair
        findings = [
            {
                "logical_id": entry.logical_id,
                "object_type": entry.object_type,
                "version": entry.version,
                "entry_status": entry.status,
                "issue": entry.issue,
                "coordinates": _root_coordinates(entry.referenced_by),
            }
            for entry in result.entries
            if entry.status in _GAP_OR_WORSE
        ]
        findings.extend(
            {
                "logical_id": violation.logical_id,
                "object_type": violation.object_type,
                "version": violation.version,
                "entry_status": STATUS_OUT_OF_SCOPE,
                "issue": violation.reason,
                "coordinates": _root_coordinates(violation.referenced_by),
            }
            for violation in result.scope_violations
        )
        checks["R.closure"] = {
            "status": "ok" if evidence.publishable else "failed",
            "closure_status": evidence.status,
            "publishable": evidence.publishable,
            "r_list_digest": evidence.r_list_digest,
            "counts": dict(sorted(evidence.counts.items())),
            "findings": findings,
        }
    checks["R2.mapping_ledger"] = dict(r2_check)
    return checks


_FAILURE_CHECK_KEYS = {
    FAMILY_H: "H.%s",
    FAMILY_W: "W.%s",
    FAMILY_R: "R.%s",
    FAMILY_R2: "R2.%s",
}


def _worst_severity(severities):
    return SEVERITY_FAILED if SEVERITY_FAILED in severities \
        else SEVERITY_INCOMPLETE


def _record_failures_in_checks(checks, failures):
    """Overlay failing family verdicts; stable codes, no exception text."""
    by_check = {}
    for failure in failures:
        pattern = _FAILURE_CHECK_KEYS.get(failure.family)
        if pattern is None:
            continue
        by_check.setdefault(pattern % failure.check, []).append(failure)
    for key, family_failures in by_check.items():
        existing = checks.get(key)
        checks[key] = {
            **(existing if isinstance(existing, dict) else {}),
            "status": _worst_severity(
                {item.severity for item in family_failures}),
            "failures": [item.normalized() for item in family_failures],
        }


# -- outcome/package assessment ------------------------------------------------


def _outcome_payload(status, backup_id, barrier_id, core_digest, failures):
    return {
        "schema": OUTCOME_SCHEMA,
        "version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "barrier_id": barrier_id,
        "status": status,
        "core_digest": core_digest,
        "failures": [failure.normalized() for failure in failures],
    }


def _assess_package_files(package_fd):
    """Verify an on-disk package directory.

    The three files must be private regular non-symlinks; the
    manifest/report digest chain must verify through
    ``verify_final_manifest`` and the bundle bytes on disk must match
    the manifest's bundle declaration. Returns
    ``(status, core_digest, report_digest, backup_id, barrier_id,
    reasons)``.
    """
    reasons = []
    loaded = {}
    for name in _PACKAGE_FILES:
        result = _probe_path(package_fd, name)
        if result is None:
            reasons.append("missing:%s" % name)
            continue
        if not stat_module.S_ISREG(result.st_mode):
            reasons.append("non_regular:%s" % name)
            continue
        if result.st_mode & 0o022 or result.st_uid != os.geteuid():
            reasons.append("unsafe_mode:%s" % name)
        try:
            loaded[name] = _read_file_at(package_fd, name)
        except OSError:
            reasons.append("unreadable:%s" % name)
    manifest = None
    core_digest = report_digest = None
    backup_id = barrier_id = None
    if MANIFEST_NAME in loaded and REPORT_NAME in loaded:
        try:
            manifest = git_format.loads_record(loaded[MANIFEST_NAME])
            report = git_format.loads_record(loaded[REPORT_NAME])
            verify_final_manifest(manifest, report)
            core = manifest["core"]
            core_digest = manifest["verification"]["core_digest"]
            report_digest = manifest["verification"]["report_digest"]
            backup_id = core.get("backup_id")
            barrier_id = core.get("barrier_id")
            if core.get("schema") != ASSEMBLY_CORE_SCHEMA:
                reasons.append("core_schema")
        except (TypeError, ValueError, KeyError, BackupManifestError):
            reasons.append("digest_chain")
            manifest = None
    if manifest is not None and BUNDLE_NAME in loaded:
        bundle_block = manifest["core"].get("bundle") or {}
        try:
            digest_hex, size_bytes = _sha256_file_at(package_fd, BUNDLE_NAME)
            expected = bundle_block.get("sha256_digest", "")
            if (expected != _DIGEST_PREFIX + digest_hex
                    or bundle_block.get("size_bytes") != size_bytes):
                reasons.append("bundle_bytes")
        except OSError:
            reasons.append("bundle_bytes")
    status = "complete" if not reasons else "damaged"
    return (status, core_digest, report_digest, backup_id, barrier_id,
            tuple(reasons))


# -- main entry point ----------------------------------------------------------


def assemble_backup(plan, backup_id, *, crash_hook=None):
    """Assemble and (if all families verify) publish one frozen cut.

    Returns :class:`AssemblyResult` for every recognisable family
    outcome (published/incomplete/failed). Structural call errors raise:
    :class:`CutNotFrozenError`, :class:`AssemblyScopeMismatchError`,
    :class:`AlreadyPublishedError`, :class:`AssemblyPackageConflictError`
    and :class:`AssemblyStorageError`. ``crash_hook(phase)`` is the
    documented crash-injection seam (phases in ``CRASH_PHASES``).
    """
    _require_capabilities()
    _validate_plan(plan)
    _safe_id(backup_id, "backup_id")
    if crash_hook is not None and not callable(crash_hook):
        raise BackupAssemblyError("crash_hook must be callable")

    def hook(phase):
        if crash_hook is not None:
            crash_hook(phase)

    root = _validated_root(plan.retention_root)
    record = read_freeze_record(root, backup_id)
    if record is None:
        raise CutNotFrozenError(
            "no freeze record exists for backup_id %r" % backup_id,
            state=None, reason="record_missing",
        )
    if record.state != FROZEN:
        raise CutNotFrozenError(
            "freeze record for backup_id %r is %r, not frozen"
            % (backup_id, record.state),
            state=record.state, reason="not_frozen",
        )
    freeze_core = record.detail.get("core_payload") if record.detail else None
    if (not isinstance(freeze_core, dict)
            or record.barrier_id != freeze_core.get("barrier_id")
            or freeze_core.get("backup_id") != backup_id):
        raise CutNotFrozenError(
            "frozen freeze record for %r carries no coherent core payload"
            % backup_id,
            state=record.state, reason=R_FREEZE_RECORD_MALFORMED,
        )
    barrier_id = _safe_id(record.barrier_id, "barrier_id")
    pin_ref = maintenance_pin_ref(backup_id, barrier_id)
    authority = freeze_core.get("authority")
    if (not isinstance(authority, dict)
            or authority.get("pin_ref") != pin_ref):
        raise CutNotFrozenError(
            "frozen core authority pin_ref is inconsistent",
            state=record.state, reason=R_FREEZE_RECORD_MALFORMED,
        )
    frozen_commit = authority["commit_oid"]
    object_format = authority["object_format"]
    if object_format != plan.repository.object_format:
        raise AssemblyScopeMismatchError(
            "plan repository object format %r does not match the frozen "
            "authority %r"
            % (plan.repository.object_format, object_format),
            reason="object_format_mismatch",
        )
    identity = plan.identity
    for key in ("repo_id", "installation_id", "company_id"):
        if freeze_core.get(key) != getattr(identity, key):
            raise AssemblyScopeMismatchError(
                "plan identity %s=%r does not match frozen cut %r"
                % (key, getattr(identity, key), freeze_core.get(key))
            )
    bound_meta = getattr(plan.repository, "_expected_meta", None)
    if isinstance(bound_meta, dict):
        for key in ("repo_id", "installation_id", "company_id"):
            if bound_meta.get(key) != getattr(identity, key):
                raise AssemblyScopeMismatchError(
                    "the opened repository is bound to another identity (%s)"
                    % key
                )
    elif bound_meta is not None:
        raise BackupAssemblyError(
            "plan.repository is not bound to an identity (use open/initialize)"
        )

    root_fd = os.open(str(root), _DIR_FLAGS)
    try:
        # Never trust the journal's cut_dir string: recompute the cut's
        # fixed coordinate and pin every level with O_NOFOLLOW.
        cut_fd = _walk_private_path(root_fd, ["cuts", backup_id, barrier_id])
        try:
            assembly_fd = _ensure_private_dir(root_fd, ASSEMBLY_DIRNAME)
            try:
                staging_root_fd = _ensure_private_dir(
                    assembly_fd, STAGING_DIRNAME)
                packages_fd = _ensure_private_dir(
                    assembly_fd, PACKAGES_DIRNAME)
                outcomes_fd = _ensure_private_dir(
                    assembly_fd, OUTCOMES_DIRNAME)
                try:
                    return _assemble_locked(
                        plan=plan,
                        backup_id=backup_id,
                        barrier_id=barrier_id,
                        pin_ref=pin_ref,
                        frozen_commit=frozen_commit,
                        object_format=object_format,
                        freeze_core=freeze_core,
                        identity=identity,
                        cut_fd=cut_fd,
                        assembly_fd=assembly_fd,
                        staging_root_fd=staging_root_fd,
                        packages_fd=packages_fd,
                        outcomes_fd=outcomes_fd,
                        hook=hook,
                    )
                finally:
                    for fd in (outcomes_fd, packages_fd, staging_root_fd):
                        os.close(fd)
            finally:
                os.close(assembly_fd)
        finally:
            os.close(cut_fd)
    finally:
        os.close(root_fd)


def _assemble_locked(*, plan, backup_id, barrier_id, pin_ref, frozen_commit,
                     object_format, freeze_core, identity, cut_fd,
                     assembly_fd, staging_root_fd, packages_fd, outcomes_fd,
                     hook):
    failures = []
    staging_name = "%s-%s-%s" % (backup_id, barrier_id, _nonce())
    os.mkdir(staging_name, _DIR_MODE, dir_fd=staging_root_fd)
    staging_fd = _open_checked_child_dir(staging_root_fd, staging_name)
    try:
        bundle = _verify_h(
            plan,
            backup_id=backup_id,
            barrier_id=barrier_id,
            pin_ref=pin_ref,
            frozen_commit=frozen_commit,
            object_format=object_format,
            staging_root_fd=staging_root_fd,
            staging_name=staging_name,
            failures=failures,
            hook=hook,
        )
        w_evidence = _verify_w(
            freeze_core=freeze_core, cut_fd=cut_fd, failures=failures)
        closure_pair = _verify_r(
            plan, frozen_commit=frozen_commit, failures=failures)
        ledger_block, r2_check = _verify_r2(plan, failures=failures)

        core_payload = _build_core_payload(
            identity=identity,
            freeze_core=freeze_core,
            bundle=bundle,
            w_evidence=w_evidence,
            closure_pair=closure_pair,
            ledger_block=ledger_block,
        )
        checks = _build_checks(
            pin_ref=pin_ref,
            frozen_commit=frozen_commit,
            bundle=bundle,
            w_evidence=w_evidence,
            closure_pair=closure_pair,
            r2_check=r2_check,
        )
        _record_failures_in_checks(checks, failures)
        report = build_verification_report(core_payload, checks)
        core_digest = report["core_digest"]
        report_digest = compute_report_digest(report)

        if failures:
            return _publish_outcome(
                backup_id=backup_id,
                barrier_id=barrier_id,
                identity=identity,
                assembly_fd=assembly_fd,
                outcomes_fd=outcomes_fd,
                failures=tuple(failures),
                core_digest=core_digest,
                report=report,
                report_digest=report_digest,
            )

        manifest = build_final_manifest(core_payload, report)
        verify_final_manifest(manifest, report)
        return _publish_green(
            backup_id=backup_id,
            barrier_id=barrier_id,
            identity=identity,
            assembly_fd=assembly_fd,
            staging_root_fd=staging_root_fd,
            staging_fd=staging_fd,
            staging_name=staging_name,
            packages_fd=packages_fd,
            report=report,
            manifest=manifest,
            core_digest=core_digest,
            report_digest=report_digest,
            hook=hook,
        )
    finally:
        os.close(staging_fd)


def _create_outcome_leaf(outcomes_fd, backup_id, barrier_id, report,
                         outcome):
    """Create outcomes/<bid>/<barrier>-<nonce>/ exclusively; return coord."""
    backup_outcomes_fd = _ensure_private_dir(outcomes_fd, backup_id)
    leaf = None
    try:
        leaf = "%s-%s" % (barrier_id, _nonce())
        os.mkdir(leaf, _DIR_MODE, dir_fd=backup_outcomes_fd)
        outcome_fd = _open_checked_child_dir(backup_outcomes_fd, leaf)
        try:
            _write_exclusive_file(
                outcome_fd, REPORT_NAME, git_format.dumps_record(report))
            _write_exclusive_file(
                outcome_fd, OUTCOME_NAME, git_format.dumps_record(outcome))
            _fsync_dir_fd(outcome_fd)
        finally:
            os.close(outcome_fd)
        _fsync_dir_fd(backup_outcomes_fd)
    finally:
        os.close(backup_outcomes_fd)
    _fsync_dir_fd(outcomes_fd)
    return "outcomes/%s/%s" % (backup_id, leaf)


def _publish_outcome(*, backup_id, barrier_id, identity, assembly_fd,
                     outcomes_fd, failures, core_digest, report,
                     report_digest):
    if any(item.severity == SEVERITY_FAILED for item in failures):
        status = STATUS_FAILED
    else:
        status = STATUS_INCOMPLETE
    outcome = _outcome_payload(
        status, backup_id, barrier_id, core_digest, failures)
    coordinate = _create_outcome_leaf(
        outcomes_fd, backup_id, barrier_id, report, outcome)
    _append_log(assembly_fd, {
        "event": "assembly_outcome",
        "backup_id": backup_id,
        "barrier_id": barrier_id,
        "status": status,
        "coordinate": coordinate,
        "failures": [item.normalized() for item in failures],
    })
    return AssemblyResult(
        status=status,
        backup_id=backup_id,
        barrier_id=barrier_id,
        core_digest=core_digest,
        report_digest=report_digest,
        failures=failures,
        current=_safe_current(assembly_fd, identity),
        package=None,
        outcome=coordinate,
        recovered=False,
    )


def _safe_current(assembly_fd, identity):
    try:
        return _read_current_at(assembly_fd, identity=identity)
    except BackupAssemblyError:
        return None


def _require_current_scope(assembly_fd, identity):
    """Refuse to publish over a current pointer of another identity."""
    existing = _safe_current(assembly_fd, None)
    if existing is not None and existing.identity != identity:
        raise AssemblyScopeMismatchError(
            "current pointer belongs to installation=%r company=%r repo=%r; "
            "refusing to move another scope's current pointer"
            % (existing.identity.installation_id, existing.identity.company_id,
               existing.identity.repo_id)
        )


def _publish_green(*, backup_id, barrier_id, identity, assembly_fd,
                   staging_root_fd, staging_fd, staging_name, packages_fd,
                   report, manifest, core_digest, report_digest, hook):
    package_coordinate = "packages/%s/%s" % (backup_id, barrier_id)
    # Scope guard before any byte moves: an existing current pointer of
    # another installation/company is never overwritten, not even with a
    # fully verified package staged beside it.
    _require_current_scope(assembly_fd, identity)
    bid_packages_fd = _ensure_private_dir(packages_fd, backup_id)
    try:
        target_stat = _probe_path(bid_packages_fd, barrier_id)
        recovered = False
        if target_stat is not None:
            # Recovery / idempotency: the package position already exists.
            # It must be a directory whose three files verify and
            # reproduce exactly the re-computed digests; anything else is
            # a conflict and is never overwritten.
            if not stat_module.S_ISDIR(target_stat.st_mode):
                raise AssemblyPackageConflictError(package_coordinate)
            existing_fd = _open_child_dir(bid_packages_fd, barrier_id)
            try:
                status, existing_core, existing_report, _bid, _bar, reasons = \
                    _assess_package_files(existing_fd)
            finally:
                os.close(existing_fd)
            if (status != "complete" or existing_core != core_digest
                    or existing_report != report_digest):
                raise AssemblyPackageConflictError(package_coordinate, reasons)
            current = _safe_current(assembly_fd, identity)
            if (current is not None and current.backup_id == backup_id
                    and current.barrier_id == barrier_id
                    and current.package == package_coordinate):
                raise AlreadyPublishedError(backup_id, package_coordinate)
            # Crash window: package published, current not yet switched.
            # Families were just re-verified and the existing package
            # reproduces the fresh digests: finish the switch (forward
            # recovery); scope was already refused before any byte moved.
            recovered = True
        else:
            _write_exclusive_file(
                staging_fd, REPORT_NAME, git_format.dumps_record(report))
            _write_exclusive_file(
                staging_fd, MANIFEST_NAME, git_format.dumps_record(manifest))
            _fsync_dir_fd(staging_fd)
            hook(PHASE_MANIFEST_STAGED)
            try:
                os.rename(
                    staging_name, barrier_id,
                    src_dir_fd=staging_root_fd, dst_dir_fd=bid_packages_fd,
                )
            except OSError as exc:
                # Target appearing concurrently, cross-device etc.: never
                # retry over an existing position.
                raise AssemblyPackageConflictError(
                    package_coordinate, ("rename_failed",)) from exc
            _fsync_dir_fd(bid_packages_fd)
            _fsync_dir_fd(packages_fd)
            _fsync_dir_fd(staging_root_fd)
            hook(PHASE_PACKAGE_PUBLISHED)

        pointer = CurrentPointer(
            backup_id=backup_id,
            barrier_id=barrier_id,
            identity=identity,
            package=package_coordinate,
            core_digest=core_digest,
            report_digest=report_digest,
        )
        _switch_current(assembly_fd, pointer)
        hook(PHASE_CURRENT_SWITCHED)
        _append_log(assembly_fd, {
            "event": "published",
            "backup_id": backup_id,
            "barrier_id": barrier_id,
            "package": package_coordinate,
            "core_digest": core_digest,
            "report_digest": report_digest,
            "recovered": recovered,
        })
        return AssemblyResult(
            status=STATUS_PUBLISHED,
            backup_id=backup_id,
            barrier_id=barrier_id,
            core_digest=core_digest,
            report_digest=report_digest,
            failures=(),
            current=pointer,
            package=package_coordinate,
            outcome=None,
            recovered=recovered,
        )
    finally:
        os.close(bid_packages_fd)


# -- crash-state scan -----------------------------------------------------------


def scan_assembly_state(retention_root, *, identity=None):
    """Classify current/packages/staging/outcomes for crash recovery.

    Distinguishes complete packages, damaged packages, verified staging
    directories (full chain that crashed before the rename), partial
    staging debris, recorded outcomes and leftover current temp files.
    """
    _require_capabilities()
    root = _validated_root(retention_root)
    if identity is not None and not isinstance(identity, AssemblyIdentity):
        raise BackupAssemblyError("identity must be an AssemblyIdentity")
    root_fd = os.open(str(root), _DIR_FLAGS)
    try:
        try:
            assembly_fd = _open_child_dir(root_fd, ASSEMBLY_DIRNAME)
        except FileNotFoundError:
            return AssemblyState(None, (), (), (), ())
        try:
            current = _safe_current(assembly_fd, identity)
            packages = _scan_packages(assembly_fd)
            staging = _scan_staging(assembly_fd)
            outcomes = _scan_outcomes(assembly_fd)
            temps = tuple(sorted(
                name for name in os.listdir(assembly_fd)
                if name.startswith(CURRENT_TMP_PREFIX)
            ))
            return AssemblyState(
                current=current,
                packages=packages,
                staging=staging,
                outcomes=outcomes,
                current_temp_debris=temps,
            )
        finally:
            os.close(assembly_fd)
    finally:
        os.close(root_fd)


def _safe_names(parent_fd):
    return sorted(
        name for name in os.listdir(parent_fd) if _SAFE_ID_RE.fullmatch(name)
    )


def _scan_packages(assembly_fd):
    try:
        packages_fd = _open_child_dir(assembly_fd, PACKAGES_DIRNAME)
    except FileNotFoundError:
        return ()
    packages = []
    try:
        for bid in _safe_names(packages_fd):
            bid_fd = _open_child_dir(packages_fd, bid)
            try:
                for barrier in _safe_names(bid_fd):
                    pkg_fd = _open_child_dir(bid_fd, barrier)
                    try:
                        (status, core_digest, report_digest,
                         pkg_bid, pkg_bar, reasons) = _assess_package_files(
                            pkg_fd)
                    finally:
                        os.close(pkg_fd)
                    packages.append(PackageState(
                        coordinate="packages/%s/%s" % (bid, barrier),
                        backup_id=pkg_bid,
                        barrier_id=pkg_bar,
                        status=status,
                        core_digest=core_digest,
                        report_digest=report_digest,
                        reasons=tuple(reasons),
                    ))
            finally:
                os.close(bid_fd)
    finally:
        os.close(packages_fd)
    return tuple(packages)


def _scan_staging(assembly_fd):
    try:
        staging_fd = _open_child_dir(assembly_fd, STAGING_DIRNAME)
    except FileNotFoundError:
        return ()
    states = []
    try:
        for name in sorted(os.listdir(staging_fd)):
            try:
                stage_fd = _open_child_dir(staging_fd, name)
            except OSError:
                # Symlink/non-directory debris is reported, never followed.
                probe = _probe_path(staging_fd, name)
                states.append(StagingState(
                    name, "partial",
                    ("non_directory_entry",) if probe is not None
                    else ("unreadable_entry",)))
                continue
            try:
                (_status, _core, _report, _bid, _bar,
                 reasons) = _assess_package_files(stage_fd)
            finally:
                os.close(stage_fd)
            if not reasons:
                states.append(StagingState(name, "verified", ()))
            else:
                states.append(StagingState(name, "partial", tuple(reasons)))
    finally:
        os.close(staging_fd)
    return tuple(states)


def _scan_outcomes(assembly_fd):
    try:
        outcomes_fd = _open_child_dir(assembly_fd, OUTCOMES_DIRNAME)
    except FileNotFoundError:
        return ()
    states = []
    try:
        for bid in _safe_names(outcomes_fd):
            bid_fd = _open_child_dir(outcomes_fd, bid)
            try:
                for leaf in sorted(os.listdir(bid_fd)):
                    try:
                        leaf_fd = _open_child_dir(bid_fd, leaf)
                    except OSError:
                        continue
                    try:
                        probe = _probe_path(leaf_fd, OUTCOME_NAME)
                        if probe is None or not stat_module.S_ISREG(
                                probe.st_mode):
                            states.append(OutcomeState(
                                "outcomes/%s/%s" % (bid, leaf),
                                None, None, None, None, ()))
                            continue
                        try:
                            payload = git_format.loads_record(
                                _read_file_at(leaf_fd, OUTCOME_NAME))
                        except (TypeError, ValueError):
                            states.append(OutcomeState(
                                "outcomes/%s/%s" % (bid, leaf),
                                None, None, None, None, ()))
                            continue
                        failures = tuple(
                            AssemblyFailure(
                                family=item.get("family", ""),
                                check=item.get("check", ""),
                                coordinate=item.get("coordinate", ""),
                                reason=item.get("reason", ""),
                                severity=item.get(
                                    "severity", SEVERITY_FAILED),
                            )
                            for item in payload.get("failures", [])
                            if isinstance(item, dict)
                        )
                        states.append(OutcomeState(
                            coordinate="outcomes/%s/%s" % (bid, leaf),
                            backup_id=payload.get("backup_id"),
                            barrier_id=payload.get("barrier_id"),
                            status=payload.get("status"),
                            core_digest=payload.get("core_digest"),
                            failures=failures,
                        ))
                    finally:
                        os.close(leaf_fd)
            finally:
                os.close(bid_fd)
    finally:
        os.close(outcomes_fd)
    return tuple(states)
