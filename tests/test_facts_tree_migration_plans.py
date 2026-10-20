"""Immutable migration plan snapshots; temporary SQLite databases only."""
import sqlite3

import pytest

from app.facts_tree.migration_plans import MigrationPlanStore


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "plans.db")
    yield conn
    conn.close()


@pytest.fixture
def store(db):
    return MigrationPlanStore(db)


def plan(**overrides):
    document = {
        "plan_id": "plan:acc-a:0001",
        "company_id": "acc-a",
        "created_by": "范范",
        "created_at": "2026-10-17T00:00:00+00:00",
        "source": {"pack_id": "generic", "pack_version": 1, "manifest_sha256": "a" * 64},
        "target": {"pack_id": "generic-v2", "pack_version": 1, "manifest_sha256": "b" * 64},
        "binding_revision": 1,
        "snapshot": {"facts": "c" * 64, "placements": "d" * 64},
        "dispositions": [{"fact_row_id": 1, "action": "keep"}],
    }
    document.update(overrides)
    return document


def test_constructor_requires_idle_caller_connection(db):
    with pytest.raises(TypeError):
        MigrationPlanStore("plans.db")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            MigrationPlanStore(db)
        assert db.in_transaction
    finally:
        db.rollback()


def test_canonicalization_and_digest_are_stable(store, db):
    with db:
        first = store.create(plan())
    assert len(first["plan_sha256"]) == 64
    int(first["plan_sha256"], 16)
    # A key-ordered-different but semantically identical document replays to
    # the same canonical content and digest.
    shuffled = {k: plan()[k] for k in reversed(list(plan()))}
    with db:
        replay = store.create(shuffled)
    assert replay == first
    assert store.db.execute("SELECT count(*) FROM tree_migration_plans").fetchone()[0] == 1
    # Unicode is stored unescaped and compact separators are used.
    assert "范范" in first["plan_json"]
    assert '", "' not in first["plan_json"] and '": "' not in first["plan_json"]


def test_create_rejects_conflicting_plan_id(store, db):
    with db:
        original = store.create(plan())
    with pytest.raises(ValueError, match="conflicting migration plan"):
        with db:
            store.create(plan(created_by="别人"))
    with pytest.raises(ValueError, match="conflicting migration plan"):
        with db:
            store.create(plan(company_id="acc-b"))
    assert store.get(plan()["plan_id"]) == original
    # plan_sha256 is reserved: the digest is a separate column, never part of
    # the hashed plan document (a digest cannot reference itself).
    with pytest.raises(ValueError, match="reserved"):
        store.create(plan(plan_sha256="0" * 64))
    with db:
        ok = store.create(plan(plan_id="plan:acc-a:0002"))
    assert ok["plan_id"] == "plan:acc-a:0002"
    with pytest.raises(ValueError, match="reserved"):
        store.create(plan(plan_id="plan:acc-a:0002", plan_sha256=ok["plan_sha256"]))
    assert "plan_sha256" not in ok["plan_json"]
    with db:
        assert store.create(plan(plan_id="plan:acc-a:0002")) == ok


def test_get_missing_and_field_validation(store, db):
    assert store.get("plan:missing") is None
    with pytest.raises(ValueError, match="plan_id must be nonempty"):
        store.get(" ")
    for bad in (None, [], "x", 42):
        with pytest.raises(ValueError, match="JSON object"):
            store.create(bad)
    for field in ("plan_id", "company_id", "created_by", "created_at"):
        with pytest.raises(ValueError, match=field):
            store.create(plan(**{field: "  "}))
        bad = plan()
        del bad[field]
        with pytest.raises(ValueError, match=field):
            store.create(bad)
    with pytest.raises(ValueError, match="serializable"):
        store.create(plan(extra=float("nan")))
    with pytest.raises(ValueError, match="serializable"):
        store.create(plan(extra=object()))
    assert store.db.execute("SELECT count(*) FROM tree_migration_plans").fetchone()[0] == 0


def test_update_and_delete_are_rejected(store, db):
    with db:
        store.create(plan())
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE tree_migration_plans SET company_id='acc-x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM tree_migration_plans")
    assert store.get(plan()["plan_id"]) is not None


def test_tampered_snapshot_fails_closed(store, db):
    with db:
        store.create(plan())
    plan_id = plan()["plan_id"]
    with db:
        db.execute("DROP TRIGGER tree_migration_plans_no_update")
        db.execute("UPDATE tree_migration_plans SET plan_json=plan_json || ' ' WHERE plan_id=?",
                   (plan_id,))
    with pytest.raises(ValueError, match="failed hash verification"):
        store.get(plan_id)
    with db:
        db.execute("UPDATE tree_migration_plans SET plan_json=? WHERE plan_id=?",
                   ('{"plan_id":"plan:other","company_id":"acc-a"}', plan_id))
    # The recomputed digest now matches neither the stored digest nor the columns.
    with pytest.raises(ValueError):
        store.get(plan_id)
