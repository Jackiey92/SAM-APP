"""G1 read-only mapping-object retention planner contracts.

Everything runs against the real VersionedMappingObjectStore cabinet and
a real MappingVersionLedger under tmp_path: multiple logical sources with
multiple registered versions (including old versions and gaps), real
orphan objects, real byte tampering and real on-disk SQLite damage — no
success-path store/ledger mocks.  Fault injection is limited to
uncertainty modelling (a store subclass whose inventory raises a digest
-mless mismatch, monkeypatched ledger read failures, unsafe cabinet
occupants), matching the established backfill-test convention.

Covers freeze brief G1 §3 invariants 1-7: zero writes/fingerprints,
ledger-as-authority (old versions and gaps protected), absent/damaged/
foreign-scope conservatism, in-flight protection with malformed-input
refusal, corrupt objects poisoning the whole enumeration, deterministic
plan_digest, and concurrent read-only planning.
"""
from __future__ import annotations

import ast
import hashlib
import os
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.facts_tree.mapping_version_index import (
    LedgerIntegrityError,
    MappingVersionLedger,
    MappingVersionScopeError,
    UnsafeLedgerRootError,
)
from app.facts_tree.object_retention_plan import (
    CORRUPT_REASON_SHA256_MISMATCH,
    CORRUPT_REASON_UNSAFE_PATH,
    REASON_EXPLICIT,
    REASON_IN_FLIGHT,
    REASON_IN_LEDGER,
    REASON_LEDGER_UNAVAILABLE,
    REASON_UNREFERENCED,
    WARNING_CORRUPT_OBJECT_PREFIX,
    WARNING_INVENTORY_INCOMPLETE,
    WARNING_LEDGER_UNAVAILABLE,
    WARNING_UNSAFE_PREFIX,
    INVENTORY_OBJECT_MISSING,
    INVENTORY_UNEXPECTED_ENTRY,
    INVENTORY_UNSAFE_PATH,
    UNKNOWN_DIGEST,
    RetentionPlan,
    _extract_digest,
    plan_mapping_retention,
)
from app.storage.versioned_mapping_objects import (
    InvalidDigestError,
    ObjectMissingError,
    ObjectMismatchError,
    VersionedMappingObjectStore,
)

INSTALL = "inst-g1-retention"
COMPANY = "company-g1-retention"

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (REPO_ROOT / "app" / "facts_tree"
               / "object_retention_plan.py")


# -- real cabinet/ledger world ------------------------------------------------

@pytest.fixture
def world(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / "cabinet")
    ledger = MappingVersionLedger(
        tmp_path / "ledger",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=store)

    def register(source, data):
        digest = store.put(data)
        result = ledger.register_version(
            logical_source_id=source, digest=digest,
            size_bytes=len(data),
            installation_id=INSTALL, company_id=COMPANY)
        return digest, result

    def put_only(data):
        return store.put(data)

    def plan(**kwargs):
        kwargs.setdefault("installation_id", INSTALL)
        kwargs.setdefault("company_id", COMPANY)
        return plan_mapping_retention(
            store=store, ledger=ledger, **kwargs)

    yield {
        "tmp": tmp_path, "store": store, "ledger": ledger,
        "register": register, "put": put_only, "plan": plan,
    }
    ledger.close()


def _tamper_bytes(store, digest):
    """Flip the first byte of one real cabinet object (test-only write)."""
    path = store.root / digest[:2] / digest
    with open(path, "r+b") as handle:
        first = handle.read(1)
        handle.seek(0)
        handle.write(b"\x00" if first != b"\x00" else b"\x01")


def _reopen_damaged_ledger(world, *sql_statements):
    """Close the world ledger, damage its SQLite file, reopen same scope."""
    db_path = world["ledger"].database_path
    world["ledger"].close()
    connection = sqlite3.connect(db_path)
    try:
        for statement in sql_statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    damaged = MappingVersionLedger(
        world["tmp"] / "ledger",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=world["store"])
    world["ledger"] = damaged
    return damaged


def _by_digest(items):
    return {item.digest: item for item in items}


# -- §3.2 ledger is authority; old versions protected; orphans reclaimable ----

def test_classifies_registered_protected_and_single_orphan_reclaimable(world):
    a1, _ = world["register"]("srcA", b"alpha version one")
    a2, _ = world["register"]("srcA", b"alpha version two")
    b1, _ = world["register"]("srcB", b"beta version one")
    orphan = world["put"](b"nobody registered me")

    plan = world["plan"]()

    assert isinstance(plan, RetentionPlan)
    assert plan.protected_count == 3
    assert plan.reclaimable_count == 1
    assert plan.corrupt_count == 0
    protected = _by_digest(plan.protected)
    assert set(protected) == {a1, a2, b1}
    # Every historical version is protected, including the superseded v1.
    assert protected[a1].reasons == (REASON_IN_LEDGER,)
    assert protected[a2].reasons == (REASON_IN_LEDGER,)
    assert protected[a1].size_bytes == len(b"alpha version one")
    reclaimable = _by_digest(plan.reclaimable_candidates)
    assert set(reclaimable) == {orphan}
    assert reclaimable[orphan].reasons == (REASON_UNREFERENCED,)
    assert plan.corrupt == ()
    assert plan.warnings == ()
    assert plan.all_protected is False
    assert plan.ledger_digest == world["ledger"].ledger_digest()
    assert plan.ledger_digest.startswith("sha256:")
    assert plan.planned_at is None
    assert plan.protected_bytes + plan.reclaimable_bytes == plan.total_bytes


def test_registered_gap_digest_still_protection_authority(world):
    gapped, _ = world["register"]("srcA", b"later lost bytes")
    live, _ = world["register"]("srcA", b"still present bytes")
    # Real gap: the physical object is gone but the ledger row stands.
    os.unlink(world["store"].root / gapped[:2] / gapped)
    shape = world["ledger"].ledger_shape()
    assert shape["total_gaps"] == 1

    plan = world["plan"]()

    assert plan.warnings == ()
    assert [item.digest for item in plan.protected] == [live]
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True


def test_in_flight_digest_for_not_yet_present_object_is_not_an_error(world):
    world["register"]("srcA", b"registered one")
    future_digest = "f" * 64
    plan = world["plan"](in_flight_digests=[future_digest])
    # A protected digest with no physical object yet matches nothing.
    assert plan.reclaimable_count == 0
    assert plan.protected_count == 1
    assert plan.warnings == ()


def test_empty_cabinet_and_ledger_plan_is_clean(world):
    plan = world["plan"]()
    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    assert plan.corrupt == ()
    assert plan.warnings == ()
    assert plan.ledger_digest == world["ledger"].ledger_digest()
    assert plan.all_protected is True
    assert plan.total_bytes == 0
    assert plan.plan_digest.startswith("sha256:")


# -- §3.3 absent / damaged / foreign-scope ledger ------------------------------

def test_ledger_none_protects_every_object_with_warning(world):
    a1, _ = world["register"]("srcA", b"alpha")
    orphan = world["put"](b"orphan")
    plan = plan_mapping_retention(
        store=world["store"], ledger=None,
        installation_id=INSTALL, company_id=COMPANY)
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True
    assert plan.warnings == (WARNING_LEDGER_UNAVAILABLE,)
    protected = _by_digest(plan.protected)
    assert set(protected) == {a1, orphan}
    assert all(REASON_LEDGER_UNAVAILABLE in item.reasons
               for item in plan.protected)
    assert plan.ledger_digest is None


def test_ledger_none_plus_in_flight_hit_lists_both_reasons_sorted(world):
    orphan = world["put"](b"orphan in flight")
    plan = plan_mapping_retention(
        store=world["store"], ledger=None,
        in_flight_digests=[orphan],
        installation_id=INSTALL, company_id=COMPANY)
    item, = plan.protected
    assert item.reasons == (REASON_IN_FLIGHT, REASON_LEDGER_UNAVAILABLE)
    assert plan.reclaimable_candidates == ()
    assert plan.warnings == (WARNING_LEDGER_UNAVAILABLE,)


def test_foreign_scope_ledger_is_refused(world):
    world["register"]("srcA", b"alpha")
    with pytest.raises(MappingVersionScopeError):
        plan_mapping_retention(
            store=world["store"], ledger=world["ledger"],
            installation_id="inst-other", company_id=COMPANY)
    with pytest.raises(MappingVersionScopeError):
        plan_mapping_retention(
            store=world["store"], ledger=world["ledger"],
            installation_id=INSTALL, company_id="company-other")


def test_ledger_bound_to_foreign_scope_is_refused(tmp_path, world):
    foreign = MappingVersionLedger(
        tmp_path / "foreign-ledger",
        installation_id="inst-foreign", company_id="company-foreign",
        object_store=world["store"])
    try:
        with pytest.raises(MappingVersionScopeError):
            plan_mapping_retention(
                store=world["store"], ledger=foreign,
                installation_id=INSTALL, company_id=COMPANY)
    finally:
        foreign.close()


def test_row_integrity_failure_means_ledger_unavailable_all_protected(world):
    registered, _ = world["register"]("srcA", b"alpha")
    orphan = world["put"](b"orphan")
    # Real tampering: the append-only UPDATE trigger first has to go,
    # then a row column is changed so its row_sha256 no longer matches.
    damaged = _reopen_damaged_ledger(
        world,
        "DROP TRIGGER fact_mapping_version_index_reject_update",
        "UPDATE fact_mapping_version_registrations "
        "SET registered_at='tampered'",
    )
    with pytest.raises(LedgerIntegrityError):
        damaged.ledger_shape()

    plan = plan_mapping_retention(
        store=world["store"], ledger=damaged,
        installation_id=INSTALL, company_id=COMPANY)
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True
    assert plan.warnings == ("ledger_unavailable:integrity",)
    assert {item.digest for item in plan.protected} == {registered, orphan}
    assert len(plan.protected) == 2
    assert plan.ledger_digest is None


def test_sqlite_database_error_is_classified(world, monkeypatch):
    # A missing table at reopen time is turned into LedgerIntegrityError
    # by ledger construction itself, so the planner's sqlite3.DatabaseError
    # branch is exercised by fault injection on a real ledger, matching
    # the unsafe-root/oserror cases.
    world["register"]("srcA", b"alpha")

    def _raise_database(*args, **kwargs):
        raise sqlite3.OperationalError("injected database disk error")

    monkeypatch.setattr(world["ledger"], "ledger_shape", _raise_database)
    plan = world["plan"]()
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True
    assert plan.warnings == ("ledger_unavailable:database",)


def test_unsafe_ledger_root_read_failure_is_classified(world, monkeypatch):
    world["register"]("srcA", b"alpha")

    def _raise_unsafe(*args, **kwargs):
        raise UnsafeLedgerRootError("injected unsafe-root read failure")

    monkeypatch.setattr(world["ledger"], "ledger_shape", _raise_unsafe)
    plan = world["plan"]()
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True
    assert plan.warnings == ("ledger_unavailable:unsafe_root",)


def test_ledger_oserror_is_classified(world, monkeypatch):
    world["register"]("srcA", b"alpha")

    def _raise_oserror(*args, **kwargs):
        raise OSError("injected io failure")

    monkeypatch.setattr(world["ledger"], "ledger_shape", _raise_oserror)
    plan = world["plan"]()
    assert plan.reclaimable_candidates == ()
    assert plan.warnings == ("ledger_unavailable:oserror",)


def test_scope_and_argument_shapes_are_validated(world, tmp_path):
    with pytest.raises(TypeError):
        plan_mapping_retention(
            store=object(), ledger=world["ledger"],
            installation_id=INSTALL, company_id=COMPANY)
    other_store = VersionedMappingObjectStore(tmp_path / "other-cabinet")
    with pytest.raises(TypeError):
        plan_mapping_retention(
            store=other_store, ledger=object(),
            installation_id=INSTALL, company_id=COMPANY)
    with pytest.raises(ValueError):
        plan_mapping_retention(
            store=other_store, ledger=None,
            installation_id="", company_id=COMPANY)
    with pytest.raises(ValueError):
        plan_mapping_retention(
            store=other_store, ledger=None,
            installation_id=INSTALL, company_id=COMPANY,
            planned_at=12345)
    with pytest.raises(ValueError):
        plan_mapping_retention(
            store=other_store, ledger=None,
            installation_id=INSTALL, company_id=COMPANY,
            planned_at="")


# -- §3.4 in-flight/explicit protection and malformed-input refusal -----------

def test_in_flight_and_explicit_protection_with_multi_hit_reasons(world):
    a1, _ = world["register"]("srcA", b"alpha")
    flight_orphan = world["put"](b"in flight orphan")
    extra_orphan = world["put"](b"explicitly reserved orphan")
    plan = world["plan"](
        in_flight_digests=[a1, flight_orphan],
        extra_protected=[a1, extra_orphan])
    protected = _by_digest(plan.protected)
    # Triple hit: all reasons, sorted.
    assert protected[a1].reasons == (
        REASON_EXPLICIT, REASON_IN_FLIGHT, REASON_IN_LEDGER)
    assert protected[flight_orphan].reasons == (REASON_IN_FLIGHT,)
    assert protected[extra_orphan].reasons == (REASON_EXPLICIT,)
    assert plan.reclaimable_candidates == ()


def test_duplicate_digests_and_generator_input_are_accepted(world):
    orphan = world["put"](b"gen orphan")
    plan = world["plan"](
        in_flight_digests=(d for d in [orphan, orphan]),
        extra_protected={orphan})
    item, = plan.protected
    assert item.reasons == (REASON_EXPLICIT, REASON_IN_FLIGHT)


@pytest.mark.parametrize("bad", [
    "g" * 64,            # uppercase hex
    "0" * 63,            # too short
    "0" * 65,            # too long
    "x" * 64,            # non-hex
    "",
    123,
    None,
    b"a" * 64,           # bytes, not str
    "a" * 64,            # a bare 64-hex string, not an iterable of digests
    ["a" * 64, 5],
    ["a" * 64, "z" * 64],
])
def test_malformed_in_flight_digests_are_rejected(world, bad):
    with pytest.raises(InvalidDigestError):
        world["plan"](in_flight_digests=bad)


@pytest.mark.parametrize("bad", [
    "b" * 64,
    [object()],
    42,
])
def test_malformed_extra_protected_digests_are_rejected(world, bad):
    with pytest.raises(InvalidDigestError):
        world["plan"](extra_protected=bad)


# -- §3.5 corrupt objects never reach reclaimable; poison semantics ------------

def test_corrupt_registered_object_poisons_whole_plan(world):
    bad, _ = world["register"]("srcA", b"soon corrupted registered")
    good, _ = world["register"]("srcB", b"untouched registered")
    orphan = world["put"](b"untouched orphan")
    _tamper_bytes(world["store"], bad)

    plan = world["plan"]()

    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    assert plan.all_protected is True
    corrupt, = plan.corrupt
    assert corrupt.digest == bad
    assert corrupt.size_bytes is None
    assert corrupt.reasons == (CORRUPT_REASON_SHA256_MISMATCH,)
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_CORRUPT_OBJECT_PREFIX + bad,
    )))
    # The ledger was read fine before the enumeration abort.
    assert plan.ledger_digest == world["ledger"].ledger_digest()
    # The planner never repaired anything: good/orphan bytes stay put.
    assert world["store"].get(good) == b"untouched registered"
    assert world["store"].get(orphan) == b"untouched orphan"


def test_corrupt_orphan_object_poisons_whole_plan(world):
    bad = world["put"](b"soon corrupted orphan")
    _tamper_bytes(world["store"], bad)
    plan = world["plan"]()
    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    corrupt, = plan.corrupt
    assert corrupt.digest == bad
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_CORRUPT_OBJECT_PREFIX + bad,
    )))


def test_corrupt_object_without_extractable_digest_still_zero_reclaim(
        world, tmp_path):
    # Fault-injection subclass of the REAL store (real cabinet root on
    # disk): inventory fails closed with a message carrying no 64-hex
    # token.  The digest must stay None and nothing may be reclaimed.
    real_store = world["store"]
    world["register"]("srcA", b"alpha")

    class _MysteryMismatchStore(VersionedMappingObjectStore):
        def inventory(self):
            raise ObjectMismatchError(
                "sha256 mismatch with no address token in text")

    mystery = _MysteryMismatchStore(real_store.root)
    plan = plan_mapping_retention(
        store=mystery, ledger=world["ledger"],
        installation_id=INSTALL, company_id=COMPANY)
    assert plan.reclaimable_candidates == ()
    assert plan.protected == ()
    assert plan.all_protected is True
    corrupt, = plan.corrupt
    assert corrupt.digest is None
    assert corrupt.size_bytes is None
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_CORRUPT_OBJECT_PREFIX + UNKNOWN_DIGEST,
    )))


def test_extract_digest_is_best_effort_only():
    token = "a" * 64
    # Real cabinet exception wording carries the address.
    assert _extract_digest(
        f"sha256 mismatch for object {token}") == token
    assert _extract_digest(f"object {token} is bad") == token
    assert _extract_digest("no token here") is None
    assert _extract_digest("") is None
    assert _extract_digest(None) is None
    assert _extract_digest(123) is None
    # A 65-hex run is not an exact 64-hex token: never guessed/truncated.
    assert _extract_digest("x" + "a" * 65) is None
    # The first exact token wins.
    assert _extract_digest(f"{token} and {'b' * 64}") == token


def test_symlinked_object_occupant_is_corrupt_unsafe(world):
    victim, _ = world["register"]("srcA", b"real object gives us a shard")
    target = world["tmp"] / "outside-target"
    target.write_bytes(b"not an object")
    shard = world["store"].root / victim[:2]
    # The rogue name must match its shard prefix or inventory rejects it
    # as an UnexpectedEntry before it ever opens the occupant.
    rogue = victim[:2] + "9" * 62
    os.symlink(target, shard / rogue)
    plan = world["plan"]()
    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    corrupt, = plan.corrupt
    assert corrupt.digest == rogue
    assert corrupt.reasons == (CORRUPT_REASON_UNSAFE_PATH,)
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_UNSAFE_PREFIX + INVENTORY_UNSAFE_PATH,
    )))


def test_unexpected_entry_poisons_listing_without_naming_corrupt(world):
    victim, _ = world["register"]("srcA", b"shard anchor")
    shard = world["store"].root / victim[:2]
    (shard / ".object-1-deadbeef.tmp").write_bytes(b"x")
    plan = world["plan"]()
    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    assert plan.corrupt == ()
    assert plan.all_protected is True
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_UNSAFE_PREFIX + INVENTORY_UNEXPECTED_ENTRY,
    )))


def test_enumerated_entry_vanishing_mid_run_is_zero_reclaim(world):
    # Fault-injection subclass on the real cabinet root: an enumerated
    # object that vanishes before its verified read aborts the listing.
    real_store = world["store"]
    world["register"]("srcA", b"alpha")
    token = "ab" + "7" * 62

    class _VanishingStore(VersionedMappingObjectStore):
        def inventory(self):
            raise ObjectMissingError(token)

    vanishing = _VanishingStore(real_store.root)
    plan = plan_mapping_retention(
        store=vanishing, ledger=world["ledger"],
        installation_id=INSTALL, company_id=COMPANY)
    assert plan.protected == ()
    assert plan.reclaimable_candidates == ()
    assert plan.corrupt == ()
    assert plan.all_protected is True
    assert plan.warnings == tuple(sorted((
        WARNING_INVENTORY_INCOMPLETE,
        WARNING_UNSAFE_PREFIX + INVENTORY_OBJECT_MISSING,
    )))


def test_ledger_unavailable_and_inventory_abort_warnings_combine(world):
    bad = world["put"](b"bad under no ledger")
    _tamper_bytes(world["store"], bad)
    plan = plan_mapping_retention(
        store=world["store"], ledger=None,
        installation_id=INSTALL, company_id=COMPANY)
    assert plan.reclaimable_candidates == ()
    assert plan.protected == ()
    corrupt, = plan.corrupt
    assert corrupt.digest == bad
    assert plan.warnings == tuple(sorted(plan.warnings))
    assert WARNING_LEDGER_UNAVAILABLE in plan.warnings
    assert WARNING_INVENTORY_INCOMPLETE in plan.warnings


# -- §3.6 determinism ----------------------------------------------------------

def test_plan_is_deterministic_across_runs_and_input_order(world):
    a1, _ = world["register"]("srcA", b"alpha")
    a2, _ = world["register"]("srcA", b"alpha two")
    flight = world["put"](b"flight")
    extra = world["put"](b"extra")
    first = world["plan"](
        in_flight_digests=[flight, a1, extra],
        extra_protected=[extra, flight, a1],
        planned_at="2026-11-02T00:00:00+00:00")
    second = world["plan"](
        in_flight_digests={extra, flight, a1},
        extra_protected=(a1, extra, flight),
        planned_at="2026-11-02T00:00:00+00:00")
    assert first == second
    assert first.plan_digest == second.plan_digest
    digests = [item.digest for item in first.protected]
    assert digests == sorted(digests)
    assert first.warnings == tuple(sorted(first.warnings))


def test_plan_digest_changes_with_scope_and_planned_at(world):
    world["register"]("srcA", b"alpha")
    base = world["plan"]()
    other_scope = plan_mapping_retention(
        store=world["store"], ledger=None,
        installation_id="inst-other", company_id=COMPANY)
    stamped = world["plan"](planned_at="2026-11-02T00:00:00+00:00")
    assert base.plan_digest != other_scope.plan_digest
    assert base.plan_digest != stamped.plan_digest
    assert stamped.planned_at == "2026-11-02T00:00:00+00:00"
    # plan_digest is exactly the digest of the exposed canonical body.
    from app.facts_tree.backup_manifest import compute_core_digest
    assert stamped.plan_digest == compute_core_digest(
        stamped.canonical_payload())


# -- §3.1 zero writes: filesystem + git fingerprints ---------------------------

def _tree_fingerprint(root):
    fingerprint = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            fingerprint[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
            )
    return fingerprint


def _git_status_porcelain():
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return result.stdout


def test_planning_writes_nothing_to_cabinet_ledger_or_git(world):
    world["register"]("srcA", b"alpha one")
    world["register"]("srcA", b"alpha two")
    world["register"]("srcB", b"beta")
    world["put"](b"orphan one")
    world["put"](b"orphan two")
    world["plan"]()
    before = (
        _tree_fingerprint(world["store"].root),
        _tree_fingerprint(world["tmp"] / "ledger"),
        _git_status_porcelain(),
    )
    for _ in range(5):
        plan = world["plan"](in_flight_digests=[], extra_protected=[])
        assert plan.reclaimable_count == 2
    after = (
        _tree_fingerprint(world["store"].root),
        _tree_fingerprint(world["tmp"] / "ledger"),
        _git_status_porcelain(),
    )
    assert before == after
    # Re-inventory after planning: every object still byte-verifies.
    assert len(world["store"].inventory()) == 5


# -- §3.7 concurrent read-only planning against a live writer ------------------

def test_concurrent_planning_survives_register_and_put_writer(world):
    stop = threading.Event()
    failures = []

    def writer():
        try:
            for index in range(120):
                data = f"concurrent payload {index}".encode()
                digest = world["store"].put(data)
                world["ledger"].register_version(
                    logical_source_id=f"src{index % 4}",
                    digest=digest, size_bytes=len(data),
                    installation_id=INSTALL, company_id=COMPANY)
                # Stretch the interleaving window so the reader overlaps
                # many put/register cycles instead of racing a millisecond.
                time.sleep(0.002)
        except BaseException as exc:  # surfaced as a test failure
            failures.append(("writer", exc))

    plans_seen = []

    def reader():
        deadline = time.monotonic() + 30
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                plan = world["plan"]()
                plans_seen.append(plan)
                # Self-consistency: disjoint classification, every
                # reclaimable item is exactly "unreferenced", and a
                # poisoned enumeration never also names reclaimables.
                protected_d = {i.digest for i in plan.protected}
                reclaim_d = {i.digest for i in plan.reclaimable_candidates}
                assert not (protected_d & reclaim_d)
                assert all(
                    i.reasons == (REASON_UNREFERENCED,)
                    for i in plan.reclaimable_candidates)
                if plan.corrupt or any(
                        w == WARNING_INVENTORY_INCOMPLETE
                        for w in plan.warnings):
                    assert plan.reclaimable_candidates == ()
        except BaseException as exc:  # surfaced as a test failure
            failures.append(("reader", exc))

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=30)
    stop.set()
    reader_thread.join(timeout=30)
    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert failures == []
    # The reader must actually have overlapped the writer's window.
    assert len(plans_seen) >= 10
    final = world["plan"]()
    assert final.corrupt == ()
    assert final.protected_count >= 120


# -- sealed sockets -------------------------------------------------------------

def test_planning_with_all_sockets_sealed(world, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("retention planning must not use the network")

    class SealedSocket(socket.socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("no connections")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("no connections")

    monkeypatch.setattr(socket, "socket", SealedSocket)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    world["register"]("srcA", b"alpha")
    world["put"](b"orphan")
    plan = world["plan"]()
    assert plan.protected_count == 1
    assert plan.reclaimable_count == 1
    none_plan = plan_mapping_retention(
        store=world["store"], ledger=None,
        installation_id=INSTALL, company_id=COMPANY)
    assert none_plan.all_protected is True


# -- §2.6 module purity: import surface and forbidden primitives ---------------

def _resolved_imports(path, node, app_root):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        package = ["app"] + list(
            path.parent.relative_to(app_root).parts)
        base = package[:len(package) - node.level + 1]
        return [".".join(base + ([module] if module else []))]
    return [module] if module else []


def test_module_import_surface_is_exact_read_only_set():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    app_root = REPO_ROOT / "app"
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(
                _resolved_imports(MODULE_PATH, node, app_root))
    assert imported == {
        "__future__",
        "re",
        "sqlite3",
        "dataclasses",
        "app.facts_tree.backup_manifest",
        "app.facts_tree.mapping_version_index",
        "app.storage.versioned_mapping_objects",
    }


def test_module_has_no_delete_write_network_or_dynamic_import_primitives():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_attributes = {
        "unlink", "remove", "removedirs", "rmdir", "mkdir", "makedirs",
        "rename", "renames", "replace", "symlink", "link", "write",
        "writelines", "truncate", "chmod", "chown", "open", "connect",
        "connect_ex", "send", "sendto", "sendfile", "execute",
        "executescript", "executemany", "system", "popen", "fork",
        "bind", "listen", "create_connection",
    }
    forbidden_call_names = {
        "open", "exec", "eval", "compile", "__import__", "globals",
        "vars", "memoryview",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attributes, node.attr
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)):
            assert node.func.id not in forbidden_call_names, node.func.id
        if isinstance(node, ast.Name):
            assert node.id not in {
                "__import__", "socket", "subprocess"}, node.id
    assert "__import__" not in source
    # sqlite3 may be imported for exception classification, but the
    # module may only name the DatabaseError type: no connect, no cursor,
    # no SQL.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "sqlite3"):
            assert node.attr == "DatabaseError", node.attr
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "sqlite3"):
            raise AssertionError("sqlite3 must never be called")
