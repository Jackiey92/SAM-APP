"""Rebuild an employee-owned L0 guide from L1 only; never access L2."""
from __future__ import annotations

import json
import logging
import os
from hashlib import sha256

from ...employees.worker import EmployeeRunner, WorkerUnavailable
from ...memory_paths import MEMORY_ROOT
from ...ports import MemoryProvider
from .l1_brief import projection_lock, publish_pair, read_optional
from .numeric_guard import validate, rejection, strip_marks
from .paths import brief_json_uri, brief_md_uri, guide_json_uri, guide_md_uri, module_document_uri

LOGGER = logging.getLogger(__name__)


class L0GuideService:
    def __init__(self, memory: MemoryProvider, *, worker=None, token_budget: int | None = None):
        self.memory = memory
        self.worker = worker or EmployeeRunner()
        self.root = getattr(memory, "memory_root", MEMORY_ROOT)
        self.token_budget = int(token_budget if token_budget is not None else os.environ.get("SAM_FACT_GUIDE_TOKEN_BUDGET", "600"))
        if self.token_budget < 0:
            raise ValueError("guide token budget must be nonnegative")

    def rebuild(self, *, company_id: str, thread_id: str | None = None, module_key: str | None = None) -> dict | None:
        l1_uri = brief_json_uri(company_id, root=self.root)
        md_uri = guide_md_uri(company_id, root=self.root)
        json_uri = guide_json_uri(company_id, root=self.root)
        if module_key is not None:
            l1_uri = module_document_uri(company_id, module_key, "brief.json")
            md_uri = module_document_uri(company_id, module_key, "guide.md")
            json_uri = module_document_uri(company_id, module_key, "guide.json")
        with projection_lock(self.root, company_id):
            raw = read_optional(self.memory, l1_uri)
            if not raw or not raw.strip():
                publish_pair(self.memory, {md_uri: None, json_uri: None},
                             metadata={"layer": "2b_L0", "company_id": company_id})
                return None
            if not self.token_budget:
                return None
            brief = json.loads(raw)
            if (not isinstance(brief, dict) or brief.get("company_id") != company_id
                    or (module_key is not None and brief.get("module_key") != module_key)):
                raise WorkerUnavailable("invalid L1 company scope")
            l1_items = brief.get("items")
            if not isinstance(l1_items, list):
                raise WorkerUnavailable("invalid L1 items")
            if not l1_items:
                publish_pair(self.memory, {md_uri: None, json_uri: None},
                             metadata={"layer": "2b_L0", "company_id": company_id})
                return None
            if any(not isinstance(item, dict) or not isinstance(item.get("entry_id"), str)
                   or not item["entry_id"] for item in l1_items):
                raise WorkerUnavailable("invalid L1 entry IDs")
            available = {item["entry_id"] for item in l1_items}
            if len(available) != len(l1_items):
                raise WorkerUnavailable("duplicate L1 entry IDs")
            request = {"l1": brief, "token_budget": self.token_budget}
            if module_key is not None:
                previous_raw = read_optional(self.memory, json_uri)
                request["previous_guide"] = json.loads(previous_raw) if previous_raw else None
            result = self.worker.run(
                skill="fact-guide", text=json.dumps(request, ensure_ascii=False),
                company_id=company_id, thread_id=thread_id,
            )
            if not result:
                LOGGER.warning("L0 employee returned no guide company_id=%s; preserving previous version", company_id)
                return None
            items = []
            # UTF-8 bytes conservatively upper-bound tokens. Retain a prefix
            # of whole employee entries, never cut a number or a URI in half.
            remaining = self.token_budget
            target = brief_md_uri(company_id, root=self.root)
            if module_key is not None:
                target = module_document_uri(company_id, module_key, "brief.md")
            sections = []
            budget_exhausted = False
            violations = []
            by_id = {item["entry_id"]: item for item in l1_items}
            for item in result:
                if not isinstance(item, dict):
                    raise WorkerUnavailable("invalid L0 item")
                conclusion, refs = item.get("conclusion"), item.get("l1_entry_ids")
                if (not isinstance(conclusion, str) or not conclusion.strip()
                        or len(conclusion.splitlines()) != 1
                        or not isinstance(refs, list) or not refs
                        or any(not isinstance(ref, str) or ref not in available for ref in refs)):
                    raise WorkerUnavailable("invalid L0 item or L1 reference")
                _, errors = validate(conclusion,
                    {ref: by_id[ref].get("verified_numbers", []) for ref in refs}, kind="l")
                violations.extend({**error, "source": {"file_hash": "", "uri": l1_uri,
                    "entry_ids": error["ids"], "span": f"{error['start']}:{error['end']}"}}
                    for error in errors)
                pointers = [target + "#" + ref for ref in refs]
                suffix = "\n" + "\n".join(f"[L1]({pointer})" for pointer in pointers) + "\n\n"
                section = strip_marks(conclusion) + suffix
                if budget_exhausted or len(section.encode("utf-8")) > remaining:
                    budget_exhausted = True
                    continue
                sections.append(section)
                remaining -= len(section.encode("utf-8"))
                items.append({"conclusion": conclusion, "l1_entry_ids": refs, "l1_uris": pointers,
                              "attribution_id": getattr(result, "attribution_id", None)})
            if violations:
                return rejection("L0", violations)
            if not items:
                LOGGER.warning("L0 guide exceeds length budget company_id=%s; preserving previous version", company_id)
                return None
            payload = {"company_id": company_id, "l1_uri": l1_uri, "items": items,
                       "l1_sha256": sha256(raw.encode("utf-8")).hexdigest(),
                       "run_id": getattr(result, "run_id", None),
                       "session_id": getattr(result, "session_id", None)}
            if module_key is not None:
                payload["module_key"] = module_key
            publish_pair(self.memory, {md_uri: "".join(sections), json_uri: json.dumps(payload, ensure_ascii=False, sort_keys=True)},
                         metadata={"layer": "2b_L0", "company_id": company_id})
            return payload

    def read_guide(self, *, company_id: str) -> str:
        """Exact SAM read for the existing resident-context mechanism."""
        with projection_lock(self.root, company_id):
            raw = read_optional(self.memory, brief_json_uri(company_id, root=self.root))
            guide = read_optional(self.memory, guide_json_uri(company_id, root=self.root))
            if not raw or not guide:
                return ""
            brief, payload = json.loads(raw), json.loads(guide)
            if (brief.get("company_id") != company_id or not brief.get("items")
                    or payload.get("company_id") != company_id or not payload.get("items")
                    or payload.get("l1_sha256") != sha256(raw.encode("utf-8")).hexdigest()):
                return ""
            return read_optional(self.memory, guide_md_uri(company_id, root=self.root)) or ""
