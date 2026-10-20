"""Company-prefiltered sqlite-vec projection, separate from authoritative content."""
import hashlib
import math
import sqlite3


def chunk_id(uri: str, index: int) -> str:
    return hashlib.md5(f"{uri}#{index}".encode(), usedforsecurity=False).hexdigest()


class VectorProjection:
    def __init__(self, connection: sqlite3.Connection, embedding):
        import sqlite_vec

        self.db, self.embedding, self.vec = connection, embedding, sqlite_vec
        if type(embedding.dim) is not int or not 1 <= embedding.dim <= 65536:
            raise ValueError("invalid embedding dimension")
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        self.upgraded_fact_ids = False
        expected = (embedding.embedder, embedding.model_version, embedding.dim)
        current = connection.execute(
            "SELECT embedder,model_version,dim FROM m1_projection_config WHERE singleton=1"
        ).fetchone()
        if current is None or tuple(current) != expected:
            raise ValueError("embedding provider/version differs: whole-store migration required")

    def blob(self, vector):
        if len(vector) != self.embedding.dim or not all(math.isfinite(x) for x in vector):
            raise ValueError("embedding returned wrong dimension or nonfinite values")
        if not any(vector):
            raise ValueError("embedding returned a zero vector")
        return self.vec.serialize_float32(vector)

    def search(self, address, query: str, *, top_k: int, filters=None, document_uris=None):
        vector = self.blob(self.embedding.embed_query(query))
        # MATERIALIZED CTE physically selects only this company's allowed rows before
        # sqlite-vec cosine distance. Exact scan is intentional at M1's small scale.
        filters = filters or {}
        allowed = ('period', 'entity', 'attribute', 'unit', 'module_key')
        clauses = "".join(f" AND f.{key}=?" for key in allowed if key in filters)
        args = [filters[key] for key in allowed if key in filters]
        if document_uris is not None:
            import json
            clauses += " AND c.fact_id IS NULL AND c.uri IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(document_uris, ensure_ascii=False))
        rows = self.db.execute(f"""
            WITH scoped AS MATERIALIZED (
                SELECT c.* FROM m1_chunks c
                LEFT JOIN facts f ON f.id=c.fact_id AND f.company_id=c.company_id
                WHERE c.company_id=? AND c.status='verified'
                    AND (c.fact_id IS NULL OR (f.status='verified' AND f.superseded_by IS NULL AND f.valid_to IS NULL))
                    AND (?='' OR c.module_key=?)
                    AND (c.uri=? OR substr(c.uri,1,length(?)+1)=?||'/')
                    {clauses}
            )
            SELECT uri,1-vec_distance_cosine(embedding,?) AS score,
                   chunk_text,module_key,company_id,chunk_index
            FROM scoped ORDER BY score DESC,chunk_id LIMIT ?
        """, (address.company_id, address.module_key, address.module_key,
              address.uri, address.uri, address.uri, *args, vector, top_k)).fetchall()
        return [dict(uri=r[0], score=r[1], snippet=r[2][:700], module_key=r[3],
                     company_id=r[4], chunk_index=r[5]) for r in rows]
