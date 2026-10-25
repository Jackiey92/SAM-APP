"""Immutable content-addressed byte objects for exact historical mappings.

Low-level F-001 R-closure primitive: regenerating the latest mapping can never
replace an exact historical version, so historical mapping bytes are kept as
immutable objects addressed by their SHA-256 digest.  Callers must inject an
explicit storage root; nothing here reads environment variables, default
``data/`` paths, or the application database.  Objects are created without
overwriting, re-validated on every read, and invalid digests, missing objects,
and content mismatches all fail closed.  Publication follows POSIX directory
sync semantics: the object bytes are fsynced before the create-only link, and
every successful put fsyncs both the digest-shard directory naming the object
and the storage root naming the shard before the digest is returned.  The
root sync is never skipped for a pre-existing shard, a lost concurrent
shard-creation race, or an object replay, because an earlier or foreign sync
may never have reached stable storage; a failed required sync fails closed
instead of reporting success, and a retry performs the full sync sequence
again.

Root durability: the constructor creates only the root leaf directory and
requires its parent to already exist — a missing parent fails explicitly
instead of recursively materializing ancestors whose own entries would never
be persisted.  The parent directory is fsynced on every construction,
whether or not the root leaf was newly created, so the root's own directory
entry is durable before any object is stored under it.  The root is frozen
to an absolute path at construction, so a later chdir can never relocate the
store; the conversion is lexical and keeps symlink components intact for the
no-symlink boundary below.

Parent pinning: a caller that verified the root's parent directory itself —
the mapping-store preparation creates and verifies that single level
relative to a pinned grandparent descriptor — can hand the verified
directory to the constructor as ``parent_fd``.  The store borrows the
descriptor for its lifetime and never closes it; the caller retains
ownership and closes it once the store is done.  While it is borrowed, the
root leaf's creation, every root open, and the parent fsync all run
exclusively relative to that descriptor, so the parent path is never
re-resolved and a parent renamed or replaced by a symlink after
verification cannot redirect any store operation.  Pinned construction
never falls back to path-based preparation: a non-descriptor, an unusable
descriptor, or one that does not reference a directory is refused at
construction.  Without ``parent_fd`` the path-based contract above is
unchanged.

Path safety: the store never follows symbolic links at the configured root,
the digest-shard directory, or the object path.  Every lookup and write is
issued relative to directory descriptors opened with ``O_NOFOLLOW`` (plus
``O_DIRECTORY`` for directories), so a symlink planted at any of those three
components — before or during an operation — fails closed instead of
redirecting access outside the explicit root; there is no check-then-open
window at or below the root.  Only a genuine ENOENT means "object missing":
a symlink, directory, or other non-regular occupant raises
:class:`UnsafeObjectPathError` and is never reported as missing.  Path
components above the configured root remain the caller's responsibility, and
the no-symlink boundary needs POSIX ``dir_fd``/``O_NOFOLLOW`` support plus
``O_DIRECTORY`` for directory-only opens; a platform missing any of them is
refused explicitly at construction instead of silently degrading a missing
flag to ``0``.

Inventory: :meth:`VersionedMappingObjectStore.inventory` is a read-only,
digest-sorted listing that re-reads and hash-verifies every object it
enumerates.  Each returned record proves only that, at the moment that
object was read, it was a regular file whose bytes matched its digest; an
enumerated object that vanishes before its open or read raises instead of
being dropped.  The listing guarantees nothing about any object at or
after the moment the method returns — a returned object may since have
been changed or removed by anything with filesystem access — and it is not
an atomic snapshot: each directory contributes only the entries its own
listdir observed, and only those enumerated entries are validated one by
one.  A concurrent write that lands after the relevant listdir — a new
object in an already-enumerated shard, or a brand-new shard — is silently
absent from the result instead of raising, so the listing may be
incomplete and forms no consistent R-backup closure; without an external
maintenance barrier it must not be consumed as one.  Enumerated entries
still fail closed: wrong shard or object names, unknown directory entries
(including an in-flight temporary visible at enumeration time),
non-regular occupants, and corrupted digests are explicit failures;
nothing is skipped, deleted, or repaired.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path
from typing import NamedTuple

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SHARD_RE = re.compile(r"[0-9a-f]{2}")

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# dir_fd-relative open/mkdir/link/unlink plus O_NOFOLLOW are what make the
# no-symlink boundary atomic; without them the guarantee cannot be enforced.
# Inventory enumeration additionally needs listdir to accept a held directory
# descriptor (os.listdir is fd-capable, not dir_fd-capable), which the
# constructor checks separately.
_DIR_FD_PRIMITIVES = (os.open, os.mkdir, os.link, os.unlink)


class InvalidDigestError(ValueError):
    """The supplied address is not a lowercase SHA-256 hex digest."""


class ObjectMissingError(KeyError):
    """No object is stored under the requested digest."""


class ObjectMismatchError(ValueError):
    """Stored bytes do not match their content address; never auto-repaired."""


class UnsafeObjectPathError(RuntimeError):
    """A root, shard, or object component is a symlink or the wrong file type.

    The store refuses to follow or use such occupants; they are explicit
    failures, never a missing object.
    """


class UnexpectedEntryError(RuntimeError):
    """Inventory found a directory entry that cannot be a valid object.

    Wrong shard or object names and unknown entries under the storage root
    are explicit failures; inventory never skips, deletes, or repairs them.
    """


class MappingObjectEntry(NamedTuple):
    """One inventory-verified object: lowercase SHA-256 digest and byte size."""

    digest: str
    size_bytes: int


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise InvalidDigestError("digest must be a lowercase sha256 hex digest")
    return digest


def _unlink_quietly(shard_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=shard_fd)
    except FileNotFoundError:
        pass


class VersionedMappingObjectStore:
    """Create-only SHA-256-addressed byte objects under an explicit root."""

    def __init__(self, root: Path | str, *, parent_fd: int | None = None) -> None:
        """Store rooted at ``root``, optionally pinned to ``parent_fd``.

        ``parent_fd`` is a descriptor the caller already verified as
        ``root.parent`` (e.g. from the mapping-store preparation).  The
        store only borrows it — the caller retains ownership and must
        close it; the store itself never does.  While borrowed, root
        creation, every root open, and the parent fsync stay relative to
        that descriptor and the parent path is never re-resolved; an
        unusable or non-directory descriptor is refused here instead of
        falling back to unverified path resolution.  Without ``parent_fd``
        the historical path-based construction contract is unchanged.
        """
        if root is None or not str(root):
            raise ValueError("an explicit storage root is required")
        if _O_NOFOLLOW == 0 or _O_DIRECTORY == 0 or not all(
            fn in os.supports_dir_fd for fn in _DIR_FD_PRIMITIVES
        ) or os.listdir not in os.supports_fd:
            raise RuntimeError(
                "symlink-safe object storage requires POSIX dir_fd, "
                "fd-based listdir, O_NOFOLLOW and O_DIRECTORY support; "
                "this platform cannot enforce the no-symlink boundary, "
                "directory-only opens, or descriptor-pinned inventory "
                "enumeration, so the store refuses to run here instead of "
                "silently degrading a missing flag to 0"
            )
        # Freeze the absolute path at construction so a later chdir can
        # never relocate the store; abspath is lexical (not resolve()), so
        # symlink components stay intact for the O_NOFOLLOW checks below.
        self.root = Path(os.path.abspath(root))
        self._parent_fd = self._borrow_parent_fd(parent_fd)
        self._ensure_root_durable()
        root_fd = self._open_root()
        os.close(root_fd)

    @staticmethod
    def _borrow_parent_fd(parent_fd: int | None) -> int | None:
        """Validate the caller-owned parent descriptor the store borrows.

        Ownership stays with the caller: the store uses the descriptor for
        root-relative creation, opens, and the parent fsync, and never
        closes it.  Fail-closed: a non-descriptor raises ``TypeError``, an
        unusable (e.g. already closed) descriptor raises ``ValueError``,
        and a descriptor that does not reference a directory raises
        :class:`UnsafeObjectPathError`; none of them ever degrades into
        re-resolving the parent by path.
        """
        if parent_fd is None:
            return None
        if isinstance(parent_fd, bool) or not isinstance(parent_fd, int):
            raise TypeError(
                "parent_fd must be an open directory descriptor or None"
            )
        try:
            st = os.fstat(parent_fd)
        except OSError as exc:
            raise ValueError(
                "parent_fd is not a usable open descriptor; pinned "
                "construction never falls back to re-resolving the "
                "parent path"
            ) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeObjectPathError(
                "parent_fd does not reference a directory; refusing to "
                "use it as the pinned parent"
            )
        return parent_fd

    def _ensure_root_durable(self) -> None:
        """Create the root leaf if needed and fsync its parent directory.

        Only the root leaf is created here; its parent must already exist,
        and a missing parent fails explicitly instead of recursively
        materializing ancestors whose own directory entries would never be
        synced.  The parent is fsynced whether or not this call created the
        leaf, because a previous creation's sync cannot be assumed to have
        reached stable storage.  A non-directory occupant at the root — a
        regular file, FIFO, or symlink — fails closed as
        :class:`UnsafeObjectPathError` through the no-follow root open and
        is never leaked as a raw ``FileExistsError``; a genuine directory
        that appeared concurrently still receives the parent fsync.

        With a pinned parent descriptor the creation, the occupant type
        re-check, and the parent fsync all run relative to that descriptor
        and the parent path is never re-resolved: the descriptor itself is
        the verified parent, so it is fsynced directly.  Without one, path
        components above the root remain the caller's responsibility.
        """
        if self._parent_fd is not None:
            try:
                os.mkdir(self.root.name, dir_fd=self._parent_fd)
            except FileExistsError:
                # Same contract as path mode: the occupant's type is
                # decided atomically by the pinned O_NOFOLLOW/O_DIRECTORY
                # open below, never by a symlink-following probe; a
                # genuine directory falls through to the parent fsync.
                os.close(self._open_root())
            os.fsync(self._parent_fd)
            return
        parent = self.root.parent
        try:
            self.root.mkdir(exist_ok=True)
        except FileNotFoundError:
            if not parent.is_dir():
                raise FileNotFoundError(
                    f"storage root parent {parent} does not exist; the "
                    "store creates only the root leaf directory and "
                    "refuses to recursively create ancestors it would "
                    "not persist"
                ) from None
            raise
        except FileExistsError:
            # pathlib re-raises EEXIST when the occupant fails its own
            # is_dir() probe; that probe follows symlinks and races with
            # concurrent replacement, so it is never trusted for the type
            # decision.  Re-check atomically through the _open_root()
            # O_NOFOLLOW/O_DIRECTORY open instead: a symlink or wrong-type
            # occupant raises UnsafeObjectPathError (fail closed), while a
            # genuine directory created concurrently falls through to the
            # parent fsync below like any pre-existing root.
            os.close(self._open_root())
        parent_fd = os.open(parent,
                            os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    @staticmethod
    def digest_of(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def put(self, data: bytes) -> str:
        """Store ``data`` under its digest; replaying the same bytes is a no-op.

        A pre-existing address is kept only when its content matches; a
        mismatched occupant raises and is never repaired or replaced.  The
        object bytes are fsynced before the create-only link, and both the
        digest-shard directory naming the object and the storage root
        naming the shard are fsynced before the digest is returned — on
        fresh writes, replays, pre-existing shards, and lost concurrent
        shard-creation races alike, since an earlier or foreign sync may
        never have reached stable storage.  Any required sync failure
        raises instead of returning success, and a retry performs the full
        sync sequence again, so a caller can never build a Git reference on
        a name whose directory entries were not synced.  Replayed digests
        re-verify the stored bytes.  The root is created and persisted at
        construction; a root that vanished afterwards fails explicitly
        instead of being recreated without a parent sync.  A symbolic link
        or other unexpected occupant at the root, shard, or object path
        raises instead of being followed or treated as missing.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        data = bytes(data)
        digest = self.digest_of(data)
        root_fd = self._open_root()
        try:
            try:
                os.mkdir(digest[:2], dir_fd=root_fd)
            except FileExistsError:
                # A shard created by a concurrent writer or an earlier run
                # may never have had its root entry synced to stable
                # storage, so the root sync below is never skipped.
                pass
            shard_fd = self._open_shard_fd(root_fd, digest[:2])
            try:
                tmp_name = self._write_temp(shard_fd, data)
                try:
                    try:
                        os.link(tmp_name, digest,
                                src_dir_fd=shard_fd, dst_dir_fd=shard_fd)
                    except FileExistsError:
                        # An occupied address is kept only when its content
                        # matches; a mismatch, a symlink, or a non-regular
                        # occupant raises and is never repaired or replaced.
                        if self._read_verified(shard_fd, digest) is None:
                            raise ObjectMissingError(digest)
                    # The linked name must be durable in its shard, and the
                    # shard durable in the root, before this digest may be
                    # referenced by a Git commit.  Both syncs run on every
                    # success path — fresh write, replay, pre-existing or
                    # concurrently created shard — and a sync failure fails
                    # closed; a retry syncs both again.
                    self._fsync_dir_fd(shard_fd)
                    self._fsync_dir_fd(root_fd)
                finally:
                    _unlink_quietly(shard_fd, tmp_name)
            finally:
                os.close(shard_fd)
        finally:
            os.close(root_fd)
        return digest

    def get(self, digest: str) -> bytes:
        """Return the exact bytes for ``digest`` after re-validating content.

        Only a genuine ENOENT is reported as missing; a symlink, directory,
        or other non-regular occupant at the root, shard, or object path
        raises :class:`UnsafeObjectPathError`.
        """
        data = self._read_object(digest)
        if data is None:
            raise ObjectMissingError(digest)
        return data

    def has(self, digest: str) -> bool:
        """True only when the object exists and still matches its digest.

        Missing means a genuine ENOENT; unsafe occupants raise instead of
        returning ``False``.
        """
        return self._read_object(digest) is not None

    def inventory(self) -> list[MappingObjectEntry]:
        """List enumerated stored objects as ``(digest, size_bytes)``, sorted.

        Read-only and fail-closed for everything actually enumerated: each
        listed object's bytes are re-read and hash-verified while
        enumerating, so a corrupted object raises
        :class:`ObjectMismatchError`; a wrong shard or object name, an
        unknown root/shard entry (including an in-flight temporary visible
        at enumeration time), or an object name whose prefix does not match
        its shard raises :class:`UnexpectedEntryError`; and a symlinked,
        directory, or other non-regular occupant raises
        :class:`UnsafeObjectPathError`.  Nothing enumerated is ever
        skipped, deleted, or repaired, and an enumerated entry that
        vanishes mid-run raises :class:`ObjectMissingError` instead of
        being silently dropped.  Enumeration runs through descriptors
        opened with ``O_NOFOLLOW``/``O_DIRECTORY``, so no symlinked root or
        shard component is ever followed.

        Each returned record proves only that, at the moment this method
        read that object, it was a regular file whose bytes matched its
        digest; an enumerated object that vanishes before its open or read
        raises :class:`ObjectMissingError`.  Nothing is guaranteed about
        any object at or after the moment the method returns — a returned
        object may since have been changed or removed by anything with
        filesystem access — so the result is not a point-in-time inventory
        of the store.

        This listing implements no concurrency barrier and is not an atomic
        snapshot: each directory contributes only the entries its own
        listdir observed, and only those enumerated entries are validated
        one by one.  A concurrent write that lands after the relevant
        listdir — a new object in an already-enumerated shard, or a
        brand-new shard under the root — is therefore silently absent from
        the result instead of raising.  The listing may thus be incomplete
        and forms no consistent R-backup closure; without an external
        maintenance barrier it must not be consumed as one.
        """
        entries: list[MappingObjectEntry] = []
        root_fd = self._open_root()
        try:
            for shard_name in sorted(os.listdir(root_fd)):
                if not _SHARD_RE.fullmatch(shard_name):
                    raise UnexpectedEntryError(
                        f"unexpected root entry {shard_name!r}; only "
                        "lowercase two-hex-digit digest shard directories "
                        "are allowed"
                    )
                shard_fd = self._open_shard_fd(root_fd, shard_name)
                try:
                    for name in sorted(os.listdir(shard_fd)):
                        if not _DIGEST_RE.fullmatch(name) \
                                or name[:2] != shard_name:
                            raise UnexpectedEntryError(
                                f"unexpected entry {name!r} in shard "
                                f"{shard_name!r}; only lowercase sha256 "
                                "object names matching the shard prefix "
                                "are allowed"
                            )
                        data = self._read_verified(shard_fd, name)
                        if data is None:
                            raise ObjectMissingError(name)
                        entries.append(MappingObjectEntry(name, len(data)))
                finally:
                    os.close(shard_fd)
        finally:
            os.close(root_fd)
        # Shard-major enumeration with prefix-matched names is already
        # digest-sorted; the explicit sort pins the contract regardless.
        entries.sort(key=lambda entry: entry.digest)
        return entries

    def _open_root(self) -> int:
        """Open the configured root as a real directory, never via symlink.

        With a pinned parent descriptor the root is opened by its leaf
        name relative to that descriptor, so resolution never touches the
        parent path — a parent renamed or replaced by a symlink after
        verification cannot redirect the open.  Without one, the frozen
        absolute path is used and O_NOFOLLOW guards its final component.
        """
        try:
            if self._parent_fd is not None:
                return os.open(self.root.name,
                               os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
                               | _O_CLOEXEC,
                               dir_fd=self._parent_fd)
            return os.open(self.root,
                           os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
                           | _O_CLOEXEC)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafeObjectPathError(
                    f"storage root {self.root} is a symbolic link or not a "
                    "directory; refusing to use it"
                ) from None
            raise

    @staticmethod
    def _open_shard_fd(root_fd: int, shard: str) -> int:
        """Open a two-hex-digit shard below ``root_fd`` as a real directory."""
        try:
            return os.open(shard,
                           os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
                           | _O_CLOEXEC,
                           dir_fd=root_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeObjectPathError(
                    f"digest shard {shard} is a symbolic link; "
                    "refusing to follow it"
                ) from None
            if exc.errno == errno.ENOTDIR:
                raise UnsafeObjectPathError(
                    f"digest shard {shard} is not a directory"
                ) from None
            raise

    @staticmethod
    def _read_verified(shard_fd: int, digest: str) -> bytes | None:
        """Read and hash-check the object inside an open shard descriptor.

        The final component is opened with ``O_NOFOLLOW`` so a symlink
        occupant fails atomically instead of being followed; ``O_NONBLOCK``
        keeps a FIFO occupant from blocking the open, and the file-type
        check runs on the opened descriptor itself, before any byte is read.
        ``None`` is returned only on a genuine ENOENT.
        """
        try:
            obj_fd = os.open(digest,
                             os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
                             | _O_CLOEXEC,
                             dir_fd=shard_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeObjectPathError(
                    f"object {digest} is a symbolic link; refusing to follow it"
                ) from None
            if exc.errno in (errno.EISDIR, errno.ENXIO):
                raise UnsafeObjectPathError(
                    f"object {digest} is not a regular file"
                ) from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(obj_fd).st_mode):
                raise UnsafeObjectPathError(
                    f"object {digest} is not a regular file"
                )
            # O_NONBLOCK only guarded the open; reads from a verified
            # regular file are unaffected by it.
            with os.fdopen(obj_fd, "rb") as stream:
                obj_fd = -1  # the stream owns the descriptor now
                data = stream.read()
        finally:
            if obj_fd >= 0:
                os.close(obj_fd)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ObjectMismatchError(f"sha256 mismatch for object {digest}")
        return data

    def _read_object(self, digest: str) -> bytes | None:
        """Verified bytes for ``digest``; ``None`` only on genuine ENOENT."""
        _validate_digest(digest)
        try:
            root_fd = self._open_root()
        except FileNotFoundError:
            return None
        try:
            try:
                shard_fd = self._open_shard_fd(root_fd, digest[:2])
            except FileNotFoundError:
                return None
            try:
                return self._read_verified(shard_fd, digest)
            finally:
                os.close(shard_fd)
        finally:
            os.close(root_fd)

    @staticmethod
    def _write_temp(shard_fd: int, data: bytes) -> str:
        """Write ``data`` to a fresh fsynced temporary name in the shard.

        A unique create-only temporary plus the later create-only link keep
        concurrent writers from ever replacing an immutable object at the
        same address; on any failure the temporary name is removed.
        """
        # os.open's audit event carries no dir_fd, so a bare relative name
        # cannot be attributed to the shard by audit hooks; on Linux the
        # /proc/self/fd magic link names the same held directory with an
        # absolute path instead, keeping resolution pinned to shard_fd.
        proc_fd_dir = f"/proc/self/fd/{shard_fd}"
        via_proc = os.path.isdir(proc_fd_dir)
        for _attempt in range(100):
            tmp_name = f".object-{os.getpid()}-{secrets.token_hex(8)}.tmp"
            try:
                if via_proc:
                    tmp_fd = os.open(os.path.join(proc_fd_dir, tmp_name),
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
                else:
                    tmp_fd = os.open(tmp_name,
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | _O_NOFOLLOW | _O_CLOEXEC,
                                     0o600, dir_fd=shard_fd)
            except FileExistsError:
                continue
            try:
                with os.fdopen(tmp_fd, "wb") as stream:
                    tmp_fd = -1  # the stream owns the descriptor now
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                if tmp_fd >= 0:
                    os.close(tmp_fd)
                _unlink_quietly(shard_fd, tmp_name)
                raise
            return tmp_name
        raise FileExistsError("no unique temporary object name available")

    @staticmethod
    def _fsync_dir_fd(fd: int) -> None:
        """fsync an already-open directory so names created through it —
        the object link in the shard, or the shard entry in the root — are
        durable before success is returned."""
        os.fsync(fd)
