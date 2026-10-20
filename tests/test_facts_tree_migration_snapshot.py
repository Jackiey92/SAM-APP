"""Read-only migration snapshot reader; synthetic in-memory SQLite only.

Every test uses acc-* fake companies and `:memory:` databases. No real
company database is ever opened; the reader is proven read-only by comparing
a full database dump before and after a read.
"""
import hashlib
import json
import sqlite3
import time

import pytest

from app.db.database import _migrate_fact_placement_binding_revision
from app.db.schema import SCHEMA
from app.document_read.notes import SCHEMA as NOTES_SCHEMA
from app.facts_tree.fact_identity import FactIdentityStore
from app.facts_tree.md_publish import SCHEMA as MD_PUBLISH_SCHEMA
from app.facts_tree.migration_plan_builder import build_migration_plan
from app.facts_tree.migration_plans import MigrationPlanStateLedger
from app.facts_tree.migration_snapshot import (
    MigrationSnapshotReader, SnapshotRejected)
from app.facts_tree.notes_publish import SCHEMA as NOTES_PUBLISH_SCHEMA
from app.facts_tree.source_registry import SCHEMA as SOURCE_SCHEMA

COMPANY = "acc-a"
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-02-01T00:00:00+00:00"
NOW = "2026-03-01T00:00:00+00:00"

MEMORY_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_kv (
    uri TEXT PRIMARY KEY, content TEXT NOT NULL, metadata TEXT NOT NULL);
"""
MD_JOB_LEASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS md_job_leases (
    job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, owner TEXT NOT NULL,
    generation INTEGER NOT NULL, expires_at REAL NOT NULL);
"""


def make_db(*, source=True, md=True, notes=True, kv=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    if source:
        conn.executescript(SOURCE_SCHEMA)
    if md:
        conn.executescript(MD_PUBLISH_SCHEMA)
    if notes:
        conn.executescript(NOTES_SCHEMA)
        conn.executescript(NOTES_PUBLISH_SCHEMA)
    if kv:
        conn.executescript(MEMORY_KV_SCHEMA)
        conn.executescript(MD_JOB_LEASES_SCHEMA)
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = make_db()
    yield conn
    conn.close()


def _manifest(document):
    text = json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_binding(conn, company=COMPANY, *, revisions=2):
    """Consistent meta+history+manifest chain; revision 1 at T0, 2 at T1."""
    packs = [("generic", 1), ("generic-v2", 1)][:revisions]
    times = [T0, T1][:revisions]
    digests = []
    for pack_id, pack_version in packs:
        text, sha = _manifest({"pack_id": pack_id, "pack_version": pack_version})
        conn.execute(
            "INSERT OR IGNORE INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            (pack_id, pack_version, sha, text, "published", "饭团",
             "synthetic", T0))
        digests.append(sha)
    for index, (pack_id, pack_version) in enumerate(packs):
        if index:
            prev = (packs[index - 1][0], packs[index - 1][1], digests[index - 1])
        else:
            prev = (None, None, None)
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company, index + 1, pack_id, pack_version, digests[index],
             prev[0], prev[1], prev[2], times[index], "饭团", "synthetic",
             None, None, "initial" if index == 0 else "rebind"))
    pack_id, pack_version = packs[-1]
    conn.execute(
        "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
        (company, "合成公司", pack_id, pack_version, times[-1], 0,
         digests[-1], "verified-manifest", revisions))
    return {"packs": packs, "digests": digests}


def add_fact(conn, company=COMPANY, *, entity="甲公司", attribute="注册资本",
             period="2025", value="100万", source_file=None, source_page=None,
             source_span=None, created_at="2026-01-15T00:00:00+00:00"):
    cursor = conn.execute(
        "INSERT INTO facts(company_id,entity,attribute,period,value,value_type,"
        "unit,source_file,source_page,source_span,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (company, entity, attribute, period, value, "string", None,
         source_file, source_page, source_span, created_at))
    return cursor.lastrowid


def _fact_row(conn, fact_id):
    cursor = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,))
    return dict(zip((c[0] for c in cursor.description), cursor.fetchone()))


def add_ledger(conn, fact_id, company=COMPANY, *, uid=None, version=1,
               content_digest=None, kind="initial"):
    uid = uid or f"fact:{company}:{fact_id}"
    digest = content_digest or FactIdentityStore._content_digest(
        _fact_row(conn, fact_id))
    conn.execute(
        "INSERT OR IGNORE INTO fact_identities VALUES(?,?,?,?)",
        (company, uid, NOW, "active"))
    conn.execute(
        "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,"
        "fact_row_id,content_sha256,revision_kind,supersedes_row_id,actor,"
        "reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (company, uid, version, fact_id, digest, kind, None, "饭团",
         "synthetic", NOW))
    return uid


def add_source_file(conn, file_hash="f" * 64):
    conn.execute(
        "INSERT OR IGNORE INTO source_files VALUES(?,?,?,?,?,?,?,?,?)",
        (file_hash, "合成.md", "text/markdown", 10, "/tmp/synthetic",
         "internal", "active", "饭团", NOW))


def add_source_chain(conn, company=COMPANY, *, source_id="src-1",
                     lifecycle="active", with_event=True):
    conn.execute(
        "INSERT INTO sources(company_id,source_id,kind,origin_zone,origin_key,"
        "display_name,lifecycle,revision,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,1,?,?)",
        (company, source_id, "file", "internal",
         hashlib.sha256(source_id.encode("utf-8")).hexdigest(), "合成文件",
         lifecycle, NOW, NOW))
    conn.execute(
        "INSERT INTO source_versions(company_id,source_id,version_id,seq,"
        "content_fingerprint,file_hash,locator,kind_meta,captured_at,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (company, source_id, f"ver-{source_id}", 1, "c" * 64,
         hashlib.sha256(("file:" + source_id).encode("utf-8")).hexdigest(),
         "p1", "{}", NOW, NOW))
    if with_event:
        conn.execute(
            "INSERT INTO source_events VALUES(?,?,?,?,?,?,?,?,?)",
            (company, f"evt-{source_id}", "source_registered", "{}", None, 1,
             None, None, NOW))


def source_fact_fingerprint(conn, fact_id, company=COMPANY):
    fact = _fact_row(conn, fact_id)
    payload = [company, fact["id"], fact["entity"], fact["attribute"],
               fact["period"], fact["value"], fact["unit"],
               fact["value_type"], fact["source_file"]]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return "sha_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def add_evidence(conn, fact_id, company=COMPANY, *, source_id="src-1",
                 evidence_id="ev-1", relation="supports", status="active",
                 fact_fingerprint=None):
    conn.execute(
        "INSERT INTO source_evidence(company_id,evidence_id,source_id,"
        "version_id,locator,quote_fingerprint,coordinate,authorization,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (company, evidence_id, source_id, f"ver-{source_id}", "p1",
         hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(),
         json.dumps({"page": 1}), json.dumps({"grant": "g-1"}), NOW))
    conn.execute(
        "INSERT INTO fact_evidence_links VALUES(?,?,?,?,?,?,?,?)",
        (company, fact_id, evidence_id, relation, status,
         fact_fingerprint or source_fact_fingerprint(conn, fact_id, company),
         json.dumps({"basis": "literal"}), NOW))


def add_placement(conn, fact_id, company=COMPANY, *, leaf_code="leaf-1",
                  created_at="2026-01-15T00:00:00+00:00", trunk="trunk-1"):
    conn.execute(
        "INSERT INTO fact_leaf_links(company_id,fact_id,trunk,leaf_code,"
        "source_file,job_id,created_at) VALUES(?,?,?,?,?,?,?)",
        (company, fact_id, trunk, leaf_code, "f" * 64, None, created_at))


def add_leaf_revision(conn, company=COMPANY, *, leaf_code="leaf-1", version=1,
                      created_at="2026-01-15T00:00:00+00:00",
                      source_files='["f"]', fact_ids="[1]"):
    conn.execute(
        "INSERT INTO leaf_revisions(company_id,leaf_code,version,content_sha,"
        "source_files,fact_ids,owner_skill,attribution_id,change_note,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (company, leaf_code, version, "sha-1", source_files, fact_ids,
         "fact-leaf", None, None, created_at))


def add_job(conn, job_id="job-1", company=COMPANY, *, status="queued"):
    add_source_file(conn)
    conn.execute(
        "INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,"
        "file_format,harness_format,size_bytes,status,stage,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, company, "f" * 64, "合成.md", "md", "md", 10, status,
         "reading", NOW, NOW))


def add_lease(conn, job_id, company=COMPANY, *, seconds=600):
    conn.execute(
        "INSERT INTO md_job_leases VALUES(?,?,?,?,?)",
        (job_id, company, "owner-1", 1, time.time() + seconds))


def add_checkpoint(conn, job_id, state):
    conn.execute(
        "INSERT OR REPLACE INTO memory_kv VALUES(?,?,?)",
        (f"sam://kv/rt/md-direct/{job_id}",
         state if isinstance(state, str) else json.dumps(state), "{}"))


def test_constructor_requires_idle_caller_connection(db):
    with pytest.raises(TypeError):
        MigrationSnapshotReader(":memory:")
    db.execute("INSERT INTO modules VALUES('m-x','模块',99)")
    try:
        assert db.in_transaction
        with pytest.raises(ValueError, match="idle connection"):
            MigrationSnapshotReader(db)
    finally:
        db.rollback()


def test_read_in_transaction_joins_host_transaction_without_ending_it(db):
    reader = MigrationSnapshotReader(db)
    with pytest.raises(ValueError, match="caller-owned transaction"):
        reader.read_in_transaction(company_id=COMPANY)

    db.execute("BEGIN IMMEDIATE")
    result = reader.read_in_transaction(company_id=COMPANY)
    assert db.in_transaction
    db.rollback()
    assert result == reader.read(company_id=COMPANY)


def test_read_requires_bounded_company_id(db):
    reader = MigrationSnapshotReader(db)
    for bad in ("", "  ", "a\nb", "a\rb", "x" * 129, 123, None):
        with pytest.raises(ValueError, match="company_id"):
            reader.read(company_id=bad)


def test_read_is_read_only_deterministic_and_leaves_no_transaction(db):
    add_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_source_chain(db)
    add_evidence(db, fact_id)
    add_placement(db, fact_id)
    add_job(db, "job-done", status="done")
    db.commit()
    before = "\n".join(db.iterdump())
    reader = MigrationSnapshotReader(db)
    first = reader.read(company_id=COMPANY)
    second = reader.read(company_id=COMPANY)
    assert first == second
    assert "\n".join(db.iterdump()) == before
    assert not db.in_transaction


def test_empty_database_marks_every_category_without_silence():
    conn = sqlite3.connect(":memory:")
    try:
        result = MigrationSnapshotReader(conn).read(company_id=COMPANY)
    finally:
        conn.close()
    assert result["binding"] is None
    assert result["coverage"]["diagnostic_refs"]["status"] == "not_implemented"
    for category in ("facts", "placements", "evidence_links",
                     "source_lifecycle", "leaf_revisions", "open_todos",
                     "active_tasks"):
        assert result["snapshot"][category] == []
        assert result["coverage"][category]["status"] == "table_absent", category
    assert result["snapshot"]["diagnostic_refs"] == []


def test_company_isolation(db):
    other_fact = add_fact(db, "acc-b")
    add_ledger(db, other_fact, "acc-b")
    add_source_chain(db, "acc-b", source_id="src-b")
    add_evidence(db, other_fact, "acc-b", source_id="src-b", evidence_id="ev-b")
    add_job(db, "job-b", "acc-b", status="parsing")
    add_checkpoint(db, "job-b", {"company_id": "acc-b"})
    db.execute(
        "INSERT INTO todos(company_id,metric,period,candidate_value,reason,"
        "suggestion,status,source_file,created_at) "
        "VALUES('acc-b','营收','2025','1','conflict','s','open',?,?)",
        ("f" * 64, NOW))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["snapshot"]["facts"] == []
    assert result["snapshot"]["evidence_links"] == []
    assert result["snapshot"]["open_todos"] == []
    assert result["snapshot"]["active_tasks"] == []
    assert result["coverage"]["active_tasks"]["status"] == "ok"
    # The other company's job checkpoint is never looked up for acc-a.
    assert not any("job-b" in note
                   for note in result["coverage"]["active_tasks"]["notes"])
    other = MigrationSnapshotReader(db).read(company_id="acc-b")
    assert [r["fact_row_id"] for r in other["snapshot"]["facts"]] == [other_fact]
    assert other["coverage"]["active_tasks"]["status"] == "active_tasks_present"


# -- binding identity and provenance --

def test_binding_and_provenance_intervals_map_creation_time(db):
    binding = add_binding(db)
    fid1, fid2 = add_fact(db), add_fact(db, entity="乙公司")
    add_placement(db, fid1, leaf_code="l-a",
                  created_at="2026-01-15T00:00:00+00:00")
    add_placement(db, fid2, leaf_code="l-b",
                  created_at="2026-02-15T00:00:00+00:00")
    add_leaf_revision(db, leaf_code="l-a", created_at="2026-01-20T00:00:00+00:00")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["binding"] == {
        "pack_id": "generic-v2", "pack_version": 1,
        "manifest_sha256": binding["digests"][1], "binding_revision": 2}
    placements = {r["leaf_code"]: r for r in result["snapshot"]["placements"]}
    assert placements["l-a"]["binding_identity"] == {
        "pack_id": "generic", "pack_version": 1,
        "manifest_sha256": binding["digests"][0], "binding_revision": 1}
    assert placements["l-b"]["binding_identity"]["binding_revision"] == 2
    assert result["coverage"]["placements"]["status"] == "ok"
    revision = result["snapshot"]["leaf_revisions"][0]
    assert revision["binding_identity"]["binding_revision"] == 1
    assert revision["source_files"] == ["f"]
    assert revision["fact_ids"] == [1]
    assert result["coverage"]["leaf_revisions"]["status"] == "ok"


def test_unprovable_provenance_never_falls_back_to_current_binding(db):
    add_binding(db)
    ambiguous_times = (
        "2025-12-31T23:59:59+00:00",  # before the first history record
        T0,                            # exactly the first switch
        T1,                            # exactly the second switch
        "2026-01-15T00:00:00",         # naive vs aware: not comparable
        "not-a-timestamp")             # unparseable
    for index, created_at in enumerate(ambiguous_times):
        add_placement(db, index + 1, leaf_code=f"l-{index}",
                      created_at=created_at)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["placements"]
    assert coverage["status"] == "ambiguous_provenance"
    assert "5 placement(s)" in coverage["notes"][0]
    for record in result["snapshot"]["placements"]:
        assert record["binding_identity"] is None


def test_placements_without_binding_baseline_are_ambiguous_not_ok(db):
    add_placement(db, 1)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["placements"]
    assert coverage["status"] == "ambiguous_provenance"
    assert "company has no binding baseline" in coverage["notes"]


def test_corrupt_leaf_revision_json_fails_closed(db):
    add_binding(db)
    add_leaf_revision(db, source_files="not-json")
    db.commit()
    with pytest.raises(SnapshotRejected, match="source_files"):
        MigrationSnapshotReader(db).read(company_id=COMPANY)


def _reject(case_db, pattern):
    with pytest.raises(SnapshotRejected, match=pattern):
        MigrationSnapshotReader(case_db).read(company_id=COMPANY)


def test_binding_meta_without_history_baseline_fails_closed():
    conn = make_db()
    try:
        conn.execute(
            "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
            (COMPANY, "合成公司", "generic", 1, T0, 0, "a" * 64,
             "verified-manifest", 1))
        conn.commit()
        _reject(conn, "no history baseline")
    finally:
        conn.close()


def test_binding_meta_missing_identity_fields_fails_closed():
    conn = make_db()
    try:
        conn.execute(
            "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
            (COMPANY, "合成公司", "generic", 1, T0, 0, None, None, 1))
        conn.commit()
        _reject(conn, "lacks required identity fields")
    finally:
        conn.close()


def test_binding_history_revision_gap_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 3, "generic-v3", 1, "c" * 64, "generic", 1, "a" * 64,
             T1, "饭团", "synthetic", None, None, "rebind"))
        conn.commit()
        _reject(conn, "revision gap")
    finally:
        conn.close()


def test_binding_history_invalid_first_record_type_fails_closed():
    conn = make_db()
    try:
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 1, "generic", 1, "a" * 64, None, None, None, T0,
             "饭团", "synthetic", None, None, "rebind"))
        conn.execute(
            "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
            (COMPANY, "合成公司", "generic", 1, T0, 0, "a" * 64,
             "verified-manifest", 1))
        conn.commit()
        _reject(conn, "invalid record type")
    finally:
        conn.close()


def test_binding_history_broken_chain_link_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 2, "generic-v2", 1, "b" * 64, "WRONG", 1, "a" * 64,
             T1, "饭团", "synthetic", None, None, "rebind"))
        conn.execute(
            "UPDATE company_tree_meta SET template_pack='generic-v2',"
            " pack_digest=?, binding_revision=2 WHERE company_id=?",
            ("b" * 64, COMPANY))
        conn.commit()
        _reject(conn, "does not link")
    finally:
        conn.close()


def test_binding_meta_newer_than_history_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        conn.execute(
            "UPDATE company_tree_meta SET binding_revision=2 WHERE company_id=?",
            (COMPANY,))
        conn.commit()
        _reject(conn, "revision differs")
    finally:
        conn.close()


def test_binding_without_archived_manifest_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 2, "ghost", 9, "d" * 64, "generic", 1,
             conn.execute(
                 "SELECT pack_digest FROM company_template_binding_history "
                 "WHERE company_id=? AND binding_revision=1",
                 (COMPANY,)).fetchone()[0],
             T1, "饭团", "synthetic", None, None, "rebind"))
        conn.execute(
            "UPDATE company_tree_meta SET template_pack='ghost',pack_version=9,"
            "pack_digest=?,binding_revision=2 WHERE company_id=?",
            ("d" * 64, COMPANY))
        conn.commit()
        _reject(conn, "no immutable archive")
    finally:
        conn.close()


def test_archived_manifest_hash_mismatch_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        # The archive row claims a digest its JSON does not hash to.
        conn.execute(
            "INSERT INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            ("tampered", 1, "0" * 64, "{}", "published", "饭团",
             "synthetic", T0))
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 2, "tampered", 1, "0" * 64, "generic", 1,
             conn.execute(
                 "SELECT pack_digest FROM company_template_binding_history "
                 "WHERE company_id=? AND binding_revision=1",
                 (COMPANY,)).fetchone()[0],
             T1, "饭团", "synthetic", None, None, "rebind"))
        conn.execute(
            "UPDATE company_tree_meta SET template_pack='tampered',"
            "pack_digest=?,binding_revision=2 WHERE company_id=?",
            ("0" * 64, COMPANY))
        conn.commit()
        _reject(conn, "failed hash verification")
    finally:
        conn.close()


def test_archived_manifest_digest_differs_from_bound_digest_fails_closed():
    conn = make_db()
    try:
        add_binding(conn, revisions=1)
        text, sha = _manifest({"pack_id": "other", "pack_version": 1})
        conn.execute(
            "INSERT INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            ("other", 1, sha, text, "published", "饭团", "synthetic", T0))
        # History and meta agree on a digest that is NOT the archive's.
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 2, "other", 1, "e" * 64, "generic", 1,
             conn.execute(
                 "SELECT pack_digest FROM company_template_binding_history "
                 "WHERE company_id=? AND binding_revision=1",
                 (COMPANY,)).fetchone()[0],
             T1, "饭团", "synthetic", None, None, "rebind"))
        conn.execute(
            "UPDATE company_tree_meta SET template_pack='other',"
            "pack_digest=?,binding_revision=2 WHERE company_id=?",
            ("e" * 64, COMPANY))
        conn.commit()
        _reject(conn, "digest differs")
    finally:
        conn.close()


def test_binding_history_unparseable_event_at_fails_closed():
    conn = make_db()
    try:
        text, sha = _manifest({"pack_id": "generic", "pack_version": 1})
        conn.execute(
            "INSERT INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            ("generic", 1, sha, text, "published", "饭团", "synthetic", T0))
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, 1, "generic", 1, sha, None, None, None,
             "yesterday", "饭团", "synthetic", None, None, "initial"))
        conn.execute(
            "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
            (COMPANY, "合成公司", "generic", 1, T0, 0, sha,
             "verified-manifest", 1))
        conn.commit()
        _reject(conn, "event_at")
    finally:
        conn.close()


# -- facts and the identity/revision/lineage ledger --

def test_facts_without_ledger_derive_legacy_bootstrap_identities(db):
    add_source_file(db)
    first = add_fact(db, source_file="f" * 64, source_page=3,
                     source_span='{"quote":"注册资本100万"}')
    second = add_fact(db, entity="乙公司", value="200万")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "legacy_bootstrap_derived"
    records = {r["fact_row_id"]: r for r in result["snapshot"]["facts"]}
    assert records[first]["identity"] == {
        "fact_uid": f"legacy:{COMPANY}:{first}", "fact_version": 1,
        "source": "derived_legacy_bootstrap"}
    assert records[second]["identity"]["fact_uid"] == \
        f"legacy:{COMPANY}:{second}"
    # Ruling 2026-10-17 #6: the old S0 source coordinates are preserved.
    assert records[first]["source_file"] == "f" * 64
    assert records[first]["source_page"] == 3
    assert records[first]["source_span"] == '{"quote":"注册资本100万"}'


def test_facts_with_consistent_ledger_read_identity_from_ledger(db):
    first = add_fact(db)
    second = add_fact(db, entity="乙公司")
    uid1 = add_ledger(db, first, kind="legacy-bootstrap")
    uid2 = add_ledger(db, second)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["facts"] == {"status": "ok"}
    records = {r["fact_row_id"]: r for r in result["snapshot"]["facts"]}
    assert records[first]["identity"] == {
        "fact_uid": uid1, "fact_version": 1,
        "revision_kind": "legacy-bootstrap", "source": "ledger"}
    assert records[second]["identity"]["fact_uid"] == uid2


def test_partial_identity_ledger_blocks(db):
    first = add_fact(db)
    add_fact(db, entity="乙公司")
    add_ledger(db, first)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "partial_identity_ledger"
    assert "1 of 2 facts" in coverage["notes"][0]


def test_identity_content_digest_mismatch_blocks(db):
    fact_id = add_fact(db)
    add_ledger(db, fact_id, content_digest="0" * 64)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("content digest mismatch" in note for note in coverage["notes"])


def test_identity_revision_gap_blocks(db):
    first = add_fact(db)
    second = add_fact(db, entity="乙公司")
    uid = add_ledger(db, first)
    add_ledger(db, second, uid=uid, version=3)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("revision gap" in note for note in coverage["notes"])


def test_identity_without_revisions_blocks(db):
    add_fact(db)
    db.execute("INSERT INTO fact_identities VALUES(?,?,?,?)",
               (COMPANY, "fact:orphan", NOW, "active"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("has no revisions" in note for note in coverage["notes"])


def test_revision_with_unknown_fact_uid_blocks(db):
    fact_id = add_fact(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,"
        "fact_row_id,content_sha256,revision_kind,supersedes_row_id,actor,"
        "reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, "fact:unknown", 1, fact_id,
         FactIdentityStore._content_digest(_fact_row(db, fact_id)),
         "initial", None, "饭团", "synthetic", NOW))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("unknown fact_uid" in note for note in coverage["notes"])


def test_ledger_events_without_any_mapping_block(db):
    add_fact(db)
    db.execute(
        "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
        ("evt-1", COMPANY, "migration", "饭团", "synthetic", None, NOW))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("has no lineage edges" in note
               for note in coverage["notes"])


def test_dangling_lineage_edges_block(db):
    fact_id = add_fact(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    add_ledger(db, fact_id)
    db.execute(
        "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
        ("evt-9", COMPANY, "migration", "饭团", "synthetic", None, NOW))
    db.execute(
        "INSERT INTO fact_lineage_edges VALUES(?,?,?,?,?,?,?,?)",
        ("evt-9", 1, COMPANY, "fact:ghost", 7, None, None, "retires"))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("missing parent revision" in note for note in coverage["notes"])


def test_orphan_lineage_event_without_edges_blocks(db):
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    db.execute(
        "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
        ("evt-orphan", COMPANY, "migration", "饭团", "synthetic", "m-1", NOW))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("has no lineage edges" in note for note in coverage["notes"])


def test_lineage_edge_relation_must_match_event_type(db):
    parent_id = add_fact(db)
    child_id = add_fact(db, entity="乙公司")
    parent_uid = add_ledger(db, parent_id)
    child_uid = add_ledger(db, child_id)
    db.execute(
        "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
        ("evt-mismatch", COMPANY, "correction", "饭团", "synthetic", None, NOW))
    db.execute(
        "INSERT INTO fact_lineage_edges VALUES(?,?,?,?,?,?,?,?)",
        ("evt-mismatch", 1, COMPANY, parent_uid, 1, child_uid, 1, "split_into"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["facts"]
    assert coverage["status"] == "identity_mismatch"
    assert any("disagrees with event type" in note for note in coverage["notes"])


# -- evidence links and source lifecycle --

def test_evidence_links_read_the_full_source_chain(db):
    fact_id = add_fact(db)
    add_source_chain(db)
    add_evidence(db, fact_id)
    add_evidence(db, fact_id, evidence_id="ev-2", relation="references",
                 status="legacy_backfilled")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["evidence_links"] == {"status": "ok"}
    records = {r["evidence_id"]: r for r in result["snapshot"]["evidence_links"]}
    record = records["ev-1"]
    assert record["relation"] == "supports"
    assert record["status"] == "active"
    assert record["source_id"] == "src-1"
    assert record["version_id"] == "ver-src-1"
    assert record["coordinate"] == {"page": 1}
    assert record["basis"] == {"basis": "literal"}
    assert record["authorization"] == {"grant": "g-1"}
    assert record["content_fingerprint"] == "c" * 64
    assert record["source_kind"] == "file"
    assert record["source_lifecycle"] == "active"
    assert records["ev-2"]["relation"] == "references"
    assert records["ev-2"]["status"] == "legacy_backfilled"


def test_evidence_link_without_resolvable_chain_blocks(db):
    fact_id = add_fact(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO fact_evidence_links VALUES(?,?,?,?,?,?,?,?)",
        (COMPANY, fact_id, "ev-ghost", "supports", "active", "fp", "{}", NOW))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["evidence_links"]
    assert coverage["status"] == "identity_mismatch"
    assert result["snapshot"]["evidence_links"] == []
    assert any("no resolvable" in note for note in coverage["notes"])


def test_evidence_link_with_stale_fact_fingerprint_blocks(db):
    fact_id = add_fact(db)
    add_source_chain(db)
    add_evidence(db, fact_id, fact_fingerprint="sha_" + "0" * 32)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["evidence_links"]
    assert coverage["status"] == "identity_mismatch"
    assert result["snapshot"]["evidence_links"] == []
    assert any("mismatched fact fingerprint" in note for note in coverage["notes"])


def test_missing_source_registry_tables_are_explicit_not_silent():
    conn = make_db(source=False)
    try:
        result = MigrationSnapshotReader(conn).read(company_id=COMPANY)
    finally:
        conn.close()
    evidence = result["coverage"]["evidence_links"]
    assert evidence["status"] == "table_absent"
    for name in ("fact_evidence_links", "source_evidence", "source_versions",
                 "sources"):
        assert any(name in note for note in evidence["notes"])
    assert result["coverage"]["source_lifecycle"]["status"] == "table_absent"


def test_source_lifecycle_records_and_pending_withdrawal(db):
    add_source_chain(db)
    add_source_chain(db, source_id="src-old", lifecycle="pending_removal")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    kinds = sorted(r["record_kind"]
                   for r in result["snapshot"]["source_lifecycle"])
    assert kinds == ["source", "source", "source_event", "source_event",
                     "source_version", "source_version"]
    coverage = result["coverage"]["source_lifecycle"]
    assert coverage["status"] == "pending_source_withdrawal"
    assert "src-old" in coverage["notes"][0]


# -- open todos --

def test_open_todos_cover_all_three_sources_with_kind_and_status(db):
    add_source_file(db)
    fact_id = add_fact(db)
    db.execute(
        "INSERT INTO todos(company_id,metric,period,candidate_value,reason,"
        "suggestion,status,source_file,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (COMPANY, "营收", "2025", "100万", "conflict", "复核", "open",
         "f" * 64, NOW))
    db.execute(
        "INSERT INTO todos(company_id,metric,period,candidate_value,reason,"
        "suggestion,status,source_file,created_at,resolved_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, "营收", "2024", "90万", "conflict", "复核", "resolved",
         "f" * 64, NOW, NOW))
    db.execute(
        "INSERT INTO md_leaf_todos VALUES(?,?,?,?,?,?,?)",
        (COMPANY, "op-1", 1, "leaf-1", 2, json.dumps({"action": "merge"}),
         "pending"))
    db.execute(
        "INSERT INTO md_leaf_todos VALUES(?,?,?,?,?,?,?)",
        (COMPANY, "op-0", 1, "leaf-0", 1, "{}", "done"))
    db.execute(
        "INSERT INTO source_backfill_quarantine VALUES(?,?,?,?,?,?,?,?)",
        (COMPANY, "batch-1", fact_id, "checksum",
         json.dumps({"detail": "x"}), NOW, None, None))
    db.execute(
        "INSERT INTO source_backfill_quarantine VALUES(?,?,?,?,?,?,?,?)",
        (COMPANY, "batch-0", fact_id, "checksum", "{}", NOW, NOW, "batch-9"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["open_todos"] == {"status": "ok"}
    records = result["snapshot"]["open_todos"]
    by_kind = {}
    for record in records:
        by_kind.setdefault(record["kind"], []).append(record)
    assert sorted(by_kind) == ["backfill_quarantine", "md_leaf_todo", "todo"]
    assert len(by_kind["todo"]) == 1
    assert by_kind["todo"][0]["source_status"] == "open"
    assert by_kind["md_leaf_todo"][0]["source_status"] == "pending"
    assert by_kind["md_leaf_todo"][0]["payload"] == {"action": "merge"}
    quarantine = by_kind["backfill_quarantine"][0]
    assert quarantine["fact_row_id"] == fact_id
    assert quarantine["source_status"] == "unresolved"
    assert quarantine["detail"] == {"detail": "x"}


def test_open_todos_missing_tables_are_explicit():
    conn = make_db(source=False, md=False)
    try:
        result = MigrationSnapshotReader(conn).read(company_id=COMPANY)
    finally:
        conn.close()
    coverage = result["coverage"]["open_todos"]
    assert coverage["status"] == "table_absent"
    assert any("md_leaf_todos" in note for note in coverage["notes"])
    assert any("source_backfill_quarantine" in note for note in coverage["notes"])


# -- active tasks --

def test_active_tasks_idle_company_reports_orphan_kv_uncertainty(db):
    add_job(db, "job-done", status="done")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "ok"
    assert result["snapshot"]["active_tasks"] == []
    assert any("orphan" in note for note in coverage["notes"])


def test_active_parse_job_blocks_validation(db):
    add_job(db, "job-run", status="parsing")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "active_tasks_present"
    record = result["snapshot"]["active_tasks"][0]
    assert record["kind"] == "parse_job"
    assert record["job_id"] == "job-run"
    assert record["status"] == "parsing"


def test_retryable_parse_job_blocks_even_without_checkpoint(db):
    add_job(db, "job-failed", status="failed")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["active_tasks"]["status"] == "active_tasks_present"
    record = next(row for row in result["snapshot"]["active_tasks"]
                  if row["kind"] == "parse_job")
    assert record["status"] == "resumable"
    assert record["source_status"] == "failed"


def test_active_lease_blocks_expired_lease_does_not(db):
    add_job(db, "job-a", status="done")
    add_job(db, "job-b", status="done")
    add_lease(db, "job-a")
    add_lease(db, "job-b", seconds=-10)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "active_tasks_present"
    records = result["snapshot"]["active_tasks"]
    assert [r["job_id"] for r in records if r["kind"] == "job_lease"] == ["job-a"]
    assert records[0]["generation"] == 1


def test_lease_of_another_company_is_an_issue_not_activity(db):
    add_job(db, "job-x", status="done")
    add_lease(db, "job-x", "acc-b")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "unidentified_checkpoint"
    assert any("different company" in note for note in coverage["notes"])


def test_checkpoint_variants_are_exact_lookups_only(db):
    # Active job with a well-formed checkpoint: genuine activity.
    add_job(db, "job-live", status="parsing")
    add_checkpoint(db, "job-live", {"company_id": COMPANY, "window": 3})
    # Terminal job whose checkpoint was never cleaned: blocking issue.
    add_job(db, "job-stale", status="done")
    add_checkpoint(db, "job-stale", {"company_id": COMPANY, "done": True})
    # Corrupt checkpoint JSON: blocking issue.
    add_job(db, "job-corrupt", status="parsing")
    add_checkpoint(db, "job-corrupt", "not-json{")
    # Checkpoint naming a different company: blocking issue.
    add_job(db, "job-foreign", status="parsing")
    add_checkpoint(db, "job-foreign", {"company_id": "acc-b"})
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "unidentified_checkpoint"
    kinds = {(r["kind"], r["job_id"]): r
             for r in result["snapshot"]["active_tasks"]}
    assert kinds[("read_checkpoint", "job-live")]["status"] == "active"
    assert kinds[("read_checkpoint", "job-stale")]["status"] == "historical_terminal"
    assert "not valid JSON" in kinds[("read_checkpoint", "job-corrupt")]["issue"]
    assert "company differs" in kinds[("read_checkpoint", "job-foreign")]["issue"]


def test_exact_upload_runtime_uris_are_snapshotted_without_prefix_scan(db):
    add_job(db, "job-done", status="done")
    db.execute(
        "INSERT INTO memory_kv VALUES(?,?,?)",
        ("sam://kv/rt/md-direct/job-done",
         json.dumps({"company_id": COMPANY, "file_hash": "f" * 64,
                     "done": True, "complete": True}), "{}"))
    db.execute(
        "INSERT INTO memory_kv VALUES(?,?,?)",
        ("sam://kv/rt/md-upload-manifest/job-done",
         json.dumps({"staging_id": "synthetic-stage",
                     "manifest": {"file_hash": "f" * 64}}), "{}"))
    db.execute(
        "INSERT INTO memory_kv VALUES(?,?,?)",
        ("sam://kv/rt/md-upload-options/job-done",
         json.dumps({"resume_unresolved": False,
                     "request_id": "synthetic-request"}), "{}"))
    # The reader must not enumerate or interpret an unrelated/orphan key.
    db.execute("INSERT INTO memory_kv VALUES(?,?,?)",
               ("sam://kv/rt/md-direct/orphan", "not-json", "{}"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["active_tasks"]["status"] == "ok"
    records = {row["kind"]: row for row in result["snapshot"]["active_tasks"]}
    assert records["read_checkpoint"]["status"] == "historical_terminal"
    assert records["upload_manifest"]["status"] == "historical_terminal"
    assert records["upload_options"]["content_sha256"]
    assert not any(row["job_id"] == "orphan"
                   for row in result["snapshot"]["active_tasks"])


def test_retryable_checkpoint_blocks_and_is_snapshotted(db):
    add_job(db, "job-partial", status="partial")
    add_checkpoint(db, "job-partial", {"company_id": COMPANY,
                                        "file_hash": "f" * 64,
                                        "done": False})
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["active_tasks"]["status"] == "active_tasks_present"
    checkpoint = next(row for row in result["snapshot"]["active_tasks"]
                      if row["kind"] == "read_checkpoint")
    assert checkpoint["status"] == "resumable"


def test_malformed_upload_manifest_blocks_validation(db):
    add_job(db, "job-done", status="done")
    db.execute(
        "INSERT INTO memory_kv VALUES(?,?,?)",
        ("sam://kv/rt/md-upload-manifest/job-done", "{bad", "{}"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["active_tasks"]["status"] == \
        "unidentified_checkpoint"
    assert any("not valid JSON" in note
               for note in result["coverage"]["active_tasks"]["notes"])


def test_memory_kv_absent_with_jobs_cannot_verify_checkpoints():
    conn = make_db(kv=False)
    try:
        add_job(conn, "job-1", status="done")
        conn.commit()
        result = MigrationSnapshotReader(conn).read(company_id=COMPANY)
    finally:
        conn.close()
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "table_absent"
    assert any("memory_kv" in note for note in coverage["notes"])


def test_unfinished_backfill_batch_is_active(db):
    db.execute(
        "INSERT INTO source_backfill_batches VALUES(?,?,?,?,?,?,?)",
        (COMPANY, "batch-open", "sha", NOW, None, None, None))
    db.execute(
        "INSERT INTO source_backfill_batches VALUES(?,?,?,?,?,?,?)",
        (COMPANY, "batch-done", "sha", NOW, NOW, "{}", "ok"))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "active_tasks_present"
    records = result["snapshot"]["active_tasks"]
    assert [r["batch_id"] for r in records] == ["batch-open"]


def test_unpublished_verified_notes_are_pending_work(db):
    fact_id = add_fact(db)
    for note_id in ("note-draft", "note-open"):
        db.execute(
            "INSERT INTO md_read_notes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (COMPANY, "job-n", note_id, "f" * 64, "fp", "w-1",
             "verified" if note_id == "note-open" else "needs_check",
             1, "sha", "{}", NOW))
    db.execute(
        "INSERT INTO md_note_publications VALUES(?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, "job-n", "note-draft", fact_id, "leaf-1", "op-1", "g-1",
         "sha", "{}", NOW))
    # A verified note without any publication row is pending publication;
    # published notes and non-verified notes are not.
    db.execute(
        "INSERT INTO md_read_notes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, "job-n", "note-published", "f" * 64, "fp", "w-1",
         "verified", 1, "sha", "{}", NOW))
    db.execute(
        "INSERT INTO md_note_publications VALUES(?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, "job-n", "note-published", fact_id, "leaf-1", "op-2",
         "g-1", "sha", "{}", NOW))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "active_tasks_present"
    records = [r for r in result["snapshot"]["active_tasks"]
               if r["kind"] == "unpublished_note"]
    assert [r["note_id"] for r in records] == ["note-open"]


def test_pending_source_removal_outranks_plain_activity(db):
    add_source_chain(db, lifecycle="pending_removal")
    add_job(db, "job-run", status="queued")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    coverage = result["coverage"]["active_tasks"]
    assert coverage["status"] == "pending_source_withdrawal"
    assert any("pending source removal" in note for note in coverage["notes"])


def test_checkpoint_issues_outrank_pending_source_removal(db):
    add_source_chain(db, lifecycle="pending_removal")
    add_job(db, "job-stale", status="done")
    add_checkpoint(db, "job-stale", "not-json{")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["active_tasks"]["status"] == \
        "unidentified_checkpoint"


# -- integration: reader output feeds the builder and the state gate --

def _plan_inputs(result, plan_id, fact_id, coverage):
    return dict(
        plan_id=plan_id, company_id=COMPANY, created_by="范范", created_at=NOW,
        source={"pack_id": result["binding"]["pack_id"],
                "pack_version": result["binding"]["pack_version"],
                "manifest_sha256": result["binding"]["manifest_sha256"]},
        target={"pack_id": "generic-v2", "pack_version": 1,
                "manifest_sha256": "b" * 64},
        binding_revision=result["binding"]["binding_revision"],
        snapshot=result["snapshot"], coverage=coverage,
        dispositions=[{"fact_row_id": fact_id, "action": "keep",
                       "reason": "合成验收",
                       "planned_fact_refs": [f"plan:{plan_id}:f1"]}],
        planned_facts=[{"plan_fact_ref": f"plan:{plan_id}:f1",
                        "operation": "keep", "source_fact_row_ids": [fact_id],
                        "target_primary_leaf": {"trunk_code": "trunk-1",
                                                "leaf_code": "leaf-1"},
                        "evidence_ids": ["ev-1"]}],
        target_leaf_codes=[{"trunk_code": "trunk-1", "leaf_code": "leaf-1"}],
        prechecks={"reversibility": {"status": "pass", "detail": "合成"},
                   "target_invariants": {"status": "pass", "detail": "合成"},
                   "independent_reviewer": "饭团"},
        impact_assessments={"boundary_disagreements": [],
                            "insufficient_sources": [],
                            "numeric_coordinate_anomalies": [],
                            "todo_invalidations": [],
                            "diagnostic_invalidations": [],
                            "projection_rebuild_targets": []})


def test_reader_output_feeds_builder_and_blocking_coverage_gates_state(db):
    add_binding(db, revisions=1)
    fact_id = add_fact(db)
    add_source_chain(db)
    add_evidence(db, fact_id)
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    # The reader's coverage passes the builder shape checks verbatim.
    plan = build_migration_plan(
        **_plan_inputs(result, "plan-int-1", fact_id, result["coverage"]))
    ledger = MigrationPlanStateLedger(db)
    with db:
        assert ledger.register_plan(plan)["state"] == "draft"
    # diagnostic_refs stays not_implemented: validated is hard-blocked and
    # writes no event (ruling 2026-10-17 #1).
    with pytest.raises(ValueError, match="diagnostic_refs: not_implemented"):
        ledger.transition(plan["plan_id"], "validated", "校验员")
    assert ledger.status(plan["plan_id"])["event_count"] == 1
    # An auditable confirmed_absent proof lifts the block; the changed
    # coverage changes the snapshot fingerprint and the plan digest.
    lifted = dict(result["coverage"])
    lifted["diagnostic_refs"] = {
        "status": "confirmed_absent",
        "proof": {"statement": "该公司无诊断/专题引用对象",
                  "confirmed_by": "范范",
                  "confirmed_at": NOW, "method": "人工巡检"}}
    lifted_plan = build_migration_plan(
        **_plan_inputs(result, "plan-int-2", fact_id, lifted))
    assert lifted_plan["snapshot_fingerprint"] != plan["snapshot_fingerprint"]
    with db:
        ledger.register_plan(lifted_plan)
        assert ledger.transition(
            lifted_plan["plan_id"], "validated", "校验员")["state"] == "validated"


# -- versioned placement ledger (P2 §3.3, §7) --

def add_leaf_binding(conn, company=COMPANY, *, revisions=1):
    """Binding chain whose archived manifests carry trunk/leaf definitions."""
    packs = [("generic", 1), ("generic-v2", 1)][:revisions]
    times = [T0, T1][:revisions]
    digests = []
    for pack_id, pack_version in packs:
        text, sha = _manifest({
            "pack": pack_id, "version": pack_version,
            "trunks": [{"code": "trunk-1"}, {"code": "trunk-2"}],
            "leaves": [{"code": "leaf-1", "trunk": "trunk-1"},
                       {"code": "leaf-2", "trunk": "trunk-2"}]})
        conn.execute(
            "INSERT OR IGNORE INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            (pack_id, pack_version, sha, text, "published", "饭团",
             "synthetic", T0))
        digests.append(sha)
    for index, (pack_id, pack_version) in enumerate(packs):
        if index:
            prev = (packs[index - 1][0], packs[index - 1][1], digests[index - 1])
        else:
            prev = (None, None, None)
        conn.execute(
            "INSERT INTO company_template_binding_history VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company, index + 1, pack_id, pack_version, digests[index],
             prev[0], prev[1], prev[2], times[index], "饭团", "synthetic",
             None, None, "initial" if index == 0 else "rebind"))
    pack_id, pack_version = packs[-1]
    conn.execute(
        "INSERT INTO company_tree_meta VALUES(?,?,?,?,?,?,?,?,?)",
        (company, "合成公司", pack_id, pack_version, times[-1], 0,
         digests[-1], "verified-manifest", revisions))
    return {"packs": packs, "digests": digests}


_NO_PREV = (None, None, None, None, None, None)


def add_versioned_placement(conn, fact_id, company=COMPANY, *, seq, pack_id,
                            pack_version, digest, trunk="trunk-1", leaf="leaf-1",
                            binding_revision=1, prev=_NO_PREV,
                            event_at="2026-01-15T00:00:00+00:00",
                            record_type="initial", migration_id=None,
                            pointer=True):
    conn.execute(
        "INSERT INTO fact_placements "
        "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
        "manifest_sha256,binding_revision,trunk_code,leaf_code,prev_pack_id,"
        "prev_pack_version,prev_manifest_sha256,prev_binding_revision,"
        "prev_trunk_code,prev_leaf_code,event_at,actor,reason,record_type,"
        "migration_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company, fact_id, seq, pack_id, pack_version, digest, binding_revision,
         trunk, leaf, prev[0], prev[1], prev[2], prev[3], prev[4], prev[5], event_at, "饭团",
         "synthetic", record_type, migration_id))
    if pointer:
        conn.execute(
            "INSERT OR REPLACE INTO fact_current_placement "
            "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
            "manifest_sha256,binding_revision,trunk_code,leaf_code,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (company, fact_id, seq, pack_id, pack_version, digest,
             binding_revision, trunk, leaf, event_at))


def _bound_digest(conn, company=COMPANY, revision=1):
    return conn.execute(
        "SELECT pack_digest FROM company_template_binding_history "
        "WHERE company_id=? AND binding_revision=?",
        (company, revision)).fetchone()[0]


def _versioned_records(result):
    return [r for r in result["snapshot"]["placements"]
            if r.get("source") == "versioned_placement_ledger"]


def test_versioned_and_legacy_placements_coexist_without_folding(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    # A legacy link and a versioned placement naming the SAME leaf code are
    # two separate records; nothing is folded by text or leaf code.
    add_placement(db, fact_id, leaf_code="leaf-1", trunk="trunk-1")
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["placements"] == {"status": "ok"}
    records = result["snapshot"]["placements"]
    legacy = [r for r in records if r.get("source") != "versioned_placement_ledger"]
    versioned = _versioned_records(result)
    assert len(records) == 2 and len(legacy) == 1 and len(versioned) == 1
    assert legacy[0]["leaf_code"] == versioned[0]["leaf_code"] == "leaf-1"
    assert "source_file" in legacy[0] and "source" not in legacy[0]
    current = versioned[0]
    assert current["is_current"] is True
    assert current["placement_seq"] == 1
    assert current["record_type"] == "initial"
    assert current["manifest_sha256"] == _bound_digest(db)
    assert current["binding_revision"] == 1
    assert current["binding_identity"]["binding_revision"] == 1
    assert current["prev_pack_id"] is None


def test_versioned_ledger_absent_or_empty_keeps_legacy_behavior(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_placement(db, fact_id)
    db.commit()
    present = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert present["coverage"]["placements"] == {"status": "ok"}
    assert _versioned_records(present) == []
    # Both tables absent: byte-identical legacy-only behavior.
    db.execute("DROP TABLE fact_current_placement")
    db.execute("DROP TABLE fact_placements")
    absent = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert absent["snapshot"]["placements"] == present["snapshot"]["placements"]
    assert absent["coverage"]["placements"] == present["coverage"]["placements"]


def test_partial_versioned_ledger_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    db.execute("DROP TABLE fact_current_placement")
    db.commit()
    with pytest.raises(SnapshotRejected, match="partially present"):
        MigrationSnapshotReader(db).read(company_id=COMPANY)


def test_reassign_history_marks_only_the_latest_event_current(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=digest, pointer=False)
    add_versioned_placement(
        db, fact_id, seq=2, pack_id="generic", pack_version=1, digest=digest,
        trunk="trunk-2", leaf="leaf-2",
        prev=("generic", 1, digest, 1, "trunk-1", "leaf-1"),
        event_at="2026-01-20T00:00:00+00:00", record_type="reassign")
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["placements"] == {"status": "ok"}
    records = _versioned_records(result)
    assert len(records) == 2
    first, second = records
    assert first["is_current"] is False
    assert second["is_current"] is True
    assert second["record_type"] == "reassign"
    assert (second["prev_pack_id"], second["prev_trunk_code"],
            second["prev_binding_revision"], second["prev_leaf_code"]) == \
        ("generic", "trunk-1", 1, "leaf-1")


def _assert_placement_mismatch(conn, pattern):
    result = MigrationSnapshotReader(conn).read(company_id=COMPANY)
    coverage = result["coverage"]["placements"]
    assert coverage["status"] == "identity_mismatch"
    assert any(pattern in note for note in coverage["notes"]), coverage["notes"]
    # Records are still emitted for audit; nothing is silently dropped.
    return result


def test_placement_sequence_gap_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    db.execute("DROP TRIGGER fact_placements_seq_guard")
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=digest, pointer=False)
    add_versioned_placement(
        db, fact_id, seq=3, pack_id="generic", pack_version=1, digest=digest,
        prev=("generic", 1, digest, 1, "trunk-1", "leaf-1"),
        event_at="2026-01-20T00:00:00+00:00", record_type="reassign")
    db.commit()
    _assert_placement_mismatch(db, "sequence gap")


def test_placement_broken_prev_chain_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=digest, pointer=False)
    add_versioned_placement(
        db, fact_id, seq=2, pack_id="generic", pack_version=1, digest=digest,
        prev=("generic", 1, digest, 1, "trunk-2", "leaf-2"),  # not the seq-1 leaf
        event_at="2026-01-20T00:00:00+00:00", record_type="reassign")
    db.commit()
    _assert_placement_mismatch(db, "does not link consecutive identities")


def test_placement_first_row_naming_prev_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    add_versioned_placement(
        db, fact_id, seq=1, pack_id="generic", pack_version=1, digest=digest,
        prev=("generic", 1, digest, 1, "trunk-1", "leaf-1"))
    db.commit()
    _assert_placement_mismatch(db, "must not name a previous identity")


def test_placement_without_current_pointer_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db),
                            pointer=False)
    db.commit()
    _assert_placement_mismatch(db, "no current placement pointer")


def test_current_pointer_differing_from_latest_event_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=digest)
    db.execute(
        "UPDATE fact_current_placement SET trunk_code='trunk-2',leaf_code='leaf-2' "
        "WHERE company_id=? AND fact_row_id=?", (COMPANY, fact_id))
    db.commit()
    result = _assert_placement_mismatch(
        db, "differs from the latest placement event")
    assert _versioned_records(result)[0]["is_current"] is False


def test_current_pointer_without_history_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    orphan_id = add_fact(db, entity="乙公司")
    add_ledger(db, fact_id)
    add_ledger(db, orphan_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO fact_current_placement "
        "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
        "manifest_sha256,binding_revision,trunk_code,leaf_code,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, orphan_id, 1, "generic", 1, _bound_digest(db), 1,
         "trunk-1", "leaf-1", NOW))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    _assert_placement_mismatch(db, "has no placement history")


def test_placement_event_outside_binding_interval_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db),
                            event_at="2025-12-15T00:00:00+00:00")
    db.commit()
    _assert_placement_mismatch(db, "no provable unique binding interval")


def test_placement_identity_differing_from_interval_fails_closed(db):
    add_leaf_binding(db, revisions=2)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    # The event time falls strictly inside revision 1 (T0, T1), but the row
    # claims the revision-2 pack identity.
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic-v2",
                            pack_version=1, digest=_bound_digest(db, revision=2),
                            event_at="2026-01-15T00:00:00+00:00")
    db.commit()
    _assert_placement_mismatch(db, "differs from its binding interval identity")


def test_placement_binding_revision_differing_from_interval_fails_closed(db):
    add_leaf_binding(db, revisions=2)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(
        db, fact_id, seq=1, pack_id="generic", pack_version=1,
        digest=_bound_digest(db, revision=1), binding_revision=2,
        event_at="2026-01-15T00:00:00+00:00")
    db.commit()
    _assert_placement_mismatch(db, "differs from its binding interval identity")


def test_legacy_placement_rows_remain_unproven_after_additive_upgrade(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TABLE fact_current_placement")
    db.execute("DROP TABLE fact_placements")
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
    db.execute(
        "INSERT INTO fact_placements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (COMPANY, fact_id, 1, "generic", 1, digest, "trunk-1", "leaf-1",
         None, None, None, None, None, "2026-01-15T00:00:00+00:00",
         "饭团", "legacy row", "initial", None))
    db.execute(
        "INSERT INTO fact_current_placement VALUES(?,?,?,?,?,?,?,?,?)",
        (COMPANY, fact_id, 1, "generic", 1, digest, "trunk-1", "leaf-1", NOW))
    db.commit()
    _assert_placement_mismatch(
        db, "lacks exact binding provenance fields")
    _migrate_fact_placement_binding_revision(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    result = _assert_placement_mismatch(
        db, "has no explicit positive binding_revision")
    placement = _versioned_records(result)[0]
    assert placement["binding_revision"] is None
    assert placement["binding_identity"] is None


def test_placement_manifest_digest_mismatch_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest="0" * 64)
    db.commit()
    _assert_placement_mismatch(db, "digest differs from the archived digest")


def test_placement_unknown_leaf_or_trunk_mismatch_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    digest = _bound_digest(db)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=digest, leaf="leaf-9")
    db.commit()
    _assert_placement_mismatch(db, "not in the archived manifest")


def test_placement_leaf_trunk_mismatch_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    # leaf-2 belongs to trunk-2 in the archived manifest, not trunk-1.
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db),
                            trunk="trunk-1", leaf="leaf-2")
    db.commit()
    _assert_placement_mismatch(db, "does not belong to trunk")


def test_placement_without_identity_revision_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)  # no add_ledger: unmapped fact version
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    db.commit()
    result = _assert_placement_mismatch(db, "without an identity ledger revision")
    # The facts category still derives legacy identities independently.
    assert result["coverage"]["facts"]["status"] == "legacy_bootstrap_derived"


def test_placement_of_missing_manifest_fails_closed(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    add_versioned_placement(db, fact_id, seq=1, pack_id="ghost",
                            pack_version=9, digest="0" * 64,
                            event_at="2026-01-15T00:00:00+00:00")
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    _assert_placement_mismatch(db, "no immutable archive")


def test_versioned_placements_are_company_isolated(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    other_fact = add_fact(db, "acc-b")
    db.execute(
        "INSERT INTO fact_placements "
        "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
        "manifest_sha256,binding_revision,trunk_code,leaf_code,prev_pack_id,"
        "prev_pack_version,prev_manifest_sha256,prev_binding_revision,"
        "prev_trunk_code,prev_leaf_code,event_at,actor,reason,record_type,"
        "migration_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acc-b", other_fact, 1, "generic", 1, _bound_digest(db), 1,
         "trunk-1", "leaf-1", None, None, None, None, None, None,
         "2026-01-15T00:00:00+00:00", "饭团", "synthetic", "initial", None))
    db.execute(
        "INSERT INTO fact_current_placement "
        "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
        "manifest_sha256,binding_revision,trunk_code,leaf_code,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("acc-b", other_fact, 1, "generic", 1, _bound_digest(db), 1,
         "trunk-1", "leaf-1", NOW))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["placements"] == {"status": "ok"}
    versioned = _versioned_records(result)
    assert len(versioned) == 1
    assert versioned[0]["fact_row_id"] == fact_id
    assert all(r["company_id"] == COMPANY for r in versioned)


def test_snapshot_with_versioned_placements_feeds_builder_and_gate(db):
    add_leaf_binding(db)
    fact_id = add_fact(db)
    add_ledger(db, fact_id)
    add_source_chain(db)
    add_evidence(db, fact_id)
    add_versioned_placement(db, fact_id, seq=1, pack_id="generic",
                            pack_version=1, digest=_bound_digest(db))
    db.commit()
    result = MigrationSnapshotReader(db).read(company_id=COMPANY)
    assert result["coverage"]["placements"] == {"status": "ok"}
    plan = build_migration_plan(
        **_plan_inputs(result, "plan-vp-1", fact_id, result["coverage"]))
    assert plan["input_snapshot"]["placements"]["count"] == 1
    ledger = MigrationPlanStateLedger(db)
    with db:
        ledger.register_plan(plan)
    # A corrupted placement ledger (identity_mismatch) blocks the validated
    # edge just like any other blocking coverage status.
    blocked = dict(result["coverage"])
    blocked["placements"] = {"status": "identity_mismatch",
                             "notes": ["synthetic placement corruption"]}
    blocked_plan = build_migration_plan(
        **_plan_inputs(result, "plan-vp-2", fact_id, blocked))
    with db:
        ledger.register_plan(blocked_plan)
    with pytest.raises(ValueError, match="placements: identity_mismatch"):
        ledger.transition(blocked_plan["plan_id"], "validated", "校验员")
    assert ledger.status(blocked_plan["plan_id"])["event_count"] == 1
