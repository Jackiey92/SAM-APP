"""Company-scoped conversation thread index and promotion boundary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from .memory.user_memory.l2_conversation import CONVERSATION_ROOT, ConversationStore
from .ports import MemoryProvider
from .memory.paths import parse, company_kv_uri, identifier


def _safe(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "", str(value))
    if not value:
        raise ValueError("thread scope is empty")
    return value


class ThreadManager:
    def __init__(self, memory: MemoryProvider, *, root: str | None = None):
        self.memory, self.root = memory, CONVERSATION_ROOT

    def _memory(self, company_id):
        return self.memory.scoped(company_id) if hasattr(self.memory, "scoped") else self.memory

    def index_uri(self, company_id: str) -> str:
        return company_kv_uri(company_id, "conversations", "thread_index.json")

    def _read_index(self, company_id: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._memory(company_id).read(self.index_uri(company_id)))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _write_index(self, company_id: str, rows: list[dict[str, Any]]) -> None:
        self._memory(company_id).put(self.index_uri(company_id), json.dumps(rows, ensure_ascii=False, sort_keys=True), metadata={"layer": "conversation", "type": "thread_index"})

    def create(self, company_id: str, thread_id: str, *, title: str, summary: str = "", substantive: bool = True) -> dict[str, Any]:
        company, thread = identifier(company_id), identifier(thread_id)
        if not substantive:
            return {"company_id": company, "thread_id": thread, "indexed": False}
        item = {"company_id": company, "thread_id": thread, "title": title[:200], "summary": summary[:1000], "created_at": datetime.now(timezone.utc).isoformat(), "last_opened_at": datetime.now(timezone.utc).isoformat(), "status": "open"}
        rows = [row for row in self._read_index(company) if not (row.get("company_id") == company and row.get("thread_id") == thread)]
        rows.append(item)
        self._write_index(company, sorted(rows, key=lambda row: row.get("thread_id", "")))
        self._memory(company).put(company_kv_uri(company, "conversations", identifier(thread), "thread.meta.json"), json.dumps(item, ensure_ascii=False, sort_keys=True), metadata={"layer": "conversation", "type": "thread_meta"})
        return item

    def list(self, company_id: str) -> list[dict[str, Any]]:
        company = identifier(company_id)
        return [row for row in self._read_index(company) if row.get("company_id") == company and row.get("status") != "archived"]

    def open(self, company_id: str, thread_id: str) -> dict[str, Any]:
        company, thread = identifier(company_id), identifier(thread_id)
        rows = self.list(company)
        for row in rows:
            if row.get("thread_id") == thread:
                return row
        raise KeyError("thread not visible")

    def archive(self, company_id: str, thread_id: str) -> None:
        company, thread = identifier(company_id), identifier(thread_id)
        rows = self._read_index(company)
        found = False
        for row in rows:
            if row.get("company_id") == company and row.get("thread_id") == thread:
                row["status"] = "archived"; found = True
        if not found:
            raise KeyError("thread not visible")
        self._write_index(company, rows)

    @staticmethod
    def recycle_candidate(thread: dict[str, Any], *, now: datetime | None = None) -> bool:
        if thread.get("status") != "open":
            return False
        last = thread.get("last_opened_at")
        try:
            old = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            return False
        stale = old < (now or datetime.now(timezone.utc)) - timedelta(days=7)
        brief = len(str(thread.get("summary", "")).split()) <= 30
        return stale and (brief or not thread.get("verified_fact_refs"))

    def promote(self, company_id: str, thread_id: str, *, fact_key: str, fact: dict[str, Any]) -> None:
        self.open(company_id, thread_id)
        if fact.get("status") != "verified" or not fact.get("source_refs") or not fact.get("derived_from_turn_ids"):
            raise ValueError("only verified facts derived from evidenced turns may be promoted")
        turns = ConversationStore(self.memory).read_range(company_id, thread_id)
        turn_ids = {item.get("turn_id") for item in turns}
        if not set(fact.get("derived_from_turn_ids", ())).issubset(turn_ids):
            raise ValueError("fact references unknown conversation turns")
        for ref in fact.get("source_refs", ()):
            uri = ref.get("uri") if isinstance(ref, dict) else ref
            if not isinstance(uri, str) or not uri.startswith("sam://original/"):
                raise ValueError("fact evidence must be a sam://original URI")
            parse(uri, company_id=company_id)
            try:
                self.memory.read(uri)
            except Exception as exc:
                raise ValueError("fact evidence is unavailable") from exc
        promoted = dict(fact)
        promoted["derived_from"] = list(fact["derived_from_turn_ids"])
        self.memory.put_fact(_safe(company_id), _safe(fact_key), promoted)


class ConversationTools:
    """Scoped read/promote tools exposed to an agent, never a global browser API."""
    def __init__(self, memory: MemoryProvider, company_id: str, thread_id: str):
        self.company_id, self.thread_id = identifier(company_id), identifier(thread_id)
        self.memory = memory.scoped(self.company_id) if hasattr(memory, "scoped") else memory
        self.store = ConversationStore(self.memory)
        self.manager = ThreadManager(self.memory)

    def conversation_read(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return self.store.read_range(self.company_id, self.thread_id, start=start, end=end)

    def thread_list(self) -> list[dict[str, Any]]:
        return self.manager.list(self.company_id)

    def thread_open(self, thread_id: str) -> str:
        row = self.manager.open(self.company_id, thread_id)
        uri = company_kv_uri(self.company_id, "conversations", identifier(thread_id), "L0", "thread_summary.md")
        try:
            text = self.memory.read(uri)
            return str(json.loads(text).get("summary", "")) if text else str(row.get("summary", ""))
        except FileNotFoundError:
            return str(row.get("summary", ""))
        except Exception as exc:
            raise RuntimeError("thread summary unavailable") from exc

    def promote(self, thread_id: str, *, fact_key: str, fact: dict[str, Any]) -> None:
        self.manager.promote(self.company_id, thread_id, fact_key=fact_key, fact=fact)
