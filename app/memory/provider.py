"""Local SAM documents and read-only projections of the facts ledger."""
from copy import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Sequence

from . import paths
from .chunking import split_markdown
from .embedding import EmbeddingProvider, LocalBgeProvider
from .projection import VectorProjection, chunk_id
from .taxonomy import projection_module, MODULE_KEYS, UNCLASSIFIED, TODO_DIRECTORY
from .. import settings
from ..db.database import validate_schema
from ..ports import RerankProvider


class ProjectionError(RuntimeError):
    """The authoritative write survived; this company's projection needs rebuilding."""


class LocalVectorMemoryProvider:
    """Host-scoped SAM memory with facts-ledger-only authority.

    Fact search is company scoped. JSON KV lives in a separate non-vector table.
    The session namespace remains reserved; runtime KV does not activate it.
    """
    def __init__(self, database: str | Path, *, embedding: EmbeddingProvider | None = None,
                 company_id: str | None = None, top_k: int = 20,
                 reranker: RerankProvider | None = None, rerank_enabled: bool = False, allow_external_rerank: bool = False):
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if rerank_enabled and reranker is None:
            raise ValueError("rerank requires an explicitly injected provider")
        if rerank_enabled and getattr(reranker, 'external', True) and not allow_external_rerank:
            raise ValueError("external rerank is disabled")
        self.reranker = reranker if rerank_enabled else None
        validate_schema(database)
        self.database = Path(database)
        self.memory_root = paths.FACTS_ROOT.rstrip("/")
        self.supports_session = False
        self.supports_runtime_company_listing = False
        self.supports_structured_filters = True
        self.company_id = paths.identifier(company_id) if company_id is not None else None
        self.top_k, self.lock = top_k, RLock()
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        try:
            self.embedding = embedding if embedding is not None else LocalBgeProvider()
            self.projection = VectorProjection(self.db, self.embedding)
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def scoped(self, company_id: str):
        """Host-only view sharing the connection/lock, not a client tool operation."""
        scoped = copy(self)
        scoped.company_id = self._company(company_id)
        return scoped

    def _kv_address(self, uri: str):
        return paths.parse(uri, company_id=self.company_id)

    def _company(self, company_id: str) -> str:
        company = paths.identifier(company_id)
        if self.company_id is not None and company != self.company_id:
            raise ValueError("company differs from injected host scope")
        return company

    def _tree_address(self, uri: str, address, *, allow_search_prefix=False):
        """Recognize canonical tree documents, without widening legacy paths."""
        from ..module_tree import MODULES
        modules = {m.directory: m.key for m in MODULES}
        parts = address.parts
        if address.area != "facts" or len(parts) < 2:
            return None
        if len(parts) >= 3 and parts[0] == "co" and parts[2] == "tree":
            return self._packed_tree_address(uri, address, allow_search_prefix=allow_search_prefix)
        directory, file_name = parts[1], parts[-1]
        if directory in modules:
            key = modules[directory]
            if len(parts) == 4 and parts[2] == "leaves":
                code = file_name.removesuffix(".md").split("-")
                if (not file_name.endswith(".md") or len(code) < 4
                        or code[:2] != ["L", key] or len(code[2]) != 2
                        or any(c not in "0123456789" for c in code[2])
                        or code[2] == "00"
                        or any(not token or not token.isalnum() for token in code[3:])):
                    raise ValueError("expected a matching structural leaf code and slug")
                canonical = paths.leaf_uri(address.company_id, directory, file_name)
            elif len(parts) == 3:
                canonical = paths.trunk_doc_uri(address.company_id, directory, file_name)
            else:
                raise ValueError("invalid tree document shape")
        elif directory == TODO_DIRECTORY:
            code = file_name.removesuffix(".md").split("-")
            if (len(parts) != 3 or not file_name.endswith(".md")
                    or len(code) < 4 or code[0] != "conflict"
                    or code[1] not in MODULE_KEYS):
                raise ValueError("expected a canonical conflict document")
            # Metric and job identifiers may themselves contain hyphens.
            for token in code[2:]:
                paths.identifier(token)
            canonical = paths.conflict_uri(address.company_id, file_name)
            key = TODO_DIRECTORY
        elif directory in {"tree-brief.md", "tree-guide.md"}:
            if len(parts) != 2:
                raise ValueError("invalid company tree document shape")
            canonical = paths.facts_uri(address.company_id, None, directory)
            key = ""
        else:
            return None
        if canonical != uri:
            raise ValueError("expected a canonical tree URI")
        return paths.Address(uri, "facts", address.company_id, key, parts)

    @staticmethod
    def _packed_tree_address(uri, address, *, allow_search_prefix=False):
        """Resolve a versioned tree document only through its declared pack."""
        from ..facts_tree.template_packs import load_released_pack
        from ..facts_tree.uris import leaf_uri, tree_doc_uri, trunk_doc_uri

        parts = address.parts
        # The company tree root is a search-only namespace. Point reads and
        # writes must resolve to a concrete, canonical pack document.
        if len(parts) == 3 and allow_search_prefix:
            return paths.Address(uri.removesuffix('/'), 'facts', address.company_id, '', parts)
        if len(parts) < 6:
            raise ValueError("incomplete packed tree document URI")
        pack_name, version_text = parts[3], parts[4]
        pack = load_released_pack(pack_name)
        if version_text != str(pack.version):
            raise ValueError("packed tree version mismatch")
        if len(parts) == 7 and parts[5] == "leaves":
            leaf = next((item for item in pack.leaves
                         if leaf_uri(address.company_id, pack, item) == uri), None)
            if leaf is None:
                raise ValueError("leaf URI does not match a leaf in its template pack")
            return paths.Address(uri, "facts", address.company_id, leaf.trunk, parts)
        if len(parts) == 8 and parts[5] == "trunks":
            trunk_code, document = parts[6], parts[7]
            trunk = pack.trunk_for(trunk_code)
            if trunk is None or trunk_doc_uri(address.company_id, pack, trunk_code, document) != uri:
                raise ValueError("trunk URI does not match its template pack")
            return paths.Address(uri, "facts", address.company_id, trunk_code, parts)
        if len(parts) == 6 and parts[5] in {"tree-brief.md", "tree-guide.md"}:
            if tree_doc_uri(address.company_id, pack, parts[5]) != uri:
                raise ValueError("tree URI does not match its template pack")
            return paths.Address(uri, "facts", address.company_id, "", parts)
        raise ValueError("invalid packed tree document shape")

    def _address(self, uri: str, *, allow_tree_search_prefix=False):
        address = paths.parse(uri, company_id=self.company_id)
        if address.area == "kv":
            raise ValueError("KV is not a fact projection or vector namespace")
        if address.area != "facts":
            raise NotImplementedError("original storage is M3/后续")
        if (len(address.parts) >= 3 and address.parts[0] == "co" and address.parts[2] == "tree"
                and not settings.SAM_FACTS_TREE):
            raise ValueError("versioned FactsTree URI is unavailable while the feature is disabled")
        if settings.SAM_FACTS_TREE:
            tree = self._tree_address(uri, address, allow_search_prefix=allow_tree_search_prefix)
            if tree is not None:
                return tree
        if address.module_key not in MODULE_KEYS | {UNCLASSIFIED, TODO_DIRECTORY, ""}:
            raise ValueError("unknown fact-memory module")
        return address

    def _search_address(self, prefix: str):
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a company-scoped URI")
        if prefix == paths.FACTS_ROOT:
            if self.company_id is None:
                raise ValueError("search requires an explicit company_id")
            prefix = paths.facts_uri(self.company_id)
        return self._address(prefix, allow_tree_search_prefix=True)

    def ensure_directory(self, uri: str) -> None:
        address = self._address(uri)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO m1_directories VALUES(?,?)",
                            (address.uri, address.company_id))

    def add_resource(self, path: str, *, parent: str, wait: bool = True) -> None:
        """Original ingestion is M3/后续; never copies a source in M1."""
        raise NotImplementedError("original ingestion is M3/后续")

    def add_resource_to(self, path: str, target_uri: str, *, wait: bool = True,
                        timeout: int = 600) -> None:
        """Original ingestion is M3/后续."""
        raise NotImplementedError("original ingestion is M3/后续")

    def wait_for_resource(self, uri: str, *, timeout: int = 600, interval: float = 2.0) -> None:
        """Original ingestion is M3/后续."""
        raise NotImplementedError("original ingestion is M3/后续")

    def _state(self, company: str) -> str | None:
        row = self.db.execute("SELECT status FROM m1_projection_state WHERE company_id=?",
                              (company,)).fetchone()
        return row[0] if row else None

    def projection_status(self, company_id: str) -> str | None:
        with self.lock:
            return self._state(self._company(company_id))

    def _stale(self, company: str, error: str | None = None) -> None:
        self.db.execute("""INSERT INTO m1_projection_state(company_id,status,error,generation) VALUES(?,'stale',?,1)
            ON CONFLICT(company_id) DO UPDATE SET status='stale',error=excluded.error,
                generation=m1_projection_state.generation+1""",
                        (company, error))

    def _project(self, row) -> None:
        metadata = json.loads(row['metadata'])
        # Render facts generically: no business inference, no classification rules.
        text = row['content']
        if row['fact_json'] is not None:
            text = "# Fact " + row['fact_key'] + "\n\n```json\n" + json.dumps(
                json.loads(row['fact_json']), ensure_ascii=False, sort_keys=True, indent=2
            ) + "\n```\n"
        chunks = split_markdown(text, fact=row['fact_json'] is not None)
        vectors = self.embedding.embed_texts([c.text for c in chunks]) if chunks else []
        if len(vectors) != len(chunks):
            raise ValueError("embedder returned wrong vector count")
        values = [(
            chunk_id(row['uri'], i), row['uri'], row['company_id'], row['module_key'], i,
            c.text, self.projection.blob(v), hashlib.sha256(c.text.encode()).hexdigest(),
            metadata.get('attribution_id'), self.embedding.embedder, self.embedding.model_version,
            metadata.get('status', 'verified'), json.dumps(c.source_span),
            json.loads(row['fact_json'])['id'] if row['fact_json'] is not None else None,
        ) for i, (c, v) in enumerate(zip(chunks, vectors))]
        self.db.execute("DELETE FROM m1_chunks WHERE company_id=? AND uri=?",
                        (row['company_id'], row['uri']))
        self.db.executemany("""INSERT INTO m1_chunks (
            chunk_id,uri,company_id,module_key,chunk_index,chunk_text,embedding,
            content_hash,attribution_id,embedder,model_version,status,source_span,fact_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)

    def _write(self, address, content, metadata):
        # Only guide/brief-like documents live here; facts are projected from the ledger.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self._state(address.company_id)
            self.db.execute("""INSERT INTO m1_documents
                (uri,company_id,module_key,content,metadata,fact_key,fact_json)
                VALUES(?,?,?,?,?,NULL,NULL) ON CONFLICT(uri) DO UPDATE SET
                content=excluded.content,metadata=excluded.metadata,fact_key=excluded.fact_key,
                fact_json=excluded.fact_json""", (
                address.uri, address.company_id, address.module_key, content,
                json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
            self._stale(address.company_id)
            generation = self.db.execute(
                'SELECT generation FROM m1_projection_state WHERE company_id=?',
                (address.company_id,)).fetchone()[0]
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                row = self.db.execute("SELECT * FROM m1_documents WHERE company_id=? AND uri=?",
                                      (address.company_id, address.uri)).fetchone()
                self._project(row)
                if previous in (None, 'ready'):
                    self.db.execute("UPDATE m1_projection_state SET status='ready',error=NULL WHERE company_id=? AND generation=?",
                                    (address.company_id, generation))
        except Exception as exc:
            with self.db:
                self._stale(address.company_id, type(exc).__name__)
            raise ProjectionError("authoritative write committed; company projection stale") from exc

    def put(self, uri: str, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        if isinstance(uri, str) and uri.startswith(paths.KV_ROOT):
            address = self._kv_address(uri)
            if len(address.parts) < 4:
                raise ValueError("KV put requires a document address")
            if not isinstance(content, str):
                raise ValueError("content must be JSON text")
            # JSONL is retained verbatim; validate without normalising bytes.
            decoder = json.JSONDecoder(parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))
            index = 0
            while index < len(content):
                while index < len(content) and content[index].isspace():
                    index += 1
                if index == len(content):
                    break
                _, index = decoder.raw_decode(content, index)
            metadata = dict(metadata or {})
            if 'company_id' in metadata and metadata['company_id'] != address.company_id:
                raise ValueError("metadata company mismatch")
            encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            with self.lock, self.db:
                self.db.execute("INSERT INTO memory_kv VALUES(?,?,?) ON CONFLICT(uri) DO UPDATE SET content=excluded.content,metadata=excluded.metadata",
                                (address.uri, content, encoded))
            return
        address = self._address(uri)
        if address.uri == paths.tree_meta_uri(address.company_id):
            raise PermissionError("tree.meta is writable only through put_tree_meta")
        if (address.parts[-1].startswith("F") and address.parts[-1][1:].split(".")[0].isdigit()) or (metadata or {}).get("layer") == "2b_L2":
            raise PermissionError("facts are writable only through ConsolidationService")
        if not isinstance(content, str):
            raise ValueError("content must be text")
        metadata = dict(metadata or {})
        if 'company_id' in metadata and metadata['company_id'] != address.company_id:
            raise ValueError("metadata company mismatch")
        json.dumps(metadata, allow_nan=False)
        with self.lock:
            self._write(address, content, metadata)

    def put_tree_meta(self, company_id: str, payload: dict) -> None:
        from ..facts_tree.tree_meta import put_tree_meta, require_enabled
        require_enabled()
        with self.lock, self.db:
            put_tree_meta(self.db, self._company(company_id), payload, scope=self.company_id)

    def read_tree_meta(self, company_id: str) -> dict | None:
        from ..facts_tree.tree_meta import read_tree_meta
        with self.lock:
            return read_tree_meta(self.db, self._company(company_id), scope=self.company_id)

    def publish_leaves(self, documents, *, metadata):
        """Opt-in reserved leaf bundle writer; no application caller in segment A."""
        from ..facts_tree.tree_meta import require_enabled
        from ..module_tree import MODULES
        require_enabled()
        if not documents:
            return
        if not isinstance(documents, dict) or not isinstance(metadata, dict):
            raise ValueError("leaf bundle and metadata must be mappings")
        modules = {m.directory: m.key for m in MODULES}
        addresses = {}
        for uri, content in documents.items():
            address = paths.parse(uri, company_id=self.company_id)
            if (address.area != "facts" or len(address.parts) != 4
                    or address.parts[1] not in modules or address.parts[2] != "leaves"
                    or not address.parts[3].endswith(".md")):
                raise ValueError("expected a leaf in an existing trunk directory")
            code_parts = address.parts[3].removesuffix(".md").split("-")
            if (len(code_parts) < 3 or code_parts[0] != "L"
                    or code_parts[1] != modules[address.parts[1]]
                    or len(code_parts[2]) != 2
                    or any(c not in "0123456789" for c in code_parts[2])
                    or code_parts[2] == "00"):
                raise ValueError("expected a matching structural leaf code")
            if paths.leaf_uri(address.company_id, address.parts[1], address.parts[3]) != uri:
                raise ValueError("expected a canonical leaf URI")
            if content is not None and not isinstance(content, str):
                raise ValueError("leaf content must be text")
            addresses[uri] = paths.Address(uri, "facts", address.company_id,
                                           modules[address.parts[1]], address.parts)
        companies = {a.company_id for a in addresses.values()}
        if len(companies) != 1 or metadata.get("company_id") not in companies:
            raise ValueError("leaf bundle must have one matching company scope")
        company = next(iter(companies))
        encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        with self.lock:
            try:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    previous = self._state(company)
                    for uri, content in documents.items():
                        address = addresses[uri]
                        if content is None:
                            self.db.execute("DELETE FROM m1_documents WHERE company_id=? AND uri=?", (company, uri))
                            self.db.execute("DELETE FROM m1_chunks WHERE company_id=? AND uri=?", (company, uri))
                        else:
                            self.db.execute("""INSERT INTO m1_documents
                                (uri,company_id,module_key,content,metadata,fact_key,fact_json)
                                VALUES(?,?,?,?,?,NULL,NULL) ON CONFLICT(uri) DO UPDATE SET
                                content=excluded.content,metadata=excluded.metadata""",
                                (uri, company, address.module_key, content, encoded))
                            row = self.db.execute("SELECT * FROM m1_documents WHERE uri=?", (uri,)).fetchone()
                            self._project(row)
                    self._stale(company)
                    if previous in (None, "ready"):
                        self.db.execute("UPDATE m1_projection_state SET status='ready',error=NULL WHERE company_id=?", (company,))
            except Exception as exc:
                with self.db:
                    self._stale(company, type(exc).__name__)
                raise ProjectionError("leaf bundle rolled back and marked stale") from exc

    def _publish_document_rows(self, documents, addresses, company, encoded):
        previous = self._state(company)
        for uri, content in documents.items():
            address = addresses[uri]
            if content is None:
                self.db.execute("DELETE FROM m1_documents WHERE company_id=? AND uri=?", (company, uri))
                self.db.execute("DELETE FROM m1_chunks WHERE company_id=? AND uri=?", (company, uri))
            else:
                self.db.execute("""INSERT INTO m1_documents
                    (uri,company_id,module_key,content,metadata,fact_key,fact_json)
                    VALUES(?,?,?,?,?,NULL,NULL) ON CONFLICT(uri) DO UPDATE SET
                    content=excluded.content,metadata=excluded.metadata""",
                    (uri, company, address.module_key, content, encoded))
                row = self.db.execute("SELECT * FROM m1_documents WHERE uri=?", (uri,)).fetchone()
                self._project(row)
        self._stale(company)
        if previous in (None, "ready"):
            self.db.execute("UPDATE m1_projection_state SET status='ready',error=NULL WHERE company_id=?", (company,))

    def _publish_documents_join(self, documents, *, metadata, connection):
        """Host-only seam: caller owns the active transaction and its rollback."""
        from ..facts_tree.tree_meta import require_enabled
        require_enabled()
        if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
            raise ValueError("join requires an active SQLite transaction")
        files = {row[1]: row[2] for row in connection.execute("PRAGMA database_list")}
        if not files.get('main') or Path(files['main']).resolve() != self.database.resolve():
            raise ValueError("join connection must share the provider database")
        if connection.row_factory is not sqlite3.Row:
            raise ValueError("join requires SQLite Row results")
        with self.lock:
            # A private view avoids swapping the live provider's connection.
            joined = copy(self)
            joined.db = connection
            if not documents:
                return
            addresses, company, encoded = joined._document_bundle(documents, metadata)
            try:
                joined._publish_document_rows(documents, addresses, company, encoded)
            except Exception as exc:
                raise ProjectionError("joined projection failed; caller must roll back") from exc

    def _document_bundle(self, documents, metadata):
        addresses = {uri: self._address(uri) for uri in documents}
        companies = {address.company_id for address in addresses.values()}
        if len(companies) != 1:
            raise ValueError("projection bundle must have one company scope")
        company = next(iter(companies))
        if metadata.get("company_id") != company:
            raise ValueError("metadata company mismatch")
        for uri, content in documents.items():
            tree = (self._tree_address(uri, addresses[uri])
                    if settings.SAM_FACTS_TREE else None)
            if tree is None and addresses[uri].parts[-1] not in {"brief.json", "brief.md", "guide.json", "guide.md"}:
                raise PermissionError("bundle only accepts brief/guide projections")
            if content is not None and not isinstance(content, str):
                raise ValueError("projection content must be text")
        encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        return addresses, company, encoded

    def publish_documents(self, documents, *, metadata):
        """Atomically replace a host-owned brief/guide bundle and its vectors.

        Does not open fact-card/KV writes. A failed disposable projection leaves
        all prior document bytes intact and marks the company stale; L2 survives.
        """
        if not documents:
            return
        addresses, company, encoded = self._document_bundle(documents, metadata)
        with self.lock:
            try:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    self._publish_document_rows(documents, addresses, company, encoded)
            except Exception as exc:
                with self.db:
                    self._stale(company, type(exc).__name__)
                raise ProjectionError("L2 retained; projection bundle rolled back and marked stale") from exc

    def read(self, uri: str) -> str:
        if isinstance(uri, str) and uri.startswith(paths.KV_ROOT):
            address = self._kv_address(uri)
            with self.lock:
                row = self.db.execute("SELECT content FROM memory_kv WHERE uri=?", (address.uri,)).fetchone()
            if row is None:
                raise FileNotFoundError(address.uri)
            return row[0]
        address = self._address(uri)
        with self.lock:
            for fact in self._fact_rows(address.company_id):
                document = self._fact_document(fact)
                if document['uri'] == address.uri:
                    return document['content']
            row = self.db.execute("SELECT content FROM m1_documents WHERE company_id=? AND uri=? AND fact_json IS NULL",
                                  (address.company_id, address.uri)).fetchone()
        if row is None:
            raise FileNotFoundError(address.uri)
        return row[0]

    @staticmethod
    def _filters(filters, company):
        """Exact already-structured employee fields; never parse the question."""
        if not isinstance(filters, dict):
            return {}
        if 'company_id' in filters and filters['company_id'] != company:
            raise ValueError("filter company differs from injected scope")
        allowed = {'period', 'entity', 'attribute', 'unit', 'module_key', 'company_id'}
        if set(filters) - allowed:
            return {}
        result = {k: v for k, v in filters.items() if k != 'company_id'}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in result.values()):
            return {}
        if 'module_key' in result and result['module_key'] not in MODULE_KEYS:
            return {}
        return result

    def _tree_rows(self, rows):
        """Select only canonical tree documents; never reinterpret old artifacts."""
        result = []
        for row in rows:
            try:
                address = paths.parse(row['uri'], company_id=self.company_id)
                if self._tree_address(row['uri'], address) is not None:
                    result.append(row)
            except (ValueError, PermissionError):
                continue
        return result

    def search(self, query: str, *, prefix: str = paths.FACTS_ROOT, filters=None) -> list[dict[str, Any]]:
        address = self._search_address(prefix)
        filters = self._filters(filters, address.company_id)
        if not isinstance(query, str) or len(query) > 8192:
            raise ValueError("query must be text of at most 8192 characters")
        if not query.strip():
            return []
        if not settings.SAM_FACTS_TREE:
            with self.lock:
                state = self.db.execute("SELECT status,error FROM m1_projection_state WHERE company_id=?",
                                        (address.company_id,)).fetchone()
                if state is None or (state['status'] == 'stale' and state['error'] is None):
                    self.rebuild_projection(address.company_id)
        with self.lock, self.db:
            # State and vectors must share one snapshot across provider instances.
            self.db.execute('BEGIN')
            if not settings.SAM_FACTS_TREE and self._state(address.company_id) == 'stale':
                raise ProjectionError("company projection stale; rebuild required")
            # Unknown values may be employee aliases or uncertain/injected text.
            # Admit only exact values already present in this company's live ledger;
            # otherwise abandon the entire prefilter, never infer a replacement.
            for key, value in filters.items():
                if self.db.execute(f"""SELECT 1 FROM facts WHERE company_id=?
                    AND status='verified' AND superseded_by IS NULL AND valid_to IS NULL
                    AND {key}=? LIMIT 1""", (address.company_id, value)).fetchone() is None:
                    filters = {}
                    break
            if settings.SAM_FACTS_TREE:
                documents = self.db.execute("SELECT * FROM m1_documents WHERE company_id=? AND fact_json IS NULL",
                                            (address.company_id,)).fetchall()
                uris = [r['uri'] for r in self._tree_rows(documents)]
                rows = self.projection.search(address, query, top_k=self.top_k, filters=filters, document_uris=uris)
            else:
                rows = self.projection.search(address, query, top_k=self.top_k, filters=filters)
        if self.reranker is None:
            return rows
        # Reranking may reorder only the supplied, already scoped candidates.
        ranked = self.reranker.rerank(query, [dict(row) for row in rows])
        by_key = {(r['uri'], r['chunk_index']): r for r in rows}
        if not isinstance(ranked, list) or any(not isinstance(r, dict) for r in ranked):
            raise ValueError("reranker changed scoped candidate identities")
        keys = [(r.get('uri'), r.get('chunk_index')) for r in ranked]
        if any(not isinstance(uri, str) or type(index) is not int for uri, index in keys):
            raise ValueError("reranker changed scoped candidate identities")
        if len(keys) != len(by_key) or len(set(keys)) != len(keys) or any(k not in by_key for k in keys):
            raise ValueError("reranker changed scoped candidate identities")
        return [by_key[key] for key in keys]

    def query(self, *, prefix: str, query: str | None = None) -> list[dict[str, Any]]:
        address = self._search_address(prefix)
        if query is not None:
            return self.search(query, prefix=address.uri)
        with self.lock:
            rows = self.db.execute("""SELECT * FROM m1_documents WHERE company_id=? AND fact_json IS NULL
                AND (uri=? OR substr(uri,1,length(?)+1)=?||'/') ORDER BY uri""",
                (address.company_id, address.uri, address.uri, address.uri)).fetchall()
        if settings.SAM_FACTS_TREE:
            rows = self._tree_rows(rows)
        else:
            with self.lock:
                rows = list(rows) + [self._fact_document(f) for f in self._fact_rows(address.company_id)
                                     if self._fact_document(f)["uri"].startswith(address.uri + "/")]
        return [dict(uri=r['uri'], score=1.0, snippet=r['content'][:700],
                     module_key=r['module_key'], company_id=r['company_id']) for r in rows
                if json.loads(r['metadata']).get('status', 'verified') == 'verified']

    def _fact_rows(self, company: str, keys=()):
        sql = "SELECT * FROM facts WHERE company_id=? AND status='verified' AND superseded_by IS NULL AND valid_to IS NULL"
        args = [company]
        if keys:
            sql += " AND CAST(id AS TEXT) IN (" + ",".join("?" for _ in keys) + ")"
            args.extend(keys)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    @staticmethod
    def _fact_document(fact):
        module = projection_module(fact.get('module_key'))
        uri = paths.fact_uri(fact['company_id'], module, str(fact['id']))
        content = "# Fact " + str(fact['id']) + "\n\n```json\n" + json.dumps(
            fact, ensure_ascii=False, sort_keys=True, indent=2) + "\n```\n"
        return dict(uri=uri, company_id=fact['company_id'], module_key=module,
                    content=content, fact_key=str(fact['id']), fact_json=json.dumps(fact, ensure_ascii=False),
                    metadata=json.dumps({'status': 'verified'}))

    def put_fact(self, company_id: str, fact_key: str, fact: dict[str, Any]) -> None:
        """Protocol compatibility: refresh an existing ledger fact, never write it."""
        company, key = self._company(company_id), paths.identifier(fact_key)
        if not isinstance(fact, dict) or fact.get('status') != 'verified':
            raise ValueError("only explicitly verified facts may be projected")
        if fact.get('company_id', company) != company:
            raise ValueError("fact company mismatch")
        with self.lock:
            existing = self.get_fact(company, key)
            if existing is None or existing != fact:
                raise PermissionError("facts are writable only through ConsolidationService")
            self.rebuild_projection(company)

    def get_fact(self, company_id: str, fact_key: str) -> dict[str, Any] | None:
        company, key = self._company(company_id), paths.identifier(fact_key)
        with self.lock:
            rows = self._fact_rows(company, [key])
        return rows[0] if rows else None

    def list_facts(self, company_id: str, *, fact_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
        company = self._company(company_id)
        keys = [paths.identifier(k) for k in fact_keys]
        with self.lock:
            return self._fact_rows(company, keys)

    def delete(self, uri: str, *, recursive: bool = False) -> None:
        if isinstance(uri, str) and uri.startswith(paths.KV_ROOT):
            address = self._kv_address(uri)
            with self.lock, self.db:
                sql = "DELETE FROM memory_kv WHERE uri=?"
                args = [address.uri]
                if recursive:
                    sql += " OR substr(uri,1,length(?)+1)=?||'/'"
                    args.extend([address.uri, address.uri])
                self.db.execute(sql, args)
            return
        address = self._address(uri)
        with self.lock, self.db:
            for table in ('m1_documents', 'm1_chunks', 'm1_directories'):
                sql = f"DELETE FROM {table} WHERE company_id=? AND (uri=?"
                args = [address.company_id, address.uri]
                if recursive:
                    sql += " OR substr(uri,1,length(?)+1)=?||'/'"
                    args.extend([address.uri, address.uri])
                self.db.execute(sql + ")", args)

    def rebuild_projection(self, company_id: str) -> None:
        company = self._company(company_id)
        if settings.SAM_FACTS_TREE:
            with self.lock:
                try:
                    with self.db:
                        self.db.execute('BEGIN IMMEDIATE')
                        rows = self.db.execute("SELECT * FROM m1_documents WHERE company_id=? AND fact_json IS NULL ORDER BY uri",
                                               (company,)).fetchall()
                        for row in self._tree_rows(rows):
                            self._project(row)  # Deletes/replaces this document's chunks only.
                except Exception as exc:
                    raise ProjectionError("tree rebuild failed; authoritative content unchanged") from exc
            return
        with self.lock:
            with self.db:
                self._stale(company)
            try:
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    self.db.execute("DELETE FROM m1_chunks WHERE company_id=?", (company,))
                    self.db.execute("DELETE FROM m1_documents WHERE company_id=? AND fact_json IS NOT NULL", (company,))
                    rows = self.db.execute("SELECT * FROM m1_documents WHERE company_id=? ORDER BY uri",
                                           (company,)).fetchall()
                    for row in rows:
                        self._project(row)
                    for fact in self._fact_rows(company):
                        self._project(self._fact_document(fact))
                    self.db.execute("UPDATE m1_projection_state SET status='ready',error=NULL WHERE company_id=?",
                                    (company,))
            except Exception as exc:
                with self.db:
                    self._stale(company, type(exc).__name__)
                raise ProjectionError("company rebuild failed; authoritative content unchanged") from exc
