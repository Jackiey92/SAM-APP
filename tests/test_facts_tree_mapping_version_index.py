"""R2 append-only mapping-version index ledger contracts.

Real VersionedMappingObjectStore plus a real SQLite ledger under
tmp_path throughout — no success-path mocks.  Fault injection only
touches the storage boundary (direct byte-file deletion/rewrite,
over-permissive directories, planted symlinks); hashing is never
mocked.  The backfiller is a later slice and has no tests here.

Coverage of freeze appendix R2 §7:
 1 V1 storage-level append-only + API surface
 2 V2 monotone ledger-issued numbers, replay, independent sources,
   real two-thread concurrency
 3 V3 ghost/index-gap verdicts (missing, size mismatch, corruption)
 4 V4 old versions stay byte-identical and are never substituted
 7 V6 scope refusal at calls, reopen, the filesystem boundary, and
   concurrent first construction of a fresh ledger root
 8 V7 zero production-import graph for the new module
 9 V8 stable secret-free digest across locations and orderings
10 zero-network: socket sealed, the ledger still completes
11 one focus per test, vocabulary contract with the R1 closure
"""
import ast
import os
import socket
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from app.facts_tree import backup_object_closure as boc
from app.facts_tree.mapping_version_index import (
    BYTE_CORRUPT,
    BYTE_MISSING,
    BYTE_PRESENT,
    GhostObjectError,
    LedgerIntegrityError,
    MappingVersionIndexError,
    MappingVersionLedger,
    MappingVersionScopeError,
    OBJECT_TYPE_MAPPING_VERSION,
    SOURCE_KIND_BACKFILL,
    STORAGE_CLASS_MAPPING_OBJECTS,
    STATUS_REGISTERED,
    STATUS_REPLAYED,
    UnsafeLedgerRootError,
    UnknownVersionError,
)
from app.storage.versioned_mapping_objects import (
    ObjectMismatchError,
    VersionedMappingObjectStore,
)

INSTALL = "inst-1"
COMPANY = "company-1"


# -- fixtures ----------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    vault = tmp_path / "vault"
    vault.mkdir()
    ledger_root = vault / "mapping-version-index"
    ledger = MappingVersionLedger(
        ledger_root,
        installation_id=INSTALL,
        company_id=COMPANY,
        object_store=cabinet,
    )

    def put(payload: bytes) -> str:
        return cabinet.put(payload)

    def register(digest: str, *, source="srcA", size=None, **kwargs):
        kwargs.setdefault("installation_id", INSTALL)
        kwargs.setdefault("company_id", COMPANY)
        return ledger.register_version(
            logical_source_id=source,
            digest=digest,
            size_bytes=len(cabinet.get(digest)) if size is None else size,
            **kwargs,
        )

    yield {
        "tmp": tmp_path,
        "cabinet": cabinet,
        "ledger": ledger,
        "ledger_root": ledger_root,
        "put": put,
        "register": register,
    }
    ledger.close()


def object_path(cabinet: VersionedMappingObjectStore, digest: str) -> Path:
    return Path(cabinet.root) / digest[:2] / digest


# -- §7.1 V1: append-only at the storage layer and API surface ---------------

def test_v1_raw_update_delete_replace_are_rejected_by_triggers(world):
    digest = world["put"](b"mapping v1")
    world["register"](digest)
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        for statement in (
            "UPDATE fact_mapping_version_registrations SET size_bytes=5",
            "DELETE FROM fact_mapping_version_registrations",
        ):
            with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError), match="append-only"):
                raw.execute(statement)
        # INSERT OR REPLACE on the already-issued (source, version) key is
        # refused before its delete-and-replace conflict path can run.
        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError), match="append-only"):
            raw.execute(
                "INSERT OR REPLACE INTO fact_mapping_version_registrations "
                "(installation_id, company_id, logical_source_id, version_no,"
                " object_type, storage_class, digest, size_bytes, "
                "source_kind, source_coordinate, h_commit_oid, "
                "registered_at, row_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (INSTALL, COMPANY, "srcA", 1, "mapping_version",
                 "mapping_objects", digest, 9, "online", None, None,
                 "2000-01-01T00:00:00+00:00", "tampered"),
            )
    finally:
        raw.close()
    # The row is still the original one.
    record = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY)
    assert record.version_no == 1
    assert record.digest == digest
    assert record.byte_status == BYTE_PRESENT


def test_v1_meta_scope_row_is_also_immutable(world):
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError), match="append-only"):
            raw.execute(
                "UPDATE fact_mapping_version_index_meta SET company_id='x'")
        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError), match="append-only"):
            raw.execute("DELETE FROM fact_mapping_version_index_meta")
    finally:
        raw.close()


def test_v1_ledger_exposes_no_rewrite_or_removal_api(world):
    forbidden = ("update", "delete", "remove", "replace", "rewrite",
                 "mutate", "edit", "put")
    public_names = [
        name for name in dir(world["ledger"])
        if not name.startswith("_") and callable(getattr(world["ledger"], name))
    ]
    assert public_names  # sanity: the check has something to check
    for name in public_names:
        assert not any(token in name.lower() for token in forbidden), name


# -- §7.2 V2: monotone ledger-issued numbering --------------------------------

def test_v2_versions_are_issued_one_two_three_by_the_ledger(world):
    d1 = world["put"](b"v1")
    d2 = world["put"](b"v2")
    d3 = world["put"](b"v3")
    assert world["register"](d1).version_no == 1
    assert world["register"](d2).version_no == 2
    assert world["register"](d3).version_no == 3


def test_v2_caller_cannot_pin_a_version_number(world):
    # There is no keyword at all to name the version; assert the signature.
    import inspect
    params = inspect.signature(
        world["ledger"].register_version).parameters
    assert "version_no" not in params
    assert "version" not in params


def test_v2_repeated_source_digest_is_one_idempotent_row(world):
    digest = world["put"](b"same bytes")
    first = world["register"](digest, source_kind="online")
    replay = world["register"](
        digest, source_kind=SOURCE_KIND_BACKFILL,
        source_coordinate="manifest:42")
    assert first.status == STATUS_REGISTERED
    assert replay.status == STATUS_REPLAYED
    assert replay.version_no == 1
    # Provenance of the first durable registration wins; replay never adds
    # or rewrites a row.
    assert replay.source_kind == "online"
    assert replay.source_coordinate is None
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        count = raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0]
    finally:
        raw.close()
    assert count == 1


def test_v2_distinct_sources_number_independently(world):
    a1 = world["put"](b"a1")
    b1 = world["put"](b"b1")
    a2 = world["put"](b"a2")
    assert world["register"](a1, source="srcA").version_no == 1
    assert world["register"](b1, source="srcB").version_no == 1
    assert world["register"](a2, source="srcA").version_no == 2
    assert world["register"](b1, source="srcB").status == STATUS_REPLAYED


def test_v2_concurrent_registrations_never_duplicate_or_skip(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    root = tmp_path / "vault" / "mvi"
    digests = [cabinet.put(f"bytes-{i}".encode()) for i in range(10)]
    errors = []

    def worker(my_digests):
        ledger = MappingVersionLedger(
            root, installation_id=INSTALL, company_id=COMPANY,
            object_store=cabinet)
        try:
            for digest in my_digests:
                ledger.register_version(
                    logical_source_id="hot-source",
                    digest=digest,
                    size_bytes=len(cabinet.get(digest)),
                    installation_id=INSTALL,
                    company_id=COMPANY)
        except BaseException as exc:  # captured, fail the test below
            errors.append(exc)
        finally:
            ledger.close()

    t1 = threading.Thread(target=worker, args=(digests[:5],))
    t2 = threading.Thread(target=worker, args=(digests[5:],))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert errors == []
    ledger = MappingVersionLedger(
        root, installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)
    try:
        records = ledger.list_versions(
            "hot-source", installation_id=INSTALL, company_id=COMPANY)
    finally:
        ledger.close()
    numbers = sorted(r.version_no for r in records)
    assert numbers == list(range(1, 11))
    assert len({r.digest for r in records}) == 10


def test_v6_concurrent_first_construction_yields_one_private_db(tmp_path):
    # Eight threads construct a ledger on the same not-yet-created root at
    # the same instant. Exactly one O_EXCL winner may initialize; every
    # loser must attach to the name born 0600 and wait for the completion
    # marker, with no 0644 window and no duplicate singleton insert.
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    root = tmp_path / "vault" / "mvi"
    thread_count = 8
    digests = [cabinet.put(f"race-{i}".encode())
               for i in range(thread_count)]
    barrier = threading.Barrier(thread_count)
    errors = []
    numbers = []

    def worker(i):
        try:
            barrier.wait()
            ledger = MappingVersionLedger(
                root, installation_id=INSTALL, company_id=COMPANY,
                object_store=cabinet)
            try:
                result = ledger.register_version(
                    logical_source_id="race-source",
                    digest=digests[i],
                    size_bytes=len(cabinet.get(digests[i])),
                    installation_id=INSTALL,
                    company_id=COMPANY)
                numbers.append(result.version_no)
            finally:
                ledger.close()
        except BaseException as exc:  # surfaced in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(numbers) == list(range(1, thread_count + 1))

    db_mode = stat.S_IMODE(os.stat(root / "mapping-version-index.sqlite")
                           .st_mode)
    assert db_mode == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700

    # A later opener attaches to the finished ledger without waiting.
    ledger = MappingVersionLedger(
        root, installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)
    try:
        records = ledger.list_versions(
            "race-source", installation_id=INSTALL, company_id=COMPANY)
    finally:
        ledger.close()
    assert sorted(r.version_no for r in records) == list(
        range(1, thread_count + 1))
    assert len({r.digest for r in records}) == thread_count


def test_batch_seam_commits_all_registrations_together(world):
    d1 = world["put"](b"batch one")
    d2 = world["put"](b"batch two")
    ledger = world["ledger"]
    with ledger.registration_batch():
        r1 = ledger.register_version(
            logical_source_id="batch-src", digest=d1,
            size_bytes=len(b"batch one"),
            installation_id=INSTALL, company_id=COMPANY,
            source_kind="backfill", source_coordinate="parse_staging#id=1")
        r2 = ledger.register_version(
            logical_source_id="batch-src", digest=d2,
            size_bytes=len(b"batch two"),
            installation_id=INSTALL, company_id=COMPANY,
            source_kind="backfill", source_coordinate="parse_staging#id=2")
    assert (r1.version_no, r2.version_no) == (1, 2)
    raw = sqlite3.connect(ledger.database_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0] == 2
    finally:
        raw.close()


def test_batch_seam_rolls_back_everything_when_one_candidate_is_a_ghost(world):
    d1 = world["put"](b"batch good one")
    ghost = "f" * 64
    ledger = world["ledger"]
    with pytest.raises(GhostObjectError):
        with ledger.registration_batch():
            ledger.register_version(
                logical_source_id="batch-src", digest=d1,
                size_bytes=len(b"batch good one"),
                installation_id=INSTALL, company_id=COMPANY,
                source_kind="backfill", source_coordinate="parse_staging#id=1")
            ledger.register_version(
                logical_source_id="batch-src", digest=ghost,
                size_bytes=9,
                installation_id=INSTALL, company_id=COMPANY,
                source_kind="backfill", source_coordinate="parse_staging#id=2")
    raw = sqlite3.connect(ledger.database_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0] == 0
    finally:
        raw.close()
    # The failed batch poisoned nothing: a later single call works and
    # numbers from 1, proving no half batch was committed.
    result = ledger.register_version(
        logical_source_id="batch-src", digest=d1,
        size_bytes=len(b"batch good one"),
        installation_id=INSTALL, company_id=COMPANY,
        source_kind="backfill", source_coordinate="parse_staging#id=1")
    assert result.version_no == 1


def test_batch_seam_cannot_be_nested(world):
    ledger = world["ledger"]
    with ledger.registration_batch():
        with pytest.raises(MappingVersionIndexError, match="nested"):
            with ledger.registration_batch():
                pass


# -- §7.3 V3: no ghost indexes; post-registration gaps -----------------------

def test_v3_digest_absent_from_cabinet_is_refused_without_a_row(world):
    ghost = "a" * 64
    with pytest.raises(GhostObjectError) as exc:
        world["register"](ghost, size=7)
    assert exc.value.reason == "object_missing"
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_v3_declared_size_mismatch_is_refused_without_a_row(world):
    digest = world["put"](b"exactly these bytes")
    with pytest.raises(GhostObjectError) as exc:
        world["register"](digest, size=999)
    assert exc.value.reason == "size_mismatch"
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_v3_cabinet_object_corrupted_before_register_is_refused(world):
    digest = world["put"](b"original bytes")
    path = object_path(world["cabinet"], digest)
    path.write_bytes(b"tampered bytes!")
    with pytest.raises(GhostObjectError) as exc:
        world["register"](digest, size=len(b"tampered bytes!"))
    assert exc.value.reason == "sha256_mismatch"
    # The ledger never repairs or deletes the cabinet object (V10).
    assert path.read_bytes() == b"tampered bytes!"


def test_v3_deleted_after_registration_is_reported_as_missing_gap(world):
    d1 = world["put"](b"kept")
    d2 = world["put"](b"later")
    world["register"](d1)
    world["register"](d2)
    os.unlink(object_path(world["cabinet"], d1))
    record = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY)
    assert record.byte_status == BYTE_MISSING
    assert record.issue == "object_missing"
    assert record.digest == d1
    assert record.content is None
    shape = world["ledger"].ledger_shape()
    assert shape["total_gaps"] == 1
    assert shape["sources"][0]["gap_versions"] == [1]


def test_v3_corrupted_after_registration_is_reported_as_corrupt_gap(world):
    digest = world["put"](b"good bytes here")
    world["register"](digest)
    path = object_path(world["cabinet"], digest)
    path.write_bytes(b"different bytes!!!")
    record = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY)
    assert record.byte_status == BYTE_CORRUPT
    assert record.issue == "sha256_mismatch"
    assert world["ledger"].ledger_shape()["total_gaps"] == 1


def test_v3_tampered_register_raises_through_real_cabinet_error(world):
    digest = world["put"](b"abc")
    object_path(world["cabinet"], digest).write_bytes(b"xyz")
    with pytest.raises(ObjectMismatchError):
        world["cabinet"].get(digest)


# -- §7.4 V4: old versions are never touched or substituted ------------------

def test_v4_old_version_fields_survive_newer_registrations(world):
    d1 = world["put"](b"first edition")
    d2 = world["put"](b"second edition")
    d3 = world["put"](b"third edition")
    world["register"](d1, source_kind="online",
                      source_coordinate="manifest-row:1",
                      h_commit_oid="1" * 40)
    world["register"](d2, source_kind="backfill",
                      source_coordinate="manifest-row:2")
    world["register"](d3)
    first = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY)
    assert first.digest == d1
    assert first.size_bytes == len(b"first edition")
    assert first.source_kind == "online"
    assert first.source_coordinate == "manifest-row:1"
    assert first.h_commit_oid == "1" * 40
    assert first.byte_status == BYTE_PRESENT
    fetched = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY,
        include_bytes=True)
    assert fetched.content == b"first edition"


def test_v4_missing_old_version_is_not_served_another_versions_bytes(world):
    d1 = world["put"](b"v1 content")
    d2 = world["put"](b"v2 content")
    world["register"](d1)
    world["register"](d2)
    os.unlink(object_path(world["cabinet"], d1))
    record = world["ledger"].get_version(
        "srcA", 1, installation_id=INSTALL, company_id=COMPANY,
        include_bytes=True)
    assert record.digest == d1
    assert record.byte_status == BYTE_MISSING
    assert record.content is None


def test_v4_unknown_version_slot_is_distinct_from_a_byte_gap(world):
    digest = world["put"](b"only version")
    world["register"](digest)
    with pytest.raises(UnknownVersionError):
        world["ledger"].get_version(
            "srcA", 9, installation_id=INSTALL, company_id=COMPANY)


# -- §7.7 V6: scope refusal at the call, reopen and filesystem boundary ------

def test_v6_foreign_installation_register_is_refused(world):
    digest = world["put"](b"scoped bytes")
    with pytest.raises(MappingVersionScopeError):
        world["register"](digest, installation_id="inst-other")
    raw = sqlite3.connect(world["ledger"].database_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM fact_mapping_version_registrations"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_v6_foreign_company_register_and_read_are_refused(world):
    digest = world["put"](b"company bytes")
    world["register"](digest)
    with pytest.raises(MappingVersionScopeError):
        world["register"](world["put"](b"more"), company_id="company-2")
    with pytest.raises(MappingVersionScopeError):
        world["ledger"].list_versions(
            "srcA", installation_id=INSTALL, company_id="company-2")
    with pytest.raises(MappingVersionScopeError):
        world["ledger"].get_version(
            "srcA", 1, installation_id=INSTALL, company_id="company-2")


def test_v6_reopening_the_database_under_foreign_scope_is_refused(world):
    digest = world["put"](b"bound bytes")
    world["register"](digest)
    with pytest.raises(MappingVersionScopeError):
        MappingVersionLedger(
            world["ledger_root"],
            installation_id="inst-1",
            company_id="company-other",
            object_store=world["cabinet"])


def test_v6_ledger_root_must_exist_under_an_existing_parent(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    with pytest.raises(FileNotFoundError):
        MappingVersionLedger(
            tmp_path / "missing-parent" / "mvi",
            installation_id=INSTALL, company_id=COMPANY,
            object_store=cabinet)


def test_v6_ledger_root_symlink_is_refused(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    real = tmp_path / "vault" / "real-dir"
    real.mkdir()
    link = tmp_path / "vault" / "link-dir"
    link.symlink_to(real)
    with pytest.raises(UnsafeLedgerRootError):
        MappingVersionLedger(
            link, installation_id=INSTALL, company_id=COMPANY,
            object_store=cabinet)


def test_v6_group_world_writable_ledger_root_is_refused(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    loose = tmp_path / "vault" / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    try:
        with pytest.raises(UnsafeLedgerRootError):
            MappingVersionLedger(
                loose, installation_id=INSTALL, company_id=COMPANY,
                object_store=cabinet)
    finally:
        loose.chmod(0o700)


def test_v6_ledger_database_file_is_private_0600(world):
    mode = stat.S_IMODE(os.stat(world["ledger"].database_path).st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(os.stat(world["ledger_root"]).st_mode)
    assert dir_mode == 0o700


def test_v6_planted_database_symlink_is_refused(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    root = tmp_path / "vault" / "mvi"
    root.mkdir(0o700)
    (root / "mapping-version-index.sqlite").symlink_to(
        tmp_path / "elsewhere.sqlite")
    with pytest.raises(UnsafeLedgerRootError):
        MappingVersionLedger(
            root, installation_id=INSTALL, company_id=COMPANY,
            object_store=cabinet)


# -- §7.9 V8: stable, secret-free, location-independent digest ---------------

def _build_ledger(root, cabinet):
    return MappingVersionLedger(
        root, installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)


def test_v8_same_sequence_in_two_locations_has_same_digest(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    d1 = cabinet.put(b"alpha")
    d2 = cabinet.put(b"beta")
    d3 = cabinet.put(b"gamma")
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    vault_a.mkdir(); vault_b.mkdir()

    def populate(root):
        ledger = _build_ledger(root / "mvi", cabinet)
        try:
            ledger.register_version(
                logical_source_id="srcA", digest=d1,
                size_bytes=len(cabinet.get(d1)),
                installation_id=INSTALL, company_id=COMPANY,
                source_kind="online")
            ledger.register_version(
                logical_source_id="srcB", digest=d2,
                size_bytes=len(cabinet.get(d2)),
                installation_id=INSTALL, company_id=COMPANY,
                source_kind="backfill", source_coordinate="manifest:7")
            ledger.register_version(
                logical_source_id="srcA", digest=d3,
                size_bytes=len(cabinet.get(d3)),
                installation_id=INSTALL, company_id=COMPANY)
            return ledger.ledger_digest(), ledger.canonical_payload()
        finally:
            ledger.close()

    digest_a, payload_a = populate(vault_a)
    digest_b, payload_b = populate(vault_b)
    assert digest_a == digest_b
    assert payload_a == payload_b


def test_v8_interleaving_registration_order_does_not_change_digest(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    d1 = cabinet.put(b"alpha")
    d2 = cabinet.put(b"beta")
    d3 = cabinet.put(b"gamma")
    vault1 = tmp_path / "v1"
    vault2 = tmp_path / "v2"
    vault1.mkdir(); vault2.mkdir()

    def seq(root, order):
        ledger = _build_ledger(root / "mvi", cabinet)
        try:
            for digest, source in order:
                ledger.register_version(
                    logical_source_id=source, digest=digest,
                    size_bytes=len(cabinet.get(digest)),
                    installation_id=INSTALL, company_id=COMPANY)
            return ledger.ledger_digest()
        finally:
            ledger.close()

    # Reordering across sources is digest-neutral; per-source arrival
    # order defines the version numbers and must stay the same.
    first = seq(vault1, [(d1, "srcA"), (d2, "srcB"), (d3, "srcA")])
    second = seq(vault2, [(d2, "srcB"), (d1, "srcA"), (d3, "srcA")])
    assert first == second


def test_v8_payload_contains_no_path_timestamp_or_object_secret(tmp_path,
                                                                world):
    marker = "SECRET-TOKEN-XYZ-密码-password"
    secret_bytes = marker.encode()
    digest = world["put"](secret_bytes)
    world["register"](digest, source_coordinate="manifest:3")
    payload = world["ledger"].canonical_payload()
    from app.facts_tree.git_format import dumps_record
    blob = dumps_record(payload)
    assert marker.encode() not in blob
    assert b"registered_at" not in blob
    assert str(world["ledger_root"]).encode() not in blob
    assert str(world["tmp"]).encode() not in blob
    # The ledger database file itself never carries object bytes.
    db_bytes = Path(world["ledger"].database_path).read_bytes()
    assert marker.encode() not in db_bytes


def test_v8_shape_reports_per_source_counts_sorted(world):
    a1 = world["put"](b"a1")
    a2 = world["put"](b"a2")
    b1 = world["put"](b"b1")
    world["register"](a1, source="srcB")
    world["register"](a2, source="srcA")
    world["register"](b1, source="srcA")
    shape = world["ledger"].ledger_shape(verify=False)
    assert [s["logical_source_id"] for s in shape["sources"]] == [
        "srcA", "srcB"]
    by_source = {s["logical_source_id"]: s["versions"]
                 for s in shape["sources"]}
    assert by_source == {"srcA": 2, "srcB": 1}
    assert shape["total_versions"] == 3
    assert shape["total_sources"] == 2


# -- structural input validation ----------------------------------------------

def test_structural_bad_digest_size_and_oid_are_call_errors(world):
    ledger = world["ledger"]
    digest = world["put"](b"x")
    kwargs = dict(logical_source_id="srcA", installation_id=INSTALL,
                  company_id=COMPANY)
    with pytest.raises(MappingVersionIndexError):
        ledger.register_version(digest="ABC" + "0" * 61, size_bytes=1,
                                **kwargs)
    with pytest.raises(MappingVersionIndexError):
        ledger.register_version(digest=digest, size_bytes=True, **kwargs)
    with pytest.raises(MappingVersionIndexError):
        ledger.register_version(digest=digest, size_bytes=1,
                                h_commit_oid="zz" + "0" * 38, **kwargs)
    with pytest.raises(MappingVersionIndexError):
        ledger.register_version(digest=digest,
                                size_bytes=len(world["cabinet"].get(digest)),
                                source_kind="replay-the-beacon", **kwargs)
    # Sixty-four-hex sha256 git oids are accepted structurally.
    result = ledger.register_version(
        digest=world["put"](b"oid64"), size_bytes=5,
        h_commit_oid="f" * 64, **kwargs)
    assert result.status == STATUS_REGISTERED


def test_row_integrity_checksum_fails_closed_on_tampered_row(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    (tmp_path / "vault").mkdir()
    root = tmp_path / "vault" / "mvi"
    digest = cabinet.put(b"rows")
    ledger = MappingVersionLedger(
        root, installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)
    ledger.register_version(
        logical_source_id="srcA", digest=digest,
        size_bytes=len(b"rows"),
        installation_id=INSTALL, company_id=COMPANY)
    ledger.close()
    # A hostile local DBA can rewrite sqlite_master; the read-side row
    # checksum must then refuse the row instead of serving it.
    attacker = sqlite3.connect(str(root / "mapping-version-index.sqlite"))
    try:
        attacker.execute("PRAGMA recursive_triggers=OFF")
        attacker.execute(
            "DROP TRIGGER fact_mapping_version_index_reject_update")
        attacker.execute(
            "UPDATE fact_mapping_version_registrations SET size_bytes=42")
        attacker.commit()
    finally:
        attacker.close()
    reopened = MappingVersionLedger(
        root, installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)
    try:
        with pytest.raises(LedgerIntegrityError, match="row-integrity"):
            reopened.get_version(
                "srcA", 1, installation_id=INSTALL, company_id=COMPANY)
        with pytest.raises(LedgerIntegrityError):
            reopened.ledger_digest()
    finally:
        reopened.close()


# -- §7.10 network sealed ------------------------------------------------------

def test_r10_full_flow_succeeds_with_sockets_sealed(world, monkeypatch):
    class SealedSocket(socket.socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("ledger must not open network connections")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("ledger must not open network connections")

        def sendto(self, *args, **kwargs):
            raise AssertionError("ledger must not send packets")

    monkeypatch.setattr(socket, "socket", SealedSocket)

    def sealed_create_connection(*args, **kwargs):
        raise AssertionError("ledger must not create connections")

    monkeypatch.setattr(socket, "create_connection",
                        sealed_create_connection)
    d1 = world["put"](b"offline one")
    d2 = world["put"](b"offline two")
    assert world["register"](d1).version_no == 1
    assert world["register"](d2).version_no == 2
    versions = world["ledger"].list_versions(
        "srcA", installation_id=INSTALL, company_id=COMPANY)
    assert [v.byte_status for v in versions] == [BYTE_PRESENT, BYTE_PRESENT]
    assert world["ledger"].ledger_digest().startswith("sha256:")
    assert world["ledger"].ledger_shape()["total_versions"] == 2


# -- §7.11 vocabulary contract with the R1 closure ----------------------------

def test_vocabulary_matches_r1_closure_constants():
    assert OBJECT_TYPE_MAPPING_VERSION == boc.OBJECT_MAPPING_VERSION
    assert STORAGE_CLASS_MAPPING_OBJECTS == boc.STORAGE_MAPPING_OBJECTS


# -- §7.8 V7: no existing production module imports the new ledger ------------

def test_v7_no_live_path_module_imports_the_ledger():
    # The only sanctioned consumer is the offline R2 backfiller sibling
    # (a runnable tool, never imported by a live path); it is named
    # explicitly below so the allow-list cannot silently grow.
    repo_root = Path(__file__).resolve().parents[1]
    app_root = repo_root / "app"
    allowed_consumers = {"mapping_version_backfill.py"}
    target = "app.facts_tree.mapping_version_index"

    def resolved_imports(path, node):
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        module = node.module or ""
        if node.level:
            package = ["app"] + list(
                path.parent.relative_to(app_root).parts)
            base = package[:len(package) - node.level + 1]
            return [".".join(base + ([module] if module else []))]
        return [module] if module else []

    offenders = []
    consumers = set()
    for path in app_root.rglob("*.py"):
        if path.name == "mapping_version_index.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for name in resolved_imports(path, node):
                if name == target or name.startswith(target + "."):
                    consumers.add(path.name)
                    if path.name not in allowed_consumers:
                        offenders.append(f"{path}: {name}")
    assert offenders == []
    assert consumers == allowed_consumers
