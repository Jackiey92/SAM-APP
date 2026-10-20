"""Module-scoped employee updates and mechanical company assembly.

SQLite is authoritative. Stage both levels before publishing any module bytes;
reject one module without rolling back successful siblings. No business rules.
"""
from hashlib import sha256
import json

from ...employees.worker import WorkerUnavailable
from ...module_taxonomy import MODULES
from .l1_brief import L1BriefService, projection_lock, publish_pair, read_optional
from .l0_guide import L0GuideService
from .numeric_guard import validate, strip_marks
from .paths import (module_document_uri, brief_json_uri, brief_md_uri,
                    guide_json_uri, guide_md_uri)


class _Stage:
    def __init__(self, memory):
        self.memory = memory
        self.memory_root = getattr(memory, "memory_root", "sam://")
        self.documents = {}

    def read(self, uri):
        if uri in self.documents:
            if self.documents[uri] is None:
                raise FileNotFoundError(uri)
            return self.documents[uri]
        return self.memory.read(uri)

    def put(self, uri, content, *, metadata=None):
        self.documents[uri] = content

    def delete(self, uri):
        self.documents[uri] = None


def _dump(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ModuleProjectionService:
    def __init__(self, db_path, memory, *, worker=None, token_budget=None):
        self.db_path, self.memory, self.worker = db_path, memory, worker
        self.token_budget = token_budget
        self.root = getattr(memory, "memory_root", "sam://")
        self.facts = L1BriefService(db_path, memory, worker=worker).facts

    def update(self, *, company_id, verified_fact_ids, thread_id=None, full=False):
        with projection_lock(self.root, company_id):
            facts = self.facts.list_verified_facts(company_id=company_id)
            ids = set(verified_fact_ids)
            touched = {f["module_key"] for f in facts if f["id"] in ids and f.get("module_key")}
            active = {f["module_key"] for f in facts if f.get("module_key")}
            # Legacy company-only data: missing active module pairs cold-start once.
            targets = {m.key for m in MODULES if full or m.key in touched or
                       (m.key in active and any(read_optional(self.memory, module_document_uri(company_id, m.key, name)) is None
                           for name in ("brief.json", "brief.md", "guide.json", "guide.md")))}
            errors, published = [], []
            for module in MODULES:
                key = module.key
                if key not in targets:
                    continue
                batch = sorted(f["id"] for f in facts if f["id"] in ids and f.get("module_key") == key)
                raw = read_optional(self.memory, module_document_uri(company_id, key, "brief.json"))
                old = json.loads(raw) if raw else None
                if not full and old and batch and old.get("applied_fact_ids") == batch:
                    continue  # successful sibling must remain byte-identical on retry
                stage = _Stage(self.memory)
                try:
                    brief = L1BriefService(self.db_path, stage, worker=self.worker).rebuild(
                        company_id=company_id, module_key=key, new_fact_ids=batch,
                        thread_id=thread_id, full=full)
                    if key in active and (not brief or brief.get("status") == "rejected"):
                        errors.append({"module_key": key, "result": brief or {"status": "unresolved"}})
                        continue
                    if brief:
                        brief["applied_fact_ids"] = batch
                        brief["fact_sources"] = {str(f["id"]): f for f in facts if f.get("module_key") == key}
                        stage.put(module_document_uri(company_id, key, "brief.json"), _dump(brief))
                    guide = L0GuideService(stage, worker=self.worker, token_budget=self.token_budget).rebuild(
                        company_id=company_id, module_key=key, thread_id=thread_id)
                    if brief and (not guide or guide.get("status") == "rejected"):
                        errors.append({"module_key": key, "result": guide or {"status": "unresolved"}})
                        continue
                    publish_pair(self.memory, stage.documents, metadata={"company_id": company_id, "layer": "2b_module"})
                    published.append(key)
                except Exception as exc:
                    errors.append({"module_key": key, "exception": exc})
            self.assemble(company_id=company_id)
            return {"touched_modules": sorted(touched), "published_modules": published, "unresolved": errors}

    def rebuild(self, *, company_id, thread_id=None):
        return self.update(company_id=company_id, verified_fact_ids=[], thread_id=thread_id, full=True)

    def assemble(self, *, company_id):
        """No company employee: copy checked sections, never synthesize numbers."""
        with projection_lock(self.root, company_id):
            sections, guide_sections, brief_md, guide_md = [], [], [], []
            seen = set()
            current = {str(f["id"]): f for f in self.facts.list_verified_facts(company_id=company_id)}
            for module in MODULES:
                raw = read_optional(self.memory, module_document_uri(company_id, module.key, "brief.json"))
                if not raw:
                    continue
                brief = json.loads(raw)
                guide_raw = read_optional(self.memory, module_document_uri(company_id, module.key, "guide.json"))
                guide = json.loads(guide_raw) if guide_raw else {}
                if brief.get("company_id") != company_id or brief.get("module_key") != module.key:
                    raise WorkerUnavailable("invalid module scope")
                # Rejected modules intentionally retain their previous verified version.
                # Validate against its persisted numeric certificates if its L2 was superseded.
                by_id = {}
                for item in brief["items"]:
                    eid = item["entry_id"]
                    if eid in seen or not eid.startswith(module.key + "-"):
                        raise WorkerUnavailable("invalid assembled entry ID")
                    seen.add(eid)
                    snapshot = brief.get("fact_sources", {})
                    sources = {str(ref): snapshot.get(str(ref), current.get(str(ref))) for ref in item["fact_ids"]}
                    if (any(not f or f.get("module_key") != module.key or f.get("company_id") != company_id
                                   or f.get("status") != "verified" for f in sources.values())
                            or validate(item["conclusion"], sources, kind="f")[1]):
                        raise WorkerUnavailable("invalid assembled brief number")
                    if validate(item["topic"], {}, kind="f")[1]:
                        raise WorkerUnavailable("invalid assembled topic")
                    by_id[eid] = item
                if guide and (guide.get("company_id") != company_id or guide.get("module_key") != module.key
                              or guide.get("l1_sha256") != sha256(raw.encode()).hexdigest()):
                    raise WorkerUnavailable("stale module guide")
                copied_guides = []
                for item in guide.get("items", []):
                    refs = item["l1_entry_ids"]
                    if any(ref not in by_id for ref in refs) or validate(item["conclusion"], {ref: by_id[ref].get("verified_numbers", []) for ref in refs}, kind="l")[1]:
                        raise WorkerUnavailable("invalid assembled guide number")
                    copied = {**item, "l1_uris": [brief_md_uri(company_id) + "#" + ref for ref in refs]}
                    copied_guides.append(copied)
                    guide_md.append(strip_marks(item["conclusion"]) + "\n" + "\n".join(f"[L1]({uri})" for uri in copied["l1_uris"]) + "\n\n")
                sections.append({"module_key": module.key, "items": brief["items"]})
                guide_sections.append({"module_key": module.key, "items": copied_guides})
                brief_md.append(f"# {module.directory}\n" + (read_optional(self.memory, module_document_uri(company_id, module.key, "brief.md")) or ""))
            payload = {"company_id": company_id, "items": [i for section in sections for i in section["items"]], "sections": sections, "run_id": None, "session_id": None}
            raw = _dump(payload)
            guide = {"company_id": company_id, "items": [i for section in guide_sections for i in section["items"]], "sections": guide_sections,
                     "l1_uri": brief_json_uri(company_id), "l1_sha256": sha256(raw.encode()).hexdigest(), "run_id": None, "session_id": None}
            documents = {brief_json_uri(company_id): raw, brief_md_uri(company_id): "\n".join(brief_md),
                         guide_json_uri(company_id): _dump(guide), guide_md_uri(company_id): "".join(guide_md)}
            # Avoid rewriting identical company projections on no-op batches/retries.
            publish_pair(self.memory, {uri: text for uri, text in documents.items() if read_optional(self.memory, uri) != text},
                         metadata={"company_id": company_id, "layer": "2b_company"})
            return payload

    def read(self, *, company_id, module_key, layer, format="md"):
        if layer not in {"brief", "guide"} or format not in {"md", "json"}:
            raise ValueError("invalid projection document")
        from ... import settings
        if settings.SAM_FACTS_TREE:
            from ...module_tree import MODULES
            from .. import paths
            module = next((m for m in MODULES if m.key==module_key), None)
            if module is None: raise ValueError('invalid module code')
            content = read_optional(self.memory, paths.trunk_doc_uri(company_id,module.directory,f'trunk-{layer}.md'))
            if content is None or format=='md': return content
            return _dump(dict(module_key=module_key,layer=layer,content=content))
        with projection_lock(self.root, company_id):
            return read_optional(self.memory, module_document_uri(company_id, module_key, f"{layer}.{format}"))
