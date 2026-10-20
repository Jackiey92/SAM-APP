"""Explicit memory-store migration, never invoked by provider construction."""

def initialize_memory(conn, *, embedding=None):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS m1_projection_config (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            embedder TEXT NOT NULL, model_version TEXT NOT NULL, dim INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS m1_chunks (
            chunk_id TEXT PRIMARY KEY, uri TEXT NOT NULL, company_id TEXT NOT NULL,
            module_key TEXT NOT NULL, chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL,
            embedding BLOB NOT NULL, content_hash TEXT NOT NULL, attribution_id TEXT,
            embedder TEXT NOT NULL, model_version TEXT NOT NULL,
            status TEXT NOT NULL, source_span TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS m1_chunks_scope
            ON m1_chunks(company_id,module_key,status);
    """)
    from ..db.schema import LEAF_SCHEMA
    conn.executescript(LEAF_SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(m1_chunks)")}
    upgraded_fact_ids = 'fact_id' not in columns
    if upgraded_fact_ids:
        conn.execute("ALTER TABLE m1_chunks ADD COLUMN fact_id INTEGER")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS m1_documents (
            uri TEXT PRIMARY KEY, company_id TEXT NOT NULL, module_key TEXT NOT NULL,
            content TEXT NOT NULL, metadata TEXT NOT NULL, fact_key TEXT,
            fact_json TEXT,
            UNIQUE(company_id,fact_key));
        CREATE INDEX IF NOT EXISTS m1_documents_scope ON m1_documents(company_id);
        CREATE TABLE IF NOT EXISTS m1_projection_state (
            company_id TEXT PRIMARY KEY,status TEXT NOT NULL,error TEXT,
            generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS memory_kv (
            uri TEXT PRIMARY KEY, content TEXT NOT NULL, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS m1_directories (
            uri TEXT PRIMARY KEY, company_id TEXT NOT NULL);
    """)
    for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS m2_facts_{event.lower()}
            AFTER {event} ON facts BEGIN
            INSERT INTO m1_projection_state(company_id,status,error,generation)
            VALUES({ref}.company_id,'stale',NULL,1)
            ON CONFLICT(company_id) DO UPDATE SET status='stale',
            generation=m1_projection_state.generation+1;
            END""")
    # Preserve the old provider's legacy invalidation, but only inside this
    # explicitly requested migration (never on application construction).
    legacy_companies = {r[0] for r in conn.execute(
        "SELECT DISTINCT company_id FROM m1_documents WHERE fact_json IS NOT NULL")}
    if upgraded_fact_ids:
        legacy_companies.update(r[0] for r in conn.execute("SELECT DISTINCT company_id FROM m1_chunks"))
    for company in legacy_companies:
        conn.execute("""INSERT INTO m1_projection_state(company_id,status,error,generation)
            VALUES(?,'stale',NULL,1)
            ON CONFLICT(company_id) DO UPDATE SET status='stale',error=excluded.error,
                generation=m1_projection_state.generation+1""", (company,))
    if embedding is None:
        return
    expected = (embedding.embedder, embedding.model_version, embedding.dim)
    current = conn.execute("SELECT embedder,model_version,dim FROM m1_projection_config WHERE singleton=1").fetchone()
    if current is not None and tuple(current) != expected:
        raise ValueError("embedding provider/version differs: whole-store migration required")
    conn.execute("INSERT OR IGNORE INTO m1_projection_config VALUES(1,?,?,?)", expected)
