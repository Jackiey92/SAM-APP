"""Fact identity/revision/lineage ledger; temporary SQLite databases only."""
import sqlite3

import pytest

from app import settings
from app.db.schema import SCHEMA
from app.facts_tree.fact_identity import FactIdentityStore

DIGEST = "a" * 64


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "identity.db")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def store(db, enabled):
    return FactIdentityStore(db, company_id="acc-a")


def add_fact(db, company="acc-a", entity="测试公司", attribute="收入", value="100"):
    # Commit immediately so the store always sees an idle caller connection.
    with db:
        cursor = db.execute(
            "INSERT INTO facts(company_id,entity,attribute,period,value,value_type,unit,"
            "status,confirm_mode,created_at) VALUES(?,?,?,'2025',?,'number','元',"
            "'verified','manual','2026-10-17T00:00:00+00:00')",
            (company, entity, attribute, value))
    return cursor.lastrowid


def insert_revision(db, company, uid, version, row_id, kind="correction"):
    db.execute(
        "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,"
        "content_sha256,revision_kind,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (company, uid, version, row_id, DIGEST, kind, "system", "合成", "2026-10-17T00:00:00+00:00"))


def test_constructor_requires_connection_and_preserves_caller_transaction(db, enabled):
    with pytest.raises(TypeError):
        FactIdentityStore("identity.db")
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO facts(company_id,entity,attribute,value,created_at) "
               "VALUES('acc-a','未提交','收入','1','2026-10-17T00:00:00+00:00')")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            FactIdentityStore(db)
        # The caller transaction is untouched: still open, content still there.
        assert db.in_transaction
        assert db.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    finally:
        db.rollback()
    assert db.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


def test_register_initial_creates_identity_and_version_one(store, db):
    row = add_fact(db)
    result = store.register_initial("acc-a", row, "员工甲", "首次登记")
    assert result["fact_uid"].startswith("fact:")
    assert (result["fact_version"], result["fact_row_id"], result["revision_kind"]) == (1, row, "initial")
    identity = store.get_identity("acc-a", result["fact_uid"])
    assert identity["identity_state"] == "active" and identity["created_at"]
    revisions = store.revisions("acc-a", result["fact_uid"])
    assert len(revisions) == 1
    assert revisions[0]["supersedes_row_id"] is None
    assert (revisions[0]["actor"], revisions[0]["reason"]) == ("员工甲", "首次登记")
    assert len(revisions[0]["content_sha256"]) == 64
    int(revisions[0]["content_sha256"], 16)
    assert store.identity_for_row("acc-a", row)["fact_uid"] == result["fact_uid"]
    with pytest.raises(ValueError, match="already has an identity"):
        store.register_initial("acc-a", row, "员工甲", "重复登记")
    with pytest.raises(ValueError, match="actor"):
        store.register_initial("acc-a", add_fact(db), " ", "登记")
    with pytest.raises(ValueError, match="different company"):
        store.register_initial("acc-a", add_fact(db, company="acc-b"), "员工甲", "跨公司")


def test_register_initial_in_transaction_joins_and_obeys_outer_rollback(store, db):
    row = add_fact(db)
    db.execute("BEGIN IMMEDIATE")
    result = store.register_initial_in_transaction(
        "acc-a", row, "system:v4-publish", "发布操作op-1")
    assert result["fact_row_id"] == row and result["fact_version"] == 1
    assert db.in_transaction
    assert store.identity_for_row("acc-a", row)["fact_uid"] == result["fact_uid"]
    db.rollback()
    assert store.identity_for_row("acc-a", row) is None
    assert db.execute("SELECT count(*) FROM fact_identities").fetchone()[0] == 0

    db.execute("BEGIN IMMEDIATE")
    committed = store.register_initial_in_transaction(
        "acc-a", row, "system:v4-publish", "发布操作op-1")
    db.commit()
    assert store.identity_for_row("acc-a", row)["fact_uid"] == committed["fact_uid"]


def test_legacy_bootstrap_deterministic_idempotent_and_rejects_mapped_rows(store, db):
    row = add_fact(db)
    first = store.bootstrap_legacy("acc-a", row)
    assert first["fact_uid"] == f"legacy:acc-a:{row}"
    assert first["revision_kind"] == "legacy-bootstrap"
    revision = store.revisions("acc-a", first["fact_uid"])[0]
    assert revision["actor"] == "system"
    assert "unknown" in revision["reason"]
    assert store.bootstrap_legacy("acc-a", row) == first
    assert len(store.revisions("acc-a", first["fact_uid"])) == 1
    # A row already mapped to any other identity is rejected, never re-mapped.
    mapped = add_fact(db)
    store.register_initial("acc-a", mapped, "员工甲", "登记")
    with pytest.raises(ValueError, match="already has an identity"):
        store.bootstrap_legacy("acc-a", mapped)


def test_identical_text_rows_keep_distinct_uids(store, db):
    row_a, row_b = add_fact(db), add_fact(db)
    uid_a = store.register_initial("acc-a", row_a, "员工甲", "登记一")["fact_uid"]
    uid_b = store.register_initial("acc-a", row_b, "员工甲", "登记二")["fact_uid"]
    assert uid_a != uid_b
    legacy_a, legacy_b = add_fact(db), add_fact(db)
    assert store.bootstrap_legacy("acc-a", legacy_a)["fact_uid"] != \
        store.bootstrap_legacy("acc-a", legacy_b)["fact_uid"]


def test_correction_closes_old_row_persists_actor_reason_and_adds_edge(store, db):
    row_v1, row_v2 = add_fact(db, value="100"), add_fact(db, value="120")
    uid = store.register_initial("acc-a", row_v1, "员工甲", "登记")["fact_uid"]
    result = store.append_revision("acc-a", uid, row_v2, "员工乙", "数字更正")
    assert (result["fact_version"], result["supersedes_row_id"]) == (2, row_v1)
    closed = db.execute("SELECT superseded_by, valid_to FROM facts WHERE id=?",
                        (row_v1,)).fetchone()
    assert closed[0] == row_v2 and closed[1] is not None
    revisions = store.revisions("acc-a", uid)
    assert [r["fact_version"] for r in revisions] == [1, 2]
    assert (revisions[1]["actor"], revisions[1]["reason"]) == ("员工乙", "数字更正")
    assert revisions[1]["revision_kind"] == "correction"
    assert revisions[0]["content_sha256"] != revisions[1]["content_sha256"]
    events = store.lineage_events("acc-a")
    assert len(events) == 1 and events[0]["event_id"] == result["event_id"]
    assert events[0]["event_type"] == "correction"
    assert (events[0]["actor"], events[0]["reason"]) == ("员工乙", "数字更正")
    edges = store.lineage_edges("acc-a")
    assert len(edges) == 1 and edges[0]["edge_seq"] == 1
    assert (edges[0]["parent_fact_uid"], edges[0]["parent_fact_version"],
            edges[0]["child_fact_uid"], edges[0]["child_fact_version"],
            edges[0]["relation"]) == (uid, 1, uid, 2, "supersedes")
    row_v3 = add_fact(db, value="130")
    assert store.append_revision("acc-a", uid, row_v3, "员工乙", "再次更正")["fact_version"] == 3


def test_append_revision_rejects_non_correction_and_bad_inputs(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    other_uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    mapped_row = store.revisions("acc-a", other_uid)[0]["fact_row_id"]
    with pytest.raises(ValueError, match="must be 'correction'"):
        store.append_revision("acc-a", uid, add_fact(db), "员工甲", "拆分", kind="split")
    with pytest.raises(ValueError, match="already has an identity"):
        store.append_revision("acc-a", uid, mapped_row, "员工甲", "重复行")
    with pytest.raises(ValueError, match="unknown fact identity"):
        store.append_revision("acc-a", "fact:nope", add_fact(db), "员工甲", "无身份")
    with pytest.raises(ValueError, match="does not exist"):
        store.append_revision("acc-a", uid, 99999, "员工甲", "无事实")
    with pytest.raises(ValueError, match="different company"):
        store.append_revision("acc-a", uid, add_fact(db, company="acc-b"), "员工甲", "跨公司")
    with pytest.raises(ValueError, match="reason"):
        store.append_revision("acc-a", uid, add_fact(db), "员工甲", " ")
    with pytest.raises(ValueError, match="host scope"):
        store.append_revision("acc-b", uid, add_fact(db, company="acc-b"), "员工甲", "跨scope")


def test_append_revision_rejects_stale_or_closed_current(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    current_row = store.revisions("acc-a", uid)[0]["fact_row_id"]
    stale_row = add_fact(db, value="200")
    with db:
        db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (stale_row, current_row))
    with pytest.raises(ValueError, match="no longer current"):
        store.append_revision("acc-a", uid, stale_row, "员工甲", "已被取代")
    assert store.revisions("acc-a", uid)[-1]["fact_version"] == 1
    # valid_to filled but superseded_by empty: still closed, cannot append.
    with db:
        db.execute("UPDATE facts SET superseded_by=NULL, valid_to='2026-10-17T01:00:00+00:00' "
                   "WHERE id=?", (current_row,))
    with pytest.raises(ValueError, match="no longer current"):
        store.append_revision("acc-a", uid, stale_row, "员工甲", "已失效")
    assert db.execute("SELECT superseded_by FROM facts WHERE id=?",
                      (current_row,)).fetchone()[0] is None
    assert store.lineage_events("acc-a") == []


def test_append_revision_rejects_closed_new_row(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    superseded_row, expired_row = add_fact(db, value="200"), add_fact(db, value="300")
    closer = add_fact(db, value="400")
    with db:
        db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (closer, superseded_row))
        db.execute("UPDATE facts SET valid_to='2026-10-17T01:00:00+00:00' WHERE id=?",
                   (expired_row,))
    for bad_row in (superseded_row, expired_row):
        with pytest.raises(ValueError, match="already closed"):
            store.append_revision("acc-a", uid, bad_row, "员工甲", "新行已关闭")
    assert len(store.revisions("acc-a", uid)) == 1
    assert store.lineage_events("acc-a") == []
    assert store.lineage_edges("acc-a") == []


def test_revision_insert_trigger_rejects_cross_company_rows(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    foreign_row = add_fact(db, company="acc-b")
    own_row = add_fact(db)
    with pytest.raises(sqlite3.IntegrityError, match="different company"):
        with db:
            insert_revision(db, "acc-a", uid, 2, foreign_row)
    with pytest.raises(sqlite3.IntegrityError, match="different company"):
        with db:
            db.execute(
                "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,"
                "content_sha256,revision_kind,supersedes_row_id,actor,reason,created_at) "
                "VALUES('acc-a',?,2,?,?,'correction',?,'system','合成','2026-10-17T00:00:00+00:00')",
                (uid, own_row, DIGEST, foreign_row))
    assert len(store.revisions("acc-a", uid)) == 1


def test_revision_gap_fails_closed(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    with db:
        insert_revision(db, "acc-a", uid, 5, add_fact(db))
    with pytest.raises(ValueError, match="revision gap"):
        store.revisions("acc-a", uid)
    with pytest.raises(ValueError, match="revision gap"):
        store.append_revision("acc-a", uid, add_fact(db), "员工甲", "缺口")


def test_duplicate_fact_row_or_revision_rejected(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    row = store.revisions("acc-a", uid)[0]["fact_row_id"]
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            insert_revision(db, "acc-a", uid, 2, row)  # fact_row_id reused
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            insert_revision(db, "acc-a", uid, 1, add_fact(db))  # version reused


def test_split_and_merge_lineage_recorded(store, db):
    parent = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    child_a = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    child_b = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    event = store.record_lineage("acc-a", "split", [
        {"parent_fact_uid": parent, "parent_fact_version": 1,
         "child_fact_uid": child_a, "child_fact_version": 1},
        {"parent_fact_uid": parent, "parent_fact_version": 1,
         "child_fact_uid": child_b, "child_fact_version": 1},
    ], "员工甲", "复合句拆分")
    assert event["edge_count"] == 2
    events = store.lineage_events("acc-a")
    assert len(events) == 1 and events[0]["event_type"] == "split"
    assert events[0]["migration_id"] is None
    edges = store.lineage_edges("acc-a")
    assert [e["edge_seq"] for e in edges] == [1, 2]
    assert {(e["parent_fact_uid"], e["child_fact_uid"], e["relation"]) for e in edges} == {
        (parent, child_a, "split_into"), (parent, child_b, "split_into")}
    merge = store.record_lineage("acc-a", "merge", [
        {"parent_fact_uid": child_a, "parent_fact_version": 1,
         "child_fact_uid": parent, "child_fact_version": 1},
        {"parent_fact_uid": child_b, "parent_fact_version": 1,
         "child_fact_uid": parent, "child_fact_version": 1, "relation": "merged_into"},
    ], "员工乙", "人工确认合并", migration_id="m-1")
    assert merge["event_id"] != event["event_id"]
    assert len(store.lineage_events("acc-a")) == 2
    merges = [e for e in store.lineage_events("acc-a") if e["event_type"] == "merge"]
    assert merges[0]["migration_id"] == "m-1"


def test_retract_is_parent_only_and_never_self_referential(store, db):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    event = store.record_lineage("acc-a", "retract", [
        {"parent_fact_uid": uid, "parent_fact_version": 1}], "员工甲", "来源撤回")
    assert event["edge_count"] == 1
    edge = store.lineage_edges("acc-a")[0]
    assert edge["relation"] == "retires"
    assert edge["child_fact_uid"] is None and edge["child_fact_version"] is None
    with pytest.raises(ValueError, match="no child"):
        store.record_lineage("acc-a", "retract", [
            {"parent_fact_uid": uid, "parent_fact_version": 1,
             "child_fact_uid": uid, "child_fact_version": 1}], "员工甲", "自指")
    uid_two = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    with pytest.raises(ValueError, match="exactly one edge"):
        store.record_lineage("acc-a", "retract", [
            {"parent_fact_uid": uid, "parent_fact_version": 1},
            {"parent_fact_uid": uid_two, "parent_fact_version": 1}], "员工甲", "多边")
    assert len(store.lineage_events("acc-a")) == 1


def test_event_shape_validation_and_atomic_rollback(store, db):
    uid_a = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    uid_b = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    edge_a = {"parent_fact_uid": uid_a, "parent_fact_version": 1,
              "child_fact_uid": uid_b, "child_fact_version": 1}
    edge_b = {"parent_fact_uid": uid_b, "parent_fact_version": 1,
              "child_fact_uid": uid_a, "child_fact_version": 1}
    with pytest.raises(ValueError, match="exactly one edge"):
        store.record_lineage("acc-a", "correction", [edge_a, edge_b], "员工甲", "多条")
    with pytest.raises(ValueError, match="at least two distinct child"):
        store.record_lineage("acc-a", "split", [edge_a], "员工甲", "单子端")
    with pytest.raises(ValueError, match="one parent"):
        store.record_lineage("acc-a", "split", [edge_a, edge_b], "员工甲", "多父端")
    with pytest.raises(ValueError, match="at least two distinct parent"):
        store.record_lineage("acc-a", "merge", [edge_a], "员工甲", "单父端")
    with pytest.raises(ValueError, match="one child"):
        store.record_lineage("acc-a", "merge", [edge_a, edge_b], "员工甲", "多子端")
    with pytest.raises(ValueError, match="duplicate lineage edge"):
        store.record_lineage("acc-a", "split", [edge_a, edge_a], "员工甲", "重复边")
    with pytest.raises(ValueError, match="invalid for a split event"):
        store.record_lineage("acc-a", "split", [
            dict(edge_a, relation="merged_into"),
            {"parent_fact_uid": uid_a, "parent_fact_version": 1,
             "child_fact_uid": uid_a, "child_fact_version": 1}], "员工甲", "关系不符")
    # migration allows one or more complete edges.
    event = store.record_lineage("acc-a", "migration", [edge_a], "员工甲", "迁移", migration_id="m-9")
    assert event["edge_count"] == 1
    assert len(store.lineage_events("acc-a")) == 1
    assert len(store.lineage_edges("acc-a")) == 1


def test_lineage_rejects_bad_endpoints_and_rolls_back(store, db):
    parent = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    other = FactIdentityStore(db)  # unscoped, same ledger
    foreign = other.register_initial("acc-b", add_fact(db, company="acc-b"), "员工甲", "登记")["fact_uid"]
    with pytest.raises(ValueError, match="does not exist"):
        store.record_lineage("acc-a", "correction", [
            {"parent_fact_uid": parent, "parent_fact_version": 1,
             "child_fact_uid": foreign, "child_fact_version": 1}], "员工甲", "跨公司边")
    with pytest.raises(ValueError, match="does not exist"):
        store.record_lineage("acc-a", "correction", [
            {"parent_fact_uid": parent, "parent_fact_version": 9,
             "child_fact_uid": parent, "child_fact_version": 1}], "员工甲", "无此版本")
    with pytest.raises(ValueError, match="unknown lineage event type"):
        store.record_lineage("acc-a", "explode", [
            {"parent_fact_uid": parent, "parent_fact_version": 1,
             "child_fact_uid": parent, "child_fact_version": 1}], "员工甲", "坏事件")
    with pytest.raises(ValueError, match="at least one edge"):
        store.record_lineage("acc-a", "migration", [], "员工甲", "无边")
    with pytest.raises(ValueError, match="reason"):
        store.record_lineage("acc-a", "retract", [
            {"parent_fact_uid": parent, "parent_fact_version": 1}], "员工甲", "")
    with pytest.raises(ValueError, match="actor"):
        store.record_lineage("acc-a", "retract", [
            {"parent_fact_uid": parent, "parent_fact_version": 1}], " ", "撤销")
    with pytest.raises(ValueError, match="missing a child endpoint"):
        store.record_lineage("acc-a", "correction", [
            {"parent_fact_uid": parent, "parent_fact_version": 1}], "员工甲", "缺子端")
    assert store.lineage_events("acc-a") == []
    assert store.lineage_edges("acc-a") == []


@pytest.mark.parametrize("table", ["fact_identities", "fact_revisions",
                                   "fact_lineage_events", "fact_lineage_edges"])
def test_ledger_tables_reject_update_and_delete(store, db, table):
    uid = store.register_initial("acc-a", add_fact(db), "员工甲", "登记")["fact_uid"]
    store.append_revision("acc-a", uid, add_fact(db, value="110"), "员工甲", "更正")
    store.record_lineage("acc-a", "retract", [
        {"parent_fact_uid": uid, "parent_fact_version": 2}], "员工甲", "撤销")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(f"UPDATE {table} SET company_id='acc-x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(f"DELETE FROM {table}")
