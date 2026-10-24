"""Immutable mapping-object primitive contracts; synthetic tmp_path bytes only."""
import hashlib

import pytest

from app.storage.versioned_mapping_objects import (
    InvalidDigestError,
    ObjectMismatchError,
    ObjectMissingError,
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
