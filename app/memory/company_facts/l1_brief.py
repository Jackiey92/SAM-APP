"""Rebuild the company brief: SQLite L2 -> aggregation employee -> SAM L1.

Content selection, merging and numeric meaning belong exclusively to the
employee skill. The host checks shape and referential integrity, not meaning.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
from threading import Lock, RLock
from weakref import WeakValueDictionary

from ...employees.worker import EmployeeRunner, WorkerUnavailable
from ...memory_paths import MEMORY_ROOT
from ...ports import MemoryProvider
from .l2_consolidation import ConsolidationService
from .numeric_guard import validate, rejection, strip_marks
from .paths import brief_json_uri, brief_md_uri, fact_uri, module_brief_json_uri, module_brief_md_uri

_LOCKS = WeakValueDictionary()
_LOCKS_GUARD = Lock()
LOGGER = logging.getLogger(__name__)


@contextmanager
def projection_lock(root, company_id):
    """Serialize company projection snapshots within the upload worker pool."""
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault((root, company_id), RLock())
    with lock:
        yield


def read_optional(memory, uri):
    try:
        return memory.read(uri)
    except FileNotFoundError:
        return None


def publish_pair(memory, documents, *, metadata):
    """Best-effort rollback for providers without multi-document transactions.

    Readers in this process share projection_lock. A provider outage during
    rollback is surfaced, never reported as a successful projection write.
    """
    atomic = getattr(memory, "publish_documents", None)
    if atomic is not None:
        atomic(documents, metadata=metadata)
        return
    previous = {uri: read_optional(memory, uri) for uri in documents}
    attempted = []
    try:
        for uri, content in documents.items():
            if content is None and previous[uri] is None:
                continue
            attempted.append(uri)
            if content is None:
                memory.delete(uri)
            else:
                memory.put(uri, content, metadata=metadata)
    except Exception:
        for uri in reversed(attempted):
            if previous[uri] is None:
                memory.delete(uri)
            else:
                memory.put(uri, previous[uri], metadata=metadata)
        raise


class L1BriefService:
    def __init__(self, db_path, memory: MemoryProvider, *, worker=None):
        self.facts = ConsolidationService(db_path)
        self.memory = memory
        self.worker = worker or EmployeeRunner()
        self.root = getattr(memory, "memory_root", MEMORY_ROOT)

    def rebuild(self, *, company_id: str, thread_id: str | None = None,
                module_key: str | None = None, new_fact_ids=None, full=False) -> dict | None:
        json_uri = brief_json_uri(company_id, root=self.root)
        md_uri = brief_md_uri(company_id, root=self.root)
        if module_key is not None:
            json_uri = module_brief_json_uri(company_id, module_key)
            md_uri = module_brief_md_uri(company_id, module_key)
        with projection_lock(self.root, company_id):
            facts = self.facts.list_verified_facts(company_id=company_id)
            if module_key is not None:
                facts = [fact for fact in facts if fact.get("module_key") == module_key]
            previous_raw = read_optional(self.memory, json_uri) if module_key else None
            previous = json.loads(previous_raw) if previous_raw else None
            if previous is not None and (not isinstance(previous, dict)
                    or previous.get("company_id") != company_id or previous.get("module_key") != module_key
                    or not isinstance(previous.get("items"), list)):
                raise WorkerUnavailable("invalid previous module scope")
            if not facts:
                # Empty source is different from employee unavailability:
                # erase only this rebuildable projection, never L2 or 2A.
                publish_pair(self.memory, {md_uri: None, json_uri: None},
                             metadata={"layer": "2b_L1", "company_id": company_id})
                return None
            # Full company snapshot, across ALL source files. No SAM L2 scan.
            input_facts = [{**fact, "fact_id": fact["id"]} for fact in facts]
            request = {"facts": input_facts}
            if module_key is not None:
                new_ids = set(new_fact_ids or ())
                request = {"module_key": module_key, "previous_brief": previous,
                           "facts": [fact for fact in input_facts if full or previous is None or fact["id"] in new_ids],
                           "conflicts": [todo for todo in self.facts.list_todos(company_id=company_id)
                               if todo["reason"] == "conflict" and json.loads(todo.get("handoff_json") or "{}").get("candidate", {}).get("module_key") == module_key]}
            result = self.worker.run(
                skill="fact-brief", text=json.dumps(request, ensure_ascii=False),
                company_id=company_id, thread_id=thread_id,
            )
            if not result:
                LOGGER.warning("L1 employee returned no brief company_id=%s; preserving previous version", company_id)
                return None  # unresolved: preserve the previous version
            available = {fact["id"] for fact in facts}
            if module_key and previous and not full:
                available &= set(new_fact_ids or ()) | {ref for item in previous["items"] for ref in item["fact_ids"]}
            by_id = {fact["id"]: fact for fact in facts}
            old_items = {item["entry_id"]: item for item in (previous or {}).get("items", [])}
            seen = set()
            next_id = max([int(key.rsplit("-", 1)[1]) for key in old_items] or [0])
            items = []
            for index, item in enumerate(result, start=1):
                if not isinstance(item, dict):
                    raise WorkerUnavailable("invalid L1 item")
                conclusion, topic, refs = item.get("conclusion"), item.get("topic"), item.get("fact_ids")
                if (not isinstance(conclusion, str) or not conclusion.strip()
                        or not isinstance(topic, str) or not topic.strip()
                        or not isinstance(refs, list) or not refs
                        or any(type(ref) is not int or ref not in available for ref in refs)):
                    raise WorkerUnavailable("invalid L1 item or L2 fact reference")
                entry_id = f"brief-{index}"
                if module_key:
                    action = item.get("action", "add" if previous is None else None)
                    if action not in {"add", "retain", "revise"}:
                        raise WorkerUnavailable("module item requires add/retain/revise action")
                    if action == "add":
                        next_id += 1
                        entry_id = f"{module_key}-{next_id}"
                    else:
                        entry_id = item.get("entry_id")
                        old = old_items.get(entry_id)
                        if old is None:
                            raise WorkerUnavailable("unknown module entry ID")
                        if action == "retain" and any(item.get(key) != old[key] for key in ("topic", "conclusion", "fact_ids")):
                            raise WorkerUnavailable("retained module entry was changed")
                        if action == "revise" and (not isinstance(item.get("reason"), str) or not item["reason"].strip()
                                or not isinstance(item.get("revised_fact_ids"), list) or not item["revised_fact_ids"]
                                or any(type(ref) is not int or ref not in old["fact_ids"] for ref in item["revised_fact_ids"])):
                            raise WorkerUnavailable("module revision requires old fact IDs and reason")
                    if entry_id in seen:
                        raise WorkerUnavailable("duplicate module entry ID")
                    seen.add(entry_id)
                items.append({"entry_id": entry_id, "topic": topic,
                              "conclusion": conclusion, "fact_ids": refs,
                              "sources": [{"fact_id": ref, "uri": fact_uri(company_id, ref, root=self.root, module_key=by_id[ref].get("module_key")),
                                           "file_hash": by_id[ref]["source_file"],
                                           "page": by_id[ref]["source_page"],
                                           "span": by_id[ref]["source_span"]} for ref in refs],
                              "attribution_id": getattr(result, "attribution_id", None)})
                if module_key and item.get("action") == "retain":
                    items[-1] = old_items[entry_id]
                elif module_key and item.get("action") == "revise":
                    items[-1]["revision"] = {"fact_ids": item["revised_fact_ids"], "reason": item["reason"]}
            if old_items.keys() - seen:
                raise WorkerUnavailable("module update omitted existing entries")
            violations = []
            for item in items:
                verified, errors = validate(item["conclusion"],
                    {str(ref): by_id[ref] for ref in item["fact_ids"]}, kind="f")
                item["verified_numbers"] = verified
                for error in errors:
                    ref = next((int(ref) for ref in error["ids"] if ref.isdigit()
                                and int(ref) in item["fact_ids"]), item["fact_ids"][0])
                    card = by_id[ref]
                    violations.append({**error, "entry_id": item["entry_id"],
                        "source": {"file_hash": card["source_file"], "page": card["source_page"],
                                   "span": card["source_span"], "entity": card.get("entity")}})
                # Topics are displayed prose, but the employee contract forbids
                # numeric topics. Reject rather than silently bypass the guard.
                _, topic_errors = validate(item["topic"], {}, kind="f")
                violations.extend({**error, "entry_id": item["entry_id"]} for error in topic_errors)
            if violations:
                return rejection("L1", violations)
            payload = {"company_id": company_id, "items": items,
                       "run_id": getattr(result, "run_id", None),
                       "session_id": getattr(result, "session_id", None)}
            if module_key:
                payload["module_key"] = module_key
            markdown = "\n\n".join(
                f"<a id=\"{item['entry_id']}\"></a>\n## {item['topic']}\n"
                f"{strip_marks(item['conclusion'])}\n\nL2 fact_id: " + ", ".join(map(str, item["fact_ids"]))
                + "\n" + "\n".join(f"[L2 {source['fact_id']}]({source['uri']})" for source in item["sources"])
                for item in items
            ) + "\n"
            publish_pair(self.memory, {md_uri: markdown, json_uri: json.dumps(payload, ensure_ascii=False, sort_keys=True)},
                         metadata={"layer": "2b_L1", "company_id": company_id})
            return payload
