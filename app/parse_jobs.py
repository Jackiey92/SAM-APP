"""In-process upload parse queue with durable, pollable job state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import os
from uuid import uuid4
from threading import Event, Lock, Timer
import time
from typing import Callable, Any

from .db.database import connect
from .ports import RetryableRuntimeError
from .runtime_errors import compact_error


class ParseJobPartial(RuntimeError):
    error_kind = 'mapped_partial'


STATUSES = {"queued", "parsing", "done", "failed", "canceled", "needs_human", "partial"}
STAGES = {"queued", "uploading", "starting_engine", "parsing", "reviewing", "reading", "consolidating", "done", "failed", "canceled", "needs_human", "partial"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectionRebuildError(RuntimeError):
    """Safe user-facing failure after the authoritative L2 commit."""

    error_kind = "projection_failed"

    def __init__(self):
        super().__init__("事实已入库，但简报/导览重建失败（安全校验或生成失败）；请重试。")


class ParseJobCanceled(RuntimeError):
    """Raised by a worker after its private parser process was terminated."""


JobWorker = Callable[[dict[str, Any], Callable[..., None], Event], None]
CancelParser = Callable[[str], bool]
LOGGER = logging.getLogger(__name__)


class ParseJobManager:
    """Durable job ledger plus a small process-local executor.

    ``worker`` owns the actual parse/consolidate pipeline.  The manager only
    controls lifecycle, cancellation races, and persisted progress so it can
    be tested without a parser or external queue.
    """

    def __init__(self, db_path, worker: JobWorker, *, cancel_parser: CancelParser | None = None,
                 workers: int | None = None, logger: Any | None = None) -> None:
        self.db_path = db_path
        self.worker = worker
        self.cancel_parser = cancel_parser or (lambda _file_hash: False)
        requested = workers if workers is not None else int(
            os.environ.get("SAM_PARSE_CONCURRENCY", os.environ.get("SAM_PARSE_WORKERS", "4"))
        )
        upper_bound = max(1, int(os.environ.get(
            "SAM_PARSE_CONCURRENCY_MAX", os.environ.get("SAM_PARSE_WORKERS_MAX", "4")
        )))
        count = min(requested, upper_bound)
        if count < 1:
            raise ValueError("SAM_PARSE_WORKERS must be positive")
        self.worker_count = count
        self.startup_log_line = f"parse workers={count} (requested={requested}, max={upper_bound})"
        (logger or LOGGER).warning(self.startup_log_line)
        self.executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="sam-parse")
        self._events: dict[str, Event] = {}
        self._critical: set[str] = set()
        self._runtime_retries: dict[str, int] = {}
        self._retry_timers: dict[str, Timer] = {}
        self._lock = Lock()
        # This queue is process-local and the SAM entry owns a single manager.
        # A terminated Flask process cannot finish its old Futures. On restart
        # expose those interrupted generations as retryable terminal jobs.
        with connect(self.db_path) as conn:
            conn.execute("""UPDATE parse_jobs SET status='failed',stage='failed',
                         message='parse worker interrupted by process restart; retry required',
                         error_kind='interrupted',finished_at=?,updated_at=?
                         WHERE status IN ('queued','parsing')""", (_now(), _now()))
            conn.commit()

    def close(self) -> None:
        with self._lock:
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        for timer in timers:
            timer.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _new_job_id() -> str:
        # Random suffix: job_id has a global UNIQUE constraint, and two
        # concurrent creates of the same file within one millisecond must
        # never collide.
        stamp = str(int(time.time() * 1000))
        return "j_" + stamp + "_" + uuid4().hex[:12]

    @staticmethod
    def _insert_queued_job(conn, *, job_id: str, company_id: str, file_hash: str,
                           original_name: str, file_format: str, harness_format: str,
                           size_bytes: int, now: str) -> None:
        conn.execute(
            """INSERT INTO parse_jobs
            (job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,
             status,stage,progress_current,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'queued','queued',0,?,?)""",
            (job_id, company_id, file_hash, original_name, file_format, harness_format,
             size_bytes, now, now),
        )

    def create(self, *, company_id: str, file_hash: str, original_name: str,
               file_format: str, harness_format: str, size_bytes: int) -> dict[str, Any]:
        job_id = self._new_job_id()
        with connect(self.db_path) as conn:
            self._insert_queued_job(
                conn, job_id=job_id, company_id=company_id, file_hash=file_hash,
                original_name=original_name, file_format=file_format,
                harness_format=harness_format, size_bytes=size_bytes, now=_now(),
            )
            conn.commit()
        with self._lock:
            self._events[job_id] = Event()
        self._submit(job_id)
        return self.get(job_id)

    def create_idempotent(self, *, company_id: str, file_hash: str, original_name: str,
                          file_format: str, harness_format: str, size_bytes: int) -> dict[str, Any]:
        """Reuse the current job for (company_id, file_hash) or create exactly one.

        Upload-entry idempotency only: duplicate submissions of the same
        content for the same company return the existing job and never
        restart the worker.  With several historical rows an active
        (queued/parsing) job wins — the newest active one — because its
        result will supersede older terminal rows; with no active job the
        latest terminal row is returned as-is, so failed/canceled/
        needs_human/partial jobs are never auto-retried.  Retry stays an
        explicit operation; ``create`` keeps its explicit multi-job semantics
        for other callers.  BEGIN IMMEDIATE serializes the check-and-insert
        so concurrent duplicates can create and submit at most one job.
        """
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT job_id FROM parse_jobs WHERE company_id=? AND file_hash=?
                   ORDER BY CASE WHEN status IN ('queued','parsing') THEN 0 ELSE 1 END,
                            created_at DESC, id DESC LIMIT 1""",
                (company_id, file_hash),
            ).fetchone()
            if row is not None:
                conn.commit()
                return self.get(row["job_id"])
            job_id = self._new_job_id()
            self._insert_queued_job(
                conn, job_id=job_id, company_id=company_id, file_hash=file_hash,
                original_name=original_name, file_format=file_format,
                harness_format=harness_format, size_bytes=size_bytes, now=_now(),
            )
            conn.commit()
        with self._lock:
            self._events[job_id] = Event()
        self._submit(job_id)
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM parse_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list(self, *, company_id: str, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM parse_jobs WHERE company_id=?"
        params: list[Any] = [company_id]
        if status and status != "all":
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC"
        with connect(self.db_path) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        status = job["status"]
        if status == "done":
            raise RuntimeError("job already completed")
        if status in {"failed", "canceled", "needs_human", "partial"}:
            return job
        with self._lock:
            critical = job_id in self._critical
        if job["stage"] == "consolidating" or critical:
            raise RuntimeError("consolidation already started")
        with self._lock:
            event = self._events.setdefault(job_id, Event())
            event.set()
        if status == "queued":
            self._update(job_id, status="canceled", stage="canceled", message="已按用户要求停止", error_kind="canceled", finished_at=_now())
            return self.get(job_id)
        # Only terminate this job's bridge group.  Shared MinerU is not owned
        # by a job and is intentionally never passed to cancel_parser.  During
        # engine setup the worker has not reached its private bridge yet; its
        # next cancellation check is sufficient and avoids a stale intent.
        if job["stage"] == "parsing":
            self.cancel_parser(str(job["file_hash"]))
        return self.get(job_id)

    def retry(self, job_id: str, *, resume_unresolved=False, renew_deadline_seconds=None) -> dict[str, Any]:
        job = self.get(job_id)
        from . import settings
        allowed = {"failed", "canceled", "needs_human"}
        if settings.SAM_FACTS_TREE: allowed.add('partial')
        if job['status'] not in allowed:
            raise RuntimeError("only failed or canceled jobs can be retried")
        if settings.SAM_FACTS_TREE:
            if type(resume_unresolved) is not bool or (renew_deadline_seconds is not None and
                    (type(renew_deadline_seconds) is not int or not 1 <= renew_deadline_seconds <= 1800)):
                raise ValueError('invalid explicit mapped retry options')
            import json
            with connect(self.db_path) as conn:
                conn.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?) ON CONFLICT(uri) DO UPDATE SET content=excluded.content',
                    ('sam://kv/rt/md-upload-options/'+job_id, json.dumps(dict(resume_unresolved=resume_unresolved,
                     renew_deadline_seconds=renew_deadline_seconds,request_id=uuid4().hex)), '{}'))
        now = _now()
        with self._lock:
            # A canceled parsing worker may still be unwinding after its
            # private bridge group was terminated.  Do not replace its event
            # with a new generation until its cleanup is complete.
            if job_id in self._events:
                raise RuntimeError("job cancellation is still finishing")
            self._events[job_id] = Event()
            self._runtime_retries.pop(job_id, None)
        self._update(job_id, status="queued", stage="queued", message=None,
                     error_kind=None, progress_current=0, progress_total=None,
                     started_at=None, finished_at=None, updated_at=now)
        self._submit(job_id)
        return self.get(job_id)

    def _run(self, job_id: str) -> None:
        event = self._events.setdefault(job_id, Event())
        rescheduled = False
        job = {}
        try:
            job = self.get(job_id)
            if job["status"] != "queued":
                return
            self._update(job_id, status="parsing", stage="starting_engine", started_at=_now())
            self.worker(job, lambda **kwargs: self._progress(job_id, **kwargs), event)
            with self._lock:
                canceled = event.is_set()
            if canceled:
                self._update(job_id, status="canceled", stage="canceled", message="已按用户要求停止", error_kind="canceled", finished_at=_now())
            else:
                current = self.get(job_id)
                self._update(job_id, status="done", stage="done",
                             progress_current=current.get("progress_total") or current.get("progress_current") or 0,
                             message="解析完成", error_kind=None, finished_at=_now())
        except RetryableRuntimeError as exc:
            LOGGER.exception("parse job runtime failure job_id=%s", job_id)
            if event.is_set():
                self._update(job_id, status="canceled", stage="canceled", message="已按用户要求停止", error_kind="canceled", finished_at=_now())
            else:
                with self._lock:
                    attempt = self._runtime_retries.get(job_id, 0) + 1
                    self._runtime_retries[job_id] = attempt
                max_attempts = max(1, int(os.environ.get("SAM_RUNTIME_RETRIES", "5")))
                if attempt > max_attempts:
                    self._update(job_id, status="failed", stage="failed", message=compact_error(exc), error_kind="runtime", finished_at=_now())
                    raise
                else:
                    delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
                    self._update(job_id, status="queued", stage="starting_engine",
                                 message=f"运行时未就绪，将在 {delay:g}s 后重试：{exc}", error_kind="runtime")
                    rescheduled = True
                    timer = Timer(delay, self._submit_retry, args=(job_id, event))
                    timer.daemon = True
                    with self._lock:
                        self._retry_timers[job_id] = timer
                    timer.start()
        except BaseException as exc:
            # Executors capture even SystemExit in an otherwise ignored
            # Future. Always publish a terminal ledger state before letting
            # the original failure escape; no silent stuck worker thread.
            LOGGER.exception(
                "parse job failed job_id=%s file_hash=%s", job_id, job.get("file_hash")
            )
            if event.is_set():
                self._update(job_id, status="canceled", stage="canceled", message="已按用户要求停止", error_kind="canceled", finished_at=_now())
            else:
                kind = getattr(exc, "error_kind", "engine")
                terminal = "partial" if isinstance(exc, ParseJobPartial) else ("needs_human" if str(kind) == "document_quality" else "failed")
                self._update(job_id, status=terminal, stage=terminal, message=compact_error(exc) or "解析失败", error_kind=str(kind), finished_at=_now())
            raise
        finally:
            with self._lock:
                if not rescheduled and self._events.get(job_id) is event:
                    self._events.pop(job_id, None)
                if not rescheduled:
                    self._critical.discard(job_id)
                    self._runtime_retries.pop(job_id, None)

    def _submit_retry(self, job_id: str, event: Event) -> None:
        with self._lock:
            self._retry_timers.pop(job_id, None)
            if self._events.get(job_id) is not event or event.is_set():
                if self._events.get(job_id) is event:
                    self._events.pop(job_id, None)
                return
        self._submit(job_id)

    def _submit(self, job_id: str) -> None:
        with self._lock:
            generation = self._events.get(job_id)
        future = self.executor.submit(self._run, job_id)
        future.add_done_callback(lambda done: self._observe_failure(job_id, done, generation))

    def _observe_failure(self, job_id: str, future, generation) -> None:
        """Observe escaped errors, including an error in lifecycle reporting.

        _run publishes the normal failure state then re-raises. Futures used
        to be discarded, hiding any exception in that error handler itself.
        This last-resort reporter never replaces done/canceled/failed state.
        """
        if future.cancelled():
            return
        exc = future.exception()
        if exc is None:
            return
        LOGGER.error("parse job future failed job_id=%s", job_id,
                     exc_info=(type(exc), exc, exc.__traceback__))
        try:
            with self._lock:
                active = self._events.get(job_id)
                if active is not None and active is not generation:
                    return  # a user retry already owns a newer generation
                if self.get(job_id)["status"] in {"queued", "parsing"}:
                    self._update(job_id, status="failed", stage="failed",
                                 message=compact_error(exc) or "parse worker failed",
                                 error_kind=str(getattr(exc, "error_kind", "engine")), finished_at=_now())
        except Exception:
            LOGGER.exception("parse job terminal reporting failed job_id=%s", job_id)

    def _progress(self, job_id: str, *, stage: str | None = None, current: int | None = None,
                  total: int | None = None, message: str | None = None,
                  critical: bool = False, critical_end: bool = False) -> None:
        if stage is not None and stage not in STAGES:
            raise ValueError(f"unknown parse stage: {stage}")
        if critical:
            with self._lock:
                event = self._events.setdefault(job_id, Event())
                if event.is_set():
                    raise ParseJobCanceled("job canceled before consolidation")
                self._critical.add(job_id)
        if critical_end:
            with self._lock:
                self._critical.discard(job_id)
        updates: dict[str, Any] = {"updated_at": _now()}
        if stage is not None:
            updates["stage"] = stage
        if current is not None:
            updates["progress_current"] = max(0, int(current))
        if total is not None:
            updates["progress_total"] = max(0, int(total))
        if message is not None:
            updates["message"] = message
        self._update(job_id, **updates)

    def _update(self, job_id: str, **updates: Any) -> None:
        if not updates:
            return
        updates["updated_at"] = updates.get("updated_at") or _now()
        columns = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [job_id]
        with connect(self.db_path) as conn:
            conn.execute(f"UPDATE parse_jobs SET {columns} WHERE job_id=?", values)
            conn.commit()
