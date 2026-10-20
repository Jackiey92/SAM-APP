"""Bounded navigation and scoped tools; external 2A never goes through memory.

Source URIs are logical handles resolved by SourceMapService, not memory objects.
Navigation is assembled from the trusted local catalog without recursive memory
listing, stale cache reads, exact-read probing, or write/rebuild side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any

from .memory_paths import MEMORY_ROOT
from .memory.paths import facts_uri, original_uri, parse, identifier
from .memory.taxonomy import projection_module, MODULES, TODO_DIRECTORY, UNCLASSIFIED
from .memory.company_facts.paths import company_uri, brief_md_uri, guide_md_uri
from .facts_tree.uris import tree_prefix
from . import settings
from .storage.mapping_store import SourceMapService
from .db.database import connect

ROOT = MEMORY_ROOT
MAP_ROOT = ROOT
MAP_LIMIT = 12_000


def _safe(value: str) -> str:
    return identifier(value)


def source_map_uri(company_id: str, file_hash: str, *, root: str = ROOT) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", file_hash):
        raise ValueError("invalid file hash")
    return original_uri(_safe(company_id), file_hash, "mapping.md")


def source_catalog_from_db(db_path) -> tuple[tuple[str, str], ...]:
    # The host catalog is the authority for company ownership, not file names
    # or arbitrary model-supplied hashes. A missing catalog fails closed.
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return tuple((str(company), str(digest)) for company, digest in conn.execute(
                "SELECT DISTINCT company_id,file_hash FROM parse_jobs ORDER BY company_id,file_hash"))
    except (OSError, sqlite3.Error):
        return ()


@dataclass(frozen=True)
class MemoryMapResult:
    company_id: str
    map: dict[str, Any]
    degraded: bool = False


class MapBuilder:
    def __init__(self, memory, *, root: str = ROOT, max_chars: int = MAP_LIMIT,
                 source_catalog=()):
        self.memory, self.root, self.max_chars = memory, root.rstrip("/"), max_chars
        self.source_catalog = tuple(source_catalog)

    def map_uri(self, company_id):
        return facts_uri(_safe(company_id), None, "map.json")

    def overview_uri(self, company_id):
        return facts_uri(_safe(company_id), None, "company-overview.md")

    def markdown_uri(self, company_id):
        return facts_uri(_safe(company_id), None, "map.md")

    def _branches(self, company_id, source_ids):
        company = _safe(company_id)
        branches = [{"module": "source", "title": f"资料 {digest[:12]}",
                     "uri": source_map_uri(company, digest, root=self.root), "kind": "2a",
                     "company_id": company, "file_hash": digest,
                     "has_children": False, "external": True}
                    for digest in sorted(set(source_ids))]
        for layer, title, uri in (("L0", "公司事实导览", guide_md_uri(company, root=self.root)),
                                  ("L1", "跨文件事实简述", brief_md_uri(company, root=self.root))):
            branches.append({"module": "facts", "title": title, "uri": uri,
                             "kind": "2b", "layer": layer, "company_id": company,
                             "has_children": True})
        return branches

    def _bounded(self, data):
        while len(json.dumps(data, ensure_ascii=False, sort_keys=True)) > self.max_chars and data["branches"]:
            data["branches"].pop()
            data["truncated"] = True
        data["revision"] = hashlib.sha256(json.dumps(data["branches"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        return data

    def rebuild_map(self, company_id, *, source_ids=(), fact_keys=()):
        company = _safe(company_id)
        sources = set(source_ids) | {digest for owner, digest in self.source_catalog if owner == company}
        data = self._bounded({"company_id": company, "updated_at": datetime.now(timezone.utc).isoformat(),
                              "branches": self._branches(company, sources), "degraded": False,
                              "modules": [{"key": m.key, "directory": m.directory, "order": m.order,
                                           "hidden": m.hidden, "uri": facts_uri(company, m.key)} for m in MODULES],
                              "special_directories": [TODO_DIRECTORY, UNCLASSIFIED]})
        return MemoryMapResult(company, data)

    def load_or_rebuild(self, company_id, *, source_ids=(), fact_keys=()):
        # Never load historical navigation caches. These are rebuildable
        # metadata, not evidence; deleted or foreign sources cannot linger.
        return self.rebuild_map(company_id, source_ids=source_ids, fact_keys=fact_keys)

    def global_read_map(self, *, source_catalog=()):
        catalog = set(self.source_catalog) | set(source_catalog)
        branches = []
        for company in sorted({owner for owner, _ in catalog}):
            branches.extend(self._branches(company, [digest for owner, digest in catalog if owner == company]))
        return self._bounded({"scope": "global_read_only", "read_only": True,
                              "branches": branches, "degraded": False})


class MemoryMapTools:
    def __init__(self, memory, files, company_id, *, source_ids=(), global_read_only=False,
                 source_catalog=(), source_maps=None):
        self.memory, self.files, self.company_id = memory, files, _safe(company_id)
        self.global_read_only = bool(global_read_only)
        self.source_catalog = tuple(source_catalog)
        self.source_maps = source_maps
        self.db_path = getattr(files, "_db_path", None)
        if self.source_maps is None and self.db_path is not None:
            self.source_maps = SourceMapService(self.db_path, objects_path=files.objects_path)
        self._sources = {source_map_uri(owner, digest): (owner, digest)
                         for owner, digest in self.source_catalog
                         if self.global_read_only or owner == self.company_id}
        self._sources.update({source_map_uri(self.company_id, digest): (self.company_id, digest)
                              for digest in source_ids})
        self._file_hashes = {digest for _, digest in self._sources.values()}
        tree_prefixes = (tree_prefix(self.company_id),) if settings.SAM_FACTS_TREE else ()
        self.company_prefixes = (company_uri(self.company_id) + "/", *tree_prefixes)
        self.global_prefixes = ("sam://facts/",)
        self.navigation = []

    def _check_uri(self, uri):
        if not isinstance(uri, str) or any(part in {".", ".."} for part in uri.split("/")):
            raise PermissionError("memory URI is outside scope")
        if uri in self._sources:
            return uri
        if uri.startswith("sam://original/"):
            raise PermissionError("source is outside catalog")
        try:
            parse(uri)
        except ValueError as exc:
            raise PermissionError("memory URI is outside scope") from exc
        allowed = self.global_prefixes if self.global_read_only else self.company_prefixes
        if not uri.startswith(allowed):
            raise PermissionError("memory URI is outside scope")
        return uri

    def memory_read(self, uri):
        uri = self._check_uri(uri)
        self.navigation.append(uri)
        if uri in self._sources:
            if self.source_maps is None:
                raise FileNotFoundError("source map unavailable")
            owner, digest = self._sources[uri]
            return self.source_maps.read_map(file_hash=digest, company_id=owner)
        # L1 exposes exact SQLite L2 handles, so drill-down never requires an
        # memory mirror of source originals or business keyword lookup.
        address = parse(uri)
        name = address.parts[-1]
        if re.fullmatch(r"F[1-9][0-9]*\.md", name):
            if self.db_path is None:
                raise FileNotFoundError("fact unavailable")
            with connect(self.db_path) as conn:
                row = conn.execute("""SELECT * FROM facts WHERE id=? AND company_id=? AND status='verified'
                    AND superseded_by IS NULL AND valid_to IS NULL""",
                    (int(name[1:-3]), address.company_id)).fetchone()
            if row is None or projection_module(row['module_key']) != address.module_key:
                raise FileNotFoundError("fact unavailable")
            return json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
        return self.memory.read(uri)

    def memory_search(self, query, *, filters=None):
        if isinstance(filters, dict) and 'company_id' in filters:
            if self.global_read_only or filters['company_id'] != self.company_id:
                raise ValueError("filter company differs from injected scope")
            filters = {k: v for k, v in filters.items() if k != 'company_id'}
        # memory searches only the current 2B namespaces. External source map
        # bodies remain available by explicit catalog handles, not memory indexes.
        companies = sorted({owner for owner, _ in self.source_catalog}) if self.global_read_only else [self.company_id]
        prefixes = tuple(prefix for company in companies
                         for prefix in ((company_uri(company) + "/", tree_prefix(company))
                                        if settings.SAM_FACTS_TREE else (company_uri(company) + "/",)))
        rows = []
        for prefix in prefixes:
            if getattr(self.memory, 'supports_structured_filters', False):
                rows.extend(self.memory.search(query, prefix=prefix, filters=filters))
            else:
                rows.extend(self.memory.search(query, prefix=prefix))
        uris = sorted({row["uri"] for row in rows if isinstance(row, dict) and isinstance(row.get("uri"), str)
                       and row["uri"].startswith(prefixes)})
        self.navigation.extend(uris)
        return uris

    def file_get(self, file_hash):
        if not isinstance(file_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", file_hash):
            raise ValueError("invalid file hash")
        if file_hash not in self._file_hashes:
            raise PermissionError("file is outside catalog")
        stored = self.files.get(file_hash)
        return {"file_hash": stored.file_hash, "original_name": stored.original_name,
                "format": stored.mime_type, "size_bytes": stored.size_bytes, "storage_path": stored.storage_path}

    def tool_manifest(self):
        return ["memory_read(uri)", "memory_search(query)", "file_get(file_hash)"]
