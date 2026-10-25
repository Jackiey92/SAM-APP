"""Immutable mapping-object primitive contracts; synthetic tmp_path bytes only.

The durability tests below assert fsync call ordering and fail-closed
behavior through explicit fault injection; they do not claim real
power-loss or filesystem durability.  The root-contract tests record
constructor fsyncs and assert the parent-directory sync, the explicit
missing-parent failure, the chdir-frozen absolute root, and the refusal
to degrade a missing O_DIRECTORY.  The path-safety tests plant symlinks
and non-regular occupants inside tmp_path sandboxes and assert the store
never follows them outside its explicit root; platforms that cannot
create symlinks skip those cases loudly instead of passing them
silently.  The inventory tests assert the read-only sorted digest/size
listing with per-object read-back verification, and its rejection of
unexpected names, foreign entries, and corrupted digests without ever
skipping, deleting, or repairing them.  The pinned-parent tests assert
the borrowed-descriptor contract: root creation, object writes, reads,
and the parent fsync all stay relative to the descriptor prepared by the
mapping store — deterministically, with the parent path renamed and
replaced by a symlink in between — and unusable descriptors are refused
instead of degrading to path resolution.
"""
import hashlib
import os
import stat
from pathlib import Path

import pytest

from app.storage import versioned_mapping_objects
from app.storage.mapping_store import _prepare_objects_parent
from app.storage.versioned_mapping_objects import (
    InvalidDigestError,
    MappingObjectEntry,
    ObjectMismatchError,
    ObjectMissingError,
    UnexpectedEntryError,
    UnsafeObjectPathError,
    VersionedMappingObjectStore,
)

PAYLOAD = b"# acc-mapping v1\n\n- exact historical bytes\n"


def test_put_addresses_object_by_sha256(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    assert digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert (tmp_path / "objects" / digest[:2] / digest).read_bytes() == PAYLOAD
    assert store.get(digest) == PAYLOAD


def test_distinct_contents_have_distinct_addresses(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    first = store.put(b"mapping v1")
    second = store.put(b"mapping v2")
    assert first != second
    assert store.get(first) == b"mapping v1"
    assert store.get(second) == b"mapping v2"


def test_replaying_identical_bytes_is_idempotent(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    path = tmp_path / "objects" / digest[:2] / digest
    before = path.stat().st_mtime_ns
    assert store.put(PAYLOAD) == digest
    assert path.stat().st_mtime_ns == before
    files = [p for p in (tmp_path / "objects").rglob("*") if p.is_file()]
    assert files == [path]


@pytest.mark.parametrize("digest", [
    "", "abc", "0" * 63, "0" * 65, "A" * 64, "g" * 64,
    "../" + "0" * 61, "0" * 32 + "/etc/passwd" + "0" * 20,
    123, None, b"0" * 64,
])
def test_malformed_digests_are_rejected(tmp_path, digest):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    with pytest.raises(InvalidDigestError):
        store.get(digest)
    with pytest.raises(InvalidDigestError):
        store.has(digest)


def test_missing_object_fails_closed(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = hashlib.sha256(b"never stored").hexdigest()
    with pytest.raises(ObjectMissingError):
        store.get(digest)
    assert store.has(digest) is False
    # An existing shard with no object under the address is still a genuine
    # ENOENT, reported as missing rather than as an unsafe path.
    (store.root / digest[:2]).mkdir()
    with pytest.raises(ObjectMissingError):
        store.get(digest)
    assert store.has(digest) is False


def test_tampered_object_fails_closed_and_is_never_repaired(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    path = tmp_path / "objects" / digest[:2] / digest
    path.write_bytes(b"tampered")
    with pytest.raises(ObjectMismatchError):
        store.get(digest)
    with pytest.raises(ObjectMismatchError):
        store.has(digest)
    with pytest.raises(ObjectMismatchError):
        store.put(PAYLOAD)
    assert path.read_bytes() == b"tampered"


def test_roots_are_isolated_and_objects_stay_under_root(tmp_path):
    store_a = VersionedMappingObjectStore(tmp_path / "a")
    store_b = VersionedMappingObjectStore(tmp_path / "b")
    digest = store_a.put(PAYLOAD)
    with pytest.raises(ObjectMissingError):
        store_b.get(digest)
    files = [p for p in (tmp_path / "a").rglob("*") if p.is_file()]
    assert files == [tmp_path / "a" / digest[:2] / digest]
    assert not [p for p in (tmp_path / "b").rglob("*") if p.is_file()]
    assert not list(tmp_path.glob("*.tmp"))


def test_put_syncs_file_then_directory_entries_in_order(tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    shard = store.root / digest[:2]
    events = []
    real_fsync = os.fsync
    real_link = os.link
    real_fsync_dir_fd = VersionedMappingObjectStore._fsync_dir_fd

    def recording_fsync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("fsync-object-bytes")
        real_fsync(fd)

    def recording_link(src, dst, **kwargs):
        events.append("link")
        real_link(src, dst, **kwargs)

    def recording_fsync_dir_fd(fd):
        # Directory descriptors are identified by inode so the assertion
        # stays valid without re-resolving paths through the filesystem.
        events.append(("fsync-dir", os.fstat(fd).st_ino))
        real_fsync_dir_fd(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "link", recording_link)
    monkeypatch.setattr(VersionedMappingObjectStore, "_fsync_dir_fd",
                        staticmethod(recording_fsync_dir_fd))

    assert store.put(PAYLOAD) == digest
    assert events == [
        "fsync-object-bytes",
        "link",
        ("fsync-dir", shard.stat().st_ino),
        ("fsync-dir", store.root.stat().st_ino),
    ]
    assert store.get(digest) == PAYLOAD


def test_replaying_digest_syncs_shard_and_root_entries(tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    shard = store.root / digest[:2]
    synced = []
    real_fsync_dir_fd = VersionedMappingObjectStore._fsync_dir_fd

    def recording_fsync_dir_fd(fd):
        synced.append(os.fstat(fd).st_ino)
        real_fsync_dir_fd(fd)

    monkeypatch.setattr(VersionedMappingObjectStore, "_fsync_dir_fd",
                        staticmethod(recording_fsync_dir_fd))
    assert store.put(PAYLOAD) == digest
    # A replay never skips the root sync: it cannot assume a previous
    # sync reached stable storage, so shard and root are synced again.
    assert synced == [shard.stat().st_ino, store.root.stat().st_ino]
    assert store.get(digest) == PAYLOAD


def _payload_in_shard(prefix: str) -> bytes:
    """Deterministically find a payload whose digest starts with ``prefix``."""
    for counter in range(1 << 20):
        candidate = b"same-shard mapping " + str(counter).encode()
        if hashlib.sha256(candidate).hexdigest().startswith(prefix):
            return candidate
    raise AssertionError(f"no payload found for shard prefix {prefix}")


def test_second_write_into_existing_shard_syncs_root(tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    first = store.put(PAYLOAD)
    second_payload = _payload_in_shard(first[:2])
    second = store.digest_of(second_payload)
    shard = store.root / first[:2]
    synced = []
    real_fsync_dir_fd = VersionedMappingObjectStore._fsync_dir_fd

    def recording_fsync_dir_fd(fd):
        synced.append(os.fstat(fd).st_ino)
        real_fsync_dir_fd(fd)

    monkeypatch.setattr(VersionedMappingObjectStore, "_fsync_dir_fd",
                        staticmethod(recording_fsync_dir_fd))
    assert store.put(second_payload) == second
    # The shard already existed, yet the root sync is not skipped: the
    # shard's creation may have raced with another writer whose root sync
    # never reached stable storage.
    assert synced == [shard.stat().st_ino, store.root.stat().st_ino]
    assert store.get(second) == second_payload


def test_shard_directory_sync_failure_fails_closed(tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    real_fsync = os.fsync

    def failing_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="simulated directory sync failure"):
        store.put(PAYLOAD)
    # No success was returned and no temporary file is left behind; the
    # linked bytes, if already linked, remain intact and unmodified.
    assert not list(store.root.rglob("*.tmp"))

    monkeypatch.setattr(os, "fsync", real_fsync)
    # A retry re-verifies the valid occupant and syncs its directory entry.
    assert store.put(PAYLOAD) == digest
    assert store.get(digest) == PAYLOAD


def test_new_shard_parent_sync_failure_fails_closed(tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    real_fsync = os.fsync
    dir_syncs = []

    def failing_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_syncs.append(fd)
            if len(dir_syncs) == 2:  # shard entry synced; root sync fails
                raise OSError("simulated parent directory sync failure")
            real_fsync(fd)
        else:
            real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="simulated parent directory sync failure"):
        store.put(PAYLOAD)
    assert not list(store.root.rglob("*.tmp"))

    monkeypatch.setattr(os, "fsync", real_fsync)
    assert store.put(PAYLOAD) == digest
    assert store.get(digest) == PAYLOAD


def test_retry_after_first_sync_failure_resyncs_both_directories(
        tmp_path, monkeypatch):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    shard = store.root / digest[:2]
    real_fsync = os.fsync
    dir_syncs = []

    def failing_first_dir_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_syncs.append(fd)
            if len(dir_syncs) == 1:
                raise OSError("simulated first directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_first_dir_sync)
    with pytest.raises(OSError, match="simulated first directory sync failure"):
        store.put(PAYLOAD)
    assert not list(store.root.rglob("*.tmp"))

    synced = []

    def recording_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    assert store.put(PAYLOAD) == digest
    # The retry must not treat the failed attempt's syncs as done: both
    # the shard and the root are synced again before success is returned.
    assert synced == [shard.stat().st_ino, store.root.stat().st_ino]
    assert store.get(digest) == PAYLOAD


def test_constructor_creates_root_leaf_and_syncs_parent(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    store = VersionedMappingObjectStore(parent / "objects")
    assert (parent / "objects").is_dir()
    # The newly created root leaf is persisted by syncing its parent.
    assert synced == [parent.stat().st_ino]
    assert store.put(PAYLOAD) == store.digest_of(PAYLOAD)


def test_constructor_syncs_parent_for_existing_root(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    root.mkdir()
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    VersionedMappingObjectStore(root)
    # An already-existing root still gets its parent synced: construction
    # cannot assume a previous creation's sync reached stable storage.
    assert synced == [tmp_path.stat().st_ino]


def test_missing_root_parent_fails_without_creating_ancestors(tmp_path):
    absent = tmp_path / "absent"
    root = absent / "objects"
    with pytest.raises(FileNotFoundError, match="parent"):
        VersionedMappingObjectStore(root)
    # The store never recursively materializes ancestors it would not
    # persist; the missing parent stays missing and no root is created.
    assert not absent.exists()
    assert not root.exists()


def test_root_location_is_frozen_against_chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    frozen = Path(os.path.abspath("objects"))
    store = VersionedMappingObjectStore(Path("objects"))
    assert store.root == frozen
    assert frozen.is_absolute()
    digest = store.put(PAYLOAD)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # A chdir after construction cannot relocate the store: reads and
    # writes keep resolving to the absolute root frozen at construction.
    assert store.get(digest) == PAYLOAD
    second = store.put(b"written after chdir")
    assert (frozen / second[:2] / second).read_bytes() == b"written after chdir"
    assert store.get(second) == b"written after chdir"
    assert not list(elsewhere.iterdir())


def test_missing_o_directory_is_refused_not_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(versioned_mapping_objects, "_O_DIRECTORY", 0)
    root = tmp_path / "objects"
    with pytest.raises(RuntimeError, match="O_DIRECTORY"):
        versioned_mapping_objects.VersionedMappingObjectStore(root)
    # The refusal happens before any filesystem side effect: a missing
    # O_DIRECTORY must never degrade to opening non-directories as roots.
    assert not root.exists()


def _symlink_or_skip(target, link_path):
    """Create ``link_path`` -> ``target`` or skip with the platform boundary.

    Platforms or filesystems that cannot create symlinks (e.g. Windows
    without the privilege) cannot exercise symlink rejection at all; those
    cases skip loudly instead of passing silently.
    """
    if not hasattr(os, "symlink"):
        pytest.skip("platform has no os.symlink; "
                    "symlink rejection is untestable here")
    try:
        os.symlink(target, link_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot create symlinks on this platform/filesystem "
                    f"({exc}); symlink rejection is untestable here")


def test_symlinked_root_is_rejected_at_construction(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside sentinel")
    _symlink_or_skip(outside, tmp_path / "link-root")
    with pytest.raises(UnsafeObjectPathError):
        VersionedMappingObjectStore(tmp_path / "link-root")
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.bin"]
    assert (outside / "sentinel.bin").read_bytes() == b"outside sentinel"


def test_regular_file_at_root_fails_closed_at_construction(tmp_path):
    root = tmp_path / "objects"
    root.write_bytes(b"configured root occupied by a regular file")
    # A plain mkdir(exist_ok=True) would leak a raw FileExistsError here;
    # the module contract maps a wrong-type root occupant to
    # UnsafeObjectPathError via the no-follow directory open instead.
    with pytest.raises(UnsafeObjectPathError):
        VersionedMappingObjectStore(root)
    # The refusal never touches the occupant: the file and its bytes stay
    # exactly as planted, and nothing else appears in the sandbox.
    assert root.is_file()
    assert root.read_bytes() == b"configured root occupied by a regular file"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["objects"]


def test_root_replaced_by_symlink_fails_closed_on_every_operation(tmp_path):
    root = tmp_path / "objects"
    store = VersionedMappingObjectStore(root)
    digest = store.digest_of(PAYLOAD)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside sentinel")
    root.rmdir()
    _symlink_or_skip(outside, root)
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.bin"]
    assert (outside / "sentinel.bin").read_bytes() == b"outside sentinel"


def test_symlinked_shard_is_never_followed(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    outside = tmp_path / "outside"
    outside.mkdir()
    # A planted lookalike object outside the root: if the shard symlink were
    # followed, get/has would read it and put would write alongside it.
    (outside / digest).write_bytes(PAYLOAD)
    _symlink_or_skip(outside, store.root / digest[:2])
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)
    assert sorted(p.name for p in outside.iterdir()) == [digest]
    assert (outside / digest).read_bytes() == PAYLOAD
    assert not list(store.root.rglob("*.tmp"))


def test_symlinked_object_is_never_followed(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    shard = store.root / digest[:2]
    shard.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    # Even exact payload bytes behind the link must never be read or written.
    sentinel.write_bytes(PAYLOAD)
    _symlink_or_skip(sentinel, shard / digest)
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.bin"]
    assert sentinel.read_bytes() == PAYLOAD
    assert not list(store.root.rglob("*.tmp"))


def test_directory_at_object_path_is_an_explicit_failure_not_missing(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    (store.root / digest[:2] / digest).mkdir(parents=True)
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)
    assert not list(store.root.rglob("*.tmp"))


def test_fifo_at_object_path_fails_fast_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform has no os.mkfifo; "
                    "the FIFO boundary is untestable here")
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    shard = store.root / digest[:2]
    shard.mkdir()
    os.mkfifo(shard / digest)
    # A plain O_RDONLY open would block forever on a FIFO with no writer;
    # the store must fail explicitly instead of hanging or reporting missing.
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)


def test_regular_file_at_shard_path_is_an_explicit_failure_not_missing(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    (store.root / digest[:2]).write_bytes(b"not a shard directory")
    with pytest.raises(UnsafeObjectPathError):
        store.get(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.has(digest)
    with pytest.raises(UnsafeObjectPathError):
        store.put(PAYLOAD)


def test_inventory_of_empty_store_is_empty(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    assert store.inventory() == []


def test_inventory_is_sorted_sized_and_read_verified(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    # Distinct shards (29…, 9e…, ce…) plus a second payload, so the listing
    # must merge shard-major enumeration into one global digest order.
    payloads = [b"mapping v1", PAYLOAD, b"mapping v2"]
    by_digest = {store.put(payload): payload for payload in payloads}
    expected = sorted(MappingObjectEntry(digest, len(payload))
                      for digest, payload in by_digest.items())
    listing = store.inventory()
    assert listing == expected
    assert [entry.digest for entry in listing] == sorted(by_digest)
    # Per item: every listed digest reads back exactly and the size is the
    # exact byte length of the stored content.
    for entry in listing:
        data = store.get(entry.digest)
        assert data == by_digest[entry.digest]
        assert entry.size_bytes == len(data)
    # Repeated listing is stable.
    assert store.inventory() == listing


def test_inventory_lists_duplicate_content_once(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    assert store.put(PAYLOAD) == digest
    assert store.inventory() == [MappingObjectEntry(digest, len(PAYLOAD))]


def test_inventory_rejects_corrupted_object_and_never_repairs(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    path = store.root / digest[:2] / digest
    path.write_bytes(b"tampered")
    with pytest.raises(ObjectMismatchError):
        store.inventory()
    # The corrupted bytes stay exactly as planted; nothing is repaired.
    assert path.read_bytes() == b"tampered"


@pytest.mark.parametrize("name", [
    "zz", "AB", "notes.txt", "0" * 3, ".object-1-deadbeef.tmp",
])
def test_inventory_rejects_unknown_root_entries(tmp_path, name):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    store.put(PAYLOAD)
    (store.root / name).write_bytes(b"foreign")
    with pytest.raises(UnexpectedEntryError):
        store.inventory()
    # Never deleted: the planted entry stays byte-identical.
    assert (store.root / name).read_bytes() == b"foreign"


def test_inventory_rejects_unknown_root_directory(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    (store.root / "zz").mkdir()
    with pytest.raises(UnexpectedEntryError):
        store.inventory()
    assert (store.root / "zz").is_dir()


def test_inventory_rejects_non_directory_shard(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    (store.root / digest[:2]).write_bytes(b"not a shard directory")
    with pytest.raises(UnsafeObjectPathError):
        store.inventory()


def test_inventory_rejects_wrong_shard_prefix_and_bad_names(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.put(PAYLOAD)
    shard = store.root / digest[:2]
    # sha256(b"foreign object") starts with 5c while PAYLOAD starts with ce,
    # so this valid digest name deterministically lands in the wrong shard.
    foreign_payload = b"foreign object"
    foreign = hashlib.sha256(foreign_payload).hexdigest()
    assert foreign[:2] != digest[:2]
    (shard / foreign).write_bytes(foreign_payload)
    with pytest.raises(UnexpectedEntryError):
        store.inventory()
    assert (shard / foreign).read_bytes() == foreign_payload
    (shard / foreign).unlink()
    (shard / "not-an-object").write_bytes(b"x")
    with pytest.raises(UnexpectedEntryError):
        store.inventory()
    # Never skipped, deleted, or repaired: the planted name stays and the
    # genuine object in the same shard is untouched.
    assert (shard / "not-an-object").read_bytes() == b"x"
    assert store.get(digest) == PAYLOAD


def test_inventory_rejects_symlinked_shard(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / digest).write_bytes(PAYLOAD)
    _symlink_or_skip(outside, store.root / digest[:2])
    with pytest.raises(UnsafeObjectPathError):
        store.inventory()
    assert (outside / digest).read_bytes() == PAYLOAD


def test_inventory_rejects_symlinked_object(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    shard = store.root / digest[:2]
    shard.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    # Even exact payload bytes behind the link must never be listed.
    sentinel.write_bytes(PAYLOAD)
    _symlink_or_skip(sentinel, shard / digest)
    with pytest.raises(UnsafeObjectPathError):
        store.inventory()
    assert sentinel.read_bytes() == PAYLOAD


def test_inventory_rejects_directory_at_object_path(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "objects")
    digest = store.digest_of(PAYLOAD)
    (store.root / digest[:2] / digest).mkdir(parents=True)
    with pytest.raises(UnsafeObjectPathError):
        store.inventory()


def test_pinned_parent_survives_rename_and_symlink_replacement(tmp_path):
    """Regression for the parent check/use gap: the pin is the fd, not the path.

    Preparation verifies and creates the single parent level and returns a
    descriptor pinned to it.  The swap below is fully deterministic — no
    concurrency, no timing: the verified directory's entry is renamed and
    a symlink to an outside sandbox is planted at the original path before
    the store is constructed.  Root creation, the object write, reads, and
    inventory must all stay relative to the pinned descriptor; nothing may
    land behind the symlink and the symlink must stay unfollowed.
    """
    objects_root = tmp_path / "2a" / "mapping-objects"
    parent_fd = _prepare_objects_parent(objects_root)
    try:
        renamed = tmp_path / "2a-verified"
        os.rename(tmp_path / "2a", renamed)
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_or_skip(outside, tmp_path / "2a")

        store = VersionedMappingObjectStore(objects_root, parent_fd=parent_fd)
        digest = store.put(PAYLOAD)

        # The root leaf and the object landed inside the renamed verified
        # directory the descriptor is pinned to — never behind the symlink.
        assert (renamed / "mapping-objects").is_dir()
        object_path = renamed / "mapping-objects" / digest[:2] / digest
        assert object_path.read_bytes() == PAYLOAD
        assert list(outside.iterdir()) == []
        # The symlink is still exactly the planted, unfollowed link.
        assert os.path.islink(tmp_path / "2a")
        assert os.readlink(tmp_path / "2a") == str(outside)
        # Reads and inventory through the same pinned store stay inside.
        assert store.get(digest) == PAYLOAD
        assert store.has(digest) is True
        assert store.inventory() == [MappingObjectEntry(digest, len(PAYLOAD))]
        # The store only borrows the descriptor: it is still open and the
        # caller stays responsible for closing it.
        assert stat.S_ISDIR(os.fstat(parent_fd).st_mode)
    finally:
        os.close(parent_fd)


def test_parent_fd_validation_is_fail_closed(tmp_path):
    """A refused parent descriptor never degrades to path resolution."""
    root = tmp_path / "objects"
    # A descriptor referencing a regular file is not a pinned directory.
    regular = tmp_path / "regular"
    regular.write_bytes(b"x")
    fd = os.open(regular, os.O_RDONLY)
    try:
        with pytest.raises(UnsafeObjectPathError):
            VersionedMappingObjectStore(root, parent_fd=fd)
    finally:
        os.close(fd)
    # A closed descriptor is refused instead of silently re-resolving the
    # parent path.
    dead = os.open(tmp_path, os.O_RDONLY)
    os.close(dead)
    with pytest.raises(ValueError):
        VersionedMappingObjectStore(root, parent_fd=dead)
    # Non-descriptors never reach the filesystem.
    with pytest.raises(TypeError):
        VersionedMappingObjectStore(root, parent_fd=str(tmp_path))
    # Nothing was created through any refused descriptor, and the plain
    # regular file stays untouched.
    assert not root.exists()
    assert regular.read_bytes() == b"x"


def test_pinned_parent_fsyncs_the_borrowed_descriptor(tmp_path, monkeypatch):
    """The construction-time parent sync targets the pinned fd itself."""
    parent = tmp_path / "2a"
    parent.mkdir()
    parent_fd = os.open(parent,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        | os.O_CLOEXEC)
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    try:
        store = VersionedMappingObjectStore(parent / "objects",
                                            parent_fd=parent_fd)
        assert (parent / "objects").is_dir()
        # Exactly one directory sync, on the borrowed descriptor itself —
        # the parent was never reopened by path for the sync.
        assert synced == [parent_fd]
        digest = store.put(PAYLOAD)
        assert (parent / "objects" / digest[:2] / digest).read_bytes() == PAYLOAD
    finally:
        os.close(parent_fd)
