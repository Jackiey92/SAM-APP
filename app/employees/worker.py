"""Bounded, scope-aware generic employee runtime seam for 2B R1."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from ..runtime_config import RuntimeConfig
from ..providers import runtime_provider
from ..harness.staging import StagingStore
from ..runtime_errors import EmployeeTransportError, check_transport_envelope, transport_exception
from ..employee_policy import call_timeout, employee_total_timeout, seconds
from .budget import EmployeeBudgetExceeded, CURRENT_PARSE_BUDGET

LOGGER = logging.getLogger(__name__)


class WorkerUnavailable(RuntimeError):
    """Employee execution failed or returned an unusable candidate payload."""


class EmployeeResult(list):
    """List-compatible candidates with optional, transport-owned replay IDs."""

    def __init__(self, items, *, run_id=None, session_id=None, unresolved=()):
        super().__init__(items)
        self.run_id, self.session_id = run_id, session_id
        self.unresolved = tuple(unresolved)

    @property
    def attribution_id(self):
        # No local ID generation and no signature/accountability ledger here.
        return self.run_id or self.session_id


class EmployeeRunner:
    def __init__(self, *, config: RuntimeConfig | None = None, runtime=None,
                 runner: Callable[..., Any] | None = None, timeout: int | None = None,
                 retries: int | None = None, retry_backoff: float | None = None,
                 retry_max_wait: float | None = None, total_timeout: float | None = None,
                 deadline: float | None = None):
        self.config = config or RuntimeConfig.from_env()
        self.runtime = runtime
        self.runner = runner
        self.timeout = timeout
        # A malformed transport envelope is occasionally produced by the
        # gateway. Retry the unchanged request once, but keep this bounded so
        # malformed output cannot turn into an unbounded business retry loop.
        self.retries = max(0, min(2, int(retries if retries is not None else os.environ.get("SAM_EMPLOYEE_RETRIES", "2"))))
        self.retry_backoff = self._seconds(retry_backoff, "SAM_EMPLOYEE_RETRY_BACKOFF", 1.0)
        self.retry_max_wait = self._seconds(retry_max_wait, "SAM_EMPLOYEE_RETRY_MAX_WAIT", 4.0)
        self.total_timeout = employee_total_timeout(total_timeout)
        self.deadline = deadline

    @staticmethod
    def _seconds(value, env, default):
        return seconds(value, env, default)

    def _skill(self, skill: str) -> str:
        path = self.config.skill_root / skill / "SKILL.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkerUnavailable("employee skill unavailable") from exc

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        """Remove one outer Markdown JSON fence without changing payload data."""
        raw = raw.strip()
        lines = raw.splitlines()
        if len(lines) < 3:
            return raw
        opening = lines[0].strip().lower()
        if opening in {"```", "```json"} and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        # Do not search for a fence inside an OpenClaw JSON envelope. An
        # envelope may legitimately contain a fenced employee response in a
        # nested text field; stripping that inner fence here corrupts the outer
        # JSON before it can be decoded and traversed.
        return raw

    @staticmethod
    def _payload(raw: Any) -> list[dict[str, Any]]:
        try:
            if isinstance(raw, str):
                raw = json.loads(EmployeeRunner._strip_json_fence(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkerUnavailable("employee returned invalid JSON") from exc
        if isinstance(raw, dict) and ("candidates" in raw or "items" in raw):
            unresolved = raw.get("unresolved") or []
            if isinstance(unresolved, dict):
                unresolved = [unresolved]
            if not isinstance(unresolved, list) or not all(isinstance(item, dict) for item in unresolved):
                raise WorkerUnavailable("employee returned invalid unresolved payload")
            items = raw.get("candidates", raw.get("items"))
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise WorkerUnavailable("employee returned invalid candidates")
            return EmployeeResult(items, unresolved=unresolved)
        elif isinstance(raw, dict):
            # OpenClaw CLI versions have emitted the same payload under
            # result.payloads and top-level payloads. Content/text/data are
            # accepted only as transport wrappers; business candidate objects
            # still must arrive through candidates/items.
            for key in ("result", "payloads", "content", "text", "payload", "data"):
                if key not in raw or raw[key] in (None, ""):
                    continue
                if key == "payloads":
                    payloads = raw[key]
                    if not isinstance(payloads, list) or not payloads:
                        raise WorkerUnavailable("employee returned invalid OpenClaw payloads")
                    candidates = EmployeeResult([])
                    unresolved = []
                    for payload in payloads:
                        if not isinstance(payload, dict):
                            raise WorkerUnavailable("employee returned invalid OpenClaw payload")
                        value = next(
                            (payload[name] for name in ("text", "content", "payload", "data")
                             if name in payload and payload[name] not in (None, "")),
                            None,
                        )
                        if value is None:
                            raise WorkerUnavailable("employee returned invalid OpenClaw payload")
                        parsed = EmployeeRunner._payload(value)
                        candidates.extend(parsed)
                        unresolved.extend(getattr(parsed, "unresolved", ()))
                    candidates.unresolved = tuple(unresolved)
                    return candidates
                try:
                    return EmployeeRunner._payload(raw[key])
                except WorkerUnavailable:
                    continue
            # A bare JSON object is the skill's direct decision form.  It is
            # not an envelope when it has no recognized transport container;
            # nested objects remain rejected so unknown wrapper shapes do not
            # silently become business candidates.
            if (
                raw
                and not {"status", "error", "message"}.intersection(raw)
                and all(not isinstance(value, (dict, list)) for value in raw.values())
            ):
                return [raw]
            raise WorkerUnavailable("employee returned invalid OpenClaw envelope")
        if isinstance(raw, list):
            if not raw:
                return []
            text_blocks = (
                all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in raw)
                and all(
                    set(item).issubset({"text", "type", "annotations", "_meta"})
                    for item in raw
                )
            )
            if text_blocks:
                candidates = EmployeeResult([])
                unresolved = []
                for payload in raw:
                    parsed = EmployeeRunner._payload(payload["text"])
                    candidates.extend(parsed)
                    unresolved.extend(getattr(parsed, "unresolved", ()))
                candidates.unresolved = tuple(unresolved)
                return candidates
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise WorkerUnavailable("employee returned invalid candidates")
        return raw

    @staticmethod
    def _replay_ids(raw: Any) -> dict:
        """Read only known transport metadata, never model candidate fields."""
        if isinstance(raw, str):
            try:
                raw = json.loads(EmployeeRunner._strip_json_fence(raw))
            except (TypeError, json.JSONDecodeError):
                return {}
        if not isinstance(raw, dict) or "candidates" in raw or "items" in raw:
            return {}
        ids = {}
        for output, key in (("run_id", "runId"), ("session_id", "sessionId")):
            if isinstance(raw.get(key), str) and raw[key]:
                ids[output] = raw[key]
        for key in ("result", "meta", "agentMeta"):
            nested = EmployeeRunner._replay_ids(raw.get(key))
            for name, value in nested.items():
                ids.setdefault(name, value)
        return ids

    def run(self, *, skill: str, text: str, company_id: str, thread_id: str | None = None,
            bounded_model: bool = False, budget=None) -> EmployeeResult:
        if not company_id:
            raise ValueError("company_id must be injected by the host")
        task_budget = CURRENT_PARSE_BUDGET.get()
        bounded_model = bounded_model or task_budget is not None
        if bounded_model:
            thread_id = None
        scope = {"company_id": company_id, "thread_id": thread_id}
        skill_text = self._skill(skill)
        per_attempt = call_timeout(skill, self.timeout)
        deadline = time.monotonic() + self.total_timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        fallback = os.environ.get("SAM_EMPLOYEE_FALLBACK_MODEL", "").strip() if bounded_model else ""
        failures = []
        attempts = self.retries + 1
        for attempt in range(attempts + bool(fallback)):
            remaining = deadline - time.monotonic()
            for guard in (budget, task_budget):
                if guard is not None:
                    remaining = min(remaining, guard.remaining())
            timeout = min(per_attempt, int(remaining))
            if timeout < 1:
                raise EmployeeTransportError(f"employee skill={skill} attempts={attempt}: total deadline exhausted; {'; '.join(failures)}")
            try:
                guards = [guard for guard in (budget, task_budget) if guard is not None]
                for guard in guards:
                    guard.check()
                for guard in guards:
                    guard.consume()
                if self.runner is not None:
                    raw = self.runner(skill=skill, skill_text=skill_text, text=text, scope=scope,
                                      timeout=timeout)
                elif bounded_model:
                    from .model_tool import EmployeeModelTool
                    raw = EmployeeModelTool(self.config).execute(
                        skill_text=skill_text, text=text, timeout=timeout,
                        model=fallback if attempt >= attempts else None)
                else:
                    if self.runtime is None:
                        self.runtime = runtime_provider(self.config, StagingStore(self.config.main_db))
                    prompt = "Load the supplied employee skill and return only its required JSON candidates/items payload."
                    raw = self.runtime.run_agent_message(
                        prompt, context_text=skill_text + "\n\nBLOCK:\n" + text,
                        company_id=company_id, thread_id=thread_id, timeout=timeout,
                    )
                check_transport_envelope(raw, require_response=True)
                parsed = self._payload(raw)
                return EmployeeResult(parsed, unresolved=getattr(parsed, "unresolved", ()), **self._replay_ids(raw))
            except Exception as exc:
                if isinstance(exc, EmployeeBudgetExceeded):
                    raise
                failure = transport_exception(exc)
                # Preserve the existing bounded malformed-envelope retry;
                # explicit employee decisions (including []/ambiguous) return
                # above and are NEVER retried for their business meaning.
                if failure is None and not isinstance(exc, WorkerUnavailable):
                    raise
                failures.append(f"attempt={attempt + 1} {failure or exc}")
                can_fallback = bool(fallback and failure is not None and failure.retryable)
                if (failure is not None and not failure.retryable) or (attempt >= self.retries and not (can_fallback and attempt < attempts)):
                    if failure is not None:
                        raise EmployeeTransportError(
                            f"employee skill={skill} attempts={attempt + 1}: {failures[-1]}; earlier={'; '.join(failures[:-1])}",
                            retryable=failure.retryable,
                        ) from exc
                    raise
                delay = 0 if attempt >= self.retries else min(self.retry_max_wait, self.retry_backoff * (2 ** attempt))
                if time.monotonic() + delay + 1 >= deadline:
                    raise EmployeeTransportError(f"employee skill={skill} attempts={attempt + 1}: retry deadline exhausted: {'; '.join(failures)}") from exc
                LOGGER.warning("employee retry skill=%s attempt=%s delay=%gs error=%s",
                               skill, attempt + 1, delay, failure or exc)
                for guard in (budget, task_budget):
                    if guard is not None and time.monotonic() + delay + 1 >= guard.deadline:
                        raise EmployeeBudgetExceeded(f"employee retry time budget exhausted attempts={attempt + 1}: {failure or exc}") from exc
                time.sleep(delay)
        # The loop either returns or re-raises the last payload error.
        raise WorkerUnavailable("employee returned invalid JSON")
