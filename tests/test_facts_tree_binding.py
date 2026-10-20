"""Binding and metadata projection use temporary SQLite databases only."""
import sqlite3
from datetime import datetime

import pytest

from app import settings
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.template_packs import load_pack
from app.memory import paths
from app.module_tree import MODULES
from app.memory.provider import ProjectionError
from tests.memory_setup import migrated_memory


class Embedding:
    dim, embedder, model_version = 2, "synthetic", "v1"
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, text): return [1., 1.]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)


@pytest.fixture
def store(tmp_path, enabled):
    store = CompanyBindingStore(tmp_path / "binding.db", company_id="acc-a")
    yield store
    store.close()


@pytest.fixture
def memory(tmp_path):
    memory = migrated_memory(tmp_path / "memory.db", embedding=Embedding(), company_id="acc-a")
    yield memory
    memory.close()


GENERIC_DIGEST = load_pack("generic").manifest_sha256


def synthetic_generic(version):
    """A hypothetical generic@version whose stored digest matches its own manifest."""
    from dataclasses import replace
    from app.facts_tree.template_packs import manifest_digest, manifest_document
    pack = replace(load_pack("generic"), version=version)
    return replace(pack, manifest_sha256=manifest_digest(manifest_document(pack)))


def payload():
    return dict(display_name="测试公司", template_pack="generic", pack_version=1,
                bound_at="2026-10-14T00:00:00+00:00", locked=0,
                pack_digest=GENERIC_DIGEST, digest_source="verified-manifest",
                binding_revision=1)


def history_rows(store, company="acc-a"):
    cursor = store.db.execute(
        "SELECT * FROM company_template_binding_history WHERE company_id=? "
        "ORDER BY binding_revision", (company,))
    return [dict(zip((c[0] for c in cursor.description), r)) for r in cursor.fetchall()]


def test_register_and_projection_are_atomic_and_repeat_rejected(store):
    row = store.register("acc-a", "测试公司")
    assert {k: v for k, v in row.items() if k != "bound_at"} == dict(
        company_id="acc-a", display_name="测试公司", template_pack="generic", pack_version=1,
        locked=0, pack_digest=GENERIC_DIGEST, digest_source="verified-manifest",
        binding_revision=1)
    assert datetime.fromisoformat(row["bound_at"]).utcoffset().total_seconds() == 0
    assert store.read_tree_meta("acc-a") == {k: v for k, v in row.items() if k != "company_id"}
    with pytest.raises(ValueError, match="already registered"):
        store.register("acc-a", "覆盖公司")
    assert store.get("acc-a") == row
    assert not store.is_locked("acc-a")


@pytest.mark.parametrize("company", ["", "bad_company", "../bad", "a" * 129, None])
def test_invalid_company_ids(store, company):
    with pytest.raises(ValueError): store.register(company, "公司")
    with pytest.raises(ValueError): store.get(company)


@pytest.mark.parametrize("pack", ["nope", "../generic", "industry", None])
def test_invalid_packs(store, pack):
    with pytest.raises(ValueError): store.register("acc-a", "公司", pack)
    assert store.get("acc-a") is None


@pytest.mark.parametrize("name", ["", " ", None, 123])
def test_invalid_display_name(store, name):
    with pytest.raises(ValueError): store.register("acc-a", name)
    assert store.get("acc-a") is None


def test_empty_rebind_idempotent_and_lock_idempotent(store):
    row = store.register("acc-a", "公司")
    assert store.bind_pack("acc-a", "generic") == row
    assert store.bind_pack("acc-a", "generic") == row
    store.mark_locked("acc-a")
    assert store.is_locked("acc-a")
    assert store.read_tree_meta("acc-a")["locked"] == 1
    store.mark_locked("acc-a")
    assert store.get("acc-a") == {**row, "locked": 1}
    with pytest.raises(PermissionError): store.bind_pack("acc-a", "generic")


def test_empty_rebind_updates_version_and_projection(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    row = store.register("acc-a", "公司")
    # A simulated new template version must also carry its own manifest digest.
    new_digest = synthetic_generic(2).manifest_sha256
    monkeypatch.setattr(company_binding, "load_pack", lambda p: synthetic_generic(2))
    updated = store.bind_pack("acc-a", "generic")
    assert updated["pack_version"] == 2
    assert updated["pack_digest"] == new_digest
    assert updated["digest_source"] == "verified-manifest"
    assert updated["binding_revision"] == 2
    assert updated["bound_at"] >= row["bound_at"]
    meta = store.read_tree_meta("acc-a")
    assert meta["pack_version"] == 2 and meta["pack_digest"] == new_digest
    assert meta["binding_revision"] == 2


def test_nonempty_or_locked_cannot_rebind_past_failed_verification(tmp_path, enabled, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    store = CompanyBindingStore(tmp_path / "nonempty-rebind.db", is_empty=lambda c: False)
    try:
        store.register("acc-a", "公司")
        monkeypatch.setattr(company_binding, "load_pack", lambda p: synthetic_generic(2))
        with pytest.raises(ValueError, match="unavailable"):
            store.bind_pack("acc-a", "generic")
        monkeypatch.undo()
        assert store.get("acc-a")["pack_version"] == 1
    finally:
        store.close()


def test_callback_nonempty_refuses_rebind(tmp_path, enabled):
    store = CompanyBindingStore(tmp_path / "callback.db", is_empty=lambda c: False)
    try:
        row = store.register("acc-a", "公司")
        with pytest.raises(PermissionError): store.bind_pack("acc-a", "generic")
        assert store.get("acc-a") == row
    finally:
        store.close()


@pytest.mark.parametrize("table", ["m1_documents", "m1_chunks", "facts"])
def test_default_nonempty_check_scoped_and_metadata_not_a_fact(tmp_path, enabled, table):
    store = CompanyBindingStore(tmp_path / "nonempty.db")
    try:
        store.register("acc-a", "公司")
        assert store.bind_pack("acc-a", "generic")
        with store.db:
            store.db.execute(f"CREATE TABLE {table}(company_id TEXT)")
            store.db.execute(f"INSERT INTO {table} VALUES('acc-other')")
        assert store.bind_pack("acc-a", "generic")
        with store.db:
            store.db.execute(f"INSERT INTO {table} VALUES('acc-a')")
        with pytest.raises(PermissionError): store.bind_pack("acc-a", "generic")
    finally:
        store.close()


def test_missing_company_and_scope(store):
    assert store.get("acc-a") is None
    assert store.read_tree_meta("acc-a") is None
    assert not store.is_locked("acc-a")
    with pytest.raises(ValueError): store.bind_pack("acc-a", "generic")
    with pytest.raises(ValueError): store.mark_locked("acc-a")
    with pytest.raises(ValueError): store.register("acc-b", "公司")
    with pytest.raises(ValueError): store.read_tree_meta("acc-b")


def test_projection_failure_rolls_back_binding(store, monkeypatch):
    from app.facts_tree import company_binding
    def fail(*args, **kwargs): raise RuntimeError("synthetic metadata failure")
    with monkeypatch.context() as patch:
        patch.setattr(company_binding, "put_tree_meta", fail)
        with pytest.raises(RuntimeError, match="synthetic"):
            store.register("acc-a", "公司")
    assert store.get("acc-a") is None
    assert store.read_tree_meta("acc-a") is None
    original = store.register("acc-a", "公司")
    with monkeypatch.context() as patch:
        patch.setattr(company_binding, "put_tree_meta", fail)
        with pytest.raises(RuntimeError): store.mark_locked("acc-a")
    assert store.get("acc-a") == original
    assert store.read_tree_meta("acc-a")["locked"] == 0


def test_external_connection_remains_open(tmp_path, enabled):
    db = sqlite3.connect(tmp_path / "external.db")
    try:
        store = CompanyBindingStore(db)
        store.register("acc-a", "公司")
        store.close()
        assert db.execute("SELECT count(*) FROM company_tree_meta").fetchone()[0] == 1
        db.execute("INSERT INTO company_tree_meta SELECT 'acc-b',display_name,template_pack,"
                   "pack_version,bound_at,locked,pack_digest,digest_source,binding_revision "
                   "FROM company_tree_meta")
        with pytest.raises(ValueError, match="idle connection"):
            CompanyBindingStore(db)
        db.rollback()
    finally:
        db.close()


def test_disabled_entrypoints(memory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)
    for call in (
        lambda: memory.publish_leaves({}, metadata={}),
        lambda: memory.put_tree_meta("acc-a", payload()),
    ):
        with pytest.raises(RuntimeError, match="^facts tree disabled$"): call()
    store = CompanyBindingStore(tmp_path / "disabled.db")
    try:
        for call in (lambda: store.register("acc-a", "公司"),
                     lambda: store.bind_pack("acc-a", "generic"),
                     lambda: store.mark_locked("acc-a")):
            with pytest.raises(RuntimeError, match="disabled"): call()
        assert store.get("acc-a") is None
    finally:
        store.close()


def test_feature_gate_default_false(monkeypatch):
    import runpy
    monkeypatch.delenv("SAM_FACTS_TREE", raising=False)
    assert runpy.run_path(settings.__file__)["SAM_FACTS_TREE"] is False


def test_controlled_meta_scope_roundtrip_and_no_vectors(memory, enabled):
    assert memory.read_tree_meta("acc-a") is None
    memory.put_tree_meta("acc-a", payload())
    assert memory.read_tree_meta("acc-a") == payload()
    with pytest.raises(ValueError): memory.put_tree_meta("acc-b", payload())
    with pytest.raises(ValueError): memory.read_tree_meta("acc-b")
    with pytest.raises(PermissionError): memory.put(paths.tree_meta_uri("acc-a"), "{}")
    with pytest.raises(PermissionError): memory.publish_documents(
        {paths.tree_meta_uri("acc-a"): "{}"}, metadata={"company_id": "acc-a"})
    assert memory.db.execute("SELECT count(*) FROM m1_documents").fetchone()[0] == 0
    assert memory.db.execute("SELECT count(*) FROM m1_chunks").fetchone()[0] == 0
    memory.rebuild_projection("acc-a")
    assert memory.read_tree_meta("acc-a") == payload()
    assert memory.query(prefix=paths.facts_uri("acc-a")) == []


@pytest.mark.parametrize("bad", [
    {"company_id": "acc-b"}, {"unexpected": "x"}, {"locked": 2}, {"locked": True},
    {"pack_version": True}, {"pack_version": 0}, {"display_name": object()},
    {"display_name": float("nan")}, {"bound_at": ""}, {"template_pack": "../bad"},
    {"pack_digest": "zz"}, {"pack_digest": GENERIC_DIGEST.upper()},
    {"pack_digest": GENERIC_DIGEST + "0"}, {"digest_source": "nope"}, {"digest_source": None},
])
def test_meta_payload_validation(memory, enabled, bad):
    with pytest.raises(ValueError): memory.put_tree_meta("acc-a", {**payload(), **bad})
    assert memory.read_tree_meta("acc-a") is None


def test_missing_meta_fields_and_non_dict(memory, enabled):
    for bad in ({}, None, []):
        with pytest.raises(ValueError): memory.put_tree_meta("acc-a", bad)


def test_store_projection_visible_through_provider_same_transaction(memory, enabled):
    store = CompanyBindingStore(memory.db, company_id="acc-a")
    row = store.register("acc-a", "公司")
    assert memory.read_tree_meta("acc-a") == {k: v for k, v in row.items() if k != "company_id"}
    store.mark_locked("acc-a")
    assert memory.read_tree_meta("acc-a")["locked"] == 1
    store.close()
    assert memory.read_tree_meta("acc-a")["locked"] == 1


def test_leaf_publish_vectors_and_atomic_rollback(memory, enabled, monkeypatch):
    uri = paths.leaf_uri("acc-a", MODULES[0].directory, "L-info-01-测试.md")
    memory.publish_leaves({uri: "原文"}, metadata={"company_id": "acc-a"})
    row = memory.db.execute("SELECT content,module_key FROM m1_documents WHERE uri=?", (uri,)).fetchone()
    assert tuple(row) == ("原文", "info")
    assert memory.db.execute("SELECT count(*) FROM m1_chunks WHERE uri=?", (uri,)).fetchone()[0] == 1
    def fail(row): raise RuntimeError("synthetic vector failure")
    with monkeypatch.context() as patch:
        patch.setattr(memory, "_project", fail)
        with pytest.raises(ProjectionError):
            memory.publish_leaves({uri: "新文"}, metadata={"company_id": "acc-a"})
    assert memory.db.execute("SELECT content FROM m1_documents WHERE uri=?", (uri,)).fetchone()[0] == "原文"
    memory.publish_leaves({uri: None}, metadata={"company_id": "acc-a"})
    assert memory.db.execute("SELECT count(*) FROM m1_documents WHERE uri=?", (uri,)).fetchone()[0] == 0
    assert memory.db.execute("SELECT count(*) FROM m1_chunks WHERE uri=?", (uri,)).fetchone()[0] == 0


@pytest.mark.parametrize("uri", [
    "sam://facts/acc-b/01-企业基本信息/leaves/L-info-01-测试.md",
    "sam://facts/acc-a/11-新枝/leaves/L-info-01-测试.md",
    "sam://facts/acc-a/info/leaves/L-info-01-测试.md",
    "sam://facts/acc-a/01-企业基本信息/brief.md",
    "sam://facts/acc-a/01-企业基本信息/leaves/_bad.md",
    "sam://facts/acc-a/01-企业基本信息/leaves/../bad.md",
    "sam://facts/acc-a/01-企业基本信息/leaves/L-info-01.json",
    "sam://facts/acc-a/01-企业基本信息/leaves/F123.md",
    "sam://facts/acc-a/01-企业基本信息/leaves/L-hr-01-测试.md",
    "sam://facts/acc-a/01-企业基本信息/leaves/L-info-00-测试.md",
])
def test_leaf_publish_bad_address_rejected(memory, enabled, uri):
    with pytest.raises(ValueError): memory.publish_leaves({uri: "正文"}, metadata={"company_id": "acc-a"})
    assert memory.db.execute("SELECT count(*) FROM m1_documents").fetchone()[0] == 0


OLD_SCHEMA = """
CREATE TABLE company_tree_meta (
    company_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    template_pack TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    bound_at TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))
);
"""


def make_legacy_db(path, version=1):
    db = sqlite3.connect(path)
    with db:
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO company_tree_meta VALUES('acc-a','旧公司','generic',?,?,0)",
                   (version, "2026-10-14T00:00:00+00:00"))
    db.close()


def test_legacy_schema_is_baselined_on_first_writable_open(tmp_path, enabled):
    make_legacy_db(tmp_path / "legacy.db")
    store = CompanyBindingStore(tmp_path / "legacy.db", company_id="acc-a")
    try:
        row = store.get("acc-a")
        assert row["pack_digest"] == GENERIC_DIGEST
        assert row["digest_source"] == "legacy-bootstrap"
        assert row["pack_version"] == 1 and row["display_name"] == "旧公司"
        # Reopening is idempotent; the baseline origin is not rewritten.
        store.close()
        store = CompanyBindingStore(tmp_path / "legacy.db", company_id="acc-a")
        assert store.get("acc-a")["digest_source"] == "legacy-bootstrap"
    finally:
        store.close()


def test_legacy_schema_with_unavailable_version_fails_closed(tmp_path, enabled):
    make_legacy_db(tmp_path / "legacy-v9.db", version=9)
    with pytest.raises(ValueError, match="cannot baseline digest"):
        CompanyBindingStore(tmp_path / "legacy-v9.db")


def test_readonly_store_on_legacy_schema_fails_closed(tmp_path, enabled):
    make_legacy_db(tmp_path / "legacy-ro.db")
    with pytest.raises(ValueError, match="baseline"):
        CompanyBindingStore(tmp_path / "legacy-ro.db", read_only=True)


def test_readonly_empty_db_without_tables_is_unbound_and_creates_no_ddl(tmp_path, enabled):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    store = CompanyBindingStore(path, read_only=True)
    try:
        assert store.get("acc-a") is None
        assert not store.is_locked("acc-a")
        assert store.db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("missing", ["pack_digest", "digest_source"])
def test_partial_digest_schema_upgrade_adds_only_missing_column(tmp_path, enabled, missing):
    path = tmp_path / "partial.db"
    db = sqlite3.connect(path)
    extra = "pack_digest TEXT" if missing == "digest_source" else (
        "digest_source TEXT CHECK(digest_source IN ('legacy-bootstrap','verified-manifest'))")
    with db:
        db.executescript(OLD_SCHEMA.replace(");", f",\n    {extra}\n);"))
        db.execute("INSERT INTO company_tree_meta(company_id,display_name,template_pack,"
                   "pack_version,bound_at,locked) VALUES('acc-a','旧公司','generic',1,"
                   "'2026-10-14T00:00:00+00:00',0)")
    db.close()
    store = CompanyBindingStore(path, company_id="acc-a")
    try:
        row = store.get("acc-a")
        assert row["pack_digest"] == GENERIC_DIGEST
        assert row["digest_source"] == "legacy-bootstrap"
    finally:
        store.close()


def make_half_migrated_db(path, digest, source):
    db = sqlite3.connect(path)
    with db:
        db.executescript(OLD_SCHEMA.replace(
            ");", ",\n    pack_digest TEXT,\n    digest_source TEXT CHECK("
            "digest_source IN ('legacy-bootstrap','verified-manifest'))\n);"))
        db.execute("INSERT INTO company_tree_meta VALUES('acc-a','旧公司','generic',1,"
                   "'2026-10-14T00:00:00+00:00',0,?,?)", (digest, source))
    db.close()


def raw_digest_state(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT pack_digest,digest_source FROM company_tree_meta "
                          "WHERE company_id='acc-a'").fetchone()
    finally:
        db.close()


def test_half_migrated_wrong_digest_fails_closed_and_preserves_state(tmp_path, enabled):
    path = tmp_path / "half-wrong.db"
    make_half_migrated_db(path, "0" * 64, None)
    with pytest.raises(ValueError, match="stored digest does not match"):
        CompanyBindingStore(path)
    assert raw_digest_state(path) == ("0" * 64, None)


def test_half_migrated_correct_digest_only_fills_source(tmp_path, enabled):
    path = tmp_path / "half-ok.db"
    make_half_migrated_db(path, GENERIC_DIGEST, None)
    store = CompanyBindingStore(path, company_id="acc-a")
    try:
        row = store.get("acc-a")
        assert row["pack_digest"] == GENERIC_DIGEST
        assert row["digest_source"] == "legacy-bootstrap"
    finally:
        store.close()
    assert raw_digest_state(path) == (GENERIC_DIGEST, "legacy-bootstrap")


def test_source_without_digest_fails_closed_and_preserves_state(tmp_path, enabled):
    path = tmp_path / "half-source.db"
    make_half_migrated_db(path, None, "legacy-bootstrap")
    with pytest.raises(ValueError, match="pack_digest is missing"):
        CompanyBindingStore(path)
    assert raw_digest_state(path) == (None, "legacy-bootstrap")


def test_legacy_baseline_rebuilds_existing_projection(tmp_path, enabled):
    import json
    from app.memory.paths import tree_meta_uri
    path = tmp_path / "legacy-proj.db"
    db = sqlite3.connect(path)
    with db:
        db.executescript(OLD_SCHEMA)
        db.execute("CREATE TABLE company_tree_meta_projection("
                   "company_id TEXT PRIMARY KEY, uri TEXT NOT NULL UNIQUE, content TEXT NOT NULL)")
        db.execute("INSERT INTO company_tree_meta VALUES('acc-a','旧公司','generic',1,"
                   "'2026-10-14T00:00:00+00:00',0)")
        old_meta = dict(display_name="旧公司", template_pack="generic", pack_version=1,
                        bound_at="2026-10-14T00:00:00+00:00", locked=0)
        db.execute("INSERT INTO company_tree_meta_projection VALUES(?,?,?)",
                   ("acc-a", tree_meta_uri("acc-a"),
                    json.dumps(old_meta, ensure_ascii=False, sort_keys=True)))
    db.close()
    store = CompanyBindingStore(path, company_id="acc-a")
    try:
        row = store.get("acc-a")
        assert row["digest_source"] == "legacy-bootstrap"
        meta = store.read_tree_meta("acc-a")
        assert meta == {k: v for k, v in row.items() if k != "company_id"}
        assert meta["pack_digest"] == GENERIC_DIGEST
        assert meta["digest_source"] == "legacy-bootstrap"
    finally:
        store.close()


def test_tampered_or_missing_digest_fails_closed(store):
    store.register("acc-a", "公司")
    with store.db:
        store.db.execute("UPDATE company_tree_meta SET pack_digest=? WHERE company_id='acc-a'",
                         ("0" * 64,))
    with pytest.raises(ValueError, match="digest mismatch"):
        store.get("acc-a")
    with store.db:
        store.db.execute("UPDATE company_tree_meta SET pack_digest=NULL WHERE company_id='acc-a'")
    with pytest.raises(ValueError, match="no digest baseline"):
        store.get("acc-a")


def test_same_version_manifest_tamper_fails_closed(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    store.register("acc-a", "公司")
    tampered = replace(load_pack("generic"), manifest_sha256="f" * 64)
    monkeypatch.setattr(company_binding, "load_pack", lambda p: tampered)
    with pytest.raises(ValueError, match="digest mismatch"):
        store.get("acc-a")
    with pytest.raises(ValueError, match="digest mismatch"):
        store.bind_pack("acc-a", "generic")
    # The stored digest was not rewritten by the failed reads.
    monkeypatch.undo()
    assert store.get("acc-a")["pack_digest"] == GENERIC_DIGEST


def test_unavailable_bound_version_fails_closed(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    store.register("acc-a", "公司")
    monkeypatch.setattr(company_binding, "load_pack",
                        lambda p: replace(load_pack("generic"), version=2))
    with pytest.raises(ValueError, match="version 1 .* unavailable"):
        store.get("acc-a")


def test_projection_carries_digest_and_baseline_source(store):
    row = store.register("acc-a", "公司")
    meta = store.read_tree_meta("acc-a")
    assert meta["pack_digest"] == row["pack_digest"] == GENERIC_DIGEST
    assert meta["digest_source"] == "verified-manifest"
    store.mark_locked("acc-a")
    assert store.read_tree_meta("acc-a") == {
        k: v for k, v in store.get("acc-a").items() if k != "company_id"}


def test_register_writes_single_initial_history_row(store):
    row = store.register("acc-a", "公司")
    assert row["binding_revision"] == 1
    history = history_rows(store)
    assert len(history) == 1
    h = history[0]
    assert h["record_type"] == "initial" and h["actor"] == "system"
    assert (h["template_pack"], h["pack_version"], h["pack_digest"]) == (
        "generic", 1, GENERIC_DIGEST)
    assert h["prev_template_pack"] is None and h["prev_pack_version"] is None
    assert h["prev_pack_digest"] is None
    assert h["migration_id"] is None and h["approval_ref"] is None
    assert h["event_at"] == row["bound_at"]


def test_rebind_appends_history_and_same_target_is_noop(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    store.register("acc-a", "公司")
    new_digest = synthetic_generic(2).manifest_sha256
    monkeypatch.setattr(company_binding, "load_pack", lambda p: synthetic_generic(2))
    updated = store.bind_pack("acc-a", "generic")
    assert updated["binding_revision"] == 2
    assert store.read_tree_meta("acc-a")["binding_revision"] == 2
    assert store.bind_pack("acc-a", "generic") == updated
    history = history_rows(store)
    assert [h["binding_revision"] for h in history] == [1, 2]
    assert history[1]["record_type"] == "rebind"
    assert history[1]["pack_version"] == 2 and history[1]["pack_digest"] == new_digest
    assert (history[1]["prev_template_pack"], history[1]["prev_pack_version"],
            history[1]["prev_pack_digest"]) == ("generic", 1, GENERIC_DIGEST)


def test_rebind_projection_failure_rolls_back_pointer_and_history(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    row = store.register("acc-a", "公司")
    monkeypatch.setattr(company_binding, "load_pack", lambda p: synthetic_generic(2))
    def fail(*args, **kwargs): raise RuntimeError("synthetic metadata failure")
    with monkeypatch.context() as patch:
        patch.setattr(company_binding, "put_tree_meta", fail)
        with pytest.raises(RuntimeError, match="synthetic"):
            store.bind_pack("acc-a", "generic")
    monkeypatch.undo()
    assert store.get("acc-a") == row
    assert [h["binding_revision"] for h in history_rows(store)] == [1]
    assert store.read_tree_meta("acc-a")["pack_version"] == 1


def test_binding_history_triggers_reject_update_and_delete(store):
    store.register("acc-a", "公司")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("UPDATE company_template_binding_history SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("DELETE FROM company_template_binding_history")
    assert len(history_rows(store)) == 1


def test_legacy_upgrade_writes_single_unknown_history_baseline(tmp_path, enabled):
    make_legacy_db(tmp_path / "legacy-hist.db")
    store = CompanyBindingStore(tmp_path / "legacy-hist.db", company_id="acc-a")
    try:
        history = history_rows(store)
        assert len(history) == 1
        h = history[0]
        assert h["binding_revision"] == 1 and h["record_type"] == "legacy-bootstrap"
        assert "earlier history is unknown" in h["reason"]
        assert (h["template_pack"], h["pack_version"], h["pack_digest"]) == (
            "generic", 1, GENERIC_DIGEST)
        assert h["prev_template_pack"] is None and h["prev_pack_digest"] is None
        store.close()
        store = CompanyBindingStore(tmp_path / "legacy-hist.db", company_id="acc-a")
        assert len(history_rows(store)) == 1
        assert store.get("acc-a")["binding_revision"] == 1
    finally:
        store.close()


def test_history_identity_mismatch_fails_closed(store):
    store.register("acc-a", "公司")
    with store.db:
        store.db.execute("DROP TRIGGER company_template_binding_history_no_update")
        store.db.execute("UPDATE company_template_binding_history SET pack_digest=? "
                         "WHERE company_id='acc-a'", ("0" * 64,))
    with pytest.raises(ValueError, match="differs from the latest history"):
        store.get("acc-a")


def test_history_chain_link_mismatch_fails_closed(store, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import company_binding
    store.register("acc-a", "公司")
    monkeypatch.setattr(company_binding, "load_pack", lambda p: synthetic_generic(2))
    store.bind_pack("acc-a", "generic")
    # The dropped trigger stays dropped; the function-scoped database is
    # discarded with tmp_path, so no restoration can pollute other tests.
    with store.db:
        store.db.execute("DROP TRIGGER company_template_binding_history_no_update")
        store.db.execute("UPDATE company_template_binding_history SET prev_pack_digest=? "
                         "WHERE company_id='acc-a' AND binding_revision=2", ("0" * 64,))
    with pytest.raises(ValueError, match="does not link consecutive identities"):
        store.get("acc-a")


@pytest.mark.parametrize("field,value,message", [
    ("record_type", "rebind", "invalid record type"),
    ("prev_pack_digest", "0" * 64, "must not name a previous identity"),
    ("prev_pack_version", 1, "must not name a previous identity"),
])
def test_first_history_row_type_and_prev_validated(store, field, value, message):
    store.register("acc-a", "公司")
    with store.db:
        store.db.execute("DROP TRIGGER company_template_binding_history_no_update")
        store.db.execute(f"UPDATE company_template_binding_history SET {field}=? "
                         "WHERE company_id='acc-a' AND binding_revision=1", (value,))
    with pytest.raises(ValueError, match=message):
        store.get("acc-a")


def test_history_revision_gap_fails_closed(store):
    store.register("acc-a", "公司")
    with store.db:
        store.db.execute(
            "INSERT INTO company_template_binding_history VALUES('acc-a',3,'generic',1,?,"
            "NULL,NULL,NULL,'2026-10-15T00:00:00+00:00','system','x',NULL,NULL,'rebind')",
            (GENERIC_DIGEST,))
    with pytest.raises(ValueError, match="revision gap"):
        store.get("acc-a")


def test_history_loss_fails_closed(store):
    store.register("acc-a", "公司")
    with store.db:
        store.db.execute("DROP TRIGGER company_template_binding_history_no_delete")
        store.db.execute("DELETE FROM company_template_binding_history")
    with pytest.raises(ValueError, match="no history baseline"):
        store.get("acc-a")


def test_leaf_publish_metadata_content_and_multi_company_rejected(memory, enabled):
    uri = paths.leaf_uri("acc-a", MODULES[0].directory, "L-info-01.md")
    with pytest.raises(ValueError): memory.publish_leaves({uri: "正文"}, metadata={"company_id": "acc-b"})
    with pytest.raises(ValueError): memory.publish_leaves({uri: {}}, metadata={"company_id": "acc-a"})
    unscoped = memory.scoped("acc-a")
    unscoped.company_id = None
    other = paths.leaf_uri("acc-b", MODULES[0].directory, "L-info-01.md")
    with pytest.raises(ValueError): unscoped.publish_leaves({uri: "a", other: "b"}, metadata={"company_id": "acc-a"})
    bad = paths.leaf_uri("acc-a", MODULES[0].directory, "L-info-00-测试.md")  # 编号 00 非法
    with pytest.raises(ValueError): memory.publish_documents({bad: "正文"}, metadata={"company_id": "acc-a"})
    assert memory.db.execute("SELECT count(*) FROM m1_documents WHERE uri=?", (bad,)).fetchone()[0] == 0
    good = paths.leaf_uri("acc-a", MODULES[0].directory, "L-info-01-测试.md")
    memory.publish_documents({good: "正文"}, metadata={"company_id": "acc-a"})
    assert memory.read(good) == "正文"


def test_generic_v2_is_loadable_but_never_bindable(store):
    # P3-1: the unreleased draft pack validates for isolated tests, yet the
    # binding layer keeps accepting only "generic" on every write path.
    assert load_pack("generic-v2").version == 1
    with pytest.raises(ValueError, match="supports only generic"):
        store.register("acc-a", "公司", "generic-v2")
    assert store.get("acc-a") is None
    store.register("acc-a", "公司")
    with pytest.raises(ValueError, match="supports only generic"):
        store.bind_pack("acc-a", "generic-v2")
    assert store.get("acc-a")["template_pack"] == "generic"
    rows = history_rows(store)
    assert len(rows) == 1 and rows[0]["record_type"] == "initial"
