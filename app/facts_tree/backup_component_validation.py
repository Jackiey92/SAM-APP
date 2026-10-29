"""Per-component size and digest verification for F-001 backups.

Implements section 10 of the approved F-001 backup design
(``98-完整备份与隔离恢复设计.md``): given a descriptor of the backup
root that the caller holds open, verify that ONE declared component is a
regular file whose exact byte count and SHA-256 match the manifest
declarations, and return that evidence as a frozen value object.

Pinning model (Linux/WSL only; missing capabilities fail closed):

- The caller's root descriptor is duplicated and every path walk step is
  descriptor-relative (``dir_fd``) with ``O_DIRECTORY | O_NOFOLLOW``, so
  renaming or replacing the root path — or swapping an intermediate
  entry for a symlink — cannot redirect the walk onto another tree.
- The terminal entry is pinned with ``O_PATH | O_NOFOLLOW`` and must
  fstat as a regular file; symlinks, directories, FIFOs, sockets and
  devices are refused without any data-mode open (a FIFO is never
  blocked on). The pinned object is then re-opened read-only through the
  host ``/proc/self/fd/<fd>`` magic link and re-checked for the same
  ``st_dev``/``st_ino`` before a single byte is read. That proc link is
  an internal pinning channel only — it is never used to follow symlinks
  inside the backup content.
- Bytes are streamed in bounded chunks; at most ``size_bytes + 1`` bytes
  are read and EOF is confirmed. Short, oversized, digest-mismatching or
  unreadable components all fail closed with no partial evidence. Legal
  empty files verify.

Scope guards: this module only reads. It never creates, modifies or
deletes files, never follows backup-content symlinks, never touches the
network, a database or a manifest, and never closes or repositions the
caller's descriptor. The returned evidence says nothing about the path
after this call (concurrent writers, hard links and mounts are out of
scope); coordinating a stable backup cut is the caller's job.
"""
import hashlib
import os
import re
import stat as stat_module
from dataclasses import dataclass

DIGEST_ALGORITHM = "sha256"

_READ_CHUNK_SIZE = 1024 * 1024
_PROC_SELF_FD = "/proc/self/fd"
_DIGEST_PREFIX = "sha256:"
_DIGEST_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z")

# Capabilities the pinning model refuses to run without; they are probed
# at call time and missing ones fail closed instead of degrading to
# string-path resolution or symlink following. O_CLOEXEC is only extra
# hardening (PEP 446 already makes new descriptors non-inheritable), so
# it is not part of the required set.
_REQUIRED_OPEN_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW", "O_PATH")
# Snapshot at import time so runtime fault injection that wraps os.open
# cannot confuse the capability probe.
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


class BackupComponentValidationError(ValueError):
    """Raised when verification input is invalid or a component fails."""


@dataclass(frozen=True)
class VerifiedBackupComponent:
    """Evidence read from the opened component during one call.

    ``size_bytes`` and ``sha256_digest`` describe exactly the bytes read
    from the pinned file. The object carries no file content, no open
    descriptor and no promise that the relative path still names the
    same object after the call.
    """

    relative_path: str
    size_bytes: int
    sha256_digest: str


def verify_backup_component(root_fd, *, relative_path, size_bytes,
                            sha256_digest):
    """Verify one backup component against its declared size and digest.

    ``root_fd`` must be the descriptor of an open backup root directory,
    held open by the caller for the whole call; it is duplicated here,
    never closed or repositioned. ``relative_path`` must be a strict
    UTF-8-encodable slash-separated relative path: non-empty, no NUL,
    no backslashes, no empty/``.``/``..`` segments, never absolute.
    ``size_bytes`` must be a non-negative built-in int and
    ``sha256_digest`` exactly ``sha256:`` plus 64 lowercase hex
    characters. Invalid input is rejected before any component is
    opened.

    Returns a frozen :class:`VerifiedBackupComponent` carrying the size
    and digest evidence read from the pinned file. Raises
    :class:`BackupComponentValidationError` on any invalid input,
    missing Linux pinning capability, non-regular terminal entry, open
    or read failure, or size/digest mismatch; every failure closes only
    this function's own descriptors and leaves the caller's root
    descriptor usable.
    """
    _require_valid_root_fd(root_fd)
    segments = _require_valid_relative_path(relative_path)
    _require_valid_size(size_bytes)
    expected_hex = _require_valid_digest(sha256_digest)
    _require_pinning_capabilities()

    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    pin_flags = os.O_PATH | os.O_NOFOLLOW | cloexec
    data_flags = os.O_RDONLY | cloexec

    try:
        # A built-in int that exceeds the C int range raises OverflowError
        # here, not OSError; both mean the descriptor cannot be held.
        held_root_fd = os.dup(root_fd)
    except (OSError, OverflowError) as exc:
        raise BackupComponentValidationError(
            "the backup root descriptor cannot be held (closed, not a "
            f"descriptor, or out of the OS descriptor range): {exc}"
        ) from exc
    try:
        _require_directory_fd(held_root_fd)
        return _verify_pinned_component(
            held_root_fd,
            segments,
            directory_flags=directory_flags,
            pin_flags=pin_flags,
            data_flags=data_flags,
            size_bytes=size_bytes,
            expected_hex=expected_hex,
            relative_path=relative_path,
        )
    finally:
        _close_owned_fds([held_root_fd], "the held backup root descriptor")


def _require_valid_root_fd(root_fd):
    # The value itself is never repr'ed: a huge int can exceed the int
    # to-str digit cap and an arbitrary object can have a raising
    # __repr__, either of which would mask this error with another one.
    if type(root_fd) is not int:
        raise BackupComponentValidationError(
            "root_fd must be a non-negative built-in int descriptor, got "
            f"{type(root_fd).__name__}"
        )
    if root_fd < 0:
        raise BackupComponentValidationError(
            "root_fd must be a non-negative built-in int descriptor, got "
            "a negative int"
        )


def _require_valid_relative_path(relative_path):
    if type(relative_path) is not str:
        raise BackupComponentValidationError(
            "relative_path must be a built-in str, got "
            f"{type(relative_path).__name__}"
        )
    if not relative_path:
        raise BackupComponentValidationError("relative_path must not be empty")
    try:
        relative_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BackupComponentValidationError(
            f"relative_path must be UTF-8 encodable: {exc}"
        ) from exc
    if "\x00" in relative_path:
        raise BackupComponentValidationError(
            "relative_path must not contain a NUL byte"
        )
    if "\\" in relative_path:
        raise BackupComponentValidationError(
            "relative_path must use '/' separators, not backslashes"
        )
    segments = relative_path.split("/")
    for segment in segments:
        if segment == "":
            raise BackupComponentValidationError(
                "relative_path must be relative and must not contain empty "
                f"segments: {relative_path!r}"
            )
        if segment in (".", ".."):
            raise BackupComponentValidationError(
                "relative_path must not contain '.' or '..' segments; no "
                f"implicit normalization: {relative_path!r}"
            )
    return segments


def _require_valid_size(size_bytes):
    # No repr of the value either, for the same reason as in
    # _require_valid_root_fd.
    if type(size_bytes) is not int:
        raise BackupComponentValidationError(
            "size_bytes must be a non-negative built-in int, got "
            f"{type(size_bytes).__name__}"
        )
    if size_bytes < 0:
        raise BackupComponentValidationError(
            "size_bytes must be a non-negative built-in int, got a "
            "negative int"
        )


def _require_valid_digest(sha256_digest):
    if type(sha256_digest) is not str or not _DIGEST_PATTERN.match(
        sha256_digest
    ):
        raise BackupComponentValidationError(
            f"sha256_digest must be a built-in str of the form "
            f"'{DIGEST_ALGORITHM}:' plus 64 lowercase hex characters"
        )
    return sha256_digest[len(_DIGEST_PREFIX):]


def _require_pinning_capabilities():
    missing = [
        name for name in _REQUIRED_OPEN_FLAGS if not hasattr(os, name)
    ]
    if not _OPEN_SUPPORTS_DIR_FD:
        missing.append("dir_fd-open")
    if missing:
        raise BackupComponentValidationError(
            "pinned component verification requires Linux capabilities "
            f"that are missing here: {', '.join(missing)}; refusing "
            "path-based or symlink-following fallbacks"
        )
    if not os.path.isdir(_PROC_SELF_FD):
        raise BackupComponentValidationError(
            "pinned component verification requires the host "
            f"{_PROC_SELF_FD} channel (Linux /proc); refusing "
            "path-based or symlink-following fallbacks"
        )


def _require_directory_fd(fd):
    if not stat_module.S_ISDIR(_fstat_held(fd).st_mode):
        raise BackupComponentValidationError(
            "root_fd must be the descriptor of an open directory"
        )


def _verify_pinned_component(held_root_fd, segments, *, directory_flags,
                             pin_flags, data_flags, size_bytes, expected_hex,
                             relative_path):
    # Every descriptor opened here is registered the moment it exists,
    # and nothing is closed mid-walk: cleanup closes each owned fd once,
    # independently, at the end, so a failing close can neither orphan a
    # descriptor opened later nor trigger a retry on a reused fd number.
    owned_fds = []
    try:
        # Every intermediate step opens relative to the parent descriptor
        # already held, so an entry swapped for a symlink mid-walk cannot
        # redirect the walk; O_NOFOLLOW refuses swapped-in symlinks.
        directory_fd = held_root_fd
        for segment in segments[:-1]:
            next_fd = _open_component_entry(
                segment, directory_flags, directory_fd, "directory"
            )
            owned_fds.append(next_fd)
            directory_fd = next_fd
        pin_fd = _open_component_entry(
            segments[-1], pin_flags, directory_fd, "terminal entry"
        )
        owned_fds.append(pin_fd)
        pin_stat = _fstat_held(pin_fd)
        if not stat_module.S_ISREG(pin_stat.st_mode):
            raise BackupComponentValidationError(
                "backup component must be a regular file; symlinks, "
                "directories, FIFOs, sockets and devices are refused "
                f"without a data-mode open: {relative_path!r}"
            )
        # Re-open the pinned inode read-only through the host proc magic
        # link. No O_NOFOLLOW here: the magic link itself must resolve,
        # and the identity re-check below proves the new descriptor still
        # names the pinned regular file (same st_dev/st_ino).
        data_fd = _open_component_entry(
            f"{_PROC_SELF_FD}/{pin_fd}", data_flags, None,
            "pinned descriptor re-open",
        )
        owned_fds.append(data_fd)
        data_stat = _fstat_held(data_fd)
        if (
            not stat_module.S_ISREG(data_stat.st_mode)
            or (data_stat.st_dev, data_stat.st_ino)
            != (pin_stat.st_dev, pin_stat.st_ino)
        ):
            raise BackupComponentValidationError(
                "the re-opened descriptor no longer names the pinned "
                f"regular file: {relative_path!r}"
            )
        measured_size, measured_hex = _stream_digest(
            data_fd, size_bytes, relative_path
        )
    finally:
        _close_owned_fds(owned_fds, "the backup component descriptors")
    if measured_size != size_bytes:
        # The numbers are deliberately not formatted into the message: a
        # huge declared size would hit the int-to-str digit cap and leak
        # a built-in ValueError instead of this defined error.
        raise BackupComponentValidationError(
            "backup component size mismatch: the declared byte count "
            f"does not match the bytes read: {relative_path!r}"
        )
    if measured_hex != expected_hex:
        raise BackupComponentValidationError(
            "backup component sha256 mismatch with the declared digest: "
            f"{relative_path!r}"
        )
    return VerifiedBackupComponent(
        relative_path=relative_path,
        size_bytes=measured_size,
        sha256_digest=f"{DIGEST_ALGORITHM}:{measured_hex}",
    )


def _close_owned_fds(fds, label):
    """Close each owned descriptor exactly once, then report as one error.

    Every fd is attempted even if an earlier close fails, and a failed
    close is never retried: the number could already have been reused by
    an unrelated open file, and closing it again would destroy that file
    description. Any failures are reported together as a single
    :class:`BackupComponentValidationError` after all cleanup ran.
    """
    failures = []
    for fd in fds:
        try:
            os.close(fd)
        except OSError as exc:
            failures.append(f"fd {fd}: {exc}")
    if failures:
        raise BackupComponentValidationError(
            f"{label} could not be fully released: "
            + "; ".join(failures)
        )


def _open_component_entry(path, flags, dir_fd, label):
    try:
        if dir_fd is None:
            return os.open(path, flags)
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise BackupComponentValidationError(
            f"backup component {label} cannot be opened pinned: "
            f"{path!r}: {exc}"
        ) from exc


def _fstat_held(fd):
    try:
        return os.fstat(fd)
    except OSError as exc:
        raise BackupComponentValidationError(
            f"a held descriptor cannot be stat'ed: {exc}"
        ) from exc


def _stream_digest(data_fd, size_bytes, relative_path):
    """Stream at most ``size_bytes + 1`` bytes; memory stays bounded."""
    hasher = hashlib.sha256()
    total = 0
    limit = size_bytes + 1
    while total < limit:
        try:
            chunk = os.read(data_fd, min(_READ_CHUNK_SIZE, limit - total))
        except OSError as exc:
            raise BackupComponentValidationError(
                f"backup component cannot be read: {relative_path!r}: {exc}"
            ) from exc
        if not chunk:
            break  # EOF confirmed before the declared size: short file.
        hasher.update(chunk)
        total += len(chunk)
    return total, hasher.hexdigest()
