"""Real SQLite recovery journal attacks, replay, rollback and isolation tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState as S, PHASES, PHASE_PROOFS, RecoveryEpoch,
    RecoveryPhase as P,
)
from app.facts_tree.recovery_state_store import GateDecision, RecoveryStateError, RecoveryStateStore
from tests.test_facts_tree_recovery_epoch import D, EPOCH, KEY, auth, evidence

AT = "2026-11-03T00:00:00.000000+00:00"


def create(store, case="case-1", epoch=EPOCH, prior=()):
    return store.create_case(case, epoch, backup_id="backup-1", barrier_digest=D,
                             prior_epochs=prior, at=AT)


def advance(store, stop=P.EFFECTS_OPEN):
    for phase in PHASES[1:PHASES.index(stop) + 1]:
        if phase == P.REAUTHORIZED:
            store.authenticate("case-1", auth(), at=AT)
        store.transition("case-1", phase,
                         proofs=tuple((n, D) for n in PHASE_PROOFS[PHASES.index(phase)]), at=AT)
    return store.get("case-1")


@pytest.fixture
def store(tmp_path):
    assert tmp_path.is_relative_to(Path("/tmp"))
    with RecoveryStateStore(tmp_path) as store:
        create(store)
        yield store


def sql(store):
    db = sqlite3.connect(store.path, isolation_level=None)
    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    return db


def assert_quarantined(exc):
    assert str(exc.value) == "recovery state unavailable; quarantined"
    safe = exc.value.safe_state
    assert safe.phase == P.QUARANTINED
    assert not any((safe.read_open, safe.tasks_open, safe.effects_open, safe.external_open))
    assert safe.case_id is None and safe.authentication is None


def test_fresh_case_defaults_closed_independent_path_and_immutable_query(store, tmp_path):
    view = store.get("case-1")
    assert store.path == tmp_path / "recovery-state.sqlite3"
    assert not (tmp_path / "app.db").exists()
    assert view.phase == P.QUARANTINED and view.admission_category == "quarantined"
    assert not any((view.read_open, view.tasks_open, view.effects_open, view.external_open))
    assert "owner_recovery_authentication" in view.evidence_gaps
    assert "external_isolation_and_rotation_unverified" in view.evidence_gaps
    with pytest.raises(FrozenInstanceError):
        view.phase = P.EFFECTS_OPEN


def test_reopen_recognizes_advanced_case_without_any_write(store, tmp_path):
    advance(store, P.REAUTHORIZED)
    before = (store.path.read_bytes(), store.path.stat().st_mtime_ns, store.journal_anchor())
    with RecoveryStateStore(tmp_path) as reopened:
        first = create(reopened)
        assert first.phase == P.REAUTHORIZED
        assert reopened.journal_anchor() == before[2]
    assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before[:2]


def test_same_case_conflicting_metadata_rolls_back(store):
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="conflict"):
        store.create_case("case-1", EPOCH, backup_id="other", barrier_digest=D, prior_epochs=(), at=AT)
    assert store.journal_anchor() == before


def test_full_phase_path_records_every_transition_without_auto_opening(store):
    view = advance(store)
    assert view.phase == P.EFFECTS_OPEN and view.admission_category == "effects"
    assert not any((view.read_open, view.tasks_open, view.effects_open, view.external_open))
    assert store.journal_anchor()[0] == 9  # create, seven transitions, independent auth
    assert len(view.proofs) == 7 and view.authentication == auth()


def test_explicit_gate_decisions_are_separate_and_per_operation(store):
    advance(store)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    view = store.open_gate("case-1", GateDecision("effects", D, "fresh-operation"), at=AT)
    assert view.read_open and view.tasks_open and view.effects_open
    assert view.effect_operations == ("fresh-operation",) and not view.external_open
    # This cannot revive the old approval/operation: its own explicit decision
    # must be epoch-local, and any tombstone wins even after a decision was made.
    key = EvidenceKey("fresh-task", "fresh-approval", "fresh-operation")
    view = store.merge("case-1", key, backup=(), evidence=(evidence(key=key),), at=AT)
    assert not view.effects_open and view.effect_operations == ()
    assert len(view.decisions) == 3


@pytest.mark.parametrize("gate", ["read", "tasks", "effects"])
def test_cannot_open_gate_early(store, gate):
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="phase prerequisite"):
        store.open_gate("case-1", GateDecision(gate, D, "op" if gate == "effects" else None), at=AT)
    assert store.journal_anchor() == before


@pytest.mark.parametrize("gate,item", [("external", None), ("read", "op"), ("effects", None),
                                      ("tasks", "op"), ("effects", "")])
def test_gate_shape_rejects_external_or_non_item_effect_decisions(gate, item):
    with pytest.raises(ValueError):
        GateDecision(gate, D, item)


def test_tasks_and_effect_gates_require_prior_explicit_gates(store):
    advance(store)
    with pytest.raises(ValueError, match="evidence prerequisite"):
        store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    with pytest.raises(ValueError, match="missing task gate"):
        store.open_gate("case-1", GateDecision("effects", D, "fresh-op"), at=AT)


def test_unknown_blocks_tasks_and_effects_but_read_can_open(store):
    advance(store)
    store.merge("case-1", KEY, backup=(S.APPROVED,), evidence=(), at=AT)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    view = store.get("case-1")
    assert view.read_open and view.pending_count == 1
    assert "unresolved_evidence" in view.evidence_gaps
    with pytest.raises(ValueError, match="evidence prerequisite"):
        store.open_gate("case-1", GateDecision("tasks", D), at=AT)


def test_late_unknown_suspends_task_and_effect_decisions(store):
    advance(store)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    store.open_gate("case-1", GateDecision("effects", D, "fresh-op"), at=AT)
    view = store.merge("case-1", KEY, backup=(S.UNUSED,), evidence=(), at=AT)
    assert view.pending_count == 1 and not view.tasks_open and not view.effects_open
    assert len(view.decisions) == 3


@pytest.mark.parametrize("state", [S.CONSUMED, S.RESERVED, S.REVOKED, S.UNKNOWN])
def test_tombstones_survive_new_epoch_and_owner_reauthorization(store, state, tmp_path):
    store.merge("case-1", KEY, backup=(state,), evidence=(), at=AT)
    epoch = RecoveryEpoch("recovery-2", "epoch-12", 12)
    view = create(store, "case-2", epoch, (EPOCH,))
    assert state in view.merges[0].restrictions
    assert view.authentication is None and not view.decisions and view.phase == P.QUARANTINED
    store.authenticate("case-2", auth(epoch), at=AT)
    view = store.merge("case-2", KEY, backup=(S.APPROVED,), evidence=(), at=AT)
    assert state in view.merges[0].restrictions
    with RecoveryStateStore(tmp_path) as reopened:
        assert reopened.get("case-2") == view


@pytest.mark.parametrize("epoch", [EPOCH, RecoveryEpoch("new", "new-epoch", 10),
                                   RecoveryEpoch("new", EPOCH.epoch_id, 12)])
def test_state_store_rejects_epoch_reuse_and_rollback(store, epoch):
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="rollback or reuse"):
        create(store, "case-2", epoch)
    assert store.journal_anchor() == before


def test_host_epoch_history_is_also_required_and_checked(store):
    with pytest.raises(ValueError, match="rollback or reuse"):
        create(store, "case-2", RecoveryEpoch("new", "epoch-12", 12),
               (RecoveryEpoch("old-external", "epoch-13", 13),))


def test_merge_persists_conflicts_and_all_independent_evidence(store, tmp_path):
    view = store.merge("case-1", KEY, backup=(S.APPROVED,),
                       evidence=(evidence(S.REVOKED, seq=100),
                                 evidence(S.CONSUMED, ledger="other", seq=1)), at=AT)
    with RecoveryStateStore(tmp_path) as reopened:
        result = reopened.get("case-1").merges[0]
        assert result == view.merges[0]
        assert result.restrictions == frozenset({S.CONSUMED, S.REVOKED})
        assert result.conflicts == ("state_conflict_stricter_preserved",)
        assert len(result.evidence) == 2
        assert not result.usable


def test_old_operation_with_tombstone_cannot_receive_effect_decision(store):
    advance(store)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    store.merge("case-1", KEY, backup=(S.APPROVED,), evidence=(evidence(),), at=AT)
    with pytest.raises(ValueError, match="tombstone"):
        store.open_gate("case-1", GateDecision("effects", D, KEY.operation_id), at=AT)


@pytest.mark.parametrize("target", PHASES[2:])
def test_persisted_illegal_stage_jump_is_zero_write(store, target):
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="illegal"):
        store.transition("case-1", target, proofs=(), at=AT)
    assert store.journal_anchor() == before


def test_missing_stage_proof_and_auth_are_zero_write(store):
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="missing"):
        store.transition("case-1", P.PACKAGE_VERIFIED, proofs=(), at=AT)
    assert store.journal_anchor() == before
    advance(store, P.EVIDENCE_MERGED)
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="authentication"):
        store.transition("case-1", P.REAUTHORIZED, proofs=(("current_authorization", D),), at=AT)
    assert store.journal_anchor() == before


def test_wrong_instance_auth_and_conflicting_proof_are_zero_write(store):
    with pytest.raises(ValueError, match="mismatch"):
        store.authenticate("case-1", auth(RecoveryEpoch("other", "epoch-12", 12)), at=AT)
    advance(store, P.PACKAGE_VERIFIED)
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="proof conflict"):
        store.transition("case-1", P.MATERIALS_STAGED,
                         proofs=(("package_verified", "b" * 64), ("materials_staged", D)), at=AT)
    assert store.journal_anchor() == before


@pytest.mark.parametrize("at", ["2026-11-03", "2026-11-03T00:00:00+00:00", "not-a-time",
                               "2026-11-03T08:00:00.000000+08:00",
                               "2026-11-02T00:00:00.000000+00:00"])
def test_noncanonical_or_rollback_time_refused(store, at):
    before = store.journal_anchor()
    with pytest.raises(ValueError):
        store.authenticate("case-1", auth(), at=at)
    assert store.journal_anchor() == before


@pytest.mark.parametrize("statement", [
    "UPDATE recovery_events SET command='{}'",
    "UPDATE recovery_events SET seq=2",
    "DELETE FROM recovery_events",
    "INSERT OR REPLACE INTO recovery_events SELECT * FROM recovery_events",
    "REPLACE INTO recovery_events SELECT * FROM recovery_events",
    "INSERT OR IGNORE INTO recovery_events SELECT * FROM recovery_events",
])
def test_event_mutation_and_replacement_abort_even_without_recursive_triggers(store, statement):
    before = store.journal_anchor()
    with sql(store) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only|replaced|sequence or chain mismatch") as exc:
            db.execute(statement)
        assert exc.value.sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
    assert store.journal_anchor() == before


@pytest.mark.parametrize("statement", [
    "UPDATE recovery_watermark SET seq=0",
    "UPDATE recovery_watermark SET seq=seq",
    "UPDATE recovery_watermark SET seq=seq-1",
    "UPDATE recovery_watermark SET seq=seq+2",
    "UPDATE recovery_watermark SET seq=seq+1",  # no corresponding event
    "UPDATE recovery_watermark SET singleton=2,seq=seq+1",
    "DELETE FROM recovery_watermark",
    "INSERT OR REPLACE INTO recovery_watermark VALUES(1,0)",
    "REPLACE INTO recovery_watermark VALUES(1,1)",
])
def test_watermark_cannot_reset_delete_replace_skip_or_advance_without_event(store, statement):
    before = store.journal_anchor()
    with sql(store) as db:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            db.execute(statement)
        assert exc.value.sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
    assert store.journal_anchor() == before


def test_watermark_allows_exactly_one_after_event_and_commit_failure_is_detected(store):
    # A raw writer can append bytes, but public readers reject invalid history.
    # The +1 trigger alone must never be represented as semantic verification.
    head = store.journal_anchor()[1]
    with sql(store) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO recovery_events VALUES(2,'case-1','{}',?,?)", (head, D))
        db.execute("UPDATE recovery_watermark SET seq=seq+1")
        assert db.execute("SELECT seq FROM recovery_watermark").fetchone() == (2,)
        db.execute("ROLLBACK")
    assert store.journal_anchor()[0] == 1


def test_watermark_write_failure_rolls_back_event_insert(store):
    # SQLite authorizer injects failure after the real INSERT, before UPDATE.
    before = store.path.read_bytes()
    store._db.set_authorizer(lambda action, table, *rest:
                             sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and
                             table == "recovery_watermark" else sqlite3.SQLITE_OK)
    with pytest.raises(RecoveryStateError) as exc:
        store.authenticate("case-1", auth(), at=AT)
    assert_quarantined(exc)
    store._db.set_authorizer(None)
    assert store.journal_anchor()[0] == 1 and store.path.read_bytes() == before


@pytest.mark.parametrize("damage", ["drop_trigger", "row_bytes", "missing_event", "watermark", "extra_table"])
def test_schema_history_and_watermark_damage_fail_closed_on_query_and_reopen(store, tmp_path, damage):
    with sql(store) as db:
        if damage == "drop_trigger":
            db.execute("DROP TRIGGER recovery_events_no_update")
        elif damage == "row_bytes":
            from app.facts_tree.recovery_state_store import _SCHEMA
            db.execute("DROP TRIGGER recovery_events_no_update")
            db.execute("UPDATE recovery_events SET command='{}'")
            trigger = _SCHEMA.split("CREATE TRIGGER recovery_events_no_update", 1)[1].split("END;", 1)[0]
            db.executescript("CREATE TRIGGER recovery_events_no_update" + trigger + "END;")
        elif damage == "missing_event":
            from app.facts_tree.recovery_state_store import _SCHEMA
            db.execute("DROP TRIGGER recovery_events_no_delete")
            db.execute("DELETE FROM recovery_events")
            trigger = _SCHEMA.split("CREATE TRIGGER recovery_events_no_delete", 1)[1].split("END;", 1)[0]
            db.executescript("CREATE TRIGGER recovery_events_no_delete" + trigger + "END;")
        elif damage == "watermark":
            from app.facts_tree.recovery_state_store import _SCHEMA
            db.execute("DROP TRIGGER recovery_watermark_increment")
            db.execute("UPDATE recovery_watermark SET seq=0")
            trigger = _SCHEMA.split("CREATE TRIGGER recovery_watermark_increment", 1)[1].split("END;", 1)[0]
            db.executescript("CREATE TRIGGER recovery_watermark_increment" + trigger + "END;")
        else:
            db.execute("CREATE TABLE unexpected(secret TEXT)")
    before = store.path.read_bytes()
    with pytest.raises(RecoveryStateError) as exc:
        store.get("case-1")
    assert_quarantined(exc)
    with pytest.raises(RecoveryStateError) as exc:
        RecoveryStateStore(tmp_path)
    assert_quarantined(exc)
    assert store.path.read_bytes() == before  # no repair or reset


def test_rehashed_but_illegal_history_is_rejected_by_semantic_replay(store, tmp_path):
    from app.facts_tree.recovery_state_store import _row_digest, _canonical
    seq, parent = store.journal_anchor()
    command = _canonical(dict(kind="transition", at=AT, target="effects_open", proofs=[]))
    with sql(store) as db:
        db.execute("INSERT INTO recovery_events VALUES(?,?,?,?,?)",
                   (seq + 1, "case-1", command, parent, _row_digest(seq + 1, "case-1", command, parent)))
        db.execute("UPDATE recovery_watermark SET seq=seq+1")
    with pytest.raises(RecoveryStateError) as exc:
        RecoveryStateStore(tmp_path)
    assert_quarantined(exc)


@pytest.mark.parametrize("payload", [b"", b"not a sqlite database", b"SQLite format 3\x00broken"])
def test_corrupt_or_empty_existing_database_is_not_reinitialized(tmp_path, payload):
    path = tmp_path / "recovery-state.sqlite3"
    path.write_bytes(payload)
    with pytest.raises(RecoveryStateError) as exc:
        RecoveryStateStore(tmp_path)
    assert_quarantined(exc)
    assert path.read_bytes() == payload


def test_business_database_at_state_path_is_rejected_without_modification(tmp_path):
    path = tmp_path / "recovery-state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE business_data(value TEXT)")
        db.execute("INSERT INTO business_data VALUES('untouched')")
    before = path.read_bytes()
    with pytest.raises(RecoveryStateError):
        RecoveryStateStore(tmp_path)
    assert path.read_bytes() == before


def test_symlink_database_or_root_is_refused(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    target = other / "data"
    target.write_bytes(b"protected")
    (tmp_path / "recovery-state.sqlite3").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        RecoveryStateStore(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="root"):
        RecoveryStateStore(linked)
    assert target.read_bytes() == b"protected"


def test_trusted_external_anchor_detects_whole_file_rollback(store, tmp_path):
    old_bytes = store.path.read_bytes()
    store.authenticate("case-1", auth(), at=AT)
    trusted = store.journal_anchor()
    store.close()
    (tmp_path / "recovery-state.sqlite3").write_bytes(old_bytes)
    with pytest.raises(RecoveryStateError) as exc:
        RecoveryStateStore(tmp_path, trusted_anchor=trusted)
    assert_quarantined(exc)


def test_valid_external_anchor_and_read_query_do_not_write(store, tmp_path):
    before = store.path.read_bytes(), store.path.stat().st_mtime_ns
    with RecoveryStateStore(tmp_path, trusted_anchor=store.journal_anchor()) as reopened:
        assert reopened.get("case-1") == store.get("case-1")
    assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before


def test_concurrent_identical_case_creation_is_single_event(tmp_path):
    with RecoveryStateStore(tmp_path):
        pass

    def worker(_):
        with RecoveryStateStore(tmp_path) as independent:
            return create(independent), independent.journal_anchor()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(8)))
    assert all(view == results[0][0] and anchor[0] == 1 for view, anchor in results)


def test_concurrent_conflicting_case_cannot_overwrite_winner(tmp_path):
    with RecoveryStateStore(tmp_path):
        pass

    def worker(index):
        with RecoveryStateStore(tmp_path) as independent:
            try:
                return independent.create_case("case-1", EPOCH, backup_id=f"backup-{index}",
                                               barrier_digest=D, prior_epochs=(), at=AT)
            except ValueError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sum(result is not None for result in results) == 1
    with RecoveryStateStore(tmp_path) as reopened:
        assert reopened.journal_anchor()[0] == 1
        assert reopened.get("case-1") in results


def test_late_restriction_in_old_case_also_constrains_new_epoch(store):
    create(store, "case-2", RecoveryEpoch("recovery-2", "epoch-12", 12), (EPOCH,))
    store.merge("case-1", KEY, backup=(), evidence=(evidence(),), at=AT)
    newest = store.get("case-2")
    assert newest.merges[0].restrictions == frozenset({S.CONSUMED})
    assert newest.phase == P.QUARANTINED


def test_shared_database_new_epoch_does_not_reuse_old_gate_decision(store):
    advance(store)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    store.open_gate("case-1", GateDecision("effects", D, "fresh-op"), at=AT)
    view = create(store, "case-2", RecoveryEpoch("recovery-2", "epoch-12", 12), (EPOCH,))
    assert not view.decisions and not view.read_open and not view.effects_open


def test_different_case_merge_keys_remain_distinct(store):
    first = store.merge("case-1", KEY, backup=(), evidence=(evidence(),), at=AT)
    other = EvidenceKey("task-2", "approval-2", "op-2")
    second = store.merge("case-1", other, backup=(S.APPROVED,), evidence=(), at=AT)
    assert len(first.merges) == 1 and len(second.merges) == 2
    assert second.pending_count == 1
    assert second.merges[0].restrictions == frozenset({S.CONSUMED})


def test_idempotent_reopen_accepts_new_observation_time_without_new_event(store):
    before = store.journal_anchor()
    result = store.create_case("case-1", EPOCH, backup_id="backup-1", barrier_digest=D,
                               prior_epochs=(), at="2026-11-04T00:00:00.000000+00:00")
    assert result.updated_at == AT and store.journal_anchor() == before


def test_raise_abort_rolls_back_entire_statement_and_preserves_prior_transaction_work(store):
    head = store.journal_anchor()[1]
    with sql(store) as db:
        db.execute("CREATE TEMP TABLE prior_work(value INTEGER)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO prior_work VALUES(7)")
        # First inserted row is legal, second row violates append-only. ABORT
        # undoes both rows of this statement; FAIL would leave the first row.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_events VALUES (2,'case-1','{}',?,?),"
                       "(1,'case-1','{}',?,?)", (head, D, head, D))
        assert db.in_transaction
        assert db.execute("SELECT count(*) FROM recovery_events").fetchone() == (1,)
        assert db.execute("SELECT * FROM prior_work").fetchall() == [(7,)]
        db.execute("ROLLBACK")
    assert store.journal_anchor()[0] == 1


def test_deeply_nested_corrupt_command_fails_closed(store, tmp_path):
    from app.facts_tree.recovery_state_store import _row_digest
    seq, parent = store.journal_anchor()
    blob = "[" * 2000 + "0" + "]" * 2000
    with sql(store) as db:
        db.execute("INSERT INTO recovery_events VALUES(?,?,?,?,?)",
                   (seq + 1, "case-1", blob, parent, _row_digest(seq + 1, "case-1", blob, parent)))
        db.execute("UPDATE recovery_watermark SET seq=seq+1")
    with pytest.raises(RecoveryStateError) as exc:
        RecoveryStateStore(tmp_path)
    assert_quarantined(exc)


def test_corruption_also_blocks_writes_without_reset(store):
    with sql(store) as db:
        db.execute("DROP TRIGGER recovery_events_no_update")
    before = store.path.read_bytes()
    with pytest.raises(RecoveryStateError) as exc:
        store.authenticate("case-1", auth(), at=AT)
    assert_quarantined(exc)
    assert store.path.read_bytes() == before


def test_closed_store_fails_closed(store):
    store.close()
    with pytest.raises(RecoveryStateError) as exc:
        store.get("case-1")
    assert_quarantined(exc)


def test_new_epoch_closes_old_local_gates_and_cannot_advance_old_case(store):
    advance(store)
    store.open_gate("case-1", GateDecision("read", D), at=AT)
    store.open_gate("case-1", GateDecision("tasks", D), at=AT)
    store.open_gate("case-1", GateDecision("effects", D, "fresh-op"), at=AT)
    create(store, "case-2", RecoveryEpoch("recovery-2", "epoch-12", 12), (EPOCH,))
    old = store.get("case-1")
    assert not old.is_current_epoch and "superseded_epoch" in old.evidence_gaps
    assert not any((old.read_open, old.tasks_open, old.effects_open, old.external_open))
    assert len(old.decisions) == 3  # historical decisions are not erased
    before = store.journal_anchor()
    with pytest.raises(ValueError, match="superseded"):
        store.authenticate("case-1", auth(), at=AT)
    assert store.journal_anchor() == before
    assert create(store).is_current_epoch is False  # zero-write historical reopen
    assert store.journal_anchor() == before
