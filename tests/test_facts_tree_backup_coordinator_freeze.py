"""F-001 vault assembly slice 1: in-barrier consistent-cut freeze tests.

Acceptance drills for appendix C1 "保险库总装第1片——屏障内冻结" against
``app.facts_tree.backup_coordinator``. Everything here is real: a real
Git authority repository (sha1 and sha256 physical formats), real WAL
SQLite work databases with real company-id rows, the real rank-1
maintenance flock contended by a real broker ``FINAL_ACCEPT`` entrant
in another thread, real publication-fault injection into the
checkpoint primitive, and a real on-disk host journal. Nothing uses
the network, model calls, SAM runtime data or real company data.

The mapping to the brief's section 5 acceptance list (1-13):

1  test_barrier_blocks_real_broker_final_accept_until_release
2  test_frozen_cut_fixes_H_pin_components_and_core_digest
2b test_pin_and_cut_stay_at_H_after_ref_facts_advances
3  test_second_w_failure_is_all_or_nothing_and_old_cut_intact
4  test_uncertain_publication_is_incomplete_with_debris_retained
   test_confirmed_checkpoint_cleanup_failure_failed_but_retained
5  test_preheld_lock_fails_within_budget_distinguishable
6  test_transaction_open_before_barrier_raises_broker_lock_order_error
   test_nested_coordination_frame_raises_broker_lock_order_error
7  test_cancel_during_wait_aborts_bounded_and_releases_barrier
   test_hold_budget_expiry_aborts_before_pin_and_releases_barrier
8  test_duplicate_backup_id_rejected_without_second_cut
   test_preparing_leftover_is_recognizable_and_blocks_repeat
9  test_physical_scope_mismatch_stops_before_barrier_and_lists_database
10 test_components_are_independent_backup_api_artifacts
11 test_freeze_succeeds_with_all_sockets_disabled
12 test_module_imports_are_offline_and_model_free
   test_source_connections_left_idle_wal_and_writable
   test_retention_root_symlink_refused
   test_retention_root_group_or_world_writable_refused
13 is a review criterion ("no padding tests"), not a test case.
"""

import ast
import hashlib
import os
import socket
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from app.facts_tree import backup_coordinator as coord
from app.facts_tree import backup_sqlite_checkpoint as checkpoint_module
from app.facts_tree import git_format
from app.facts_tree.backup_coordinator import (
    AssemblyIdentity,
    AssemblyValidationError,
    BarrierBudget,
    BarrierUnavailableError,
    FreezeAlreadyRecordedError,
    FreezeAssembly,
    FreezeCancelled,
    FROZEN,
    FAILED,
    INCOMPLETE,
    ScopeMismatchError,
    WorkDatabaseSpec,
    freeze_consistent_cut,
    list_freeze_records,
    read_freeze_record,
)
from app.facts_tree.backup_component_validation import verify_backup_component
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.broker_coordination import (
    FINAL_ACCEPT,
    TASK_CANCEL,
    BrokerCoordination,
    LockOrderError as BrokerLockOrderError,
)
from app.facts_tree.git_repository import (
    FactsGitRepository,
    maintenance_pin_ref,
)

_IDS = {
    "repo_id": "repo-c1-0001",
    "installation_id": "inst-c1-0001",
    "company_id": "co-c1-0001",
}
_IDENTITY = AssemblyIdentity(**_IDS)
_OTHER_COMPANY = "co-other-9999"
_MARKER_PATH = git_format.record_path(
    "fact_current", fact_uid="c1-post-cut-marker"
)


# ---------------------------------------------------------------------------
# Fixtures and synthetic builders
# ---------------------------------------------------------------------------


def _repo(tmp_path, object_format):
    return FactsGitRepository.initialize(
        tmp_path / "facts.git",
        object_format=object_format,
        **_IDS,
    )


@pytest.fixture
def retention_root(tmp_path):
    root = tmp_path / "retention"
    root.mkdir(0o700)
    return root


@pytest.fixture
def coordination(tmp_path):
    # check_same_thread off because the barrier drills drive a real
    # broker entrant from a second thread against this ledger.
    db = sqlite3.connect(":memory:", check_same_thread=False)
    coordination = BrokerCoordination(
        tmp_path / "domain", _IDS["repo_id"], db
    )
    yield coordination
    db.close()


def _work_database(directory, role, *, companies=(_IDS["company_id"],),
                   orphan_foreign_key=False):
    """One real WAL work database carrying physical company-id evidence."""
    source_path = directory / (role + ".db")
    connection = sqlite3.connect(source_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE work_state ("
        "id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, payload TEXT)"
    )
    connection.execute(
        "CREATE TABLE work_parent (id INTEGER PRIMARY KEY)"
    )
    connection.execute(
        "CREATE TABLE work_child ("
        "id INTEGER PRIMARY KEY, pid INTEGER REFERENCES work_parent(id))"
    )
    for index, company in enumerate(companies, start=1):
        connection.execute(
            "INSERT INTO work_state VALUES (?, ?, ?)",
            (index, company, "payload-%d" % index),
        )
    if orphan_foreign_key:
        connection.execute("INSERT INTO work_child VALUES (99, 999)")
    connection.commit()

    def scope_probe(conn):
        return tuple(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT company_id FROM work_state ORDER BY 1"
            )
        )

    spec = WorkDatabaseSpec(
        role=role,
        connection=connection,
        scope_probe=scope_probe,
        schema={"tables": ["work_state", "work_parent", "work_child"]},
        scope_watermark={"max_id": len(companies)},
    )
    return spec, connection, source_path


def _assembly(backup_id, coordination, repository, specs, retention_root,
              *, wait_timeout=5.0, hold_timeout=30.0, cancel_event=None,
              poll_interval=0.01):
    return FreezeAssembly(
        backup_id=backup_id,
        identity=_IDENTITY,
        coordination=coordination,
        repository=repository,
        databases=tuple(specs),
        retention_root=retention_root,
        budget=BarrierBudget(wait_timeout, hold_timeout, poll_interval),
        cancel_event=cancel_event,
    )


def _append_marker_commit(repository):
    """A real formal writer append: marker record accepted onto REF_FACTS."""
    old = repository.head_oid()
    snapshot = repository.read_snapshot_at(old)
    records = [(record.path, record.content) for record in snapshot.records]
    records.append((_MARKER_PATH, git_format.dumps_record({"marker": True})))
    expected_paths = sorted({record.path for record in snapshot.records}
                            | {_MARKER_PATH})
    tree = repository.build_tree(records, expected_paths=expected_paths)
    commit = repository.create_commit(tree, old)
    accepted = repository.accept(old, commit, expected_paths=expected_paths)
    assert accepted.accepted
    return commit


def _hold_maintenance(coordination, released):
    with coordination.maintenance_admission():
        released.wait(10)


def _cut_files(evidence):
    return sorted(path.name for path in evidence.cut_dir.iterdir())


def _journal_detail(retention_root, backup_id):
    record = read_freeze_record(retention_root, backup_id)
    assert record is not None
    return record.state, record.detail


# ---------------------------------------------------------------------------
# 1. The barrier really blocks a formal broker acceptance
# ---------------------------------------------------------------------------


def test_barrier_blocks_real_broker_final_accept_until_release(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    observed = {"during_barrier": []}
    entered = threading.Event()
    claimed = threading.Event()
    done = threading.Event()
    writer_errors = []

    spec, connection, _ = _work_database(tmp_path, "w1")
    inside_barrier = threading.Event()
    probes = {"count": 0}

    def observing_probe(conn):
        rows = tuple(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT company_id FROM work_state ORDER BY 1"
            )
        )
        probes["count"] += 1
        if probes["count"] == 2:
            # This invocation runs inside the held rank-1 barrier:
            # release the formal writer and prove it cannot pass
            # admission while the barrier is held.
            inside_barrier.set()
            assert claimed.wait(5)
            entered.wait(0.3)
            observed["during_barrier"].append(entered.is_set())
        return rows

    object.__setattr__(spec, "scope_probe", observing_probe)

    ready = threading.Event()

    def formal_writer():
        try:
            # The broker lease is long-lived and claimed while the
            # rank-1 barrier is still free (the normal steady state).
            lease = coordination.claim_broker()
            ready.set()
            # Only attempt final admission once the freeze is
            # demonstrably inside its barrier hold (rendezvous set
            # from the in-barrier probe): admit queues on the flock.
            assert inside_barrier.wait(5)
            claimed.set()
            with coordination.admit(FINAL_ACCEPT, lease=lease) as gate:
                with gate.work_transaction():
                    pass
                entered.set()
                # The formal writer's real authority mutation.
                _append_marker_commit(repository)
        except BaseException as exc:  # surfaced in the main thread
            writer_errors.append(exc)
        finally:
            done.set()

    writer = threading.Thread(target=formal_writer)
    writer.start()
    assert ready.wait(5)
    try:
        evidence = freeze_consistent_cut(_assembly(
            "bkp-barrier-1", coordination, repository, [spec],
            retention_root,
        ))

        assert evidence.frozen
        # The in-barrier probe saw the writer still queued.
        assert observed["during_barrier"] == [False]

        frozen_H = evidence.authority.commit_oid
        assert repository.head_oid() == frozen_H
        assert done.wait(10)
        assert writer_errors == []

        # After release the writer's commit lands, but the frozen cut
        # stays at H: H does not contain its marker.
        new_head = repository.head_oid()
        assert new_head != frozen_H
        frozen_snapshot = repository.read_snapshot_at(frozen_H)
        assert _MARKER_PATH not in {
            record.path for record in frozen_snapshot.records
        }
        pin = repository.read_maintenance_pin(
            "bkp-barrier-1", evidence.barrier_id
        )
        assert pin is not None and pin.commit_oid == frozen_H
    finally:
        writer.join(5)
        connection.close()


# ---------------------------------------------------------------------------
# 2. Consistent cut: pin target == H, every component verifies, digest fixed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_frozen_cut_fixes_H_pin_components_and_core_digest(
        tmp_path, coordination, retention_root, object_format):
    repository = _repo(tmp_path, object_format)
    spec_a, conn_a, path_a = _work_database(tmp_path, "w_items")
    spec_b, conn_b, path_b = _work_database(
        tmp_path, "w_ledger", companies=(_IDS["company_id"],)
    )

    backup_id = "bkp-cut-" + object_format
    evidence = freeze_consistent_cut(_assembly(
        backup_id, coordination, repository, [spec_a, spec_b],
        retention_root,
    ))

    assert evidence.status == FROZEN
    assert evidence.reason is None
    head_H = repository.head_oid()
    assert evidence.authority.commit_oid == head_H
    assert evidence.authority.object_format == object_format
    assert evidence.authority.pin_ref == maintenance_pin_ref(
        backup_id, evidence.barrier_id
    )
    pin = repository.read_maintenance_pin(backup_id, evidence.barrier_id)
    assert pin.commit_oid == head_H

    # Independent re-verification of every W through the strict reader.
    cut_fd = os.open(str(evidence.cut_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _cut_files(evidence) == ["w_items.sqlite", "w_ledger.sqlite"]
        for item in evidence.checkpoints:
            verified = verify_backup_component(
                cut_fd,
                relative_path=item.role + ".sqlite",
                size_bytes=item.size_bytes,
                sha256_digest=item.sha256_digest,
            )
            assert verified.relative_path == item.role + ".sqlite"
    finally:
        os.close(cut_fd)

    # Digest values are independently recomputed from the actual bytes.
    for item, source_path in ((evidence.checkpoints[0], path_a),
                              (evidence.checkpoints[1], path_b)):
        artifact = evidence.cut_dir / (item.role + ".sqlite")
        payload = artifact.read_bytes()
        assert item.size_bytes == len(payload)
        assert item.sha256_digest == (
            "sha256:" + hashlib.sha256(payload).hexdigest()
        )
        assert artifact.stat().st_ino != source_path.stat().st_ino
        mode = stat.S_IMODE(artifact.stat().st_mode)
        assert mode == 0o600

    # The core digest fixes the canonical core payload recorded in the
    # host journal; recomputing it yields exactly the evidence digest.
    record = read_freeze_record(retention_root, backup_id)
    assert record.state == FROZEN
    assert record.barrier_id == evidence.barrier_id
    assert record.detail["reason"] == "frozen"
    assert compute_core_digest(record.detail["core_payload"]) == \
        evidence.core_digest
    assert evidence.core_digest.startswith("sha256:")

    conn_a.close()
    conn_b.close()


# ---------------------------------------------------------------------------
# 2b. REF_FACTS advances afterwards; pin and evidence stay at old H
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_pin_and_cut_stay_at_H_after_ref_facts_advances(
        tmp_path, coordination, retention_root, object_format):
    repository = _repo(tmp_path, object_format)
    spec, connection, _ = _work_database(tmp_path, "w1")

    evidence = freeze_consistent_cut(_assembly(
        "bkp-stay-" + object_format, coordination, repository, [spec],
        retention_root,
    ))
    frozen_H = evidence.authority.commit_oid

    advanced = _append_marker_commit(repository)
    assert advanced != frozen_H
    assert repository.head_oid() == advanced

    pin = repository.read_maintenance_pin(
        "bkp-stay-" + object_format, evidence.barrier_id
    )
    assert pin.commit_oid == frozen_H
    assert evidence.authority.commit_oid == frozen_H
    # The frozen files remain exactly in place.
    assert (evidence.cut_dir / "w1.sqlite").is_file()
    connection.close()


# ---------------------------------------------------------------------------
# 3. A failing second W: all-or-nothing, old backup intact, lock released
# ---------------------------------------------------------------------------


def test_second_w_failure_is_all_or_nothing_and_old_cut_intact(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    good_a, conn_a, _ = _work_database(tmp_path, "w1")
    good_b, conn_b, _ = _work_database(tmp_path, "w2")
    bad_spec, conn_bad, _ = _work_database(
        tmp_path, "w3", orphan_foreign_key=True
    )

    first = freeze_consistent_cut(_assembly(
        "bkp-ok-1", coordination, repository, [good_a, good_b],
        retention_root,
    ))
    assert first.frozen
    first_files = sorted(
        (path.name, path.stat().st_size,
         hashlib.sha256(path.read_bytes()).hexdigest())
        for path in first.cut_dir.iterdir()
    )

    second = freeze_consistent_cut(_assembly(
        "bkp-fail-2", coordination, repository, [good_a, bad_spec],
        retention_root,
    ))
    assert second.status == FAILED
    assert second.reason == "checkpoint-failed"
    assert second.core_digest is None
    assert second.cut_dir is None

    # No completed position for the failed identity.
    assert not (retention_root / "cuts" / "bkp-fail-2").exists()
    # The good W1 file survives as private-staging debris for inspection.
    debris = list((retention_root / ".staging").rglob("w1.sqlite"))
    assert len(debris) == 1 and debris[0].is_file()
    assert not list((retention_root / ".staging").rglob("w3.sqlite"))

    # The old frozen cut is byte-identical and still verifies.
    old_files = sorted(
        (path.name, path.stat().st_size,
         hashlib.sha256(path.read_bytes()).hexdigest())
        for path in first.cut_dir.iterdir()
    )
    assert old_files == first_files

    state, detail = _journal_detail(retention_root, "bkp-fail-2")
    assert state == FAILED
    assert detail["failing_role"] == "w3"
    assert detail["publication"] == "absent"

    # Lock is released: a fresh formal entrant passes immediately.
    entered = threading.Event()
    with coordination.admit(TASK_CANCEL) as gate:
        with gate.work_transaction():
            entered.set()
    assert entered.is_set()

    for connection in (conn_a, conn_b, conn_bad):
        connection.close()


# ---------------------------------------------------------------------------
# 4. UNCERTAIN and CONFIRMED checkpoint publication faults
# ---------------------------------------------------------------------------


def _fail_all_directory_fsyncs(monkeypatch):
    real_fsync = coord.os.fsync

    def fail_on_directories(file_descriptor):
        if stat.S_ISDIR(coord.os.fstat(file_descriptor).st_mode):
            raise OSError(5, "simulated directory fsync failure")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(checkpoint_module.os, "fsync", fail_on_directories)


def test_uncertain_publication_is_incomplete_with_debris_retained(
        tmp_path, coordination, retention_root, monkeypatch):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    backup_id = "bkp-uncertain-1"

    _fail_all_directory_fsyncs(monkeypatch)
    real_unlink = coord.os.unlink

    def block_destination_unlink(name, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and name == "w1.sqlite":
            raise OSError(1, "simulated rollback unlink failure")
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(
        checkpoint_module.os, "unlink", block_destination_unlink
    )

    calls = []
    real_create = coord.create_sqlite_checkpoint

    def counting_create(source, destination):
        calls.append(Path(destination).name)
        return real_create(source, destination)

    monkeypatch.setattr(coord, "create_sqlite_checkpoint", counting_create)

    evidence = freeze_consistent_cut(_assembly(
        backup_id, coordination, repository, [spec], retention_root,
    ))

    assert evidence.status == INCOMPLETE
    assert evidence.reason == "checkpoint-publication-uncertain"
    assert evidence.cut_dir is None
    assert evidence.core_digest is None
    # Exactly one attempt: no automatic retry.
    assert calls == ["w1.sqlite"]
    # The possibly-existing link is retained as debris for inspection,
    # and no completed position exists.
    assert not (retention_root / "cuts").exists()
    debris = list((retention_root / ".staging").rglob("w1.sqlite"))
    assert len(debris) == 1 and debris[0].is_file()
    restored = sqlite3.connect(debris[0])
    try:
        assert restored.execute(
            "SELECT company_id FROM work_state"
        ).fetchall() == [(_IDS["company_id"],)]
    finally:
        restored.close()
    state, detail = _journal_detail(retention_root, backup_id)
    assert state == INCOMPLETE
    assert detail["reason"] == "checkpoint-publication-uncertain"
    connection.close()


def test_confirmed_checkpoint_cleanup_failure_failed_but_retained(
        tmp_path, coordination, retention_root, monkeypatch):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    backup_id = "bkp-confirmed-1"

    real_unlink = coord.os.unlink

    def block_snapshot_unlink(name, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and isinstance(name, str) \
                and name.endswith(".tmp"):
            raise OSError(1, "simulated snapshot removal failure")
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(
        checkpoint_module.os, "unlink", block_snapshot_unlink
    )

    evidence = freeze_consistent_cut(_assembly(
        backup_id, coordination, repository, [spec], retention_root,
    ))

    assert evidence.status == FAILED
    assert evidence.reason == "checkpoint-staging-cleanup-failed"
    assert evidence.cut_dir is None
    debris = list((retention_root / ".staging").rglob("w1.sqlite"))
    assert len(debris) == 1 and debris[0].is_file()
    restored = sqlite3.connect(debris[0])
    try:
        assert restored.execute("PRAGMA integrity_check").fetchall() == \
            [("ok",)]
    finally:
        restored.close()
    state, detail = _journal_detail(retention_root, backup_id)
    assert state == FAILED
    assert detail["publication"] == "confirmed"
    connection.close()


# ---------------------------------------------------------------------------
# 5. Pre-held lock: fail within budget, distinguishable, no degradation
# ---------------------------------------------------------------------------


def test_preheld_lock_fails_within_budget_distinguishable(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    released = threading.Event()
    holder = threading.Thread(
        target=_hold_maintenance, args=(coordination, released)
    )
    holder.start()
    time.sleep(0.2)
    try:
        started = time.monotonic()
        with pytest.raises(BarrierUnavailableError) as record:
            freeze_consistent_cut(_assembly(
                "bkp-locked-1", coordination, repository, [spec],
                retention_root, wait_timeout=0.3,
            ))
        elapsed = time.monotonic() - started
        # Distinguishable typed failure (not an ordinary evidence row),
        # and bounded by the wait budget rather than the holder.
        assert isinstance(record.value, BarrierUnavailableError)
        assert elapsed < 2.0

        state, detail = _journal_detail(retention_root, "bkp-locked-1")
        assert state == INCOMPLETE
        assert detail["reason"] == "barrier-timeout"
        assert not (retention_root / "cuts").exists()
        assert not (retention_root / ".staging").exists()
    finally:
        released.set()
        holder.join(5)

    # After release the barrier is usable: a new identity freezes.
    evidence = freeze_consistent_cut(_assembly(
        "bkp-locked-2", coordination, repository, [spec], retention_root,
    ))
    assert evidence.frozen
    connection.close()


# ---------------------------------------------------------------------------
# 6. Lock-order violations surface the broker's own error unchanged
# ---------------------------------------------------------------------------


def test_transaction_open_before_barrier_raises_broker_lock_order_error(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    coordination.db.execute("CREATE TABLE synthetic (value)")
    coordination.db.execute("BEGIN IMMEDIATE")
    coordination.db.execute("INSERT INTO synthetic VALUES (1)")
    try:
        with pytest.raises(BrokerLockOrderError):
            freeze_consistent_cut(_assembly(
                "bkp-order-1", coordination, repository, [spec],
                retention_root,
            ))
        # The caller's transaction is untouched.
        assert coordination.db.in_transaction
        assert coordination.db.execute(
            "SELECT value FROM synthetic"
        ).fetchall() == [(1,)]
    finally:
        coordination.db.rollback()

    state, detail = _journal_detail(retention_root, "bkp-order-1")
    assert state == FAILED and detail["reason"] == "lock-order"
    # The lock path was never taken: another entrant admits cleanly now.
    with coordination.admit(TASK_CANCEL) as gate:
        with gate.work_transaction():
            pass
    connection.close()


def test_nested_coordination_frame_raises_broker_lock_order_error(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    with coordination.maintenance_admission():
        with pytest.raises(BrokerLockOrderError):
            freeze_consistent_cut(_assembly(
                "bkp-order-2", coordination, repository, [spec],
                retention_root,
            ))
    state, detail = _journal_detail(retention_root, "bkp-order-2")
    assert state == FAILED and detail["reason"] == "lock-order"
    connection.close()


# ---------------------------------------------------------------------------
# 7. Cancellation and hold deadline: bounded abort, barrier released
# ---------------------------------------------------------------------------


def test_cancel_during_wait_aborts_bounded_and_releases_barrier(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    released = threading.Event()
    holder = threading.Thread(
        target=_hold_maintenance, args=(coordination, released)
    )
    holder.start()
    time.sleep(0.2)
    cancel = threading.Event()
    cancel.set()
    try:
        started = time.monotonic()
        with pytest.raises(FreezeCancelled) as record:
            freeze_consistent_cut(_assembly(
                "bkp-cancel-1", coordination, repository, [spec],
                retention_root, wait_timeout=10.0, cancel_event=cancel,
            ))
        assert time.monotonic() - started < 2.0
        assert record.value.reason == "cancelled"
        state, detail = _journal_detail(retention_root, "bkp-cancel-1")
        assert state == INCOMPLETE and detail["reason"] == "cancelled"
        assert not (retention_root / "cuts").exists()
    finally:
        released.set()
        holder.join(5)

    # Barrier released and reusable; no success marker anywhere.
    evidence = freeze_consistent_cut(_assembly(
        "bkp-cancel-2", coordination, repository, [spec], retention_root,
    ))
    assert evidence.frozen
    connection.close()


def test_hold_budget_expiry_aborts_before_pin_and_releases_barrier(
        tmp_path, coordination, retention_root, monkeypatch):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    backup_id = "bkp-hold-1"

    # Deterministic expiry inside the authority phase rather than racing
    # the clock at entry: H capture burns the hold budget, so the
    # pre-pin deadline check fires before any named pin exists.
    real_head = repository.head_oid

    def slow_head():
        result = real_head()
        time.sleep(0.15)
        return result

    monkeypatch.setattr(repository, "head_oid", slow_head)

    with pytest.raises(FreezeCancelled) as record:
        freeze_consistent_cut(_assembly(
            backup_id, coordination, repository, [spec], retention_root,
            wait_timeout=2.0, hold_timeout=0.05,
        ))
    assert record.value.reason == "hold-timeout"
    # The abort happens before H is fixed: no pin ref was created.
    assert repository.list_maintenance_pins() == ()
    state, detail = _journal_detail(retention_root, backup_id)
    assert state == INCOMPLETE and detail["reason"] == "hold-timeout"
    assert not (retention_root / "cuts").exists()
    # Empty staging debris was removed; no files linger.
    assert not list((retention_root / ".staging").rglob("*.sqlite"))
    with coordination.admit(TASK_CANCEL) as gate:
        with gate.work_transaction():
            pass
    connection.close()


# ---------------------------------------------------------------------------
# 8. Duplicate backup_id rejection (D2) and crash-leftover recognition (D1)
# ---------------------------------------------------------------------------


def test_duplicate_backup_id_rejected_without_second_cut(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    backup_id = "bkp-dup-1"

    first = freeze_consistent_cut(_assembly(
        backup_id, coordination, repository, [spec], retention_root,
    ))
    assert first.frozen
    leaves = sorted(
        path.name
        for path in (retention_root / "cuts" / backup_id).iterdir()
    )
    assert len(leaves) == 1

    with pytest.raises(FreezeAlreadyRecordedError) as record:
        freeze_consistent_cut(_assembly(
            backup_id, coordination, repository, [spec], retention_root,
        ))
    assert record.value.prior_state == FROZEN

    assert sorted(
        path.name
        for path in (retention_root / "cuts" / backup_id).iterdir()
    ) == leaves
    assert len(list_freeze_records(retention_root)) == 1
    connection.close()


def test_preparing_leftover_is_recognizable_and_blocks_repeat(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    # Establish the minimal journal with one completed freeze.
    seeded = freeze_consistent_cut(_assembly(
        "bkp-journal-seed", coordination, repository, [spec],
        retention_root,
    ))
    assert seeded.frozen

    # A crash between preparing and the terminal state: the row is
    # recognizable and a repeat is refused.
    journal_path = retention_root / ".coordinator" / "freezes.sqlite"
    crashed = sqlite3.connect(journal_path)
    try:
        with crashed:
            crashed.execute(
                "INSERT INTO freeze_journal (backup_id, state, barrier_id, "
                "created_at, updated_at, detail) VALUES "
                "('bkp-crash-1', 'preparing', 'b20260101T000000-dead', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'{\"reason\": \"preparing\"}')"
            )
    finally:
        crashed.close()

    leftover = read_freeze_record(retention_root, "bkp-crash-1")
    assert leftover is not None and leftover.state == "preparing"
    with pytest.raises(FreezeAlreadyRecordedError) as record:
        freeze_consistent_cut(_assembly(
            "bkp-crash-1", coordination, repository, [spec], retention_root,
        ))
    assert record.value.prior_state == "preparing"
    assert not (retention_root / "cuts" / "bkp-crash-1").exists()
    connection.close()


# ---------------------------------------------------------------------------
# 9. Physical single-company scope mismatch stops before the barrier
# ---------------------------------------------------------------------------


def test_physical_scope_mismatch_stops_before_barrier_and_lists_database(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    good, conn_good, _ = _work_database(tmp_path, "w1")
    foreign, conn_foreign, _ = _work_database(
        tmp_path, "w2",
        companies=(_IDS["company_id"], _OTHER_COMPANY),
    )
    with pytest.raises(ScopeMismatchError) as record:
        freeze_consistent_cut(_assembly(
            "bkp-scope-1", coordination, repository, [good, foreign],
            retention_root,
        ))
    mismatch = record.value.mismatches[0]
    assert mismatch.role == "w2"
    assert set(mismatch.observed) == {
        _IDS["company_id"], _OTHER_COMPANY
    }
    # Nothing was started: no journal row, no pin, no directories.
    assert read_freeze_record(retention_root, "bkp-scope-1") is None
    assert repository.list_maintenance_pins() == ()
    assert not (retention_root / "cuts").exists()
    assert not (retention_root / ".staging").exists()
    assert not (retention_root / ".coordinator").exists()

    # A database that cannot produce company evidence is refused too.
    def empty_probe(conn):
        return ()

    silent_spec = WorkDatabaseSpec(
        role="w3", connection=conn_good, scope_probe=empty_probe,
    )
    with pytest.raises(ScopeMismatchError) as record:
        freeze_consistent_cut(_assembly(
            "bkp-scope-2", coordination, repository, [silent_spec],
            retention_root,
        ))
    assert record.value.mismatches[0].role == "w3"

    # A spec declared against another company is a structural refusal.
    misdeclared = WorkDatabaseSpec(
        role="w4", connection=conn_good,
        scope_probe=lambda conn: (_OTHER_COMPANY,),
        company_id=_OTHER_COMPANY,
    )
    with pytest.raises(ScopeMismatchError):
        freeze_consistent_cut(_assembly(
            "bkp-scope-3", coordination, repository, [misdeclared],
            retention_root,
        ))
    conn_good.close()
    conn_foreign.close()


# ---------------------------------------------------------------------------
# 10. Components are Backup-API artifacts, never raw copies
# ---------------------------------------------------------------------------


def test_components_are_independent_backup_api_artifacts(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, source_path = _work_database(tmp_path, "w1")
    evidence = freeze_consistent_cut(_assembly(
        "bkp-inode-1", coordination, repository, [spec], retention_root,
    ))
    assert evidence.frozen
    artifact = evidence.cut_dir / "w1.sqlite"

    source_stat = source_path.stat()
    artifact_stat = artifact.stat()
    assert artifact_stat.st_ino != source_stat.st_ino
    assert artifact_stat.st_dev == source_stat.st_dev
    assert stat.S_ISREG(artifact_stat.st_mode)
    assert not artifact.is_symlink()

    restored = sqlite3.connect(artifact)
    try:
        # The single-file snapshot is a collapsed (delete-mode) database.
        assert restored.execute("PRAGMA journal_mode").fetchone() == \
            ("delete",)
        assert restored.execute(
            "SELECT payload FROM work_state WHERE id = 1"
        ).fetchone() == ("payload-1",)
        assert restored.execute("PRAGMA integrity_check").fetchall() == \
            [("ok",)]
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()
    # Verification opens created no sidecars in the cut directory.
    assert _cut_files(evidence) == ["w1.sqlite"]
    connection.close()


# ---------------------------------------------------------------------------
# 11. Sockets disabled: the freeze still succeeds (B4-3 posture)
# ---------------------------------------------------------------------------


def test_freeze_succeeds_with_all_sockets_disabled(
        tmp_path, coordination, retention_root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the freeze coordinator must not use network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    repository = _repo(tmp_path, "sha1")
    spec, connection, _ = _work_database(tmp_path, "w1")
    evidence = freeze_consistent_cut(_assembly(
        "bkp-offline-1", coordination, repository, [spec], retention_root,
    ))
    assert evidence.frozen
    assert evidence.core_digest.startswith("sha256:")
    connection.close()


# ---------------------------------------------------------------------------
# 12. Module purity and caller-resource boundaries
# ---------------------------------------------------------------------------


_ALLOWED_IMPORTS = {
    "contextlib", "dataclasses", "datetime", "decimal", "fcntl", "json",
    "os", "pathlib", "re", "secrets", "sqlite3", "stat", "threading",
    "time",
    "app.facts_tree.backup_component_validation",
    "app.facts_tree.backup_manifest",
    "app.facts_tree.backup_sqlite_checkpoint",
    "app.facts_tree.broker_coordination",
    "app.facts_tree.git_format",
    "app.facts_tree.git_repository",
}


def test_module_imports_are_offline_and_model_free():
    source = Path(coord.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= _ALLOWED_IMPORTS

    forbidden_tokens = (
        "socket", "subprocess", "urllib", "http.client", "requests",
        "httpx", "openai", "anthropic", "ssl", "smtplib", "ftplib",
        "paramiko",
    )
    # No textual mention of a network/model module beyond error strings:
    # assert on the import graph above, and separately that no dynamic
    # import hides behind it.
    assert "__import__" not in source
    for token in forbidden_tokens:
        assert token not in imported


def test_source_connections_left_idle_wal_and_writable(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    spec, connection, source_path = _work_database(tmp_path, "w1")
    assert connection.in_transaction is False
    evidence = freeze_consistent_cut(_assembly(
        "bkp-untouched-1", coordination, repository, [spec],
        retention_root,
    ))
    assert evidence.frozen
    # The host connection is not closed, committed, rolled back or
    # reconfigured.
    assert connection.in_transaction is False
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    connection.execute(
        "INSERT INTO work_state VALUES (2, ?, 'after')",
        (_IDS["company_id"],),
    )
    connection.commit()
    assert connection.execute(
        "SELECT COUNT(*) FROM work_state"
    ).fetchone() == (2,)
    # The frozen artifact does not contain the post-cut row.
    restored = sqlite3.connect(evidence.cut_dir / "w1.sqlite")
    try:
        assert restored.execute(
            "SELECT COUNT(*) FROM work_state"
        ).fetchone() == (1,)
    finally:
        restored.close()
    connection.close()


def test_retention_root_symlink_refused(tmp_path, coordination):
    repository = _repo(tmp_path, "sha1")
    real_root = tmp_path / "real"
    real_root.mkdir(0o700)
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root)
    connection = sqlite3.connect(":memory:")
    spec = WorkDatabaseSpec(
        role="w1",
        connection=connection,
        scope_probe=lambda conn: (_IDS["company_id"],),
    )
    with pytest.raises(AssemblyValidationError, match="symlink"):
        freeze_consistent_cut(_assembly(
            "bkp-symlink-1", coordination, repository, [spec], link_root,
        ))
    connection.close()


def test_retention_root_group_or_world_writable_refused(
        tmp_path, coordination, retention_root):
    repository = _repo(tmp_path, "sha1")
    connection = sqlite3.connect(":memory:")
    spec = WorkDatabaseSpec(
        role="w1",
        connection=connection,
        scope_probe=lambda conn: (_IDS["company_id"],),
    )
    retention_root.chmod(0o722)
    try:
        with pytest.raises(AssemblyValidationError, match="writable"):
            freeze_consistent_cut(_assembly(
                "bkp-perm-1", coordination, repository, [spec],
                retention_root,
            ))
    finally:
        retention_root.chmod(0o700)
    connection.close()
