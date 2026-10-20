"""SQLite schema for M1: source files, classification, fact ledger, dictionaries."""

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 原文 manifest（不可变，一份原件一行）
CREATE TABLE IF NOT EXISTS source_files (
    file_hash      TEXT PRIMARY KEY,
    original_name  TEXT NOT NULL,
    mime_type      TEXT,
    size_bytes     INTEGER NOT NULL,
    storage_path   TEXT NOT NULL,
    origin_zone    TEXT NOT NULL DEFAULT 'internal',  -- internal | quarantine
    status         TEXT NOT NULL DEFAULT 'active',
    uploaded_by    TEXT,
    uploaded_at    TEXT NOT NULL
);

-- 模块字典
CREATE TABLE IF NOT EXISTS modules (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- 正交文件类型标签字典
CREATE TABLE IF NOT EXISTS doc_types (
    code           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    sort_order     INTEGER NOT NULL DEFAULT 0
);

-- 归类记录（append-only；改类 = 追加新行，旧行 superseded）。
-- module 可为空：正文没有足够证据时，宁可进入未归类视图也不猜测。
CREATE TABLE IF NOT EXISTS file_classifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT NOT NULL REFERENCES source_files(file_hash),
    company_id    TEXT NOT NULL DEFAULT 'default',
    module        TEXT REFERENCES modules(code),
    doc_type      TEXT REFERENCES doc_types(code),
    confidence    REAL NOT NULL DEFAULT 0.0,
    needs_review  INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
    status        TEXT NOT NULL DEFAULT 'auto',      -- auto | confirmed | superseded
    classified_by TEXT NOT NULL DEFAULT 'auto',      -- auto | human
    basis         TEXT NOT NULL DEFAULT 'name',      -- name | content | human
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_file ON file_classifications(file_hash);
CREATE INDEX IF NOT EXISTS idx_class_status ON file_classifications(status);
CREATE INDEX IF NOT EXISTS idx_class_company_current
    ON file_classifications(company_id, status, file_hash, id);

-- 文件补充标签（多对多）
CREATE TABLE IF NOT EXISTS file_tags (
    file_hash TEXT NOT NULL REFERENCES source_files(file_hash),
    tag       TEXT NOT NULL,
    PRIMARY KEY (file_hash, tag)
);

-- 事实台账（FactVoucher，append-only）
CREATE TABLE IF NOT EXISTS facts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL DEFAULT 'default',
    entity         TEXT NOT NULL,
    attribute      TEXT NOT NULL,
    period         TEXT NOT NULL DEFAULT 'unspecified',
    value          TEXT NOT NULL,
    value_type     TEXT NOT NULL DEFAULT 'string',
    unit           TEXT,
    source_file    TEXT REFERENCES source_files(file_hash),
    source_page    INTEGER,
    source_span    TEXT,
    valid_from     TEXT,
    valid_to       TEXT,
    confidence     REAL NOT NULL DEFAULT 1.0,
    status         TEXT NOT NULL DEFAULT 'verified', -- verified | claimed | inferred
    confirm_mode   TEXT NOT NULL DEFAULT 'manual',   -- auto | manual
    superseded_by  INTEGER REFERENCES facts(id),
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity, attribute);
CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(valid_to);
CREATE INDEX IF NOT EXISTS idx_facts_coordinate
    ON facts(company_id, attribute, period, superseded_by);

-- 需要人工决策的候选事实。候选从不覆盖当前 facts；处理结果留在此表。
CREATE TABLE IF NOT EXISTS todos (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id           TEXT NOT NULL,
    entity               TEXT NOT NULL DEFAULT '',
    metric               TEXT NOT NULL,
    period               TEXT NOT NULL,
    candidate_value      TEXT NOT NULL,
    candidate_value_type TEXT NOT NULL DEFAULT 'string',
    candidate_unit       TEXT,
    existing_value       TEXT,
    reason               TEXT NOT NULL, -- conflict | critical_review | anomaly | uncertain
    suggestion           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open', -- open | resolved | dismissed
    related_fact_id      INTEGER REFERENCES facts(id),
    source_file          TEXT NOT NULL REFERENCES source_files(file_hash),
    source_page          INTEGER,
    source_span          TEXT,
    handoff_json         TEXT,
    handoff_key          TEXT,
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_company_status ON todos(company_id, status, id);
CREATE INDEX IF NOT EXISTS idx_todos_coordinate ON todos(company_id, metric, period, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_handoff ON todos(company_id, handoff_key)
    WHERE handoff_key IS NOT NULL;

-- 异步上传中转区任务。原件在 source_files，解析与固化状态独立可轮询。
CREATE TABLE IF NOT EXISTS parse_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL UNIQUE,
    company_id        TEXT NOT NULL,
    file_hash         TEXT NOT NULL REFERENCES source_files(file_hash),
    original_name     TEXT NOT NULL,
    file_format       TEXT NOT NULL,
    harness_format    TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued',
    stage             TEXT NOT NULL DEFAULT 'queued',
    l2_committed      INTEGER NOT NULL DEFAULT 0,
    projection_fact_ids TEXT,
    quality_status TEXT,
    quality_model_calls INTEGER NOT NULL DEFAULT 0,
    quality_script_calls INTEGER NOT NULL DEFAULT 0,
    quality_reworks INTEGER NOT NULL DEFAULT 0,
    quality_issues TEXT,
    progress_current  INTEGER NOT NULL DEFAULT 0,
    progress_total    INTEGER,
    message           TEXT,
    error_kind        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_parse_jobs_company_status
    ON parse_jobs(company_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parse_jobs_status
    ON parse_jobs(status, created_at);

-- Parser staging/audit are migrated explicitly, never created on first use.
CREATE TABLE IF NOT EXISTS parse_staging (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    file_hash TEXT,
    format TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

-- 2A controlled corrections. Original blobs and old rows remain immutable;
-- active is the current projection and superseded rows are audit history.
CREATE TABLE IF NOT EXISTS source_edits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash       TEXT NOT NULL REFERENCES source_files(file_hash),
    company_id      TEXT NOT NULL,
    operation       TEXT NOT NULL CHECK(operation IN ('replace', 'remove')),
    target_locator  TEXT NOT NULL,
    replacement_text TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    supersedes_id   INTEGER REFERENCES source_edits(id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_edits_current
    ON source_edits(file_hash, company_id, target_locator, status, id);

-- 2.0 deterministic entity roster suggestions and review transitions.
-- Rows are retained; superseded_by points to the next review state.
CREATE TABLE IF NOT EXISTS entity_roster (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT NOT NULL,
    entity_name   TEXT NOT NULL,
    entity_type   TEXT,
    aliases       TEXT NOT NULL DEFAULT '[]',
    credit_code   TEXT,
    stock_code    TEXT,
    origin        TEXT NOT NULL DEFAULT 'extracted'
                  CHECK(origin IN ('declared', 'extracted')),
    status        TEXT NOT NULL DEFAULT 'suggested'
                  CHECK(status IN ('suggested', 'active', 'rejected')),
    source_file   TEXT REFERENCES source_files(file_hash),
    source_page   INTEGER,
    source_span   TEXT,
    superseded_by INTEGER REFERENCES entity_roster(id),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_roster_current
    ON entity_roster(company_id, status, superseded_by, id);

-- 2.1.a deterministic entity attribution runs and immutable source blocks.
CREATE TABLE IF NOT EXISTS entity_bridge_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL,
    file_hash    TEXT NOT NULL REFERENCES source_files(file_hash),
    status       TEXT NOT NULL DEFAULT 'completed',
    block_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entity_bridge_runs_company
    ON entity_bridge_runs(company_id, created_at DESC, id DESC);

-- Independent per employee/file attribution artifact. This is deliberately
-- not a child of entity_bridge_runs; its only evidence FK is source_files.
CREATE TABLE IF NOT EXISTS entity_bridge_artifacts (
    artifact_id  TEXT PRIMARY KEY,
    run_id       INTEGER NOT NULL UNIQUE,
    company_id   TEXT NOT NULL,
    file_hash    TEXT NOT NULL REFERENCES source_files(file_hash),
    status       TEXT NOT NULL DEFAULT 'completed',
    block_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entity_bridge_artifacts_file
    ON entity_bridge_artifacts(company_id, file_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS entity_bridge_blocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    -- ``run_id`` is a compatibility grouping token only.  New writes do not
    -- depend on the legacy entity_bridge_runs parent table.
    run_id         INTEGER NOT NULL,
    artifact_id    TEXT NOT NULL,
    company_id     TEXT NOT NULL,
    file_hash      TEXT NOT NULL REFERENCES source_files(file_hash),
    block_index    INTEGER NOT NULL,
    block_type     TEXT NOT NULL CHECK(block_type IN ('text', 'table_row')),
    content        TEXT NOT NULL,
    source_page    INTEGER,
    source_span    TEXT,
    classification TEXT NOT NULL CHECK(classification IN ('self', 'related', 'foreign', 'ambiguous')),
    relation       TEXT,
    subject        TEXT,
    reason         TEXT,
    source         TEXT NOT NULL DEFAULT 'deterministic'
                   CHECK(source IN ('deterministic', 'employee', 'model', 'human')),
    needs_review   INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
    consolidated   INTEGER CHECK(consolidated IN (0, 1)),
    decision       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_bridge_blocks_run
    ON entity_bridge_blocks(run_id, block_index);
CREATE INDEX IF NOT EXISTS idx_entity_bridge_blocks_review
    ON entity_bridge_blocks(company_id, needs_review, status, id);

-- 2B.a deterministic L1 extraction ledger.
CREATE TABLE IF NOT EXISTS fact_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL,
    file_hash       TEXT NOT NULL REFERENCES source_files(file_hash),
    bridge_run_id   INTEGER REFERENCES entity_bridge_runs(id),
    bridge_artifact_id TEXT,
    status          TEXT NOT NULL DEFAULT 'completed',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    fact_count      INTEGER NOT NULL DEFAULT 0,
    todo_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_runs_company
    ON fact_runs(company_id, created_at DESC, id DESC);

-- Company-scoped dashboard visibility only; this never deletes source data.
CREATE TABLE IF NOT EXISTS dashboard_preferences (
    company_id   TEXT NOT NULL,
    dashboard_id TEXT NOT NULL,
    card_id      TEXT NOT NULL,
    hidden       INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0, 1)),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY(company_id, dashboard_id, card_id)
);

"""

# Non-vector metadata projection is separate from brief/guide and facts/KV.
FACTS_TREE_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_tree_meta (
    company_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    template_pack TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    bound_at TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
    pack_digest TEXT,
    digest_source TEXT CHECK(digest_source IN ('legacy-bootstrap','verified-manifest')),
    binding_revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS company_template_binding_history (
    company_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    template_pack TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    pack_digest TEXT NOT NULL,
    prev_template_pack TEXT,
    prev_pack_version INTEGER,
    prev_pack_digest TEXT,
    event_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    migration_id TEXT,
    approval_ref TEXT,
    record_type TEXT NOT NULL,
    PRIMARY KEY (company_id, binding_revision)
);
CREATE TRIGGER IF NOT EXISTS company_template_binding_history_no_update
BEFORE UPDATE ON company_template_binding_history
BEGIN SELECT RAISE(ABORT,'binding history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS company_template_binding_history_no_delete
BEFORE DELETE ON company_template_binding_history
BEGIN SELECT RAISE(ABORT,'binding history is append-only'); END;
CREATE TABLE IF NOT EXISTS company_tree_meta_projection (
    company_id TEXT PRIMARY KEY,
    uri TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL
);
"""

# Immutable template manifest registry (P2 §3.1, §7). One published archive
# per (pack_id, pack_version); archived rows never change.
TEMPLATE_MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS template_manifests (
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('published')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (pack_id, pack_version)
);
CREATE TRIGGER IF NOT EXISTS template_manifests_no_update
BEFORE UPDATE ON template_manifests
BEGIN SELECT RAISE(ABORT,'template manifests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS template_manifests_no_delete
BEFORE DELETE ON template_manifests
BEGIN SELECT RAISE(ABORT,'template manifests are immutable'); END;
"""
FACTS_TREE_SCHEMA += TEMPLATE_MANIFEST_SCHEMA

# Immutable migration plan snapshots (P2 §5.1). The full normalized plan
# document (source/target identity, snapshot fingerprints, per-fact
# dispositions, creator, timestamps) lives in plan_json; the digest covers
# the canonical document. No status column: the state machine will be an
# append-only event table in a later slice.
TREE_MIGRATION_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tree_migration_plans (
    plan_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    plan_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS tree_migration_plans_no_update
BEFORE UPDATE ON tree_migration_plans
BEGIN SELECT RAISE(ABORT,'migration plans are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tree_migration_plans_no_delete
BEFORE DELETE ON tree_migration_plans
BEGIN SELECT RAISE(ABORT,'migration plans are immutable'); END;
"""
FACTS_TREE_SCHEMA += TREE_MIGRATION_PLAN_SCHEMA

# Append-only migration plan state events (P2 §5.2). Current state is always
# rebuilt from the ordered event log; plan rows never carry a status. The
# enum includes the executor states so future slices need no DDL change, but
# the service refuses to write them until a migration executor exists.
TREE_MIGRATION_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS tree_migration_plan_events (
    plan_id TEXT NOT NULL REFERENCES tree_migration_plans(plan_id),
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN
        ('draft','validated','awaiting_user_approval','approved','rejected',
         'blocked','expired','executing','applied','failed','rolled_back')),
    plan_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, event_seq)
);
CREATE TRIGGER IF NOT EXISTS tree_migration_plan_events_no_update
BEFORE UPDATE ON tree_migration_plan_events
BEGIN SELECT RAISE(ABORT,'plan events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tree_migration_plan_events_no_delete
BEFORE DELETE ON tree_migration_plan_events
BEGIN SELECT RAISE(ABORT,'plan events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tree_migration_plan_events_seq_guard
BEFORE INSERT ON tree_migration_plan_events
BEGIN
    SELECT RAISE(ABORT,'plan event sequence gap')
    WHERE NEW.event_seq != (SELECT COALESCE(MAX(event_seq),0)+1
                            FROM tree_migration_plan_events WHERE plan_id=NEW.plan_id);
    SELECT RAISE(ABORT,'plan event digest differs from the stored plan')
    WHERE (SELECT plan_sha256 FROM tree_migration_plans WHERE plan_id=NEW.plan_id) IS NULL
       OR NEW.plan_sha256 != (SELECT plan_sha256 FROM tree_migration_plans
                              WHERE plan_id=NEW.plan_id);
END;
"""
FACTS_TREE_SCHEMA += TREE_MIGRATION_EVENT_SCHEMA

# Fact identity / revision / lineage ledger (P2 design §3.2, §3.3). Storage
# contract only: nothing here scans, backfills or rewrites existing facts.
# facts.id remains the permanent fact-row key; fact_uid is the stable logical
# identity. Append-only via triggers. supersedes_row_id points from the new
# version to the replaced old row, matching facts.superseded_by in reverse.
FACT_IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_identities (
    company_id TEXT NOT NULL,
    fact_uid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    identity_state TEXT NOT NULL DEFAULT 'active' CHECK(identity_state IN ('active','retired')),
    PRIMARY KEY (company_id, fact_uid)
);
CREATE TABLE IF NOT EXISTS fact_revisions (
    company_id TEXT NOT NULL,
    fact_uid TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    fact_row_id INTEGER NOT NULL UNIQUE REFERENCES facts(id),
    content_sha256 TEXT NOT NULL,
    revision_kind TEXT NOT NULL CHECK(revision_kind IN
        ('initial','correction','split','merge','migration','legacy-bootstrap')),
    supersedes_row_id INTEGER REFERENCES facts(id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (company_id, fact_uid, fact_version),
    FOREIGN KEY (company_id, fact_uid) REFERENCES fact_identities(company_id, fact_uid)
);
CREATE TABLE IF NOT EXISTS fact_lineage_events (
    event_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN
        ('correction','split','merge','retract','migration')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    migration_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_lineage_edges (
    event_id TEXT NOT NULL REFERENCES fact_lineage_events(event_id),
    edge_seq INTEGER NOT NULL,
    company_id TEXT NOT NULL,
    parent_fact_uid TEXT NOT NULL,
    parent_fact_version INTEGER NOT NULL,
    child_fact_uid TEXT,
    child_fact_version INTEGER,
    relation TEXT NOT NULL CHECK(relation IN
        ('supersedes','split_into','merged_into','retires','migrated_into')),
    PRIMARY KEY (event_id, edge_seq),
    CHECK ((relation = 'retires' AND child_fact_uid IS NULL AND child_fact_version IS NULL)
        OR (relation != 'retires' AND child_fact_uid IS NOT NULL
            AND child_fact_version IS NOT NULL)),
    FOREIGN KEY (company_id, parent_fact_uid, parent_fact_version)
        REFERENCES fact_revisions(company_id, fact_uid, fact_version),
    FOREIGN KEY (company_id, child_fact_uid, child_fact_version)
        REFERENCES fact_revisions(company_id, fact_uid, fact_version)
);
CREATE TRIGGER IF NOT EXISTS fact_revisions_company_guard
BEFORE INSERT ON fact_revisions
BEGIN
    SELECT RAISE(ABORT,'fact row belongs to a different company')
    WHERE (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) IS NULL
       OR (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) != NEW.company_id;
    SELECT RAISE(ABORT,'superseded fact row belongs to a different company')
    WHERE NEW.supersedes_row_id IS NOT NULL
      AND ((SELECT company_id FROM facts WHERE id=NEW.supersedes_row_id) IS NULL
        OR (SELECT company_id FROM facts WHERE id=NEW.supersedes_row_id) != NEW.company_id);
END;
CREATE TRIGGER IF NOT EXISTS fact_identities_no_update
BEFORE UPDATE ON fact_identities
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_identities_no_delete
BEFORE DELETE ON fact_identities
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_revisions_no_update
BEFORE UPDATE ON fact_revisions
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_revisions_no_delete
BEFORE DELETE ON fact_revisions
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_lineage_events_no_update
BEFORE UPDATE ON fact_lineage_events
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_lineage_events_no_delete
BEFORE DELETE ON fact_lineage_events
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_lineage_edges_no_update
BEFORE UPDATE ON fact_lineage_edges
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_lineage_edges_no_delete
BEFORE DELETE ON fact_lineage_edges
BEGIN SELECT RAISE(ABORT,'fact identity ledger is append-only'); END;
"""
FACTS_TREE_SCHEMA += FACT_IDENTITY_SCHEMA

# Versioned fact placement attribution ledger (P2 design §3.3, §7). Storage
# contract only: nothing here scans, backfills or rewrites fact_leaf_links
# or facts.module_key; those legacy rows keep their legacy semantics.
# fact_placements is the append-only per-fact-version placement history,
# chained like company_template_binding_history; fact_current_placement is
# the mutable current pointer with at most one row per fact version
# (mirroring company_tree_meta), referencing the exact history sequence it
# reflects. Placement retirement is not supported in this slice.
FACT_PLACEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_placements (
    company_id TEXT NOT NULL,
    fact_row_id INTEGER NOT NULL REFERENCES facts(id),
    placement_seq INTEGER NOT NULL,
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    binding_revision INTEGER NOT NULL CHECK(binding_revision > 0),
    trunk_code TEXT NOT NULL,
    leaf_code TEXT NOT NULL,
    prev_pack_id TEXT,
    prev_pack_version INTEGER,
    prev_manifest_sha256 TEXT,
    prev_binding_revision INTEGER CHECK(prev_binding_revision IS NULL OR prev_binding_revision > 0),
    prev_trunk_code TEXT,
    prev_leaf_code TEXT,
    event_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    record_type TEXT NOT NULL CHECK(record_type IN ('initial','reassign','migration')),
    migration_id TEXT,
    PRIMARY KEY (company_id, fact_row_id, placement_seq),
    FOREIGN KEY (pack_id, pack_version)
        REFERENCES template_manifests(pack_id, pack_version)
);
CREATE TABLE IF NOT EXISTS fact_current_placement (
    company_id TEXT NOT NULL,
    fact_row_id INTEGER NOT NULL,
    placement_seq INTEGER NOT NULL,
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    binding_revision INTEGER NOT NULL CHECK(binding_revision > 0),
    trunk_code TEXT NOT NULL,
    leaf_code TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (company_id, fact_row_id),
    FOREIGN KEY (company_id, fact_row_id, placement_seq)
        REFERENCES fact_placements(company_id, fact_row_id, placement_seq)
);
CREATE TRIGGER IF NOT EXISTS fact_placements_company_guard
BEFORE INSERT ON fact_placements
BEGIN
    SELECT RAISE(ABORT,'fact row belongs to a different company')
    WHERE (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) IS NULL
       OR (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) != NEW.company_id;
END;
CREATE TRIGGER IF NOT EXISTS fact_placements_seq_guard
BEFORE INSERT ON fact_placements
BEGIN
    SELECT RAISE(ABORT,'placement sequence gap')
    WHERE NEW.placement_seq != (SELECT COALESCE(MAX(placement_seq),0)+1
                                FROM fact_placements
                                WHERE company_id=NEW.company_id
                                  AND fact_row_id=NEW.fact_row_id);
END;
CREATE TRIGGER IF NOT EXISTS fact_current_placement_company_guard
BEFORE INSERT ON fact_current_placement
BEGIN
    SELECT RAISE(ABORT,'fact row belongs to a different company')
    WHERE (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) IS NULL
       OR (SELECT company_id FROM facts WHERE id=NEW.fact_row_id) != NEW.company_id;
END;
CREATE TRIGGER IF NOT EXISTS fact_placements_no_update
BEFORE UPDATE ON fact_placements
BEGIN SELECT RAISE(ABORT,'placement ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fact_placements_no_delete
BEFORE DELETE ON fact_placements
BEGIN SELECT RAISE(ABORT,'placement ledger is append-only'); END;
"""
FACTS_TREE_SCHEMA += FACT_PLACEMENT_SCHEMA
SCHEMA += FACTS_TREE_SCHEMA

# B1 leaf history and stable URI index; no vector/provider coupling.
LEAF_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_leaf_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL, fact_id INTEGER NOT NULL,
    trunk TEXT NOT NULL, leaf_code TEXT NOT NULL, source_file TEXT NOT NULL,
    job_id TEXT, created_at TEXT NOT NULL,
    UNIQUE(company_id, fact_id, leaf_code)
);
CREATE INDEX IF NOT EXISTS idx_fll_leaf ON fact_leaf_links(company_id, leaf_code);
CREATE INDEX IF NOT EXISTS idx_fll_file ON fact_leaf_links(company_id, source_file);
CREATE TABLE IF NOT EXISTS leaf_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL, leaf_code TEXT NOT NULL, version INTEGER NOT NULL,
    content_sha TEXT NOT NULL, source_files TEXT NOT NULL, fact_ids TEXT NOT NULL,
    owner_skill TEXT NOT NULL, attribution_id TEXT, change_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(company_id, leaf_code, version)
);
CREATE INDEX IF NOT EXISTS idx_leaf_rev_company
    ON leaf_revisions(company_id, leaf_code, version);
CREATE TABLE IF NOT EXISTS facts_tree_leaf_index (
    company_id TEXT NOT NULL, leaf_code TEXT NOT NULL, uri TEXT NOT NULL UNIQUE,
    PRIMARY KEY(company_id, leaf_code)
);
"""
SCHEMA += LEAF_SCHEMA

# Structure only: employee decisions are never inferred from this dictionary.
from ..module_taxonomy import MODULES as MEMORY_MODULES

MODULES = [(m.key, m.directory, m.order) for m in MEMORY_MODULES]

DOC_TYPES = [
    ("subsidiary_profile", "下属公司简介", 1),
    ("business_overview", "业务概述", 2),
    ("development_milestones", "发展大事记", 3),
    ("financial_statement", "财务报表", 1),
    ("audit_report", "审计报告", 2),
    ("tax_document", "税务资料", 3),
    ("budget_forecast", "预算与预测", 4),
    ("contract", "合同与协议", 1),
    ("litigation", "争议与诉讼", 2),
    ("compliance", "合规与监管", 3),
    ("intellectual_property", "知识产权", 4),
    ("product_spec", "产品与规格", 1),
    ("research_development", "研发项目", 2),
    ("patent", "专利", 3),
    ("quality_certification", "质量认证", 4),
    ("customer_profile", "客户资料", 1),
    ("sales_order", "销售订单", 2),
    ("market_research", "市场研究", 3),
    ("channel_campaign", "渠道与活动", 4),
    ("personnel", "人员与组织", 1),
    ("compensation", "薪酬", 2),
    ("equity", "股权", 3),
    ("governance", "治理与股东", 4),
    ("unclassified", "未归类", 1),
]
