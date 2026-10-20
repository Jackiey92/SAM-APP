"""Database initialization and connection helpers."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import os
from pathlib import Path

from .schema import (
    DOC_TYPES,
    MODULES,
    SCHEMA,
    SCHEMA_VERSION,
)


def _configured_db_path() -> Path:
    """Resolve the default ledger under the deployment's SAM data root."""
    data_root = os.environ.get("SAM_DATA_ROOT")
    if not data_root:
        raise RuntimeError("Set SAM_DATA_ROOT explicitly (or pass an explicit database path)")
    return Path(data_root) / "app.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    # An explicit path is authoritative for tests and scoped stores.  Only
    # the no-argument path follows the deployment data-root contract.
    db_path = _configured_db_path() if db_path is None else Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Multiple parse workers deliberately share the same SQLite ledger.  Wait
    # for the current writer instead of letting a short lock race surface as a
    # misleading downstream integrity failure.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn



def validate_schema(db_path: Path | str | None = None, *, snapshot: bool = False) -> None:
    path = _configured_db_path() if db_path is None else Path(db_path)
    hint = f"Run python -m app.db.migrate --db {path}" + (" --snapshot" if snapshot else "")
    if not path.is_file():
        raise RuntimeError(f"Database is missing. {hint}")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        table = "cloud_snapshot" if snapshot else "parse_jobs"
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if version != SCHEMA_VERSION or not exists:
            raise RuntimeError(f"Schema version {version}, expected {SCHEMA_VERSION}. {hint}")


def init_db(db_path: Path | str, *, embedding=None) -> None:
    db_path = Path(db_path)
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Refusing schema downgrade: {version} > {SCHEMA_VERSION}")
        # Legacy M1 databases have a NOT NULL module column.  Upgrade before
        # SCHEMA creates its new company/current index, which refers to the
        # fields absent from that old table.
        if _classification_needs_rebuild(conn):
            _migrate_file_classifications(conn)
        _migrate_classification_review_flag(conn)
        _migrate_facts(conn)
        _migrate_todos(conn)
        _migrate_entity_roster(conn)
        _migrate_entity_bridge(conn)
        conn.executescript(SCHEMA)
        _migrate_fact_placement_binding_revision(conn)
        _migrate_facts(conn)
        if "l2_committed" not in {row["name"] for row in conn.execute("PRAGMA table_info(parse_jobs)")}:
            conn.execute("ALTER TABLE parse_jobs ADD COLUMN l2_committed INTEGER NOT NULL DEFAULT 0")
        if "projection_fact_ids" not in {row["name"] for row in conn.execute("PRAGMA table_info(parse_jobs)")}:
            conn.execute("ALTER TABLE parse_jobs ADD COLUMN projection_fact_ids TEXT")
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(parse_jobs)")}
        for name, definition in {
            "quality_status": "TEXT", "quality_model_calls": "INTEGER NOT NULL DEFAULT 0",
            "quality_script_calls": "INTEGER NOT NULL DEFAULT 0", "quality_reworks": "INTEGER NOT NULL DEFAULT 0",
            "quality_issues": "TEXT",
        }.items():
            if name not in job_columns:
                conn.execute(f"ALTER TABLE parse_jobs ADD COLUMN {name} {definition}")
        _migrate_module_dictionary(conn)
        from ..memory.initialize import initialize_memory
        initialize_memory(conn, embedding=embedding)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _migrate_file_classifications(conn: sqlite3.Connection) -> None:
    """Upgrade the early M1 classification table without discarding history.

    The original table made ``module`` NOT NULL and had no company or basis
    fields.  SQLite cannot relax that constraint in place, so rebuild only
    this append-only ledger and carry existing rows forward as name-based
    classifications in the default company scope.
    """
    if not _classification_needs_rebuild(conn):
        return

    conn.execute("DROP INDEX IF EXISTS idx_class_file")
    conn.execute("DROP INDEX IF EXISTS idx_class_status")
    conn.execute("DROP INDEX IF EXISTS idx_class_company_current")
    conn.executescript(
        """
        CREATE TABLE file_classifications_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash     TEXT NOT NULL REFERENCES source_files(file_hash),
            company_id    TEXT NOT NULL DEFAULT 'default',
            module        TEXT REFERENCES modules(code),
            doc_type      TEXT REFERENCES doc_types(code),
            confidence    REAL NOT NULL DEFAULT 0.0,
            needs_review  INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
            status        TEXT NOT NULL DEFAULT 'auto',
            classified_by TEXT NOT NULL DEFAULT 'auto',
            basis         TEXT NOT NULL DEFAULT 'name',
            created_at    TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO file_classifications_new
            (id, file_hash, company_id, module, doc_type, confidence, needs_review, status,
             classified_by, basis, created_at)
        SELECT id, file_hash, 'default', module, doc_type, confidence,
               0, CASE WHEN status='pending' THEN 'auto' ELSE status END,
               classified_by, 'name', created_at
        FROM file_classifications
        """
    )
    conn.execute("DROP TABLE file_classifications")
    conn.execute("ALTER TABLE file_classifications_new RENAME TO file_classifications")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_class_file ON file_classifications(file_hash);
        CREATE INDEX IF NOT EXISTS idx_class_status ON file_classifications(status);
        CREATE INDEX IF NOT EXISTS idx_class_company_current
            ON file_classifications(company_id, status, file_hash, id);
        """
    )


def _classification_needs_rebuild(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info(file_classifications)").fetchall()
    }
    if not columns:
        return False
    module_column = columns.get("module")
    module_not_null = bool(module_column["notnull"]) if module_column is not None else False
    return module_not_null or not {"company_id", "basis"}.issubset(columns)


def _migrate_classification_review_flag(conn: sqlite3.Connection) -> None:
    """Add the employee review marker without changing existing decisions."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(file_classifications)").fetchall()
    }
    if columns and "needs_review" not in columns:
        conn.execute(
            "ALTER TABLE file_classifications "
            "ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0 "
            "CHECK(needs_review IN (0, 1))"
        )


def _migrate_facts(conn: sqlite3.Connection) -> None:
    """Add 2B consolidation coordinates to the pre-existing M1 facts table."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if not columns:
        return
    # SQLite permits ADD COLUMN with a literal default, preserving all historic
    # M1 rows under their explicit legacy/default coordinate.
    if "module_key" not in columns:
        conn.execute("ALTER TABLE facts ADD COLUMN module_key TEXT")
    if "company_id" not in columns:
        conn.execute("ALTER TABLE facts ADD COLUMN company_id TEXT NOT NULL DEFAULT 'default'")
    if "period" not in columns:
        conn.execute("ALTER TABLE facts ADD COLUMN period TEXT NOT NULL DEFAULT 'unspecified'")
    if "confirm_mode" not in columns:
        conn.execute("ALTER TABLE facts ADD COLUMN confirm_mode TEXT NOT NULL DEFAULT 'manual'")
    fact_run_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(fact_runs)").fetchall()
    }
    if fact_run_columns and "bridge_artifact_id" not in fact_run_columns:
        conn.execute("ALTER TABLE fact_runs ADD COLUMN bridge_artifact_id TEXT")


def _migrate_fact_placement_binding_revision(conn: sqlite3.Connection) -> None:
    """Add exact binding-revision fields without inventing old provenance.

    Existing placement history cannot be losslessly assigned a binding
    revision after the fact. The nullable columns intentionally leave those
    rows unproven; snapshot validation and reassignment reject them until an
    explicitly designed repair is authorized.
    """
    for table, column, definition in (
        ("fact_placements", "binding_revision",
         "INTEGER CHECK(binding_revision IS NULL OR binding_revision > 0)"),
        ("fact_placements", "prev_binding_revision",
         "INTEGER CHECK(prev_binding_revision IS NULL OR prev_binding_revision > 0)"),
        ("fact_current_placement", "binding_revision",
         "INTEGER CHECK(binding_revision IS NULL OR binding_revision > 0)"),
    ):
        columns = {str(row[1]) for row in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if columns and column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_todos(conn: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if columns and "entity" not in columns:
        conn.execute("ALTER TABLE todos ADD COLUMN entity TEXT NOT NULL DEFAULT ''")
    for name in ("handoff_json", "handoff_key"):
        if columns and name not in columns:
            conn.execute(f"ALTER TABLE todos ADD COLUMN {name} TEXT")


def _migrate_module_dictionary(conn: sqlite3.Connection) -> None:
    """Orthogonalize labels and invalidate legacy ownership, atomically.

    A legacy parent_module column is the one-time migration marker, including
    old legal/sales/hr codes that collide with new keys. Keep every historical
    type label verbatim; neither prototype mapping infers an owner or a type.
    SQLite table replacement needs FK checks temporarily disabled outside a
    transaction. All changes below commit together or roll back together.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        legacy = any(row["name"] == "parent_module"
                     for row in conn.execute("PRAGMA table_info(doc_types)"))
        if legacy:
            old_codes = [row["code"] for row in conn.execute("SELECT code FROM modules")]
            for code in old_codes:
                conn.execute(
                    "UPDATE file_classifications SET module=NULL, needs_review=1 WHERE module=?",
                    (code,),
                )
            conn.execute("CREATE TABLE doc_types_new (code TEXT PRIMARY KEY, name TEXT NOT NULL, "
                         "sort_order INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO doc_types_new SELECT code,name,sort_order FROM doc_types")
            conn.execute("DROP TABLE doc_types")
            conn.execute("ALTER TABLE doc_types_new RENAME TO doc_types")
        conn.executemany(
            "INSERT INTO modules(code,name,sort_order) VALUES (?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, sort_order=excluded.sort_order",
            MODULES,
        )
        conn.executemany(
            "INSERT INTO doc_types(code,name,sort_order) VALUES (?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, sort_order=excluded.sort_order",
            DOC_TYPES,
        )
        marks = ",".join("?" for _ in MODULES)
        codes = [code for code, _, _ in MODULES]
        conn.execute(
            f"UPDATE file_classifications SET module=NULL, needs_review=1 "
            f"WHERE module IS NOT NULL AND module NOT IN ({marks})", codes,
        )
        conn.execute(f"DELETE FROM modules WHERE code NOT IN ({marks})", codes)
        # Pre-FK databases can contain orphan labels. Retain the exact tag as
        # an historical dictionary label instead of inventing a replacement.
        conn.execute("INSERT OR IGNORE INTO doc_types(code,name,sort_order) "
                     "SELECT DISTINCT doc_type,doc_type,0 FROM file_classifications "
                     "WHERE doc_type IS NOT NULL")
        violations = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check(file_classifications)")
                      if row["parent"] in {"modules", "doc_types"}]
        if violations:
            raise sqlite3.IntegrityError(f"dictionary migration foreign key violations: {violations!r}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_entity_roster(conn: sqlite3.Connection) -> None:
    """Add the declared/extracted provenance to an existing 2.0 roster."""
    rows = conn.execute("PRAGMA table_info(entity_roster)").fetchall()
    columns = {str(row["name"]): row for row in rows}
    if columns and "origin" not in columns:
        conn.execute("ALTER TABLE entity_roster ADD COLUMN origin TEXT NOT NULL DEFAULT 'extracted'")
        rows = conn.execute("PRAGMA table_info(entity_roster)").fetchall()
        columns = {str(row["name"]): row for row in rows}
    source_file = columns.get("source_file")
    if source_file is not None and bool(source_file["notnull"]):
        # Early 2.0 created source_file NOT NULL, which made a host-declared
        # self impossible to store without inventing a source. Preserve every
        # row while relaxing only that constraint.
        conn.execute("DROP INDEX IF EXISTS idx_entity_roster_current")
        conn.executescript(
            """
            CREATE TABLE entity_roster_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                entity_type TEXT,
                aliases TEXT NOT NULL DEFAULT '[]',
                credit_code TEXT,
                stock_code TEXT,
                origin TEXT NOT NULL DEFAULT 'extracted',
                status TEXT NOT NULL DEFAULT 'suggested',
                source_file TEXT REFERENCES source_files(file_hash),
                source_page INTEGER,
                source_span TEXT,
                superseded_by INTEGER REFERENCES entity_roster_new(id),
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO entity_roster_new
               (id,company_id,entity_name,entity_type,aliases,credit_code,stock_code,origin,
                status,source_file,source_page,source_span,superseded_by,created_at)
               SELECT id,company_id,entity_name,entity_type,aliases,credit_code,stock_code,
                      COALESCE(origin,'extracted'),status,source_file,source_page,source_span,
                      superseded_by,created_at FROM entity_roster"""
        )
        conn.execute("DROP TABLE entity_roster")
        conn.execute("ALTER TABLE entity_roster_new RENAME TO entity_roster")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_entity_roster_current
               ON entity_roster(company_id, status, superseded_by, id)"""
        )


def _migrate_entity_bridge(conn: sqlite3.Connection) -> None:
    """Migrate bridge rows to independent per-file employee artifacts.

    The old table made every immutable block depend on a shared
    ``entity_bridge_runs`` parent.  That parent ordering was unnecessary for
    attribution and exposed concurrent workers to FK failures.  Keep the
    historical table for reads, but remove only that parent dependency; the
    source-file FK remains the immutable evidence guard.
    """
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(entity_bridge_blocks)").fetchall()}
    if not columns:
        return
    definition = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_bridge_blocks'"
    ).fetchone()
    foreign_keys = conn.execute("PRAGMA foreign_key_list(entity_bridge_blocks)").fetchall()
    has_run_parent = any(str(row["table"]) == "entity_bridge_runs" for row in foreign_keys)
    needs_rebuild = (
        definition is not None
        and (
            "'employee'" not in str(definition[0])
            or has_run_parent
            or "artifact_id" not in columns
            or "reason" not in columns
            or "source" not in columns
        )
    )
    if needs_rebuild:
        # SQLite cannot alter CHECK/FK constraints. Rebuild only this bridge
        # audit table; source_files and all R1 tables remain untouched.
        conn.execute("DROP INDEX IF EXISTS idx_entity_bridge_blocks_run")
        conn.execute("DROP INDEX IF EXISTS idx_entity_bridge_blocks_review")
        artifact_expr = "artifact_id" if "artifact_id" in columns else "CAST(run_id AS TEXT)"
        reason_expr = "reason" if "reason" in columns else "NULL"
        source_expr = "source" if "source" in columns else "'deterministic'"
        conn.executescript(
            f"""
            CREATE TABLE entity_bridge_blocks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                artifact_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                file_hash TEXT NOT NULL REFERENCES source_files(file_hash),
                block_index INTEGER NOT NULL,
                block_type TEXT NOT NULL CHECK(block_type IN ('text', 'table_row')),
                content TEXT NOT NULL,
                source_page INTEGER,
                source_span TEXT,
                classification TEXT NOT NULL CHECK(classification IN ('self', 'related', 'foreign', 'ambiguous')),
                relation TEXT,
                subject TEXT,
                reason TEXT,
                source TEXT NOT NULL DEFAULT 'deterministic'
                    CHECK(source IN ('deterministic', 'employee', 'model', 'human')),
                needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
                decision TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO entity_bridge_blocks_new
                (id,run_id,artifact_id,company_id,file_hash,block_index,block_type,content,source_page,source_span,
                 classification,relation,subject,reason,source,needs_review,decision,status,created_at)
            SELECT id,run_id,{artifact_expr},company_id,file_hash,block_index,block_type,content,source_page,source_span,
                   classification,relation,subject,{reason_expr},{source_expr},needs_review,decision,status,created_at
            FROM entity_bridge_blocks;
            DROP TABLE entity_bridge_blocks;
            ALTER TABLE entity_bridge_blocks_new RENAME TO entity_bridge_blocks;
            CREATE INDEX IF NOT EXISTS idx_entity_bridge_blocks_run
                ON entity_bridge_blocks(run_id, block_index);
            CREATE INDEX IF NOT EXISTS idx_entity_bridge_blocks_review
                ON entity_bridge_blocks(company_id, needs_review, status, id);
            """
        )

    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(entity_bridge_blocks)")}
    if "consolidated" not in columns:
        conn.execute("ALTER TABLE entity_bridge_blocks ADD COLUMN consolidated INTEGER CHECK(consolidated IN (0, 1))")


if __name__ == "__main__":
    from .migrate import main
    main()
