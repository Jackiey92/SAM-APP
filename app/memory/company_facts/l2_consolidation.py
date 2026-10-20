"""2B consolidation: review employee L2 candidates before fact writes.

This module is intentionally independent of conversation ``promote``.  It is
the document-import path: employee candidates ->
auto-verified operating facts or human-gated todos.
"""
from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ...db.database import connect
from .l2_fact_extractor import ExtractedFact
from ..taxonomy import validated_module


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ConsolidationResult:
    verified_fact_ids: tuple[int, ...]
    todo_ids: tuple[int, ...]
    duplicate_fact_ids: tuple[int, ...]


SuggestionProvider = Callable[[ExtractedFact, str | None, str], str]
Authorizer = Callable[[dict, str], None]


def deterministic_suggestion(candidate: ExtractedFact, existing_value: str | None, reason: str) -> str:
    if reason == "critical_review":
        return (
            f"「{candidate.metric}」属于重大指标。建议核验原件坐标后，"
            f"由负责人选择是否采用候选值 {candidate.value}。"
        )
    if reason in {"uncertain", "anomaly"}:
        return f"「{candidate.metric}」候选值需核对原件坐标和语义后再确认，系统不会自动写入。"
    return (
        f"检测到 {candidate.metric} 在期间 {candidate.period} 的候选值 {candidate.value} "
        f"与当前值 {existing_value} 不一致。请核对双方原件坐标后人工决定；系统不作取舍。"
    )


class ConsolidationService:
    """Append-only fact/todo workflow with a pluggable, non-authoritative suggester."""

    def __init__(self, db_path, *, suggestion_provider: SuggestionProvider | None = None,
                 authorizer: Authorizer | None = None) -> None:
        self._db_path = db_path
        self._suggestion_provider = suggestion_provider
        # First release intentionally has no authentication.  The injected
        # hook is the sole extension point for a later password/SSO policy.
        self._authorizer = authorizer or (lambda _todo, _action: None)

    def _conn(self):
        return connect(self._db_path)

    def consolidate_candidates(self, candidates, *, unresolved=(), company_id=None, conn=None) -> ConsolidationResult:
        """Consolidate already ownership-filtered L2 candidates."""
        verified: list[int] = []
        todos: list[int] = []
        duplicates: list[int] = []
        owned = conn is None
        with self._conn() if owned else nullcontext(conn) as conn:
            conn.execute("BEGIN IMMEDIATE") if owned else None
            for candidate in candidates:
                current = self._current_fact(conn, candidate)
                if current is None:
                    if candidate.critical or candidate.review_reason:
                        reason = candidate.review_reason or "critical_review"
                        todo_id = self._open_todo(conn, candidate, existing=None, reason=reason)
                        if todo_id is not None:
                            todos.append(todo_id)
                    else:
                        verified.append(self._insert_fact(conn, candidate, confirm_mode="auto"))
                    continue
                if str(current["value"]) == candidate.value and current["unit"] == candidate.unit:
                    # Same coordinate/value remains one current fact. A resend
                    # of the same source is strictly idempotent; a second
                    # source with equal content also cannot create ambiguity.
                    duplicates.append(int(current["id"]))
                    continue
                todo_id = self._open_todo(conn, candidate, existing=current, reason="conflict")
                if todo_id is not None:
                    todos.append(todo_id)
            for item in unresolved:
                todo_id = self._unresolved_todo(conn, company_id, item)
                if todo_id is not None:
                    todos.append(todo_id)
            if owned:
                conn.commit()
        return ConsolidationResult(tuple(verified), tuple(todos), tuple(duplicates))

    @staticmethod
    def _unresolved_todo(conn, company_id, item):
        if not company_id:
            raise ValueError("company_id must be injected by the host")
        source = item["source"]
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
        # Evidence identity, independent of transport retries or bridge run IDs.
        key = sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cur = conn.execute(
            """INSERT INTO todos(company_id,entity,metric,period,candidate_value,reason,suggestion,
                 source_file,source_page,source_span,handoff_json,handoff_key,created_at)
               VALUES (?,?,'','unspecified','',?,?,?,?,?,?,?,?)
               ON CONFLICT(company_id,handoff_key) WHERE handoff_key IS NOT NULL DO NOTHING""",
            (company_id, source.get("entity") or "", item["reason"], item["blocking"],
             source["file_hash"], source.get("page"), source.get("span"), encoded, key, _now()),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    @staticmethod
    def _current_fact(conn, candidate: ExtractedFact):
        return conn.execute(
            """
            SELECT * FROM facts
            WHERE company_id=? AND entity=? AND attribute=? AND period=?
              AND superseded_by IS NULL AND valid_to IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            candidate.entity_coordinate,
        ).fetchone()

    def _open_todo(self, conn, candidate: ExtractedFact, *, existing, reason: str) -> int | None:
        duplicate = conn.execute(
            """
            SELECT id FROM todos WHERE company_id=? AND entity=? AND metric=? AND period=?
              AND candidate_value=? AND source_file=? AND source_page IS ? AND source_span IS ?
              AND reason=? AND status='open' AND handoff_key IS NULL
            LIMIT 1
            """,
            (candidate.company_id, candidate.entity or candidate.company_id, candidate.metric, candidate.period, candidate.value,
             candidate.source_file, candidate.source_page, candidate.source_span, reason),
        ).fetchone()
        if duplicate is not None:
            return None
        existing_value = str(existing["value"]) if existing is not None else None
        suggestion = self._suggest(candidate, existing_value, reason)
        handoff = dict(candidate.handoff or {})
        handoff.update(
            status="conflict" if existing is not None else "review",
            system_reason=reason,
            reason=handoff.get("reason") or candidate.review_reason or reason,
            blocking=handoff.get("blocking") or "human review required; system does not choose a value",
            existing={"fact_id": int(existing["id"]), "value": existing_value,
                      "source": {"file_hash": existing["source_file"], "page": existing["source_page"],
                                 "span": existing["source_span"]}} if existing is not None else None,
            candidate_value=candidate.value,
        )
        # Older/direct callers may have no source text. Do not fabricate it;
        # the import pipeline supplies the complete original block handoff.
        handoff.setdefault("source", {"file_hash": candidate.source_file, "page": candidate.source_page,
                                      "span": candidate.source_span, "text": None})
        handoff.setdefault("attribution", {"employee": "document_manager", "attribution_id": None})
        handoff.setdefault("candidate", {"metric": candidate.metric, "period": candidate.period,
                                          "entity": candidate.entity, "value": candidate.value,
                                          "review_reason": candidate.review_reason, "module_key": candidate.module_key})
        # One immutable candidate/evidence identity, independent of current
        # fact IDs, employee run IDs, reason wording or todo resolution status.
        identity = {"kind": "fact_review", "coordinate": candidate.entity_coordinate,
                    "value": candidate.value, "source_file": candidate.source_file,
                    "source_page": candidate.source_page, "source_span": candidate.source_span}
        key = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cur = conn.execute(
            """
            INSERT INTO todos(company_id,entity,metric,period,candidate_value,candidate_value_type,
                candidate_unit,existing_value,reason,suggestion,status,related_fact_id,
                source_file,source_page,source_span,handoff_json,handoff_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?)
            ON CONFLICT(company_id,handoff_key) WHERE handoff_key IS NOT NULL DO NOTHING
            """,
            (candidate.company_id, candidate.entity or candidate.company_id, candidate.metric, candidate.period, candidate.value,
             candidate.value_type, candidate.unit, existing_value, reason, suggestion,
             int(existing["id"]) if existing is not None else None, candidate.source_file,
             candidate.source_page, candidate.source_span,
             json.dumps(handoff, ensure_ascii=False, sort_keys=True), key, _now()),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def _suggest(self, candidate: ExtractedFact, existing_value: str | None, reason: str) -> str:
        if self._suggestion_provider is not None:
            try:
                text = self._suggestion_provider(candidate, existing_value, reason)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            except Exception:
                # A model-backed optional suggestion must never block fact
                # processing or change the deterministic decision path.
                pass
        return deterministic_suggestion(candidate, existing_value, reason)

    @staticmethod
    def _insert_fact(conn, candidate: ExtractedFact, *, confirm_mode: str) -> int:
        cur = conn.execute(
            """
            INSERT INTO facts(company_id,entity,attribute,period,value,value_type,unit,
                source_file,source_page,source_span,valid_from,confidence,status,confirm_mode,created_at,module_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (candidate.company_id, candidate.entity or candidate.company_id, candidate.metric, candidate.period,
             candidate.value, candidate.value_type, candidate.unit, candidate.source_file,
             candidate.source_page, candidate.source_span, _now(), candidate.confidence,
             "verified", confirm_mode, _now(), validated_module(candidate.module_key)),
        )
        return int(cur.lastrowid)

    def list_facts(self, *, company_id: str, file_hash: str | None = None) -> list[dict]:
        sql = "SELECT * FROM facts WHERE company_id=? AND superseded_by IS NULL AND valid_to IS NULL"
        params: list[str] = [company_id]
        if file_hash:
            sql += " AND source_file=?"
            params.append(file_hash)
        sql += " ORDER BY id ASC"
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def list_verified_facts(self, *, company_id: str) -> list[dict]:
        """All current, verified L2 facts in one host-injected company."""
        if not company_id:
            raise ValueError("company_id must be injected by the host")
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT * FROM facts WHERE company_id=? AND status='verified'
                   AND superseded_by IS NULL AND valid_to IS NULL ORDER BY id ASC""",
                (company_id,),
            ).fetchall()]

    def list_todos(self, *, company_id: str, status: str | None = "open") -> list[dict]:
        sql = "SELECT * FROM todos WHERE company_id=?"
        params: list[str] = [company_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id ASC"
        with self._conn() as conn:
            result = [dict(row) for row in conn.execute(sql, params).fetchall()]
        from ... import settings
        if settings.SAM_FACTS_TREE:
            from ...facts_tree.todos import list_md_todos
            result = [dict(item, kind='fact_conflict') for item in result]
            result += list_md_todos(self._db_path, company_id=company_id, status=status)
        return result

    def resolve(self, todo_id: int, *, choose: str) -> dict:
        """Resolve a reviewed candidate through the injectable policy hook.

        Conflicting values from one imported source are reviewed as one
        evidence bundle: accepting its candidate applies every still-open
        conflict from that source atomically. Each coordinate retains its own
        append-only todo and supersession trail.
        """
        with self._conn() as conn:
            todo = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
            if todo is None:
                raise KeyError(todo_id)
            if todo["status"] != "open":
                raise ValueError("todo is not open")
            if todo["handoff_json"] is not None and json.loads(todo["handoff_json"]).get("status") == "unresolved":
                raise ValueError("unresolved evidence needs employee re-attribution and extraction; cannot promote a placeholder")
            self._authorizer(dict(todo), "resolve")
            if choose in {"candidate", "existing"} and todo["reason"] == "conflict":
                targets = conn.execute(
                    """SELECT * FROM todos WHERE company_id=? AND source_file=?
                       AND reason='conflict' AND status='open' ORDER BY id ASC""",
                    (todo["company_id"], todo["source_file"]),
                ).fetchall()
            else:
                targets = [todo]
            resolved_ids: list[int] = []
            selected_fact_id: int | None = None
            now = _now()
            for target in targets:
                new_fact_id = None
                if choose != "existing":
                    value = str(target["candidate_value"]) if choose == "candidate" else str(choose)
                    candidate = ExtractedFact(
                        company_id=target["company_id"], metric=target["metric"], period=target["period"],
                        value=value, value_type=target["candidate_value_type"], unit=target["candidate_unit"],
                        source_file=target["source_file"], source_page=target["source_page"],
                        source_span=target["source_span"], confidence=1.0, critical=False,
                        entity=target["entity"] or target["company_id"],
                        module_key=validated_module(json.loads(target["handoff_json"] or "{}").get("candidate", {}).get("module_key")),
                    )
                    new_fact_id = self._insert_fact(conn, candidate, confirm_mode="manual")
                    if target["related_fact_id"] is not None:
                        conn.execute(
                            "UPDATE facts SET superseded_by=?,valid_to=? WHERE id=? AND superseded_by IS NULL",
                            (new_fact_id, now, target["related_fact_id"]),
                        )
                conn.execute(
                    "UPDATE todos SET status='resolved',resolved_at=? WHERE id=?", (now, target["id"])
                )
                resolved_ids.append(int(target["id"]))
                if int(target["id"]) == todo_id:
                    selected_fact_id = new_fact_id
            conn.commit()
            return {"id": todo_id, "status": "resolved", "fact_id": selected_fact_id,
                    "resolved_ids": resolved_ids}

    def dismiss(self, todo_id: int) -> dict:
        with self._conn() as conn:
            todo = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
            if todo is None:
                raise KeyError(todo_id)
            if todo["status"] != "open":
                raise ValueError("todo is not open")
            self._authorizer(dict(todo), "dismiss")
            conn.execute(
                "UPDATE todos SET status='dismissed',resolved_at=? WHERE id=?", (_now(), todo_id)
            )
            conn.commit()
            return {"id": todo_id, "status": "dismissed"}
