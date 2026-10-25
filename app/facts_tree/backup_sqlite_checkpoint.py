"""Single-SQLite-database consistent checkpoint artifact for F-001 W backups.

Implements the per-database W checkpoint primitive from sections 3 and 5
of the approved F-001 backup design (``98-完整备份与隔离恢复设计.md``)
with the SQLite Online Backup API. This module produces exactly ONE
immutable single-file checkpoint of ONE SQLite database; it is not a
complete H/W/R backup and by itself never claims a cross-database cut.

Caller preconditions (this module cannot prove any of them):

- The caller must have entered the host maintenance barrier covering
  EVERY writer and cleanup task of the source database before calling,
  and must keep that barrier until :func:`create_sqlite_checkpoint`
  returns successfully. The SQLite online backup may restart copying
  when the source database changes, so releasing the barrier after the
  backup started does not fix a consistent W snapshot. This function
  never acquires or releases a host barrier and never opens another
  business database.
- The caller keeps full ownership of ``source_connection``: this module
  never closes, commits, rolls back, reconfigures or otherwise mutates
  its transaction state. The backup only reads the source database.

Scope guards: the SQLite Backup API guarantees single-database copy
consistency only. It is not a substitute for the cross-database
maintenance barrier, and a checkpoint produced while active writes are
not frozen must not be claimed to share a cut with other W databases or
with H.

Durability note: publication (file fsync, atomic no-overwrite hard link
and destination-directory fsync) only keeps its promises on a
filesystem that honours ``fsync``; network or fused filesystems with
different crash semantics need deployment validation, per the design.
"""
import contextlib
import hashlib
import os
import sqlite3
import stat as stat_module
import tempfile
from dataclasses import dataclass
from pathlib import Path

DIGEST_ALGORITHM = "sha256"

_TEMP_PREFIX = ".sqlite-checkpoint-"
_TEMP_SUFFIX = ".tmp"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_HASH_CHUNK_SIZE = 1024 * 1024

# Never follow a swapped-in symlink while reading our own temp artifact,
# and only open directories as directories for the fsync step.
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class SqliteCheckpointError(Exception):
    """Raised when a single-DB checkpoint cannot be produced or published."""


@dataclass(frozen=True)
class SqliteCheckpointResult:
    """An immutable published single-file SQLite checkpoint.

    ``path`` is the final destination, ``size_bytes`` its exact byte
    count and ``digest`` ``sha256:`` plus 64 lowercase hexadecimal
    characters, computed over exactly those bytes. A published
    checkpoint is treated as immutable input: this module never opens it
    for writing or modifies it again.
    """

    path: Path
    size_bytes: int
    digest: str


def create_sqlite_checkpoint(source_connection, destination_path):
    """Create one consistent single-file SQLite checkpoint.

    Copies the source database into a uniquely named temporary snapshot
    in the destination's own directory via ``source_connection.backup``,
    collapses it to a single rollback-journal file, runs
    ``PRAGMA integrity_check`` (must return exactly ``ok``) and
    ``PRAGMA foreign_key_check`` (must be empty) on the snapshot, then
    hashes, fsyncs and publishes it atomically without overwriting.

    Preconditions (see module docstring): a maintenance barrier covering
    every writer of the source database is held by the caller until this
    function returns successfully. The source connection must be open,
    usable and have no unfinished transaction; the destination's parent
    directory must already exist; ``destination_path`` must not exist,
    not even as a dangling symlink (symlinks are never followed).

    Returns a frozen :class:`SqliteCheckpointResult` with the final
    path, byte size and ``sha256:`` digest. Raises
    :class:`SqliteCheckpointError` on any invalid input, backup or
    verification failure, or unsupported hard-link/fsync publication
    semantic; in every failure case the destination is never published
    and temporary artifacts are removed on a best-effort basis.
    """
    source = _require_live_idle_source(source_connection)
    destination, parent = _require_unused_destination(destination_path)

    temp_path = _make_temp_path(parent)
    try:
        _backup_and_verify(source, temp_path)
        size_bytes, digest = _hash_and_sync_file(temp_path)
        _publish_atomic(temp_path, destination, parent)
    except BaseException:
        # Best-effort cleanup of anything still private to this attempt.
        # A successfully published destination is never touched here.
        _remove_temp_family(temp_path)
        raise
    return SqliteCheckpointResult(
        path=Path(os.fsdecode(destination)),
        size_bytes=size_bytes,
        digest=digest,
    )


def _require_live_idle_source(connection):
    if not isinstance(connection, sqlite3.Connection):
        raise SqliteCheckpointError(
            "source must be an open sqlite3.Connection, got "
            f"{type(connection).__name__}"
        )
    # Accessing in_transaction on a closed connection raises
    # ProgrammingError; use it as the closed/unusable probe.
    try:
        in_transaction = connection.in_transaction
    except sqlite3.ProgrammingError as exc:
        raise SqliteCheckpointError(
            "source sqlite3.Connection is closed or unusable"
        ) from exc
    if in_transaction:
        raise SqliteCheckpointError(
            "source sqlite3.Connection has an unfinished transaction; all "
            "writers must be quiesced behind the maintenance barrier first"
        )
    return connection


def _require_unused_destination(destination_path):
    try:
        destination = os.fspath(destination_path)
    except TypeError as exc:
        raise SqliteCheckpointError(
            "destination path must be a str or os.PathLike, got "
            f"{type(destination_path).__name__}"
        ) from exc
    if isinstance(destination, str):
        empty = destination == ""
        nul = "\x00" in destination
        parent = os.path.dirname(destination) or "."
    else:
        empty = destination == b""
        nul = b"\x00" in destination
        parent = os.path.dirname(destination) or b"."
    if empty or nul:
        raise SqliteCheckpointError(
            "destination path is empty or contains a NUL byte"
        )
    if not os.path.isdir(parent):
        raise SqliteCheckpointError(
            f"destination directory does not exist or is not a directory: {parent!r}"
        )
    # lexists refuses regular files, directories and symlinks alike,
    # including dangling symlinks, and never follows one.
    if os.path.lexists(destination):
        raise SqliteCheckpointError(
            "destination already exists (file, directory or symlink); "
            f"refusing to overwrite or follow it: {destination!r}"
        )
    return destination, parent


def _make_temp_path(parent):
    # mkstemp creates the file O_EXCL with mode 0600 in the destination's
    # own directory, so the later hard link is atomic on one filesystem.
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=parent
    )
    # SQLite opens the snapshot itself; the empty descriptor is unused.
    os.close(file_descriptor)
    return temp_path


def _backup_and_verify(source, temp_path):
    try:
        target = sqlite3.connect(temp_path, isolation_level=None)
    except sqlite3.Error as exc:
        raise SqliteCheckpointError(
            f"cannot open temporary snapshot database: {exc}"
        ) from exc
    try:
        try:
            source.backup(target)
        except sqlite3.Error as exc:
            raise SqliteCheckpointError(
                f"SQLite Backup API failed: {exc}"
            ) from exc
        try:
            # The backup copies the source database header, including a
            # persistent WAL journal mode. Truncate the copied WAL and
            # switch to rollback-journal mode so the published artifact is
            # a single self-contained file with no -wal/-shm sidecars.
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            target.execute("PRAGMA journal_mode=DELETE").fetchall()
            integrity = target.execute("PRAGMA integrity_check").fetchall()
            foreign_key_violations = target.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SqliteCheckpointError(
                f"snapshot verification raised a SQLite error: {exc}"
            ) from exc
        if integrity != [("ok",)]:
            detail = "; ".join(str(row[0]) for row in integrity[:3])
            raise SqliteCheckpointError(
                "PRAGMA integrity_check did not return exactly 'ok': "
                f"{detail}"
            )
        if foreign_key_violations:
            raise SqliteCheckpointError(
                "PRAGMA foreign_key_check found "
                f"{len(foreign_key_violations)} violating row(s), first: "
                f"{foreign_key_violations[0]!r}"
            )
    finally:
        try:
            target.close()
        except sqlite3.Error as exc:
            raise SqliteCheckpointError(
                f"snapshot database did not close cleanly: {exc}"
            ) from exc
    leftovers = [
        name for name in _sidecar_paths(temp_path) if os.path.lexists(name)
    ]
    if leftovers:
        raise SqliteCheckpointError(
            "snapshot produced SQLite sidecar files; a single-file checkpoint "
            f"cannot be published: {leftovers!r}"
        )


def _hash_and_sync_file(path):
    hasher = hashlib.sha256()
    size_bytes = 0
    try:
        file_descriptor = os.open(path, _FILE_OPEN_FLAGS)
        try:
            while True:
                chunk = os.read(file_descriptor, _HASH_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                size_bytes += len(chunk)
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        stat_result = os.stat(path)
    except OSError as exc:
        raise SqliteCheckpointError(
            f"cannot hash and synchronize the snapshot file: {exc}"
        ) from exc
    if not stat_module.S_ISREG(stat_result.st_mode):
        raise SqliteCheckpointError("snapshot path is not a regular file")
    if stat_result.st_size != size_bytes:
        raise SqliteCheckpointError(
            "snapshot changed while hashing: read "
            f"{size_bytes} bytes but size is now {stat_result.st_size}"
        )
    return size_bytes, f"{DIGEST_ALGORITHM}:{hasher.hexdigest()}"


def _publish_atomic(temp_path, destination, parent):
    # hard link = atomic create that fails if the destination appeared in
    # the meantime; it never overwrites and never follows symlinks.
    try:
        os.link(temp_path, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise SqliteCheckpointError(
            "destination appeared while the checkpoint was being produced; "
            "refusing to overwrite it"
        ) from exc
    except OSError as exc:
        raise SqliteCheckpointError(
            "atomic no-overwrite publication via hard link is unsupported on "
            f"this filesystem; not degrading to overwrite: {exc}"
        ) from exc
    try:
        _fsync_directory(parent)
    except OSError as exc:
        # Durability of the new entry is unproven: remove only the exact
        # inode we published, never a racer's replacement, then fail closed.
        _remove_if_same_inode(destination, temp_path)
        raise SqliteCheckpointError(
            "could not synchronize the destination directory; checkpoint "
            "not published"
        ) from exc
    # From here on the checkpoint is durable at destination and is never
    # removed again, even if the temp-name cleanup or its directory sync
    # fail; report the failure honestly instead of claiming success.
    try:
        os.unlink(temp_path)
    except OSError as exc:
        raise SqliteCheckpointError(
            "checkpoint is linked and synchronized at the destination, but "
            "removing the temporary snapshot failed; inspect before retry"
        ) from exc
    try:
        _fsync_directory(parent)
    except OSError as exc:
        raise SqliteCheckpointError(
            "checkpoint exists and was synchronized at the destination, but "
            "the directory sync after temporary-file removal failed"
        ) from exc


def _fsync_directory(path):
    file_descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _remove_if_same_inode(path, reference_path):
    """Best-effort unlink ``path`` only when it is the same regular inode."""
    try:
        reference = os.stat(reference_path, follow_symlinks=False)
        current = os.lstat(path)
    except OSError:
        return
    if not stat_module.S_ISREG(current.st_mode):
        return
    if (current.st_dev, current.st_ino) != (reference.st_dev, reference.st_ino):
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


def _sidecar_paths(path):
    return [path + suffix for suffix in _SIDECAR_SUFFIXES]


def _remove_temp_family(temp_path):
    for name in [temp_path, *_sidecar_paths(temp_path)]:
        with contextlib.suppress(OSError):
            os.unlink(name)
