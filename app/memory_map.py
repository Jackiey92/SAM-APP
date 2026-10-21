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
from .memory.taxonomy import MODULES, TODO_DIRECTORY, UNCLASSIFIED
from .memory.company_facts.paths import company_uri, brief_md_uri, guide_md_uri
from .facts_tree.tree_read import TreeReadError, TreeReadService
from .facts_tree.uris import tree_prefix
from . import settings
from .ports import MemoryUnavailable
from .storage.mapping_store import SourceMapService

ROOT = MEMORY_ROOT
MAP_ROOT = ROOT
MAP_LIMIT = 12_000

# Canonical FactsTree leaf file names carry their strict L-<trunk>-<NN> code;
# both address families below are written only by leaf publish.
_TREE_LEAF_NAME = re.compile(r"^(L-[A-Za-z0-9-]+-[0-9]{2})-.+\.md$")


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

    def _tree_leaf_claim(self, uri):
        """The leaf code when a URI claims a current-tree leaf address, else None.

        Both canonical address families (the pack/version namespace and the
        frozen generic pack's legacy directory) are exclusive to leaf publish,
        so a markdown file directly under them is always a leaf claim: an
        unparseable name returns "" and must fail closed, never fall back to
        legacy memory content. The company always comes from the injected
        host scope; a claim is only recognized inside it.
        """
        if self.global_read_only:
            return None
        versioned = tree_prefix(self.company_id)
        if uri.startswith(versioned):
            parts = uri[len(versioned):].split("/")
            if len(parts) != 4 or not parts[1].isdigit() or parts[2] != "leaves":
                return None
            name = parts[3]
        else:
            base = company_uri(self.company_id) + "/"
            if not uri.startswith(base):
                return None
            parts = uri[len(base):].split("/")
            if len(parts) != 3 or parts[1] != "leaves":
                return None
            name = parts[2]
        if not name.endswith(".md"):
            return None
        match = _TREE_LEAF_NAME.match(name)
        return match.group(1) if match else ""

    def _read_tree_leaf(self, uri, code):
        """Serve a claimed tree leaf only through the read-only TreeReadService.

        The service re-verifies the gate, binding, pack/version, revision,
        projection, fact identity and unique placement inside one read-only
        SQLite snapshot; only an exact canonical-URI match in state 'ready'
        with string content is returned verbatim. Every other outcome fails
        closed under the existing memory_read error contract — no fallback to
        legacy or vector content, and nothing is written or repaired.
        """
        if not code or self.db_path is None:
            raise FileNotFoundError("leaf unavailable")
        try:
            with TreeReadService(self.db_path, self.memory, company_id=self.company_id) as service:
                leaf = service.read_leaf(self.company_id, code)
        except TreeReadError as exc:
            raise FileNotFoundError("leaf unavailable") from exc
        if leaf["state"] != "ready" or not isinstance(leaf["content"], str) \
                or not leaf["content"] or leaf["uri"] != uri:
            raise FileNotFoundError("leaf unavailable")
        return leaf["content"] + self._ledger_note(leaf["facts"])

    @staticmethod
    def _ledger_note(facts):
        """Fact status/source metadata appended after the verbatim leaf body.

        Decision 13-A: the employee sees each formally referenced fact's
        status and source locator, clearly labeled as dynamic ledger metadata
        that is not part of the leaf text. Values come only from the verified
        read result; a missing locator is reported as such, never fabricated.
        """
        lines = ['## 系统读取信息（动态账本元数据，不属于叶正文）', '']
        if not facts:
            lines.append('- 本叶当前无正式引用的事实。')
        for fact in facts:
            source = fact.get('source') or {}
            handle = source.get('original_name') or source.get('file_hash') or '无来源记录'
            value = f"值：{fact['value']}" + (f" {fact['unit']}" if fact.get('unit') else '')
            parts = [f"F{fact['fact_id']} 状态：{fact['status']}",
                     f"主体：{fact['entity']}", f"属性：{fact['attribute']}",
                     f"期间：{fact['period']}", value, f"来源：{handle}"]
            if source.get('page') is not None:
                parts.append(f"页码：{source['page']}")
            if source.get('span'):
                parts.append(f"坐标：{source['span']}")
            lines.append('- ' + '；'.join(parts))
        return '\n\n---\n\n' + '\n'.join(lines) + '\n'

    def memory_read(self, uri):
        uri = self._check_uri(uri)
        self.navigation.append(uri)
        if uri in self._sources:
            if self.source_maps is None:
                raise FileNotFoundError("source map unavailable")
            owner, digest = self._sources[uri]
            return self.source_maps.read_map(file_hash=digest, company_id=owner)
        code = self._tree_leaf_claim(uri)
        if code is not None:
            return self._read_tree_leaf(uri, code)
        # L1 exposes exact SQLite L2 handles, so drill-down never requires an
        # memory mirror of source originals or business keyword lookup.
        address = parse(uri)
        name = address.parts[-1]
        if re.fullmatch(r"F[1-9][0-9]*\.md", name):
            return self._read_fact_card(address, name)
        return self.memory.read(uri)

    def _read_fact_card(self, address, name):
        """Serve an F<ID>.md claim only through the verified tree read (13-A).

        TreeReadService.read_fact re-verifies the gate, the injected company
        scope, the current binding, the fact's readability (claimed/verified,
        not superseded or expired), its identity and current revision, the
        unique current leaf placement and the leaf's formal reference — all
        inside one read-only snapshot. The legacy facts.module_key hint is
        never consulted: the URI's module segment does not decide ownership.
        Company selection and cross-company reads stay refused; the global
        read-only exception remains isolated from this single-company
        verification. Every failure is FileNotFoundError — never a fallback
        to plain memory content.
        """
        if (self.global_read_only or self.db_path is None or not settings.SAM_FACTS_TREE
                or address.company_id != self.company_id):
            raise FileNotFoundError("fact unavailable")
        try:
            with TreeReadService(self.db_path, self.memory, company_id=self.company_id) as service:
                result = service.read_fact(self.company_id, int(name[1:-3]))
        except TreeReadError as exc:
            raise FileNotFoundError("fact unavailable") from exc
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    def _search_tree_leaves(self, query, *, filters=None):
        """Employee leaf search only through the read-only TreeReadService.

        DEC-091 option A with decision 13-A: the service re-verifies the
        gate, binding, filters and every candidate leaf inside one read-only
        snapshot and returns formal leaf URIs of the current bound pack only.
        Invalid filters surface as ValueError, storage/gate conflicts as
        MemoryUnavailable and every other verified refusal as
        FileNotFoundError — never a fallback to legacy namespace or planted
        memory content.
        """
        if self.db_path is None:
            raise FileNotFoundError("leaf search unavailable")
        try:
            with TreeReadService(self.db_path, self.memory, company_id=self.company_id) as service:
                return service.search_leaves(self.company_id, query, filters=filters)
        except TreeReadError as exc:
            if exc.status == 400:
                raise ValueError(str(exc)) from exc
            if exc.status == 503:
                raise MemoryUnavailable(str(exc)) from exc
            raise FileNotFoundError(str(exc)) from exc

    def memory_search(self, query, *, filters=None):
        if isinstance(filters, dict) and 'company_id' in filters:
            if self.global_read_only or filters['company_id'] != self.company_id:
                raise ValueError("filter company differs from injected scope")
            filters = {k: v for k, v in filters.items() if k != 'company_id'}
        if settings.SAM_FACTS_TREE and not self.global_read_only:
            # Employee search of the current single company is the verified
            # tree read: only formal leaf URIs of the exact current binding.
            uris = self._search_tree_leaves(query, filters=filters)
            self.navigation.extend(uris)
            return uris
        if not self.global_read_only and isinstance(filters, dict) \
                and ('trunk_code' in filters or 'leaf_code' in filters):
            # trunk_code/leaf_code exist only on the verified tree search.
            # With the gate closed there is no tree read, so refuse
            # explicitly — the same unavailable outcome the open-gate path
            # maps from status 503 — instead of silently dropping the filter
            # and widening the legacy prefix search below.
            raise MemoryUnavailable("trunk/leaf filters require the FactsTree gate")
        # The closed gate and the isolated global read-only exception keep
        # the existing prefix search over the current 2B namespaces. External
        # source map bodies remain available by explicit catalog handles.
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
