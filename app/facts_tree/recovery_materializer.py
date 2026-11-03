"""Install verified F-001 recovery materials, stopping at materials_staged.

The host supplies the read-only lifeboat package, the three identities,
the exact authority head, the caller-given install layout and the trust
admission evidence. This module never guesses a role or a path, opens no
runtime admission (no queue, model, gate or startup), migrates no
database and fills no default. Success only appends the two R5 phase
events ``package_verified`` and ``materials_staged`` to the existing
quarantined case through :class:`RecoveryStateStore`'s public API.

Routing is strictly byte-level. Every landing is an O_EXCL 0600 pinned
create streamed from the fixed package descriptor, file-fsynced,
parent-directory-fsynced and re-read through
``verify_backup_component``. After placement each target root's physical
file set must equal the routed set exactly, the R closure is replayed
from A3 primitives against installed bytes, R2 is rechecked through
pure SQL on immutable mode=ro connections (no ledger/store object is
ever constructed), and W is compared fingerprint-equal to its packaged
bytes. A half-installed root never produces a success receipt; on
failure everything this call created is removed and the roots are void.
The append-only R5 case keeps the phase it actually reached.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from app.facts_tree.backup_assembly import (
    BUNDLE_NAME, MANIFEST_NAME, PAYLOAD_SCHEMA, PAYLOAD_VERSION, REPORT_NAME,
    _SAFE_ID_RE, _copy_regular_pinned, _replay_closure_bytes,
    _replay_closure_digest,
)
from app.facts_tree.backup_component_validation import verify_backup_component
from app.facts_tree.backup_manifest import compute_core_digest, verify_final_manifest
from app.facts_tree.mapping_version_index import (
    DIGEST_ALGORITHM, OBJECT_TYPE_MAPPING_VERSION, SCHEMA as LEDGER_SCHEMA,
    SCHEMA_VERSION, STORAGE_CLASS_MAPPING_OBJECTS, _COLUMNS, _DB_FILENAME,
    _row_digest,
)
from app.facts_tree.recovery_epoch import RecoveryPhase
from app.facts_tree.recovery_repository import (
    TrustedBareReport, bootstrap_trusted_bare,
)
from app.facts_tree.recovery_state_store import RecoveryStateStore
from app.facts_tree.recovery_verifier import verify_recovery_package

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
_PATH_FLAGS = getattr(os, "O_PATH", 0) | os.O_NOFOLLOW | _O_CLOEXEC
_HEX = frozenset("0123456789abcdef")
_READ_CHUNK = 1024 * 1024
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_NAME = "recovery-materialize.lock"
_STATE_DB_NAME = "recovery-state.sqlite3"
_PAYLOAD = "payload"
_LEDGER_RELATIVE = _PAYLOAD + "/" + _DB_FILENAME
_ENVELOPE_VERSION = 2
_PROOF_KIND_PACKAGE = "sam.f001.recovery.proof.v1:package_verified"
_PROOF_KIND_MATERIALS = "sam.f001.recovery.proof.v1:materials_staged"

# W hot-copy preconditions this install relies on but cannot establish for
# the host's frozen cut. They are reported as requirements, not guarantees.
_WAL_PRECONDITIONS = (
    "w_consistent_checkpoint: the packaged bytes must be one consistent "
    "frozen checkpoint; PRAGMA integrity_check is re-run on the landing",
    "w_no_writer_during_copy: no writer may have been active when the W "
    "bytes were cut, and the recovery root is held single-writer locked",
    "w_no_hot_wal_dependency: the landing carries no -wal/-shm sidecars "
    "and is reopened immutable mode=ro; a hot WAL cannot repair it",
)
_ADVISORIES = (
    "r4_recoverable_is_not_admission: package byte verification alone "
    "never opens anything",
    "lazy_fetch_not_drilled: a real lazy fetch cannot be reproduced "
    "offline; the bootstrap pins protocol.allow=never and "
    "GIT_NO_LAZY_FETCH=1, and any missing object fails closed at fsck "
    "and the exact object-set comparison",
    "materials_staged_is_placement_only: placed and per-file verified; "
    "not runnable, not admitted, not open",
)


class MaterialInstallError(Exception):
    """Fixed reason codes; hostile file/subprocess text never propagates."""

    def __init__(self, reason, *, stage_reached="quarantined"):
        super().__init__(reason)
        self.reason = reason
        self.stage_reached = stage_reached


@dataclass(frozen=True)
class AdmissionEvidence:
    """Shape-only admission record. The D slice authenticates it later.

    verdict is exactly admitted/denied; core_digest binds the full core
    payload the admission decided about; anchors are the version/build
    compatibility anchors, compared verbatim to package-derived values.
    """

    verdict: str
    core_digest: str
    anchors: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class InstallLayout:
    """Every landing directory is caller-given; the installer never guesses.

    state_root holds the existing R5 journal and the single-writer lock;
    bare_parent receives the one fresh bare; w_roots maps each W role to
    its files-only root; archive_root/mapping_root receive the R shards;
    r2_index_root receives the single ledger file when R2 status is ok.
    """

    state_root: str
    bare_parent: str
    w_roots: tuple[tuple[str, str], ...]
    archive_root: str
    mapping_root: str
    r2_index_root: str


@dataclass(frozen=True)
class RootLanding:
    name: str
    files: tuple[tuple[str, int, str], ...]  # (relative, size, sha256:...)


@dataclass(frozen=True)
class MaterialReceipt:
    stage: str
    case_id: str
    backup_id: str
    barrier_id: str
    core_digest: str
    bare: TrustedBareReport
    roots: tuple[RootLanding, ...]
    r2_status: str
    journal_anchor: tuple[int, str]
    proofs: tuple[tuple[str, str], ...]
    wal_preconditions: tuple[str, ...]
    advisories: tuple[str, ...]


@dataclass(frozen=True)
class ReverifyReport:
    complete: bool
    case_id: str
    phase: str
    core_digest: str
    bare_path: str
    roots: tuple[RootLanding, ...]
    r2_status: str
    proofs_match: bool
    reasons: tuple[str, ...]


# --------------------------------------------------------------------- helpers

def _hex(value, length):
    return type(value) is str and len(value) == length and set(value) <= _HEX


def _digest_value(value):
    return type(value) is str and value.startswith("sha256:") and _hex(value[7:], 64)


def _safe_id(value):
    return type(value) is str and bool(_SAFE_ID_RE.fullmatch(value))


def _open_abs(path):
    """Pin an absolute directory from `/`; every segment refuses symlinks."""
    path = os.path.abspath(os.fspath(path))
    if "\x00" in path:
        raise MaterialInstallError("layout_path_invalid")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for segment in path.split("/")[1:]:
            if not segment:
                continue
            child = os.open(segment, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path
    except MaterialInstallError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise MaterialInstallError("layout_root_missing") from exc


def _open_file(root_fd, relative):
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise MaterialInstallError("coordinate_unsafe")
    parent = os.dup(root_fd)
    try:
        for segment in parts[:-1]:
            child = os.open(segment, _DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        pinned = os.open(parts[-1], _PATH_FLAGS, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(pinned).st_mode):
                raise MaterialInstallError("component_not_regular:" + relative)
            # The O_PATH pin already enforced O_NOFOLLOW on the leaf; the
            # /proc magic link is itself a symlink and must be reopened plain.
            return os.open("/proc/self/fd/%d" % pinned, os.O_RDONLY | _O_CLOEXEC)
        finally:
            os.close(pinned)
    except MaterialInstallError:
        raise
    except OSError as exc:
        raise MaterialInstallError("component_unreadable:" + relative) from exc
    finally:
        os.close(parent)


def _read_bytes(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        part = os.read(fd, _READ_CHUNK)
        if not part:
            return b"".join(chunks)
        chunks.append(part)


def _json_file(root_fd, relative):
    fd = _open_file(root_fd, relative)
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate json key")
                result[key] = value
            return result
        return json.loads(_read_bytes(fd), object_pairs_hook=pairs)
    except ValueError as exc:
        raise MaterialInstallError("package_envelope_malformed") from exc
    finally:
        os.close(fd)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _proof(kind, payload):
    return hashlib.sha256(
        _canonical({"kind": kind, "payload": payload}).encode("ascii")).hexdigest()


def barrier_digest(barrier_id):
    """Bind an R5 case to the host barrier named in the package envelope.

    The envelope carries the host-issued barrier id (e.g. ``b...-........``);
    R5 cases store a 64-hex ``barrier_digest``. One fixed, domain-separated
    hash bridges them. The host that quarantines the package must seed the
    case with this same derivation.
    """
    if type(barrier_id) is not str or not barrier_id:
        raise ValueError("invalid barrier id")
    return hashlib.sha256(
        b"sam.f001.recovery.barrier-id.v1\0" + barrier_id.encode("utf-8")
    ).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _open_ro_sqlite(fd):
    return sqlite3.connect("file:/proc/self/fd/%d?mode=ro&immutable=1" % fd,
                           uri=True)


def _quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _db_fingerprint(db):
    """Tables, columns, FKs, indexes and triggers — shape only, no bytes."""
    tables = tuple(row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"))
    result = {}
    for table in tables:
        ident = _quote_ident(table)
        columns = tuple((row[1], row[2], row[3], row[4], row[5])
                        for row in db.execute("PRAGMA table_info(" + ident + ")"))
        foreign_keys = tuple(tuple(row) for row in db.execute(
            "PRAGMA foreign_key_list(" + ident + ")"))
        indexes = []
        for row in db.execute("PRAGMA index_list(" + ident + ")"):
            index_name = row[1]
            info = tuple(tuple(item) for item in db.execute(
                "PRAGMA index_info(" + _quote_ident(index_name) + ")"))
            indexes.append((index_name, tuple(row[1:]), info))
        triggers = tuple(sorted(
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name=?", (table,))))
        result[table] = (columns, foreign_keys, tuple(sorted(indexes)), triggers)
    return result


def _regular_file_set(root_fd):
    """Recursive pinned enumeration; symlinks/specials fail closed."""
    files = set()

    def walk(fd, prefix):
        for name in os.listdir(fd):
            pinned = os.open(name, _PATH_FLAGS, dir_fd=fd)
            try:
                mode = os.fstat(pinned).st_mode
            finally:
                os.close(pinned)
            relative = name if not prefix else prefix + "/" + name
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise MaterialInstallError("target_root_unsafe_entry:" + relative)
            if stat.S_ISDIR(mode):
                opened = os.open(name, _DIR_FLAGS, dir_fd=fd)
                try:
                    walk(opened, relative)
                finally:
                    os.close(opened)
            else:
                files.add(relative)
    walk(os.dup(root_fd), "")
    return files


def _create_locked_at(dir_fd, name):
    """O_EXCL create through the pinned directory descriptor.

    Going via the /proc/self/fd magic link keeps the path absolute while the
    open itself stays create-only, no-follow, pinned to this directory.
    """
    return os.open(
        "/proc/self/fd/%d/%s" % (dir_fd, name),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
        _FILE_MODE)


def _probe_is_regular(dir_fd, name):
    try:
        pinned = os.open(name, _PATH_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(pinned).st_mode)
    finally:
        os.close(pinned)


# ----------------------------------------------------------- package binding

def _bind_package(package_fd, evidence, identity, head_oid):
    """Re-read the fixed descriptors; return the bound core and its digest."""
    if not isinstance(evidence, AdmissionEvidence):
        raise MaterialInstallError("admission_evidence_invalid")
    if evidence.verdict == "denied":
        raise MaterialInstallError("admission_denied")
    if evidence.verdict != "admitted" or not _digest_value(evidence.core_digest):
        raise MaterialInstallError("admission_evidence_invalid")
    anchors_seen = {}
    if type(evidence.anchors) is not tuple:
        raise MaterialInstallError("admission_evidence_invalid")
    for pair in evidence.anchors:
        if (type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not str or type(pair[1]) is not str
                or pair[0] in anchors_seen):
            raise MaterialInstallError("admission_evidence_invalid")
        anchors_seen[pair[0]] = pair[1]
    manifest = _json_file(package_fd, MANIFEST_NAME)
    report = _json_file(package_fd, REPORT_NAME)
    try:
        verify_final_manifest(manifest, report)
    except ValueError as exc:
        raise MaterialInstallError("package_envelope_binding_failed") from exc
    if manifest["version"] != _ENVELOPE_VERSION:
        raise MaterialInstallError("package_envelope_version_unsupported")
    core = manifest["core"]
    if type(core) is not dict:
        raise MaterialInstallError("package_envelope_malformed")
    payload = core.get("payload")
    if (not isinstance(payload, dict)
            or payload.get("schema") != PAYLOAD_SCHEMA
            or payload.get("version") != PAYLOAD_VERSION
            or type(payload.get("files")) is not list):
        raise MaterialInstallError("package_payload_anchor_mismatch")
    core_digest = compute_core_digest(core)
    if (core_digest != manifest["verification"]["core_digest"]
            or core_digest != evidence.core_digest):
        raise MaterialInstallError("admission_binding_mismatch")
    authority = core.get("authority")
    if not isinstance(authority, dict):
        raise MaterialInstallError("package_authority_invalid")
    fmt = authority.get("object_format")
    if anchors_seen != {
            "envelope_version": str(_ENVELOPE_VERSION),
            "payload_schema": PAYLOAD_SCHEMA,
            "payload_version": str(PAYLOAD_VERSION),
            "object_format": str(fmt)}:
        raise MaterialInstallError("admission_anchors_mismatch")
    if type(identity) is not dict or set(identity) != {
            "repo_id", "installation_id", "company_id"}:
        raise MaterialInstallError("identity_binding_mismatch")
    if any(type(identity[k]) is not str or not identity[k] for k in identity):
        raise MaterialInstallError("identity_binding_mismatch")
    if core.get("identity") != identity:
        raise MaterialInstallError("identity_binding_mismatch")
    if fmt not in {"sha1", "sha256"}:
        raise MaterialInstallError("package_authority_invalid")
    length = 40 if fmt == "sha1" else 64
    if not _hex(head_oid, length) or authority.get("commit_oid") != head_oid:
        raise MaterialInstallError("head_anchor_mismatch")
    if not _hex(authority.get("genesis_oid"), length):
        raise MaterialInstallError("package_authority_invalid")
    if type(authority.get("commit_count")) is not int or authority["commit_count"] < 1:
        raise MaterialInstallError("package_authority_invalid")
    if type(authority.get("pin_ref")) is not str or not authority["pin_ref"].startswith("refs/"):
        raise MaterialInstallError("package_authority_invalid")
    if not _safe_id(core.get("barrier_id")):
        raise MaterialInstallError("package_authority_invalid")
    if not _safe_id(core.get("backup_id")):
        raise MaterialInstallError("package_authority_invalid")
    return core, core_digest


def _validate_layout(layout, files):
    if not isinstance(layout, InstallLayout):
        raise MaterialInstallError("layout_invalid")
    roots = {"state": layout.state_root, "bare": layout.bare_parent,
             "archive": layout.archive_root, "mapping": layout.mapping_root,
             "r2": layout.r2_index_root,
             **{"w:" + role: root for role, root in layout.w_roots}}
    if any(type(value) is not str or not os.path.isabs(value) or "\x00" in value
           for value in roots.values()):
        raise MaterialInstallError("layout_invalid")
    if len(set(roots.values())) != len(roots):
        raise MaterialInstallError("layout_roots_overlap")
    roles = sorted({entry["role"] for entry in files if entry["family"] == "w"})
    if sorted(role for role, _root in layout.w_roots) != roles:
        raise MaterialInstallError("layout_w_roots_mismatch")
    return roles


# ------------------------------------------------------------- placement core

class _Placement:
    def __init__(self, package_fd, layout):
        self.package_fd = package_fd
        self.layout = layout
        self.fds = {}
        self.created_files = []   # (root_key, relative)
        self.created_dirs = []    # (root_key, shard)
        self.landings = {}        # (root_key, relative) -> (size, digest)

    def root_fd(self, key, path):
        if key not in self.fds:
            fd, _path = _open_abs(path)
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                os.close(fd)
                raise MaterialInstallError("layout_root_not_private_dir")
            self.fds[key] = fd
        return self.fds[key]

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()

    def _shard(self, root_key, root_fd, shard):
        if not (len(shard) == 2 and all(c in _HEX for c in shard)):
            raise MaterialInstallError("coordinate_unsafe")
        try:
            os.mkdir(shard, _DIR_MODE, dir_fd=root_fd)
            self.created_dirs.append((root_key, shard))
        except FileExistsError:
            pass
        except OSError as exc:
            raise MaterialInstallError("target_shard_create_failed") from exc
        fd = os.open(shard, _DIR_FLAGS, dir_fd=root_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise MaterialInstallError("target_shard_not_private_dir")
        finally:
            os.close(fd)

    def _discard(self, root_fd, relative):
        """Best-effort remove a half-landed file so a failed place leaves no
        unaccounted bytes behind; shard dirs are reaped by void()."""
        try:
            os.unlink(relative, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError:
            pass

    def place(self, root_key, root_path, src, relative, size, digest, *, shard=False):
        root_fd = self.root_fd(root_key, root_path)
        key = (root_key, relative)
        if key in self.landings:
            # Shared digest (R copy and R2 backing) is installed exactly once.
            if self.landings[key] != (size, digest):
                raise MaterialInstallError("shared_digest_conflict:" + relative)
            return
        if shard:
            self._shard(root_key, root_fd, relative.split("/", 1)[0])
        try:
            _copy_regular_pinned(self.package_fd, src, root_fd, relative)
        except OSError as exc:
            self._discard(root_fd, relative)
            raise MaterialInstallError("material_landing_failed:" + relative) from exc
        parent_fd = root_fd
        if shard:
            parent_fd = os.open(relative.split("/", 1)[0], _DIR_FLAGS, dir_fd=root_fd)
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            self._discard(root_fd, relative)
            raise MaterialInstallError("material_landing_failed:" + relative) from exc
        finally:
            if parent_fd is not root_fd:
                os.close(parent_fd)
        try:
            verify_backup_component(
                root_fd, relative_path=relative, size_bytes=size,
                sha256_digest=digest)
        except ValueError as exc:
            self._discard(root_fd, relative)
            raise MaterialInstallError("material_reverify_failed:" + relative) from exc
        self.created_files.append(key)
        self.landings[key] = (size, digest)

    def void(self):
        """Remove exactly what this placement created, newest first."""
        for root_key, relative in reversed(self.created_files):
            fd = self.fds.get(root_key)
            if fd is None:
                continue
            try:
                os.unlink(relative, dir_fd=fd)
            except OSError:
                pass
        for root_key, shard in reversed(self.created_dirs):
            fd = self.fds.get(root_key)
            if fd is None:
                continue
            try:
                os.rmdir(shard, dir_fd=fd)
            except OSError:
                pass
        for fd in self.fds.values():
            try:
                os.fsync(fd)
            except OSError:
                pass

    def summary(self, name):
        ordered = tuple(sorted(
            (relative, size, digest)
            for (root_key, relative), (size, digest) in self.landings.items()
            if root_key == name))
        return RootLanding(name, ordered)

    def load_installed(self, layout):
        """Read back an existing install (size + sha256) for proof rebuild.

        Every path segment is pinned no-follow by _open_file, and the set of
        relative names was just proven exact against the package inventory.
        """
        for key, path in _targets(layout):
            root_fd = self.root_fd(key, path)
            for relative in _regular_file_set(root_fd):
                fd = _open_file(root_fd, relative)
                try:
                    info = os.fstat(fd)
                    if info.st_mode & 0o077:
                        raise MaterialInstallError(
                            "material_not_private:" + relative)
                    hasher = hashlib.sha256()
                    while True:
                        chunk = os.read(fd, _READ_CHUNK)
                        if not chunk:
                            break
                        hasher.update(chunk)
                finally:
                    os.close(fd)
                self.landings[(key, relative)] = (
                    info.st_size, "sha256:" + hasher.hexdigest())


# --------------------------------------------------------------- verification

def _replay_r(core, files, placement):
    reasons = []

    def fail(reason):
        reasons.append(reason)

    _replay_closure_digest(core, fail)
    archive_fd = placement.fds.get("archive")
    mapping_fd = placement.fds.get("mapping")

    def hash_coordinate(coordinate):
        # The lexical package coordinate is evidence only; it selects which
        # already-installed root descriptor to read, never a path to walk.
        parts = coordinate.split("/")
        if len(parts) != 5 or parts[:2] != ["payload", "r"]:
            raise KeyError(coordinate)
        cls, shard, bare = parts[2], parts[3], parts[4]
        if cls == "archive":
            fd_root = archive_fd
        elif cls == STORAGE_CLASS_MAPPING_OBJECTS:
            fd_root = mapping_fd
        else:
            raise KeyError(coordinate)
        if fd_root is None:
            raise OSError("routing root absent")
        fd = _open_file(fd_root, shard + "/" + bare)
        try:
            hasher = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
            return "sha256:" + hasher.hexdigest(), total
        finally:
            os.close(fd)

    _replay_closure_bytes(core, files, hash_coordinate, fail)
    if reasons:
        raise MaterialInstallError(
            "r_closure_replay_failed:" + ",".join(sorted(set(reasons))))


def _recheck_w(core, files, placement, package_fd):
    anchors = {entry["role"]: entry for entry in core["databases"]}
    for entry in files:
        if entry["family"] != "w":
            continue
        role = entry["role"]
        name = role + ".sqlite"
        w_fd = placement.fds["w:" + role]
        installed = _open_file(w_fd, name)
        packaged = _open_file(package_fd, _PAYLOAD + "/w/" + name)
        try:
            db_installed = _open_ro_sqlite(installed)
            db_packaged = _open_ro_sqlite(packaged)
            try:
                if db_installed.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise MaterialInstallError("w_integrity_failed:" + role)
                if db_installed.execute("PRAGMA foreign_key_check").fetchall():
                    raise MaterialInstallError("w_foreign_key_failed:" + role)
                fingerprint = _db_fingerprint(db_installed)
                if fingerprint != _db_fingerprint(db_packaged):
                    raise MaterialInstallError("w_fingerprint_mismatch:" + role)
                anchor = anchors.get(role, {})
                declared = anchor.get("schema", {}).get("tables")
                if type(declared) is not list or set(fingerprint) != set(declared):
                    raise MaterialInstallError("w_schema_anchor_mismatch:" + role)
            finally:
                db_packaged.close()
                db_installed.close()
        finally:
            os.close(packaged)
            os.close(installed)


def _recheck_r2(core, files, placement):
    block = core["payload"]["r2"]
    status = block["status"]
    r2_entries = [entry for entry in files if entry["family"] == "r2"]
    if status == "skipped":
        if r2_entries or core.get("mapping_ledger") is not None:
            raise MaterialInstallError("r2_shape_mismatch")
        return "skipped"
    if status != "ok":
        raise MaterialInstallError("r2_shape_mismatch")
    mapping_fd = placement.fds["mapping"]
    r2_fd = placement.fds["r2"]
    ledger_fd = _open_file(r2_fd, _DB_FILENAME)
    try:
        db = _open_ro_sqlite(ledger_fd)
        try:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MaterialInstallError("r2_integrity_failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise MaterialInstallError("r2_foreign_key_failed")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise MaterialInstallError("r2_shape_mismatch")
            identity = core["identity"]
            meta = db.execute(
                "SELECT singleton,installation_id,company_id "
                "FROM fact_mapping_version_index_meta").fetchall()
            if meta != [(1, identity["installation_id"], identity["company_id"])]:
                raise MaterialInstallError("r2_row_integrity")
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                "SELECT " + ",".join(_COLUMNS) +
                " FROM fact_mapping_version_registrations "
                "ORDER BY logical_source_id, version_no")]
        finally:
            db.close()
    finally:
        os.close(ledger_fd)
    registrations = []
    sources = {}
    for row in rows:
        checksum = row.pop("row_sha256")
        if (checksum != _row_digest(**row) or not _hex(row["digest"], 64)
                or row["object_type"] != OBJECT_TYPE_MAPPING_VERSION
                or row["storage_class"] != STORAGE_CLASS_MAPPING_OBJECTS
                or row["source_kind"] not in {"online", "backfill"}
                or type(row["version_no"]) is not int or row["version_no"] < 1
                or not isinstance(row["logical_source_id"], str)
                or not row["logical_source_id"]
                or any(row[k] != identity[k]
                       for k in ("installation_id", "company_id"))):
            raise MaterialInstallError("r2_row_integrity")
        source = row["logical_source_id"]
        record = sources.setdefault(
            source, {"logical_source_id": source, "versions": 0,
                     "gaps": 0, "gap_versions": []})
        record["versions"] += 1
        if row["version_no"] != record["versions"]:
            raise MaterialInstallError("r2_shape_mismatch")
        relative = row["digest"][:2] + "/" + row["digest"]
        backing_fd = _open_file(mapping_fd, relative)
        try:
            hasher = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(backing_fd, _READ_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
            if hasher.hexdigest() != row["digest"] or total != row["size_bytes"]:
                record["gaps"] += 1
                record["gap_versions"].append(row["version_no"])
                raise MaterialInstallError("r2_backing_mismatch")
        finally:
            os.close(backing_fd)
        registrations.append({
            "logical_source_id": source, "version_no": row["version_no"],
            "sha256_digest": DIGEST_ALGORITHM + ":" + row["digest"],
            "size_bytes": row["size_bytes"], "source_kind": row["source_kind"],
            "source_coordinate": row["source_coordinate"],
            "h_commit_oid": row["h_commit_oid"]})
    payload = {
        "schema": LEDGER_SCHEMA, "version": SCHEMA_VERSION,
        "scope": {"kind": "company",
                  "installation_id": identity["installation_id"],
                  "company_id": identity["company_id"]},
        "object_type": OBJECT_TYPE_MAPPING_VERSION,
        "storage_class": STORAGE_CLASS_MAPPING_OBJECTS,
        "digest_algorithm": DIGEST_ALGORITHM,
        "registrations": registrations}
    if compute_core_digest(payload) != block["ledger_digest"]:
        raise MaterialInstallError("r2_ledger_digest_mismatch")
    anchor = core["mapping_ledger"]
    if anchor is None or block["ledger_digest"] != anchor["ledger_digest"]:
        raise MaterialInstallError("r2_ledger_digest_mismatch")
    shape = {"sources": [sources[key] for key in sorted(sources)],
             "total_sources": len(sources), "total_versions": len(rows),
             "total_gaps": sum(item["gaps"] for item in sources.values())}
    if any(block.get(k) != v or anchor.get(k) != v for k, v in shape.items()):
        raise MaterialInstallError("r2_shape_mismatch")
    if (block["backing_files"] != len(r2_entries) - 1
            or block["ledger_file_sha256"] is None):
        raise MaterialInstallError("r2_shape_mismatch")
    return "ok"


def _expected_sets(files, r2_status):
    """Routed root_key -> {relative} sets from the package inventory."""
    expected = {}
    for entry in files:
        family = entry["family"]
        digest = entry["sha256_digest"]
        if family == "w":
            key = ("w:" + entry["role"], entry["role"] + ".sqlite")
        elif family == "r":
            cls = entry["storage_class"]
            key = ("archive" if cls == "archive" else "mapping",
                   digest[7:9] + "/" + digest[7:])
        elif family == "r2":
            if entry["path"] == _LEDGER_RELATIVE:
                if r2_status != "ok":
                    raise MaterialInstallError("r2_shape_mismatch")
                key = ("r2", _DB_FILENAME)
            else:
                key = ("mapping", digest[7:9] + "/" + digest[7:])
        else:
            raise MaterialInstallError("payload_family_unknown")
        expected.setdefault(key[0], set()).add(key[1])
    return expected


def _targets(layout):
    items = [("archive", layout.archive_root),
             ("mapping", layout.mapping_root), ("r2", layout.r2_index_root)]
    items += [("w:" + role, root) for role, root in layout.w_roots]
    return items


def _assert_exact_sets(placement, expected, layout):
    for key, path in _targets(layout):
        fd = placement.root_fd(key, path)
        if _regular_file_set(fd) != expected.get(key, set()):
            raise MaterialInstallError("root_set_not_exact:" + key)


def _root_summaries(placement, layout):
    names = ("r2", "archive", "mapping") + tuple(
        "w:" + role for role, _root in layout.w_roots)
    return tuple(placement.summary(name) for name in names)


def _live_bare(bare_parent):
    fd, path = _open_abs(bare_parent)
    try:
        live = [name for name in os.listdir(fd)
                if name.startswith("sam-recovery-facts-")
                and name.endswith(".git")
                and not name.endswith(".abandoned.git")]
        if len(live) != 1:
            raise MaterialInstallError("bare_layout_not_unique")
        return os.path.join(path, live[0])
    finally:
        os.close(fd)


def _package_proof(case_id, core_digest, evidence, identity, core):
    return _proof(_PROOF_KIND_PACKAGE, {
        "case_id": case_id,
        "core_digest": core_digest,
        "verdict": evidence.verdict,
        "anchors": tuple(sorted(evidence.anchors)),
        "identity": tuple(sorted(identity.items())),
        "backup_id": core["backup_id"],
        "barrier_id": core["barrier_id"],
    })


def _materials_proof(case_id, core_digest, bare_anchors, r2_status, roots):
    return _proof(_PROOF_KIND_MATERIALS, {
        "case_id": case_id,
        "core_digest": core_digest,
        "bare": bare_anchors,
        "r2_status": r2_status,
        "roots": tuple(
            {"name": landing.name,
             "files": tuple((rel, size, digest)
                            for rel, size, digest in landing.files)}
            for landing in roots),
    })


# ------------------------------------------------------------------- public API

def install_materials(*, package_dir, layout, case_id, identity, head_oid,
                      evidence, git_executable, at=None, seam_after_file=None):
    """Place H/W/R/R2 materials and advance one quarantined case twice.

    On failure the files this call created are removed, the bare is
    abandoned by the bootstrap, and the append-only case keeps the phase
    it actually reached. ``seam_after_file(root_key, relative)`` is a
    TEST-ONLY hook fired after one file is fully landed; production
    callers never pass it.
    """
    if at is not None and type(at) is not str:
        raise MaterialInstallError("timestamp_invalid")
    package_fd, package_path = _open_abs(package_dir)
    try:
        # Bind #1 happens before any target write; denied/missing evidence
        # never creates a directory, lock, connection or bare.
        core, core_digest = _bind_package(package_fd, evidence, identity, head_oid)
        files = core["payload"]["files"]
        _validate_layout(layout, files)
        verdict = verify_recovery_package(package_path, expected_identity=identity)
        if not verdict.recoverable:
            raise MaterialInstallError("package_verification_failed")
        if (verdict.backup_id != core["backup_id"]
                or verdict.barrier_id != core["barrier_id"]
                or verdict.object_format != core["authority"]["object_format"]):
            raise MaterialInstallError("package_verification_failed")

        state_dir_fd, state_path = _open_abs(layout.state_root)
        try:
            if not _probe_is_regular(state_dir_fd, _STATE_DB_NAME):
                raise MaterialInstallError("state_case_not_quarantined")
            store = RecoveryStateStore(Path(state_path))
            try:
                try:
                    view = store.get(case_id)
                except ValueError as exc:
                    raise MaterialInstallError(
                        "state_case_not_quarantined") from exc
                if (view.phase != RecoveryPhase.QUARANTINED
                        or view.backup_id != core["backup_id"]
                        or view.barrier_digest != barrier_digest(core["barrier_id"])):
                    raise MaterialInstallError("state_case_not_quarantined")
                try:
                    lock_fd = _create_locked_at(state_dir_fd, _LOCK_NAME)
                except OSError as exc:
                    raise MaterialInstallError(
                        "single_writer_lock_held") from exc
                try:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        raise MaterialInstallError("single_writer_lock_held") from exc
                    at1 = at or _utcnow()
                    proof1 = _package_proof(
                        case_id, core_digest, evidence, identity, core)
                    store.transition(
                        case_id, RecoveryPhase.PACKAGE_VERIFIED,
                        proofs=(("package_verified", proof1),), at=at1)
                    return _place_all(
                        package_fd=package_fd, layout=layout, files=files,
                        core=core, core_digest=core_digest, case_id=case_id,
                        identity=identity, head_oid=head_oid, evidence=evidence,
                        proof1=proof1, store=store, at=at1,
                        git_executable=git_executable,
                        seam_after_file=seam_after_file)
                finally:
                    os.close(lock_fd)
            finally:
                store.close()
        finally:
            os.close(state_dir_fd)
    finally:
        os.close(package_fd)


def _place_all(*, package_fd, layout, files, core, core_digest, case_id,
               identity, head_oid, evidence, proof1, store, at,
               git_executable, seam_after_file):
    placement = _Placement(package_fd, layout)
    r2_status = core["payload"]["r2"]["status"]
    try:
        try:
            # Bind #2 from the same fixed descriptors right before bytes move.
            core_again, digest_again = _bind_package(
                package_fd, evidence, identity, head_oid)
            if (digest_again != core_digest
                    or core_again["payload"] != core["payload"]):
                raise MaterialInstallError(
                    "package_toctou_binding_failed",
                    stage_reached="package_verified")

            bundle_fd = _open_file(package_fd, BUNDLE_NAME)
            try:
                bare = bootstrap_trusted_bare(
                    isolated_parent=layout.bare_parent, bundle_fd=bundle_fd,
                    head_oid=head_oid, pin_ref=core["authority"]["pin_ref"],
                    genesis_oid=core["authority"]["genesis_oid"],
                    commit_count=core["authority"]["commit_count"],
                    object_format=core["authority"]["object_format"],
                    identity=identity, git_executable=git_executable)
            finally:
                os.close(bundle_fd)

            w_root_by_role = dict(layout.w_roots)
            for entry in files:
                family = entry["family"]
                digest = entry["sha256_digest"]
                size = entry["size_bytes"]
                if family == "w":
                    root_key = "w:" + entry["role"]
                    root_path = w_root_by_role[entry["role"]]
                    relative = entry["role"] + ".sqlite"
                    src = _PAYLOAD + "/w/" + relative
                    shard = False
                elif family == "r":
                    if entry["storage_class"] == "archive":
                        root_key, root_path = "archive", layout.archive_root
                    else:
                        root_key, root_path = "mapping", layout.mapping_root
                    relative = digest[7:9] + "/" + digest[7:]
                    src, shard = entry["path"], True
                else:
                    if entry["path"] == _LEDGER_RELATIVE:
                        root_key, root_path = "r2", layout.r2_index_root
                        relative, shard = _DB_FILENAME, False
                    else:
                        root_key, root_path = "mapping", layout.mapping_root
                        relative, shard = digest[7:9] + "/" + digest[7:], True
                    src = entry["path"]
                placement.place(root_key, root_path, src, relative, size,
                                digest, shard=shard)
                if seam_after_file is not None:
                    seam_after_file(root_key, relative)

            expected = _expected_sets(files, r2_status)
            _assert_exact_sets(placement, expected, layout)
            _replay_r(core, files, placement)
            _recheck_w(core, files, placement, package_fd)
            r2_status = _recheck_r2(core, files, placement)
            _assert_exact_sets(placement, expected, layout)

            roots = _root_summaries(placement, layout)
            bare_anchors = {
                "object_format": bare.object_format,
                "head_oid": bare.head_oid,
                "genesis_oid": bare.genesis_oid,
                "commit_count": bare.commit_count,
            }
            proof2 = _materials_proof(
                case_id, core_digest, bare_anchors, r2_status, roots)
            store.transition(
                case_id, RecoveryPhase.MATERIALS_STAGED,
                proofs=(("materials_staged", proof2),), at=at)
            view = store.get(case_id)
            return MaterialReceipt(
                stage=view.phase.value, case_id=case_id,
                backup_id=core["backup_id"], barrier_id=core["barrier_id"],
                core_digest=core_digest, bare=bare, roots=roots,
                r2_status=r2_status, journal_anchor=store.journal_anchor(),
                proofs=(("package_verified", proof1),
                        ("materials_staged", proof2)),
                wal_preconditions=_WAL_PRECONDITIONS, advisories=_ADVISORIES)
        except MaterialInstallError as exc:
            placement.void()
            # Every error from _place_all lands after the package_verified
            # transition was appended; the journal phase is the real stage.
            if exc.stage_reached == "quarantined":
                exc.stage_reached = "package_verified"
            raise
        except Exception as exc:
            placement.void()
            raise MaterialInstallError(
                "material_install_failed",
                stage_reached="package_verified") from exc
    finally:
        placement.close()


def reverify_materials(*, package_dir, layout, case_id, identity, head_oid,
                       evidence, git_executable, at=None):
    """Complete same-identity read-only re-verification; never writes.

    A second process after a crash uses this: it takes a shared flock on
    the existing lock file, rebinds the fixed package, reopens the bare
    through the read-only adapter, re-enumerates every root set and
    replays every byte check, and compares the R5 proofs. It never
    resumes, repairs or advances anything. ``at`` is accepted for seam
    symmetry with install but unused: no journal event is appended.
    """
    if at is not None and type(at) is not str:
        raise MaterialInstallError("timestamp_invalid")
    package_fd, _package_path = _open_abs(package_dir)
    try:
        core, core_digest = _bind_package(package_fd, evidence, identity, head_oid)
        files = core["payload"]["files"]
        _validate_layout(layout, files)
        r2_status = core["payload"]["r2"]["status"]
        state_dir_fd, state_path = _open_abs(layout.state_root)
        try:
            if not _probe_is_regular(state_dir_fd, _STATE_DB_NAME):
                return ReverifyReport(
                    False, case_id, "absent", core_digest, "", (), r2_status,
                    False, ("state_journal_absent",))
            try:
                lock_fd = os.open(_LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW
                                  | _O_CLOEXEC, dir_fd=state_dir_fd)
            except OSError:
                return ReverifyReport(
                    False, case_id, RecoveryPhase.QUARANTINED.value, core_digest,
                    "", (), r2_status, False,
                    ("no_install_attempt_recorded",))
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    return ReverifyReport(
                        False, case_id, RecoveryPhase.QUARANTINED.value,
                        core_digest, "", (), r2_status, False,
                        ("single_writer_active",))
                store = RecoveryStateStore(Path(state_path))
                try:
                    try:
                        view = store.get(case_id)
                    except ValueError:
                        return ReverifyReport(
                            False, case_id, "unknown", core_digest, "", (),
                            r2_status, False, ("state_case_unknown",))
                    reasons = []
                    if (view.backup_id != core["backup_id"]
                            or view.barrier_digest != barrier_digest(core["barrier_id"])):
                        reasons.append("case_binding_mismatch")
                    placement = _Placement(package_fd, layout)
                    bare_path = ""
                    roots = ()
                    bare_anchors = {}
                    try:
                        from app.facts_tree.git_repository import (
                            FactsGitRepository)
                        bare_path = _live_bare(layout.bare_parent)
                        repo = FactsGitRepository.open(
                            bare_path, repo_id=identity["repo_id"],
                            installation_id=identity["installation_id"],
                            company_id=identity["company_id"])
                        report = repo.verify_integrity(expected_head=head_oid)
                        if (report.genesis_oid
                                != core["authority"]["genesis_oid"]
                                or report.commit_count
                                != core["authority"]["commit_count"]
                                or report.object_format
                                != core["authority"]["object_format"]):
                            reasons.append("bare_anchor_mismatch")
                        expected = _expected_sets(files, r2_status)
                        _assert_exact_sets(placement, expected, layout)
                        _replay_r(core, files, placement)
                        _recheck_w(core, files, placement, package_fd)
                        r2_status = _recheck_r2(core, files, placement)
                        _assert_exact_sets(placement, expected, layout)
                        placement.load_installed(layout)
                        roots = _root_summaries(placement, layout)
                        bare_anchors = {
                            "object_format": report.object_format,
                            "head_oid": report.head_oid,
                            "genesis_oid": report.genesis_oid,
                            "commit_count": report.commit_count}
                    except MaterialInstallError as exc:
                        reasons.append(exc.reason)
                    except Exception:
                        reasons.append("material_reverification_failed")
                    finally:
                        placement.close()
                    proof1 = _package_proof(
                        case_id, core_digest, evidence, identity, core)
                    proof2 = _materials_proof(
                        case_id, core_digest, bare_anchors if bare_path else {},
                        r2_status, roots)
                    proofs_match = (
                        dict(view.proofs).get("package_verified") == proof1
                        and dict(view.proofs).get("materials_staged") == proof2)
                    if not proofs_match:
                        reasons.append("proofs_mismatch")
                    if view.phase != RecoveryPhase.MATERIALS_STAGED:
                        reasons.append("phase_not_materials_staged")
                    return ReverifyReport(
                        not reasons, case_id, view.phase.value, core_digest,
                        bare_path, roots, r2_status, proofs_match,
                        tuple(sorted(set(reasons))))
                finally:
                    store.close()
            finally:
                os.close(lock_fd)
        finally:
            os.close(state_dir_fd)
    finally:
        os.close(package_fd)
