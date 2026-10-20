"""Versioned fact placement ledger; temporary SQLite databases only."""
import sqlite3

import pytest

from app import settings
from app.db.database import _migrate_fact_placement_binding_revision
from app.db.schema import SCHEMA
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.fact_identity import FactIdentityStore
from app.facts_tree.fact_placements import FactPlacementStore
from app.facts_tree.template_packs import load_pack

GENERIC = load_pack("generic")
LEAF_A = GENERIC.leaves[0]
LEAF_B = next(l for l in GENERIC.leaves if l.trunk != LEAF_A.trunk)
DIGEST = "a" * 64


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "placements.db")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def binding(db, enabled):
    store = CompanyBindingStore(db)
    store.register("acc-a", "合成公司")
    return store


@pytest.fixture
def store(db, enabled, binding):
    return FactPlacementStore(db, company_id="acc-a")


def test_legacy_placement_upgrade_adds_nullable_revision_without_backfill():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE fact_placements (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, prev_pack_id TEXT,
            prev_pack_version INTEGER, prev_manifest_sha256 TEXT,
            prev_trunk_code TEXT, prev_leaf_code TEXT, event_at TEXT,
            actor TEXT, reason TEXT, record_type TEXT, migration_id TEXT);
        CREATE TABLE fact_current_placement (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, updated_at TEXT);
        INSERT INTO fact_placements VALUES
            ('acc-a',1,1,'generic',1,'digest','trunk-1','leaf-1',
             NULL,NULL,NULL,NULL,NULL,'2026-01-15','synthetic','legacy','initial',NULL);
        INSERT INTO fact_current_placement VALUES
            ('acc-a',1,1,'generic',1,'digest','trunk-1','leaf-1','2026-01-15');
    """)
    try:
        _migrate_fact_placement_binding_revision(db)
        _migrate_fact_placement_binding_revision(db)
        history = db.execute(
            "SELECT binding_revision,prev_binding_revision FROM fact_placements").fetchone()
        pointer = db.execute(
            "SELECT binding_revision FROM fact_current_placement").fetchone()
        assert history == (None, None)
        assert pointer == (None,)
    finally:
        db.close()


def add_fact(db, company="acc-a", value="100"):
    # Commit immediately so stores always see an idle caller connection.
    with db:
        cursor = db.execute(
            "INSERT INTO facts(company_id,entity,attribute,period,value,value_type,unit,"
            "status,confirm_mode,created_at) VALUES(?,?,'收入','2025',?,'number','元',"
            "'verified','manual','2026-10-17T00:00:00+00:00')",
            (company, "测试公司", value))
    return cursor.lastrowid


def mapped_fact(db, company="acc-a", value="100"):
    row = add_fact(db, company=company, value=value)
    uid = FactIdentityStore(db).register_initial(
        company, row, "员工甲", "登记")["fact_uid"]
    return row, uid


def place(store, row, leaf=LEAF_A, **kwargs):
    return store.place_initial("acc-a", row, leaf.trunk, leaf.code,
                               "员工甲", "首次归属", **kwargs)


def test_constructor_requires_connection_and_preserves_caller_transaction(db, enabled):
    with pytest.raises(TypeError):
        FactPlacementStore("placements.db")
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO facts(company_id,entity,attribute,value,created_at) "
               "VALUES('acc-a','未提交','收入','1','2026-10-17T00:00:00+00:00')")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            FactPlacementStore(db)
        assert db.in_transaction
        assert db.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    finally:
        db.rollback()
    assert db.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


def test_writes_require_facts_tree_enabled(db, enabled, binding, monkeypatch):
    row, _ = mapped_fact(db)
    store = FactPlacementStore(db, company_id="acc-a")
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)
    with pytest.raises(RuntimeError, match="facts tree disabled"):
        store.place_initial("acc-a", row, LEAF_A.trunk, LEAF_A.code,
                            "员工甲", "门关闭")
    assert store.history("acc-a", row) == []


def test_place_initial_records_history_pointer_and_receipt(store, db):
    row, _ = mapped_fact(db)
    receipt = place(store, row)
    assert receipt["placement_seq"] == 1
    assert receipt["record_type"] == "initial"
    assert receipt["migration_id"] is None
    assert (receipt["pack_id"], receipt["pack_version"],
            receipt["manifest_sha256"]) == ("generic", GENERIC.version,
                                            GENERIC.manifest_sha256)
    assert receipt["binding_revision"] == 1
    assert (receipt["trunk_code"], receipt["leaf_code"]) == (LEAF_A.trunk, LEAF_A.code)
    history = store.history("acc-a", row)
    assert len(history) == 1
    event = history[0]
    for field in ("prev_pack_id", "prev_pack_version", "prev_manifest_sha256",
                  "prev_binding_revision", "prev_trunk_code", "prev_leaf_code"):
        assert event[field] is None
    assert (event["actor"], event["reason"]) == ("员工甲", "首次归属")
    pointer = store.current("acc-a", row)
    assert pointer is not None
    for field in ("pack_id", "pack_version", "manifest_sha256", "binding_revision",
                  "trunk_code", "leaf_code", "placement_seq"):
        assert pointer[field] == event[field]


def test_one_current_primary_leaf_per_fact_version(store, db):
    row, _ = mapped_fact(db)
    place(store, row)
    with pytest.raises(ValueError, match="already has a placement"):
        place(store, row, leaf=LEAF_B)
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["leaf_code"] == LEAF_A.code


def test_reassign_appends_history_and_moves_pointer(store, db):
    row, _ = mapped_fact(db)
    first = place(store, row)
    receipt = store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                             "员工乙", "改归属", expected_seq=1)
    assert receipt["placement_seq"] == 2
    assert receipt["record_type"] == "reassign"
    history = store.history("acc-a", row)
    assert len(history) == 2
    initial, moved = history
    # The old placement is preserved untouched; the new one chains to it.
    assert (initial["trunk_code"], initial["leaf_code"]) == (LEAF_A.trunk, LEAF_A.code)
    assert (moved["prev_pack_id"], moved["prev_pack_version"],
            moved["prev_manifest_sha256"], moved["prev_trunk_code"],
            moved["prev_leaf_code"]) == (
        initial["pack_id"], initial["pack_version"], initial["manifest_sha256"],
        initial["trunk_code"], initial["leaf_code"])
    assert (moved["actor"], moved["reason"]) == ("员工乙", "改归属")
    pointer = store.current("acc-a", row)
    assert pointer["placement_seq"] == 2
    assert (pointer["trunk_code"], pointer["leaf_code"]) == (LEAF_B.trunk, LEAF_B.code)
    assert first["event_at"] <= moved["event_at"]


def test_reassign_requires_history_and_matching_expected_seq(store, db):
    row, _ = mapped_fact(db)
    with pytest.raises(ValueError, match="no placement history"):
        store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "无历史", expected_seq=1)
    place(store, row)
    with pytest.raises(ValueError, match="expected placement sequence 3"):
        store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "过期序号", expected_seq=3)
    with pytest.raises(ValueError, match="expected_seq"):
        store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "坏序号", expected_seq=0)
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["placement_seq"] == 1


def test_reassign_with_inconsistent_pointer_fails_closed(store, db):
    row, _ = mapped_fact(db)
    place(store, row)
    # The pointer is a mutable projection; tamper it directly.
    with db:
        db.execute("UPDATE fact_current_placement SET trunk_code=? "
                   "WHERE company_id='acc-a' AND fact_row_id=?",
                   (LEAF_B.trunk, row))
    with pytest.raises(ValueError, match="pointer is inconsistent"):
        store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "指针损坏", expected_seq=1)
    assert len(store.history("acc-a", row)) == 1
    with db:
        db.execute("DELETE FROM fact_current_placement "
                   "WHERE company_id='acc-a' AND fact_row_id=?", (row,))
    with pytest.raises(ValueError, match="pointer is inconsistent"):
        store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "指针丢失", expected_seq=1)
    assert len(store.history("acc-a", row)) == 1


def test_placement_requires_mapped_current_fact_version(store, db):
    unmapped = add_fact(db)
    with pytest.raises(ValueError, match="no identity ledger revision"):
        place(store, unmapped)
    row_v1, uid = mapped_fact(db, value="100")
    row_v2 = add_fact(db, value="120")
    FactIdentityStore(db).append_revision("acc-a", uid, row_v2, "员工甲", "更正")
    with pytest.raises(ValueError, match="not the current revision"):
        place(store, row_v1)
    place(store, row_v2)
    assert store.current("acc-a", row_v2)["placement_seq"] == 1
    # A manually closed latest row is no longer placeable either.
    row_v3, _ = mapped_fact(db, value="200")
    with db:
        db.execute("UPDATE facts SET valid_to='2026-10-17T01:00:00+00:00' "
                   "WHERE id=?", (row_v3,))
    with pytest.raises(ValueError, match="no longer current"):
        place(store, row_v3)
    with pytest.raises(ValueError, match="does not exist"):
        place(store, 99999)


def test_placement_rejects_scope_and_foreign_company(db, store, binding):
    foreign = FactIdentityStore(db).register_initial(
        "acc-b", add_fact(db, company="acc-b"), "员工甲", "登记")
    with pytest.raises(ValueError, match="host scope"):
        store.place_initial("acc-b", foreign["fact_row_id"],
                            LEAF_A.trunk, LEAF_A.code, "员工甲", "跨scope")
    unscoped = FactPlacementStore(db)
    with pytest.raises(ValueError, match="different company"):
        unscoped.place_initial("acc-a", foreign["fact_row_id"],
                               LEAF_A.trunk, LEAF_A.code, "员工甲", "跨公司")
    # acc-b has no binding at all.
    with pytest.raises(ValueError, match="has no binding"):
        unscoped.place_initial("acc-b", foreign["fact_row_id"],
                               LEAF_A.trunk, LEAF_A.code, "员工甲", "无绑定")
    assert store.history("acc-a", foreign["fact_row_id"]) == []


def test_unknown_trunk_leaf_and_mismatch_rejected_atomically(store, db):
    row, _ = mapped_fact(db)
    with pytest.raises(ValueError, match="trunk 'trunk-x' is not in the bound manifest"):
        store.place_initial("acc-a", row, "trunk-x", LEAF_A.code, "员工甲", "坏树枝")
    with pytest.raises(ValueError, match="is not in the bound manifest"):
        store.place_initial("acc-a", row, LEAF_A.trunk, "L-team-99", "员工甲", "坏叶子")
    with pytest.raises(ValueError, match="does not belong to trunk"):
        store.place_initial("acc-a", row, LEAF_B.trunk, LEAF_A.code, "员工甲", "叶枝不符")
    # Every failure rolled back: no history, no pointer.
    assert store.history("acc-a", row) == []
    assert store.current("acc-a", row) is None
    place(store, row)
    assert store.current("acc-a", row)["leaf_code"] == LEAF_A.code


def test_corrupt_binding_fails_closed(store, db, binding):
    row, _ = mapped_fact(db)
    with db:
        db.execute("UPDATE company_tree_meta SET pack_digest=? WHERE company_id='acc-a'",
                   ("b" * 64,))
    with pytest.raises(ValueError, match="latest history row"):
        place(store, row)
    with db:
        db.execute("UPDATE company_tree_meta SET pack_digest=? WHERE company_id='acc-a'",
                   (GENERIC.manifest_sha256,))
    with db:
        db.execute("INSERT INTO company_template_binding_history VALUES"
                   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("acc-a", 2, "generic", 1, GENERIC.manifest_sha256,
                    "generic", 1, GENERIC.manifest_sha256,
                    "2026-10-17T02:00:00+00:00", "员工甲", "伪造", None, None, "rebind"))
    with pytest.raises(ValueError, match="revision differs"):
        place(store, row)
    assert store.history("acc-a", row) == []


def test_identity_revision_gap_fails_closed(store, db):
    row, uid = mapped_fact(db)
    with db:
        db.execute(
            "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,"
            "content_sha256,revision_kind,actor,reason,created_at) "
            "VALUES('acc-a',?,5,?,?,'correction','员工甲','缺口','2026-10-17T00:00:00+00:00')",
            (uid, add_fact(db), DIGEST))
    with pytest.raises(ValueError, match="revision gap"):
        place(store, row)
    assert store.history("acc-a", row) == []


def test_input_validation(store, db):
    row, _ = mapped_fact(db)
    for bad_actor, bad_reason in ((" ", "x"), ("员工甲", ""), (None, "x")):
        with pytest.raises(ValueError):
            store.place_initial("acc-a", row, LEAF_A.trunk, LEAF_A.code,
                                bad_actor, bad_reason)
    for bad_row in (0, -1, "1", None):
        with pytest.raises(ValueError, match="fact_row_id"):
            store.place_initial("acc-a", bad_row, LEAF_A.trunk, LEAF_A.code,
                                "员工甲", "坏行号")
    with pytest.raises(ValueError, match="trunk_code"):
        store.place_initial("acc-a", row, " ", LEAF_A.code, "员工甲", "空树枝")
    with pytest.raises(ValueError, match="leaf_code"):
        store.place_initial("acc-a", row, LEAF_A.trunk, "", "员工甲", "空叶子")
    with pytest.raises(ValueError, match="migration_id"):
        place(store, row, migration_id=" ")
    assert store.history("acc-a", row) == []


def test_migration_id_marks_record_type(store, db):
    row, _ = mapped_fact(db)
    receipt = place(store, row, migration_id="m-1")
    assert receipt["record_type"] == "migration"
    moved = store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                           "员工乙", "迁移改归属", expected_seq=1, migration_id="m-1")
    assert moved["record_type"] == "migration"
    plain = store.reassign("acc-a", row, LEAF_A.trunk, LEAF_A.code,
                           "员工乙", "普通改归属", expected_seq=2)
    assert plain["record_type"] == "reassign"
    assert plain["migration_id"] is None
    assert [h["record_type"] for h in store.history("acc-a", row)] == \
        ["migration", "migration", "reassign"]


def test_history_is_append_only_and_seq_guarded(store, db):
    row, _ = mapped_fact(db)
    place(store, row)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE fact_placements SET leaf_code='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM fact_placements")
    with pytest.raises(sqlite3.IntegrityError, match="sequence gap"):
        with db:
            db.execute(
                "INSERT INTO fact_placements "
                "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
                "manifest_sha256,binding_revision,trunk_code,leaf_code,prev_pack_id,"
                "prev_pack_version,prev_manifest_sha256,prev_binding_revision,"
                "prev_trunk_code,prev_leaf_code,event_at,actor,reason,record_type,"
                "migration_id) VALUES('acc-a',?,5,'generic',1,?,1,?,?,"
                "NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-17T03:00:00+00:00',"
                "'员工甲','跳号','reassign',NULL)",
                (row, GENERIC.manifest_sha256, LEAF_B.trunk, LEAF_B.code))
    with pytest.raises(sqlite3.IntegrityError, match="different company"):
        with db:
            db.execute(
                "INSERT INTO fact_placements "
                "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
                "manifest_sha256,binding_revision,trunk_code,leaf_code,prev_pack_id,"
                "prev_pack_version,prev_manifest_sha256,prev_binding_revision,"
                "prev_trunk_code,prev_leaf_code,event_at,actor,reason,record_type,"
                "migration_id) VALUES('acc-b',?,1,'generic',1,?,1,?,?,"
                "NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-17T03:00:00+00:00',"
                "'员工甲','跨公司','initial',NULL)",
                (row, GENERIC.manifest_sha256, LEAF_A.trunk, LEAF_A.code))
    assert len(store.history("acc-a", row)) == 1


def test_pointer_requires_history_row(store, db):
    row, _ = mapped_fact(db)
    place(store, row)
    foreign_row, _ = mapped_fact(db, value="300")
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute(
                "INSERT INTO fact_current_placement "
                "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
                "manifest_sha256,binding_revision,trunk_code,leaf_code,updated_at) "
                "VALUES('acc-a',?,9,'generic',1,?,1,?,?,"
                "'2026-10-17T03:00:00+00:00')",
                (foreign_row, GENERIC.manifest_sha256, LEAF_A.trunk, LEAF_A.code))
    assert store.current("acc-a", foreign_row) is None


def test_joined_writes_reject_idle_connection_without_side_effects(store, db):
    row, _ = mapped_fact(db)
    with pytest.raises(ValueError, match="caller-owned transaction"):
        store.place_initial_in_transaction(
            "acc-a", row, LEAF_A.trunk, LEAF_A.code, "员工甲", "迁移归属", "m-1")
    place(store, row)
    with pytest.raises(ValueError, match="caller-owned transaction"):
        store.reassign_in_transaction(
            "acc-a", row, LEAF_B.trunk, LEAF_B.code, "员工乙", "迁移改归属",
            expected_seq=1, migration_id="m-1")
    assert not db.in_transaction
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["placement_seq"] == 1


def test_joined_writes_require_migration_id(store, db):
    row, _ = mapped_fact(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        for bad_migration_id in (None, "", "   "):
            with pytest.raises(ValueError, match="migration_id is required"):
                store.place_initial_in_transaction(
                    "acc-a", row, LEAF_A.trunk, LEAF_A.code,
                    "员工甲", "迁移归属", bad_migration_id)
        assert db.in_transaction
    finally:
        db.rollback()
    assert store.history("acc-a", row) == []


def test_place_initial_in_transaction_commit_and_rollback(store, db):
    row, _ = mapped_fact(db)
    db.execute("BEGIN IMMEDIATE")
    receipt = store.place_initial_in_transaction(
        "acc-a", row, LEAF_A.trunk, LEAF_A.code, "员工甲", "迁移归属", "m-1")
    assert receipt["placement_seq"] == 1
    assert receipt["record_type"] == "migration"
    assert receipt["migration_id"] == "m-1"
    assert (receipt["pack_id"], receipt["pack_version"],
            receipt["manifest_sha256"]) == ("generic", GENERIC.version,
                                            GENERIC.manifest_sha256)
    assert (receipt["trunk_code"], receipt["leaf_code"]) == (LEAF_A.trunk, LEAF_A.code)
    assert db.in_transaction
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["leaf_code"] == LEAF_A.code
    db.rollback()
    assert store.history("acc-a", row) == []
    assert store.current("acc-a", row) is None

    db.execute("BEGIN IMMEDIATE")
    store.place_initial_in_transaction(
        "acc-a", row, LEAF_A.trunk, LEAF_A.code, "员工甲", "迁移归属", "m-1")
    assert db.in_transaction
    db.commit()
    assert len(store.history("acc-a", row)) == 1
    event = store.history("acc-a", row)[0]
    assert event["record_type"] == "migration"
    assert event["migration_id"] == "m-1"
    assert store.current("acc-a", row)["placement_seq"] == 1


def test_place_initial_for_publication_joins_without_migration_identity(store, db):
    row, _ = mapped_fact(db)
    db.execute("BEGIN IMMEDIATE")
    receipt = store.place_initial_for_publication_in_transaction(
        "acc-a", row, LEAF_A.trunk, LEAF_A.code,
        "system:v4-publish", "V4 publication op-1 chose top1 leaf")
    assert receipt["record_type"] == "initial"
    assert receipt["migration_id"] is None
    assert receipt["placement_seq"] == 1
    assert db.in_transaction
    assert store.current("acc-a", row)["leaf_code"] == LEAF_A.code
    db.rollback()
    assert store.history("acc-a", row) == []
    assert store.current("acc-a", row) is None

    db.execute("BEGIN IMMEDIATE")
    committed = store.place_initial_for_publication_in_transaction(
        "acc-a", row, LEAF_A.trunk, LEAF_A.code,
        "system:v4-publish", "V4 publication op-1 chose top1 leaf")
    db.commit()
    assert committed["record_type"] == "initial"
    assert len(store.history("acc-a", row)) == 1


def test_reassign_in_transaction_moves_pointer_and_validates_cas(store, db):
    row, _ = mapped_fact(db)
    place(store, row, migration_id="m-1")
    db.execute("BEGIN IMMEDIATE")
    receipt = store.reassign_in_transaction(
        "acc-a", row, LEAF_B.trunk, LEAF_B.code, "员工乙", "迁移改归属",
        expected_seq=1, migration_id="m-1")
    assert receipt["placement_seq"] == 2
    assert receipt["record_type"] == "migration"
    assert receipt["migration_id"] == "m-1"
    assert db.in_transaction
    assert len(store.history("acc-a", row)) == 2
    pointer = store.current("acc-a", row)
    assert pointer["placement_seq"] == 2
    assert (pointer["trunk_code"], pointer["leaf_code"]) == (LEAF_B.trunk, LEAF_B.code)
    db.rollback()
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["placement_seq"] == 1

    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="expected placement sequence 3"):
        store.reassign_in_transaction(
            "acc-a", row, LEAF_B.trunk, LEAF_B.code, "员工乙", "过期序号",
            expected_seq=3, migration_id="m-1")
    assert db.in_transaction
    with pytest.raises(ValueError, match="is not in the bound manifest"):
        store.reassign_in_transaction(
            "acc-a", row, LEAF_A.trunk, "L-team-99", "员工乙", "坏叶子",
            expected_seq=1, migration_id="m-1")
    assert db.in_transaction
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["placement_seq"] == 1
    db.rollback()
    assert len(store.history("acc-a", row)) == 1


def test_joined_write_savepoint_undoes_partial_write(store, db):
    row, _ = mapped_fact(db)
    place(store, row)
    # Fault injection: history INSERT must succeed, the pointer UPDATE aborts.
    with db:
        db.execute(
            "CREATE TRIGGER fail_pointer_update BEFORE UPDATE ON fact_current_placement "
            "BEGIN SELECT RAISE(ABORT,'injected pointer failure'); END")
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="injected pointer failure"):
        store.reassign_in_transaction(
            "acc-a", row, LEAF_B.trunk, LEAF_B.code, "员工乙", "迁移改归属",
            expected_seq=1, migration_id="m-1")
    # The savepoint undid the partial history row; the caller transaction
    # is still open and can decide to retry or roll back.
    assert db.in_transaction
    assert len(store.history("acc-a", row)) == 1
    assert store.current("acc-a", row)["placement_seq"] == 1
    db.execute("DROP TRIGGER fail_pointer_update")
    receipt = store.reassign_in_transaction(
        "acc-a", row, LEAF_B.trunk, LEAF_B.code, "员工乙", "迁移改归属",
        expected_seq=1, migration_id="m-1")
    assert receipt["placement_seq"] == 2
    db.commit()
    assert len(store.history("acc-a", row)) == 2
    assert store.current("acc-a", row)["leaf_code"] == LEAF_B.code


def test_reads_are_company_scoped(store, db, binding):
    row, _ = mapped_fact(db)
    place(store, row)
    assert store.current("acc-a", row) is not None
    with pytest.raises(ValueError, match="host scope"):
        store.history("acc-b", row)
    with pytest.raises(ValueError, match="host scope"):
        store.current("acc-b", row)
    unscoped = FactPlacementStore(db)
    assert unscoped.history("acc-b", row) == []
    assert unscoped.current("acc-b", row) is None


def test_verified_identity_read_joins_current_revision_and_placement(store, db):
    row, uid = mapped_fact(db)
    place(store, row)

    resolved = store.resolve_current_identity("acc-a", uid)

    assert resolved["fact_uid"] == uid
    assert resolved["fact_version"] == 1
    assert resolved["fact_row_id"] == row
    assert resolved["identity_state"] == "active"
    assert resolved["placement"] == store.history("acc-a", row)[0]
    assert resolved["placement"]["binding_revision"] == 1


def test_verified_identity_read_distinguishes_unplaced_and_unknown(store, db):
    _, uid = mapped_fact(db)

    resolved = store.resolve_current_identity("acc-a", uid)

    assert resolved["placement"] is None
    assert store.resolve_current_identity("acc-a", "missing-fact") is None


def test_verified_identity_read_rejects_pointer_and_fact_body_drift(store, db):
    row, uid = mapped_fact(db)
    place(store, row)
    with db:
        db.execute("UPDATE fact_current_placement SET leaf_code=? "
                   "WHERE company_id='acc-a' AND fact_row_id=?", (LEAF_B.code, row))
    with pytest.raises(ValueError, match="pointer is inconsistent"):
        store.resolve_current_identity("acc-a", uid)

    with db:
        db.execute("UPDATE facts SET value='999' WHERE id=?", (row,))
        db.execute("UPDATE fact_current_placement SET leaf_code=? "
                   "WHERE company_id='acc-a' AND fact_row_id=?", (LEAF_A.code, row))
    with pytest.raises(ValueError, match="immutable identity revision"):
        store.resolve_current_identity("acc-a", uid)


def test_verified_identity_read_checks_append_only_predecessor_chain(store, db):
    row, uid = mapped_fact(db)
    place(store, row)
    store.reassign("acc-a", row, LEAF_B.trunk, LEAF_B.code,
                   "员工乙", "调整归属", expected_seq=1)
    with db:
        db.execute("DROP TRIGGER fact_placements_no_update")
        db.execute("UPDATE fact_placements SET prev_leaf_code='tampered' "
                   "WHERE company_id='acc-a' AND fact_row_id=? AND placement_seq=2", (row,))

    with pytest.raises(ValueError, match="predecessor link is inconsistent"):
        store.resolve_current_identity("acc-a", uid)


def test_verified_identity_read_obeys_closed_tree_gate(store, db, monkeypatch):
    _, uid = mapped_fact(db)
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)

    with pytest.raises(RuntimeError, match="facts tree disabled"):
        store.resolve_current_identity("acc-a", uid)


def test_verified_identity_read_preserves_caller_transaction(store, db):
    _, uid = mapped_fact(db)
    db.execute("BEGIN IMMEDIATE")

    resolved = store.resolve_current_identity("acc-a", uid)

    assert resolved["fact_uid"] == uid
    assert db.in_transaction
    db.rollback()


def test_verified_identity_read_rejects_corrupt_archived_manifest(store, db):
    row, uid = mapped_fact(db)
    place(store, row)
    with db:
        db.execute("DROP TRIGGER template_manifests_no_update")
        db.execute("UPDATE template_manifests SET manifest_json='{}' "
                   "WHERE pack_id='generic' AND pack_version=1")

    with pytest.raises(ValueError, match="manifest archive failed digest verification"):
        store.resolve_current_identity("acc-a", uid)


def test_verified_identity_read_does_not_inherit_placement_across_correction(store, db):
    row_v1, uid = mapped_fact(db, value="100")
    place(store, row_v1)
    row_v2 = add_fact(db, value="120")
    FactIdentityStore(db).append_revision("acc-a", uid, row_v2, "员工甲", "更正")

    resolved = store.resolve_current_identity("acc-a", uid)

    # The new current version carries no inherited placement.
    assert resolved["fact_version"] == 2
    assert resolved["fact_row_id"] == row_v2
    assert resolved["placement"] is None
    # The old version's placement history is preserved untouched.
    old_history = store.history("acc-a", row_v1)
    assert len(old_history) == 1
    assert (old_history[0]["trunk_code"], old_history[0]["leaf_code"]) == \
        (LEAF_A.trunk, LEAF_A.code)
    # The new version can be placed explicitly on its own.
    place(store, row_v2, leaf=LEAF_B)
    moved = store.resolve_current_identity("acc-a", uid)
    assert moved["placement"]["placement_seq"] == 1
    assert (moved["placement"]["trunk_code"], moved["placement"]["leaf_code"]) == \
        (LEAF_B.trunk, LEAF_B.code)


def test_verified_identity_read_rejects_inactive_identity(store, db):
    _, uid = mapped_fact(db)
    with db:
        db.execute("DROP TRIGGER fact_identities_no_update")
        db.execute("UPDATE fact_identities SET identity_state='retired' "
                   "WHERE company_id='acc-a' AND fact_uid=?", (uid,))

    with pytest.raises(ValueError, match="not active"):
        store.resolve_current_identity("acc-a", uid)


def test_verified_identity_read_rejects_unproven_or_mismatched_binding(store, db):
    row, uid = mapped_fact(db)
    place(store, row)
    with db:
        db.execute("DROP TRIGGER fact_placements_no_update")
        db.execute("UPDATE fact_placements SET binding_revision=99 "
                   "WHERE company_id='acc-a' AND fact_row_id=?", (row,))
    with pytest.raises(ValueError, match="absent from company history"):
        store.resolve_current_identity("acc-a", uid)

    with db:
        db.execute("UPDATE fact_placements SET binding_revision=1,manifest_sha256=? "
                   "WHERE company_id='acc-a' AND fact_row_id=?", ("b" * 64, row))
    with pytest.raises(ValueError, match="differs from its archived company binding"):
        store.resolve_current_identity("acc-a", uid)


def _db_snapshot(conn):
    """Full schema plus table contents, for proving nothing was written."""
    schema = conn.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    data = {name: conn.execute(f"SELECT * FROM {name}").fetchall()
            for kind, name, _ in schema
            if kind == "table" and not name.startswith("sqlite_")}
    return schema, data


def test_read_only_store_resolves_on_ro_connection_without_any_write(
        tmp_path, enabled, monkeypatch):
    path = tmp_path / "placements-ro.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    CompanyBindingStore(db).register("acc-a", "合成公司")
    store = FactPlacementStore(db, company_id="acc-a")
    row, uid = mapped_fact(db)
    place(store, row)
    _, unplaced_uid = mapped_fact(db, value="200")
    before = _db_snapshot(db)
    db.close()

    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    statements = []
    ro.set_trace_callback(statements.append)
    try:
        ro_store = FactPlacementStore(ro, company_id="acc-a", read_only=True)
        resolved = ro_store.resolve_current_identity("acc-a", uid)
        assert resolved["fact_uid"] == uid
        assert resolved["identity_state"] == "active"
        assert resolved["placement"]["placement_seq"] == 1
        assert resolved["placement"]["binding_revision"] == 1
        assert (resolved["placement"]["trunk_code"],
                resolved["placement"]["leaf_code"]) == (LEAF_A.trunk, LEAF_A.code)
        assert ro_store.history("acc-a", row) == [resolved["placement"]]
        assert ro_store.current("acc-a", row)["leaf_code"] == LEAF_A.code
        # Unknown identity stays None; registered-but-unplaced stays None.
        assert ro_store.resolve_current_identity("acc-a", "missing-fact") is None
        assert ro_store.resolve_current_identity(
            "acc-a", unplaced_uid)["placement"] is None
        # The closed feature gate still fails closed in read-only mode.
        monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)
        with pytest.raises(RuntimeError, match="facts tree disabled"):
            ro_store.resolve_current_identity("acc-a", uid)
    finally:
        ro.close()

    # SQL trace: no write of any kind, no writable PRAGMA, no DDL.
    writes = [s for s in statements
              if s.lstrip().split(None, 1)[0].upper() in
              {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER",
               "DROP", "VACUUM", "REINDEX"}]
    assert writes == []
    pragmas = [s for s in statements if s.lstrip().upper().startswith("PRAGMA")]
    assert pragmas and all("table_info" in s for s in pragmas)

    check = sqlite3.connect(path)
    try:
        assert _db_snapshot(check) == before
    finally:
        check.close()


def test_read_only_constructor_rejects_missing_table_without_repair(
        tmp_path, enabled):
    db = sqlite3.connect(tmp_path / "missing-table.db")
    db.executescript(SCHEMA)
    with db:
        db.execute("DROP TABLE template_manifests")
    before = _db_snapshot(db)
    try:
        with pytest.raises(ValueError, match="template_manifests"):
            FactPlacementStore(db, read_only=True)
        # No repair: the table is still gone and the schema is untouched.
        assert _db_snapshot(db) == before
    finally:
        db.close()


def test_read_only_constructor_rejects_missing_column_without_repair(
        tmp_path, enabled):
    # Legacy-shaped placement tables predate the binding-revision columns;
    # read-only construction must fail closed, never ALTER or backfill.
    db = sqlite3.connect(tmp_path / "missing-column.db")
    db.executescript("""
        CREATE TABLE fact_placements (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, prev_pack_id TEXT,
            prev_pack_version INTEGER, prev_manifest_sha256 TEXT,
            prev_trunk_code TEXT, prev_leaf_code TEXT, event_at TEXT,
            actor TEXT, reason TEXT, record_type TEXT, migration_id TEXT);
        CREATE TABLE fact_current_placement (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, updated_at TEXT);
    """)
    db.executescript(SCHEMA)
    before = _db_snapshot(db)
    try:
        with pytest.raises(ValueError, match="fact_placements.binding_revision"):
            FactPlacementStore(db, read_only=True)
        assert _db_snapshot(db) == before
        assert "binding_revision" not in {
            str(r[1]) for r in db.execute("PRAGMA table_info(fact_placements)")}
    finally:
        db.close()


def test_read_only_constructor_preserves_caller_transaction(db, enabled):
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            FactPlacementStore(db, read_only=True)
        assert db.in_transaction
    finally:
        db.rollback()


def test_read_only_mode_rejects_every_write_api_on_writable_connection(
        db, enabled, binding):
    row, _ = mapped_fact(db)
    placed_row, placed_uid = mapped_fact(db, value="300")
    place(FactPlacementStore(db, company_id="acc-a"), placed_row)
    store = FactPlacementStore(db, company_id="acc-a", read_only=True)
    before = _db_snapshot(db)

    with pytest.raises(ValueError, match="read-only"):
        store.place_initial("acc-a", row, LEAF_A.trunk, LEAF_A.code,
                            "员工甲", "只读拒绝")
    with pytest.raises(ValueError, match="read-only"):
        store.reassign("acc-a", placed_row, LEAF_B.trunk, LEAF_B.code,
                       "员工乙", "只读拒绝", expected_seq=1)
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="read-only"):
            store.place_initial_in_transaction(
                "acc-a", row, LEAF_A.trunk, LEAF_A.code,
                "员工甲", "只读拒绝", "m-1")
        with pytest.raises(ValueError, match="read-only"):
            store.place_initial_for_publication_in_transaction(
                "acc-a", row, LEAF_A.trunk, LEAF_A.code,
                "system:v4-publish", "只读拒绝")
        with pytest.raises(ValueError, match="read-only"):
            store.reassign_in_transaction(
                "acc-a", placed_row, LEAF_B.trunk, LEAF_B.code,
                "员工乙", "只读拒绝", expected_seq=1, migration_id="m-1")
        assert db.in_transaction
    finally:
        db.rollback()

    assert _db_snapshot(db) == before
    # Verified reads still work on the same writable connection.
    resolved = store.resolve_current_identity("acc-a", placed_uid)
    assert resolved["placement"]["placement_seq"] == 1


def test_read_only_store_fails_closed_on_corrupt_ledger_and_scope(
        tmp_path, enabled):
    path = tmp_path / "corrupt-ro.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    CompanyBindingStore(db).register("acc-a", "合成公司")
    store = FactPlacementStore(db, company_id="acc-a")
    row, uid = mapped_fact(db)
    place(store, row)
    with db:
        db.execute("UPDATE fact_current_placement SET leaf_code=? "
                   "WHERE company_id='acc-a' AND fact_row_id=?",
                   (LEAF_B.code, row))
    before = _db_snapshot(db)
    db.close()

    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ro_store = FactPlacementStore(ro, company_id="acc-a", read_only=True)
        with pytest.raises(ValueError, match="pointer is inconsistent"):
            ro_store.resolve_current_identity("acc-a", uid)
        with pytest.raises(ValueError, match="host scope"):
            ro_store.resolve_current_identity("acc-b", uid)
    finally:
        ro.close()

    check = sqlite3.connect(path)
    try:
        assert _db_snapshot(check) == before
    finally:
        check.close()


def test_verified_identity_read_rejects_legacy_null_binding_revision(tmp_path, enabled):
    # An upgraded legacy database keeps nullable revision columns; rows written
    # before the upgrade have no proven binding revision and must fail closed.
    db = sqlite3.connect(tmp_path / "legacy.db")
    db.executescript("""
        CREATE TABLE fact_placements (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, prev_pack_id TEXT,
            prev_pack_version INTEGER, prev_manifest_sha256 TEXT,
            prev_trunk_code TEXT, prev_leaf_code TEXT, event_at TEXT,
            actor TEXT, reason TEXT, record_type TEXT, migration_id TEXT);
        CREATE TABLE fact_current_placement (
            company_id TEXT, fact_row_id INTEGER, placement_seq INTEGER,
            pack_id TEXT, pack_version INTEGER, manifest_sha256 TEXT,
            trunk_code TEXT, leaf_code TEXT, updated_at TEXT);
    """)
    db.executescript(SCHEMA)
    try:
        CompanyBindingStore(db).register("acc-a", "合成公司")
        row, uid = mapped_fact(db)
        with db:
            db.execute(
                "INSERT INTO fact_placements VALUES "
                "('acc-a',?,1,'generic',1,?,'trunk-1','leaf-1',"
                "NULL,NULL,NULL,NULL,NULL,'2026-01-15','synthetic','legacy','initial',NULL)",
                (row, GENERIC.manifest_sha256))
            db.execute(
                "INSERT INTO fact_current_placement VALUES "
                "('acc-a',?,1,'generic',1,?,'trunk-1','leaf-1','2026-01-15')",
                (row, GENERIC.manifest_sha256))
        store = FactPlacementStore(db, company_id="acc-a")
        with pytest.raises(ValueError, match="refusing to infer"):
            store.resolve_current_identity("acc-a", uid)
        # The unproven legacy rows stay exactly as they were: no backfill.
        assert db.execute(
            "SELECT binding_revision,prev_binding_revision "
            "FROM fact_placements").fetchone() == (None, None)
        assert db.execute(
            "SELECT binding_revision FROM fact_current_placement").fetchone() == (None,)
    finally:
        db.close()
