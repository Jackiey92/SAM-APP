"""Immutable template manifest registry; temporary SQLite databases only."""
from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest
import yaml

from app import settings
from app.facts_tree import template_packs as packs
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.manifest_registry import ManifestRegistry
from app.facts_tree.template_packs import (
    LeafCode, load_pack, manifest_digest, manifest_document, manifest_json)

GENERIC = load_pack("generic")
GENERIC_DIGEST = GENERIC.manifest_sha256


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "registry.db")
    yield conn
    conn.close()


@pytest.fixture
def registry(db):
    return ManifestRegistry(db)


def synthetic_generic(version, mutate=None):
    pack = replace(GENERIC, version=version)
    if mutate is not None:
        pack = mutate(pack)
    return replace(pack, manifest_sha256=manifest_digest(manifest_document(pack)))


def wider_scope(pack):
    leaves = list(pack.leaves)
    first = leaves[0]
    leaves[0] = LeafCode(first.code, first.trunk, first.title, first.shape,
                         first.scope + " ")
    return replace(pack, leaves=tuple(leaves))


def test_canonical_manifest_ignores_yaml_comments_and_key_order(tmp_path, monkeypatch):
    raw = yaml.safe_load((packs.TEMPLATES_ROOT / "generic" / "leaves.yaml").read_text("utf-8"))
    reordered = {"leaves": raw["leaves"], "trunks": raw["trunks"],
                 "is_industry_pack": raw["is_industry_pack"],
                 "version": raw["version"], "pack": raw["pack"]}
    path = tmp_path / "generic" / "leaves.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("# 注释\n" + yaml.safe_dump(reordered, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    reloaded = load_pack("generic")
    assert reloaded.manifest_sha256 == GENERIC_DIGEST
    assert manifest_json(manifest_document(reloaded)) == manifest_json(manifest_document(GENERIC))
    assert manifest_digest(manifest_document(GENERIC)) == GENERIC_DIGEST


def test_ensure_archived_idempotent_and_conflict_rejected(registry, db):
    with db:
        record = registry.ensure_archived(GENERIC, "system", "首次存档")
    assert record["status"] == "published" and record["manifest_sha256"] == GENERIC_DIGEST
    assert record["manifest_json"] == manifest_json(manifest_document(GENERIC))
    with db:
        again = registry.ensure_archived(GENERIC, "system", "重复登记")
    assert again == record
    # Same version, different content/digest: hard reject, original preserved.
    pack_v1_altered = synthetic_generic(1, mutate=wider_scope)
    with pytest.raises(ValueError, match="conflicting manifest"):
        with db:
            registry.ensure_archived(pack_v1_altered, "system", "篡改")
    assert registry.get("generic", 1)["manifest_json"] == record["manifest_json"]
    with pytest.raises(ValueError, match="actor"):
        registry.ensure_archived(GENERIC, " ", "原因")


def test_get_reverify_and_digest_mismatch(registry, db):
    assert registry.get("generic", 1) is None
    with db:
        registry.ensure_archived(GENERIC, "system", "存档")
    assert registry.get("generic", 1, expected_digest=GENERIC_DIGEST)["status"] == "published"
    with pytest.raises(ValueError, match="differs from the expected digest"):
        registry.get("generic", 1, expected_digest="0" * 64)
    # Corrupt the archived content (bypassing the immutability trigger) and
    # the read re-verification must fail closed.
    with db:
        db.execute("DROP TRIGGER template_manifests_no_update")
        db.execute("UPDATE template_manifests SET manifest_json=? WHERE pack_id='generic'",
                   (manifest_json(manifest_document(GENERIC)) + " ",))
    with pytest.raises(ValueError, match="failed hash verification"):
        registry.get("generic", 1)


def test_registry_rejects_update_and_delete(registry, db):
    with db:
        registry.ensure_archived(GENERIC, "system", "存档")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE template_manifests SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM template_manifests")


def test_binding_register_archives_in_same_transaction(tmp_path, enabled):
    store = CompanyBindingStore(tmp_path / "bound.db", company_id="acc-a")
    try:
        row = store.register("acc-a", "公司")
        archived = store.manifests.get("generic", 1)
        assert archived["manifest_sha256"] == row["pack_digest"] == GENERIC_DIGEST
        assert archived["reason"] == "archived with initial registration"
        assert store.get("acc-a")["pack_digest"] == GENERIC_DIGEST
    finally:
        store.close()


def test_binding_projection_failure_rolls_back_archive_too(tmp_path, enabled, monkeypatch):
    from app.facts_tree import company_binding
    store = CompanyBindingStore(tmp_path / "rollback.db", company_id="acc-a")
    try:
        def fail(*args, **kwargs): raise RuntimeError("synthetic metadata failure")
        with monkeypatch.context() as patch:
            patch.setattr(company_binding, "put_tree_meta", fail)
            with pytest.raises(RuntimeError, match="synthetic"):
                store.register("acc-a", "公司")
        assert store.get("acc-a") is None
        assert store.manifests.get("generic", 1) is None
        assert store.db.execute(
            "SELECT count(*) FROM company_template_binding_history").fetchone()[0] == 0
    finally:
        store.close()


def test_binding_get_requires_exact_immutable_archive(tmp_path, enabled):
    store = CompanyBindingStore(tmp_path / "strict.db", company_id="acc-a")
    try:
        store.register("acc-a", "公司")
        with store.db:
            store.db.execute("DROP TRIGGER template_manifests_no_delete")
            store.db.execute("DELETE FROM template_manifests")
        with pytest.raises(ValueError, match="no immutable archive"):
            store.get("acc-a")
    finally:
        store.close()


def test_legacy_baseline_archives_manifest_and_gets(tmp_path, enabled):
    db = sqlite3.connect(tmp_path / "legacy.db")
    with db:
        db.executescript("""
        CREATE TABLE company_tree_meta (
            company_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            template_pack TEXT NOT NULL, pack_version INTEGER NOT NULL,
            bound_at TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))
        );""")
        db.execute("INSERT INTO company_tree_meta VALUES('acc-a','旧公司','generic',1,"
                   "'2026-10-14T00:00:00+00:00',0)")
    db.close()
    store = CompanyBindingStore(tmp_path / "legacy.db", company_id="acc-a")
    try:
        archived = store.manifests.get("generic", 1)
        assert archived["manifest_sha256"] == GENERIC_DIGEST
        assert "legacy-bootstrap" in archived["reason"]
        assert store.get("acc-a")["pack_digest"] == GENERIC_DIGEST
    finally:
        store.close()
