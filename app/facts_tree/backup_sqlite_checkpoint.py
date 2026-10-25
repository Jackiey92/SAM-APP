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
  returns. The SQLite online backup may restart copying when the source
  database changes, so releasing the barrier after the backup started
  does not fix a consistent W snapshot. This function never acquires or
  releases a host barrier and never opens another business database.
- The caller keeps full ownership of ``source_connection``: this module
  never closes, commits, rolls back, reconfigures or otherwise mutates
  its transaction state. The backup only reads the source database.

Destination-directory trust precondition (enforced by this module):

- The destination directory is opened once with
  ``O_DIRECTORY | O_NOFOLLOW`` and pinned by file descriptor; every
  later operation is descriptor-relative (``*at`` / ``linkat``), so a
  renamed or replaced path component cannot redirect it.
- Before any artifact is created, the pinned directory must be owned by
  the process's effective user and must have no group- or world-write
  permission bits. This module cannot defend against an attacker who can
  mutate the destination directory (rename the private staging entry,
  swap names), so such directories are refused up front instead of being
  made to look safe.
- The destination directory itself must not be a symlink (prefix
  components are resolved once, at pin time).
- Staging happens in a freshly created private ``0700`` directory with
  an unpredictable name; the SQLite snapshot is a ``0600`` file inside
  it and SQLite itself is handed a descriptor-pinned ``/proc/self/fd``
  path, so replacing the staging directory entry cannot redirect any
  read or write. Descriptor-pinned staging therefore requires a mounted
  Linux ``/proc`` (``/proc/self/fd``); this is refused clearly on
  platforms without it.

Scope guards: the SQLite Backup API guarantees single-database copy
consistency only. It is not a substitute for the cross-database
maintenance barrier, and a checkpoint produced while active writes are
not frozen must not be claimed to share a cut with other W databases or
with H.

Publication contract (see :class:`PublicationState`): publication is the
no-overwrite ``linkat`` into the pinned directory followed by an fsync of
that directory. Every raised :class:`SqliteCheckpointError` carries a
``publication`` state that tells the caller whether the destination is
confirmed absent, confirmed published, or uncertain. The only durable
success is either a returned :class:`SqliteCheckpointResult` or an error
whose state is ``CONFIRMED``.

Durability note: publication (file fsync, atomic no-overwrite hard link
and destination-directory fsync) only keeps its promises on a filesystem
that honours ``fsync``; network or fused filesystems with different
crash semantics need deployment validation, per the design.
"""
import contextlib
import hashlib
import os
import secrets
import sqlite3
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

DIGEST_ALGORITHM = "sha256"

_STAGING_PREFIX = ".sqlite-checkpoint-stage-"
_TEMP_PREFIX = ".sqlite-checkpoint-"
_TEMP_SUFFIX = ".tmp"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_HASH_CHUNK_SIZE = 1024 * 1024
_NAME_COLLISION_RETRIES = 8

# Descriptor-pinned SQLite staging: the snapshot is opened through this
# directory, so it requires Linux /proc.
_PROC_SELF_FD = "/proc/self/fd"

# Open files without following a swapped-in symlink; only ever open
# directories as directories for the pinned-directory and fsync steps.
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_PATH_OPEN_FLAGS = getattr(os, "O_PATH", 0) | getattr(os, "O_NOFOLLOW", 0)
_TEMP_CREATE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
)


class PublicationState:
    """Whether a checkpoint exists at the destination after a failure.

    - ``ABSENT``: this call durably left no destination entry. Either the
      failure happened before publication, or a link created just before
      a directory-sync failure was rolled back and that removal was
      synced. Retrying is safe.
    - ``CONFIRMED``: the checkpoint was hard-linked at the destination and
      the destination-directory fsync succeeded, so the destination bytes
      are durable on an fsync-honouring filesystem. An error carrying
      this state only reports that *staging cleanup* failed afterwards;
      the attached :attr:`SqliteCheckpointError.result` is a usable
      published checkpoint and the destination is never removed.
    - ``UNCERTAIN``: the hard link was created but the
      destination-directory sync failed and rollback could not be proven
      durable (the rollback unlink or its directory sync failed, or the
      destination entry was found to have been replaced). The destination
      may or may not exist after a crash; it must be inspected before any
      retry. This state is never hidden behind an "all failures leave no
      destination" promise.
    """

    ABSENT = "absent"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"


class SqliteCheckpointError(Exception):
    """Raised when a single-DB checkpoint cannot be produced or published.

    ``publication`` is always one of the :class:`PublicationState`
    values (defaulting to ``ABSENT`` for failures before or fully rolled
    back publication). When ``publication == CONFIRMED``, ``result``
    holds the published checkpoint.
    """

    def __init__(
        self,
        message,
        *,
        publication=PublicationState.ABSENT,
        result=None,
    ):
        super().__init__(message)
        self.publication = publication
        self.result = result


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


@dataclass(frozen=True)
class _Staging:
    """Pinned private staging area: one 0700 directory and one 0600 file."""

    dir_name: str
    dir_fd: int
    dir_stat: os.stat_result
    temp_name: str
    pinned_temp_path: str

    def close(self):
        if self.dir_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.dir_fd)
            object.__setattr__(self, "dir_fd", -1)


def create_sqlite_checkpoint(source_connection, destination_path):
    """Create one consistent single-file SQLite checkpoint.

    Copies the source database into a descriptor-pinned ``0600``
    snapshot in a private ``0700`` staging directory via
    ``source_connection.backup``, collapses it to a single
    rollback-journal file, runs ``PRAGMA integrity_check`` (must return
    exactly ``ok``) and ``PRAGMA foreign_key_check`` (must be empty) on
    the snapshot, then hashes, fsyncs and publishes it without
    overwriting via an atomic hard link into the pinned destination
    directory plus a destination-directory fsync.

    Preconditions (see module docstring): a maintenance barrier covering
    every writer of the source database is held by the caller until this
    function returns. The source connection must be open, usable and
    have no unfinished transaction. ``destination_path`` must be a
    non-empty ``str``/``os.PathLike[str]`` (bytes paths are rejected);
    its parent directory must exist, must not itself be a symlink, must
    be owned by the current effective user with no group/world-write
    bits, and the destination name must not exist, not even as a dangling
    symlink (symlinks are never followed).

    Returns a frozen :class:`SqliteCheckpointResult` when the checkpoint
    is published and all staging was cleaned. Raises
    :class:`SqliteCheckpointError` on any invalid input, backup or
    verification failure, or unsupported hard-link/fsync publication
    semantic. Inspect ``error.publication``:

    - ``ABSENT`` (default): no destination was published; retrying is
      safe. Temporary artifacts are removed on a best-effort basis.
    - ``CONFIRMED``: the checkpoint *was* published durably and
      ``error.result`` describes it; only post-publication staging
      cleanup failed. The destination is never rolled back.
    - ``UNCERTAIN``: publication cannot be proven or disproven; the
      destination must be inspected before retry.
    """
    source = _require_live_idle_source(source_connection)
    parent_fd, destination_name, destination_display = _open_pinned_parent(
        destination_path
    )
    staging = None
    try:
        staging = _create_staging(parent_fd)
        try:
            _backup_and_verify(source, staging)
            size_bytes, digest = _hash_and_sync_file(staging)
            return _publish(
                staging,
                parent_fd,
                destination_name,
                destination_display,
                size_bytes,
                digest,
            )
        except BaseException:
            # Removes only this attempt's private staging. A published
            # destination in the pinned parent is never touched here.
            _abort_staging(staging, parent_fd)
            raise
    finally:
        if staging is not None:
            staging.close()
        with contextlib.suppress(OSError):
            os.close(parent_fd)


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


def _open_pinned_parent(destination_path):
    """Return (pinned parent fd, entry name, display path)."""
    try:
        destination = os.fspath(destination_path)
    except TypeError as exc:
        raise SqliteCheckpointError(
            "destination path must be a str or os.PathLike, got "
            f"{type(destination_path).__name__}"
        ) from exc
    # Bytes end-to-end would silently break suffix concatenation and
    # descriptor-relative cleanup; reject them before touching anything.
    if isinstance(destination, bytes):
        raise SqliteCheckpointError(
            "bytes destination paths are not supported; pass a str or "
            "os.PathLike[str]"
        )
    if destination == "" or "\x00" in destination:
        raise SqliteCheckpointError(
            "destination path is empty or contains a NUL byte"
        )
    parent = os.path.dirname(destination) or "."
    name = os.path.basename(destination)
    if name in ("", ".", ".."):
        raise SqliteCheckpointError(
            "destination must name a file inside a directory, got "
            f"{destination!r}"
        )
    if not os.path.isdir(_PROC_SELF_FD):
        raise SqliteCheckpointError(
            "descriptor-pinned SQLite staging requires "
            f"{_PROC_SELF_FD} (Linux /proc); refusing path-based staging"
        )
    try:
        parent_fd = os.open(parent, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise SqliteCheckpointError(
            "destination directory cannot be opened pinned (it must exist, be "
            f"a directory and not itself be a symlink): {parent!r}: {exc}"
        ) from exc
    try:
        _require_trusted_directory(parent_fd, parent)
        if _entry_exists_at(parent_fd, name):
            # lexists-at refuses regular files, directories and symlinks
            # alike, including dangling symlinks, and never follows one.
            # The atomic linkat below remains the authoritative guard.
            raise SqliteCheckpointError(
                "destination already exists (file, directory or symlink); "
                f"refusing to overwrite or follow it: {destination!r}"
            )
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(parent_fd)
        raise
    return parent_fd, name, destination


def _require_trusted_directory(dir_fd, display):
    """Ownership/permission precondition, enforced before artifacts exist."""
    stat_result = os.fstat(dir_fd)
    if not stat_module.S_ISDIR(stat_result.st_mode):
        raise SqliteCheckpointError(
            f"destination path is not a directory: {display!r}"
        )
    if not hasattr(os, "geteuid") or stat_result.st_uid != os.geteuid():
        raise SqliteCheckpointError(
            "destination directory must be owned by the current effective "
            "user; refusing to stage into another user's directory: "
            f"{display!r}"
        )
    if stat_result.st_mode & 0o022:
        raise SqliteCheckpointError(
            "destination directory must not be group- or world-writable so "
            "its entries cannot be replaced concurrently; fix permissions to "
            f"drop group/other write bits: {display!r}"
        )


def _create_staging(parent_fd):
    last_error = None
    for _ in range(_NAME_COLLISION_RETRIES):
        dir_name = _STAGING_PREFIX + secrets.token_hex(8)
        try:
            # mkdir is create-exclusive: an attacker who predicted the
            # random name cannot pre-create it (EEXIST -> new name).
            os.mkdir(dir_name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            last_error = exc
            continue
        except OSError as exc:
            raise SqliteCheckpointError(
                f"cannot create private staging directory {dir_name!r}: {exc}"
            ) from exc
        try:
            dir_fd = os.open(dir_name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.rmdir(dir_name, dir_fd=parent_fd)
            raise SqliteCheckpointError(
                f"cannot reopen private staging directory pinned: {exc}"
            ) from exc
        try:
            dir_stat = os.fstat(dir_fd)
            if (
                not stat_module.S_ISDIR(dir_stat.st_mode)
                or dir_stat.st_uid != os.geteuid()
                or dir_stat.st_mode & 0o077
            ):
                raise SqliteCheckpointError(
                    "private staging directory did not come back as a 0700 "
                    "directory owned by this user: mode="
                    f"{oct(dir_stat.st_mode & 0o777)}"
                )
            temp_name, pinned_path = _create_temp_snapshot_file(dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(dir_fd)
            with contextlib.suppress(OSError):
                os.rmdir(dir_name, dir_fd=parent_fd)
            raise
        return _Staging(
            dir_name=dir_name,
            dir_fd=dir_fd,
            dir_stat=dir_stat,
            temp_name=temp_name,
            pinned_temp_path=pinned_path,
        )
    raise SqliteCheckpointError(
        "could not obtain a uniquely named private staging directory after "
        f"{_NAME_COLLISION_RETRIES} attempts: {last_error}"
    )


def _create_temp_snapshot_file(staging_fd):
    last_error = None
    for _ in range(_NAME_COLLISION_RETRIES):
        temp_name = _TEMP_PREFIX + secrets.token_hex(8) + _TEMP_SUFFIX
        # The absolute /proc path is itself descriptor-relative: the
        # kernel resolves it through the pinned staging fd, so a replaced
        # staging entry cannot redirect creation (dir_fd is intentionally
        # not used, since the pinned path already names the directory).
        pinned_path = f"{_PROC_SELF_FD}/{staging_fd}/{temp_name}"
        try:
            file_fd = os.open(pinned_path, _TEMP_CREATE_FLAGS, 0o600)
        except FileExistsError as exc:
            last_error = exc
            continue
        except OSError as exc:
            raise SqliteCheckpointError(
                f"cannot create private snapshot file {temp_name!r}: {exc}"
            ) from exc
        # SQLite opens the snapshot itself through the pinned path; the
        # empty descriptor is unused and already non-inheritable (PEP 446).
        os.close(file_fd)
        return temp_name, pinned_path
    raise SqliteCheckpointError(
        "could not obtain a uniquely named snapshot file after "
        f"{_NAME_COLLISION_RETRIES} attempts: {last_error}"
    )


def _backup_and_verify(source, staging):
    try:
        target = sqlite3.connect(
            staging.pinned_temp_path, isolation_level=None
        )
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
    sidecar_names = (
        staging.temp_name + suffix for suffix in _SIDECAR_SUFFIXES
    )
    leftovers = [
        name
        for name in sidecar_names
        if _entry_exists_at(staging.dir_fd, name)
    ]
    if leftovers:
        raise SqliteCheckpointError(
            "snapshot produced SQLite sidecar files; a single-file checkpoint "
            f"cannot be published: {leftovers!r}"
        )


def _hash_and_sync_file(staging):
    hasher = hashlib.sha256()
    size_bytes = 0
    try:
        file_fd = os.open(
            staging.temp_name, _FILE_OPEN_FLAGS, dir_fd=staging.dir_fd
        )
        try:
            while True:
                chunk = os.read(file_fd, _HASH_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                size_bytes += len(chunk)
            os.fsync(file_fd)
            file_stat = os.fstat(file_fd)
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise SqliteCheckpointError(
            f"cannot hash and synchronize the snapshot file: {exc}"
        ) from exc
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise SqliteCheckpointError("snapshot path is not a regular file")
    if file_stat.st_size != size_bytes:
        raise SqliteCheckpointError(
            "snapshot changed while hashing: read "
            f"{size_bytes} bytes but size is now {file_stat.st_size}"
        )
    # Defense in depth inside the private staging directory: the directory
    # entry the hashed inode came through must still name that same inode.
    entry_stat = _entry_stat_at(staging.dir_fd, staging.temp_name)
    if (entry_stat.st_dev, entry_stat.st_ino) != (
        file_stat.st_dev,
        file_stat.st_ino,
    ):
        raise SqliteCheckpointError(
            "snapshot directory entry was replaced while hashing; refusing "
            "to publish bytes that are not pinned to the private staging inode"
        )
    return size_bytes, f"{DIGEST_ALGORITHM}:{hasher.hexdigest()}"


def _publish(staging, parent_fd, destination_name, destination_display,
             size_bytes, digest):
    # linkat is atomic create-exclusive: it fails if the destination
    # appeared meanwhile, never overwrites and never follows symlinks.
    # Both ends are pinned directory fds.
    try:
        os.link(
            staging.temp_name,
            destination_name,
            src_dir_fd=staging.dir_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
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
    # The destination entry now exists. Durability is NOT proven until the
    # pinned destination-directory fsync below succeeds.
    try:
        os.fsync(parent_fd)
    except BaseException as exc:
        state = _rollback_published_link(staging, parent_fd, destination_name)
        if isinstance(exc, Exception):
            message = (
                "could not synchronize the destination directory after linking"
            )
            if state == PublicationState.ABSENT:
                message += (
                    "; rolled the link back and synced its removal, so no "
                    "checkpoint is published"
                )
            else:
                message += (
                    "; rollback could not be proven durable, so the "
                    f"destination {destination_display!r} may exist - inspect "
                    "it before retrying"
                )
            raise SqliteCheckpointError(message, publication=state) from exc
        # KeyboardInterrupt/SystemExit: rollback was attempted, but do not
        # disguise a control-flow exception as a checkpoint error.
        raise
    # Publication point: the destination is durable on an fsync-honouring
    # filesystem. It is never unlinked again, even if every cleanup step
    # below fails; such failures are reported as CONFIRMED, not hidden.
    result = SqliteCheckpointResult(
        path=Path(destination_display),
        size_bytes=size_bytes,
        digest=digest,
    )
    cleanup_failure = _finalize_staging(staging, parent_fd)
    if cleanup_failure is not None:
        raise SqliteCheckpointError(
            "checkpoint is published and synchronized at "
            f"{destination_display!r} (use the attached result), but private "
            f"staging cleanup did not complete cleanly: {cleanup_failure}",
            publication=PublicationState.CONFIRMED,
            result=result,
        )
    return result


def _rollback_published_link(staging, parent_fd, destination_name):
    """Best-effort remove just-published link. Returns ABSENT or UNCERTAIN."""
    try:
        destination_stat = _entry_stat_at(parent_fd, destination_name)
    except FileNotFoundError:
        destination_stat = None
    except OSError:
        return PublicationState.UNCERTAIN
    if destination_stat is not None:
        snapshot_stat = _entry_stat_at(staging.dir_fd, staging.temp_name)
        if not stat_module.S_ISREG(destination_stat.st_mode):
            return PublicationState.UNCERTAIN
        if (destination_stat.st_dev, destination_stat.st_ino) != (
            snapshot_stat.st_dev,
            snapshot_stat.st_ino,
        ):
            # The destination entry no longer names our inode; never remove
            # a replacement someone else put there.
            return PublicationState.UNCERTAIN
        try:
            os.unlink(destination_name, dir_fd=parent_fd)
        except OSError:
            return PublicationState.UNCERTAIN
    # Whether the entry was found gone or just unlinked, only a synced
    # directory makes the absence durable; without it the state after a
    # crash is unknown.
    try:
        os.fsync(parent_fd)
    except OSError:
        return PublicationState.UNCERTAIN
    return PublicationState.ABSENT


def _finalize_staging(staging, parent_fd):
    """Post-publication cleanup; None on success else a failure detail."""
    failures = []
    try:
        os.unlink(staging.temp_name, dir_fd=staging.dir_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        failures.append(f"removing the snapshot file failed: {exc}")
    try:
        os.fsync(staging.dir_fd)
    except OSError as exc:
        failures.append(f"synchronizing the staging directory failed: {exc}")
    staging.close()
    # Only remove the staging entry while it still names our pinned
    # directory; never rmdir a replacement placed under the same name.
    replaced = False
    try:
        entry_stat = _entry_stat_at(parent_fd, staging.dir_name)
        replaced = (entry_stat.st_dev, entry_stat.st_ino) != (
            staging.dir_stat.st_dev,
            staging.dir_stat.st_ino,
        )
        if replaced:
            failures.append(
                "the private staging directory entry was replaced before it "
                "could be removed; the original remains elsewhere on disk"
            )
        else:
            os.rmdir(staging.dir_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        failures.append(f"removing the staging directory failed: {exc}")
    if not replaced:
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            failures.append(
                f"synchronizing removal of the staging directory failed: {exc}"
            )
    return "; ".join(failures) or None


def _abort_staging(staging, parent_fd):
    """Best-effort removal of this attempt's private staging artifacts."""
    if staging.dir_fd >= 0:
        for name in [
            staging.temp_name,
            *[staging.temp_name + suffix for suffix in _SIDECAR_SUFFIXES],
        ]:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=staging.dir_fd)
        with contextlib.suppress(OSError):
            os.fsync(staging.dir_fd)
    staging.close()
    try:
        entry_stat = _entry_stat_at(parent_fd, staging.dir_name)
        if (entry_stat.st_dev, entry_stat.st_ino) == (
            staging.dir_stat.st_dev,
            staging.dir_stat.st_ino,
        ):
            with contextlib.suppress(OSError):
                os.rmdir(staging.dir_name, dir_fd=parent_fd)
            with contextlib.suppress(OSError):
                os.fsync(parent_fd)
    except OSError:
        pass


def _entry_exists_at(dir_fd, name):
    """True for any existing entry (incl. dangling symlink) in pinned dir."""
    try:
        fd = os.open(name, _PATH_OPEN_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    os.close(fd)
    return True


def _entry_stat_at(dir_fd, name):
    fd = os.open(name, _PATH_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)
