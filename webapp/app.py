"""Startup AI Manager prototype and document-import APIs."""
from __future__ import annotations

import json
import hmac
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import re

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

import sys as _sys
BASE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = BASE_DIR.parent
_sys.path.insert(0, str(_PROJECT_ROOT))

from app import settings

from app.storage import ArchiveFileStore
from app.db.database import validate_schema
from app.classifier import ClassificationService
from app.module_taxonomy import module_definitions
from app.db.database import connect
from app.classifier.semantic_folders import classify as classify_resource_folder
from app.memory.company_facts import ConsolidationService, L2CompleteFactService, L2ProjectionService, L1BriefService, L0GuideService
from app.memory.company_facts.module_projection import ModuleProjectionService
from app.entities import EntityBridgeService, EntityRosterService
from app.guidance import GuideJobCoordinator, ImportGuideService, ModelUnavailable
from app.harness.staging import StagingStore
from app.memory_map import MapBuilder, MemoryMapTools, source_catalog_from_db
from app.memory.user_memory.l2_conversation import ConversationStore, deterministic_summary
from app.context_assembler import ContextAssembler
from app.thread_manager import ConversationTools
from app.config_sync import sync_agent_config, CONFIG_FILES
from app.ports import MemoryUnavailable
from app.providers import memory_provider, runtime_provider
from app.runtime_config import RuntimeConfig
from app.employees import EmployeeRunner, SemanticEmployee
from app.memory.user_memory.l1_memory_brief import RuntimeWorkingMemory
from app.upload_status import parse_result_status
from app.parse_jobs import ParseJobCanceled, ParseJobManager
from app.runtime_errors import EmployeeTransportError
from app.business_overview import BusinessOverviewService
from app.dashboard import DashboardPreferenceStore, REGISTRY, assemble_dashboard
from app.knowledge import KnowledgeService, mapping_redacted_projection
from app.home_shell import render_home_shell

RUNTIME_CONFIG = RuntimeConfig.from_env(project_root=_PROJECT_ROOT)
DATA_ROOT = RUNTIME_CONFIG.data_root
DATA_DIR = RUNTIME_CONFIG.app_db.parent
OBJECTS_DIR = RUNTIME_CONFIG.objects_dir
MAIN_DB = RUNTIME_CONFIG.main_db

if not RUNTIME_CONFIG.cloud_readonly:
    validate_schema(MAIN_DB)

app = Flask(__name__)
app.extensions["sam_runtime_config"] = RUNTIME_CONFIG
app.extensions["sam_memory_provider"] = memory_provider(RUNTIME_CONFIG)

# Both parsing/consolidation and model guidance run outside the Flask request
# thread.  The upload endpoint only stores the immutable original and creates
# a durable parse-job row; clients observe actual parser stages via /inbox.
_GUIDE_EXECUTOR = None if RUNTIME_CONFIG.cloud_readonly else ThreadPoolExecutor(max_workers=2, thread_name_prefix="sam-leader")
_GUIDE_COORDINATOR = None if RUNTIME_CONFIG.cloud_readonly else GuideJobCoordinator(
    _GUIDE_EXECUTOR, debounce_seconds=float(os.environ.get("SAM_GUIDE_DEBOUNCE", "3"))
)


@app.before_request
def require_knowledge_auth():
    if RUNTIME_CONFIG.cloud_readonly:
        return None  # cloud_boundary applies this same auth to the entire window
    if request.url_rule is None or request.url_rule.rule not in {
        "/knowledge", "/api/knowledge", "/knowledge/sources/<file_hash>",
        "/api/knowledge/sources/<file_hash>/original",
    }:
        return None
    username = os.environ.get("SAM_VIEW_USER")
    password = os.environ.get("SAM_VIEW_PASS")
    if username is None and password is None:
        return None

    # Incomplete/empty configuration fails closed; compare UTF-8 credentials.
    auth = request.authorization
    if username and password and auth is not None and auth.type == "basic":
        user_matches = hmac.compare_digest((auth.username or "").encode("utf-8"), username.encode("utf-8"))
        pass_matches = hmac.compare_digest((auth.password or "").encode("utf-8"), password.encode("utf-8"))
        if user_matches and pass_matches:
            return None
    return "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="SAM knowledge"'}


@app.after_request
def add_cors(resp):
    if RUNTIME_CONFIG.cloud_readonly:
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp  # cloud snapshots and credentials are same-origin only
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def now() -> str:
    return datetime.now(timezone.utc).isoformat()



def classification_service(*, employee=None) -> ClassificationService:
    """Return the company-scoped 2A navigation-label ledger."""
    return ClassificationService(
        MAIN_DB, employee=employee or SemanticEmployee(EmployeeRunner(config=RUNTIME_CONFIG)),
    )


def semantic_employee(*, deadline=None) -> SemanticEmployee:
    """Build the generic file employee used by classification and attribution."""
    return SemanticEmployee(EmployeeRunner(config=RUNTIME_CONFIG, deadline=deadline))


def consolidation_service() -> ConsolidationService:
    """2B document-consolidation path; deliberately unrelated to chat promote."""
    return ConsolidationService(MAIN_DB)


def _scoped_todos(*, company_id, status='open'):
    result = consolidation_service().list_todos(company_id=company_id, status=status)
    if settings.SAM_FACTS_TREE:
        host = os.environ.get('SAM_COMPANY_ID', 'default').strip()
        result = [t for t in result if t.get('kind') != 'md_content_conflict' or company_id == host]
    return result


def business_overview_service() -> BusinessOverviewService:
    """Read-only deterministic business profile extracted from parsed L2."""
    return BusinessOverviewService(MAIN_DB, app.extensions["sam_memory_provider"])


def dashboard_preferences() -> DashboardPreferenceStore:
    return DashboardPreferenceStore(MAIN_DB)


def init_db() -> None:
    """Validate the ledger; migration is an explicit administrator operation."""
    validate_schema(MAIN_DB)


def _has_declared_self(company_id: str) -> bool:
    """Require an explicit host identity before content attribution begins."""
    rows = EntityRosterService(MAIN_DB).list(company_id=company_id, status="active")
    return any(row.get("origin") == "declared" and row.get("entity_type") == "self" for row in rows)


# Ensure the durable queue table exists for both the Flask entry point and the
# test client.  This is schema initialization only; no upload is processed at
# import time.
if not RUNTIME_CONFIG.cloud_readonly:
    init_db()


def detect_format(filename: str) -> str:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return "excel"
    if name.endswith((".ppt", ".pptx")):
        return "ppt"
    if name.endswith((".doc", ".docx")):
        return "doc"
    if name.endswith(".pdf"):
        return "pdf"
    return "unknown"


def import_guide(*, event: str, uploaded_file_hash: str | None = None,
                 company_id: str | None = None) -> dict:
    """Ask the configured OpenClaw main Agent for an import guide."""
    staging = StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / "map")
    service = ImportGuideService(
        store=ArchiveFileStore(objects_path=OBJECTS_DIR, db_path=MAIN_DB),
        db_path=MAIN_DB,
        runtime_provider=runtime_provider(RUNTIME_CONFIG, staging),
        company_id=company_id or os.environ.get("SAM_COMPANY_ID", "default"),
    )
    return service.generate(event=event, uploaded_file_hash=uploaded_file_hash)


def _guide_key(company_id: str) -> str:
    return f"{company_id}:import-guide"


def _redact_guide_diagnostic(raw: str) -> str:
    """Keep debugging local while ensuring accidental credentials never persist."""
    raw = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", raw)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", raw)


def _record_guide_diagnostic(company_id: str, exc: Exception) -> None:
    raw = getattr(exc, "raw_response", None)
    diagnostic_dir = DATA_ROOT / "guide-diagnostics"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}.json"
    path = diagnostic_dir / filename
    path.write_text(json.dumps({
        "created_at": now(), "company_id": company_id,
        "reason": getattr(exc, "reason", "invalid_json"),
        "raw_envelope": _redact_guide_diagnostic(raw[:32768]) if isinstance(raw, str) else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    app.logger.warning("import guide invalid envelope captured locally: %s", path.name)


def _run_import_guide(*, event: str, uploaded_file_hash: str | None, company_id: str) -> dict:
    try:
        return import_guide(event=event, uploaded_file_hash=uploaded_file_hash, company_id=company_id)
    except ModelUnavailable as exc:
        _record_guide_diagnostic(company_id, exc)
        raise


def queue_import_guide(uploaded_file_hash: str, *, company_id: str | None = None):
    """Queue/revise one company guide without holding the upload request."""
    company = company_id or os.environ.get("SAM_COMPANY_ID", "default")
    return _GUIDE_COORDINATOR.queue(
        _guide_key(company),
        lambda: _run_import_guide(event="upload", uploaded_file_hash=uploaded_file_hash, company_id=company),
    )


def _rebuild_fact_projections(job, *, deadline):
    from app.parse_jobs import ProjectionRebuildError
    try:
        memory = app.extensions["sam_memory_provider"]
        L2ProjectionService(MAIN_DB, memory).publish(company_id=job["company_id"])
        with connect(MAIN_DB) as conn:
            row = conn.execute("SELECT projection_fact_ids FROM parse_jobs WHERE job_id=? AND company_id=?",
                               (job["job_id"], job["company_id"])).fetchone()
        if row and row["projection_fact_ids"] is not None:
            ids = json.loads(row["projection_fact_ids"])
        else:
            # Upgrade old durable jobs without expanding their input to all files.
            ids = [fact["id"] for fact in ConsolidationService(MAIN_DB).list_verified_facts(company_id=job["company_id"])
                   if fact["source_file"] == job["file_hash"]]
        result = ModuleProjectionService(MAIN_DB, memory,
            worker=EmployeeRunner(config=RUNTIME_CONFIG, deadline=deadline)).update(
                company_id=job["company_id"], verified_fact_ids=ids,
                thread_id=f"parse-job:{job['job_id']}:modules")
        if result["unresolved"]:
            app.logger.warning("module projection unresolved job_id=%s modules=%s", job["job_id"],
                               [item["module_key"] for item in result["unresolved"]])
            transport = next((item.get("exception") for item in result["unresolved"]
                              if isinstance(item.get("exception"), EmployeeTransportError)), None)
            if transport is not None:
                raise transport
            raise ProjectionRebuildError()
    except EmployeeTransportError:
        raise
    except Exception as exc:
        app.logger.exception("L1/L0 rebuild failed after L2 commit job_id=%s company_id=%s",
                             job["job_id"], job["company_id"])
        raise ProjectionRebuildError() from exc


def _classify_committed_document(job: dict, *, deadline: float) -> None:
    """Retryable metadata step; authoritative verified facts are already committed."""
    staging = StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / "map")
    with connect(MAIN_DB) as conn:
        row = conn.execute("SELECT id FROM parse_staging WHERE file_hash=? ORDER BY id DESC LIMIT 1",
                           (job["file_hash"],)).fetchone()
    if row is None:
        raise RuntimeError("committed document staging missing")
    staging_id = row["id"]
    payload = staging.get(staging_id)["payload"]
    if payload.get("classification_complete"):
        return
    employee = semantic_employee(deadline=deadline)
    # Content classification is a local label, independent of the 2B
    # transaction. No source content or mapping is sent to OV.
    classification = classification_service(employee=employee).classify_parsed(
        file_hash=job["file_hash"], company_id=job["company_id"], manifest=payload,
    )
    resource_folder = classification.get("resource_folder") or classify_resource_folder(
        text=json.dumps(payload, ensure_ascii=False),
        skills_root=RUNTIME_CONFIG.skill_root,
        employee=employee, company_id=job["company_id"],
        thread_id=f"parse-job:{job['job_id']}",
    )
    staging.update_payload(staging_id, {"resource_folder": resource_folder})

    staging.update_payload(staging_id, {"classification_complete": True})


def _parse_job_worker(job: dict, progress, cancel_event) -> None:
    """Task-scoped employee model tool budget, with durable/logged counters."""
    from app.employees.budget import parse_model_budget
    if settings.SAM_FACTS_TREE:
        # MD reader owns durable lifetime phase budgets. Do not wrap it in the
        # old short aggregate employee budget or reset counts on upload retry.
        return _parse_job_pipeline(job, progress, cancel_event)
    with parse_model_budget() as model_budget:
        try:
            _parse_job_pipeline(job, progress, cancel_event)
            model_budget.remaining()
        finally:
            metrics = {"job_id": job["job_id"], "company_id": job["company_id"],
                       "model_calls": model_budget.model_calls,
                       "model_call_scope": "post_parse_employees", "model_call_limit": model_budget.max_calls,
                       "elapsed_seconds": round(time.monotonic() - model_budget.started, 3),
                       "timeout_seconds": model_budget.timeout}
            app.logger.info("parse employee model metrics=%s", json.dumps(metrics))
            try:
                with connect(MAIN_DB) as conn:
                    row = conn.execute("SELECT id FROM parse_staging WHERE file_hash=? ORDER BY id DESC LIMIT 1",
                                       (job["file_hash"],)).fetchone()
                if row is not None:
                    StagingStore(MAIN_DB).update_payload(row["id"], {"parse_model_metrics": metrics})
            except Exception:
                app.logger.exception("failed to persist parse employee model metrics job_id=%s", job["job_id"])


def _review_parsed_document(job, adapter, staging, staging_id, timeout, check_cancel):
    from app.document_quality.service import review_parsed_document
    return review_parsed_document(job=job, adapter=adapter, staging=staging, staging_id=staging_id,
                                  config=RUNTIME_CONFIG, db_path=MAIN_DB, objects_dir=OBJECTS_DIR,
                                  timeout=timeout, cancel=check_cancel)


def _parse_job_pipeline(job: dict, progress, cancel_event) -> None:
    """Run the existing deterministic upload pipeline off the request thread."""
    adapter = _PARSE_ADAPTER
    from app.employee_policy import parse_total_timeout
    from app.employees.budget import CURRENT_PARSE_BUDGET
    active_budget = CURRENT_PARSE_BUDGET.get()
    deadline = active_budget.deadline if active_budget is not None else time.monotonic() + parse_total_timeout()

    def budget(maximum: int) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("parse job total timeout")
        return max(1, min(maximum, int(remaining)))

    def check_cancel() -> None:
        if cancel_event.is_set():
            # A cancel request can arrive in the tiny gap before the adapter
            # registers its Popen process.  Clear that one-shot intent when
            # this check wins the race and no child will be spawned.
            clear_cancel = getattr(adapter, "clear_parse_cancel", None)
            if clear_cancel is not None:
                clear_cancel(job["file_hash"])
            raise ParseJobCanceled("job canceled")

    if settings.SAM_FACTS_TREE:
        from app.document_read.upload import phase_limits, run_mapped_upload
        phase_limits()  # Explicit host configuration, never enlarge silently.
        # A committed subset of facts is NOT a completed document. Resume only
        # the QC-bound staging associated with this job, never any latest hash.
        with connect(MAIN_DB) as conn:
            checkpoint = conn.execute('SELECT content FROM memory_kv WHERE uri=?',
                ('sam://kv/rt/md-upload-manifest/'+job['job_id'],)).fetchone()
        if checkpoint:
            check_cancel()
            saved = json.loads(checkpoint[0])
            reviewed = StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / 'map').get(saved['staging_id'])['payload']
            from app.document_read.dossier import Dossier
            current = Dossier.from_quality(reviewed['document'],file_hash=job['file_hash'],objects_root=OBJECTS_DIR,
                company_id=job['company_id'],job_id=job['job_id'],quality=reviewed.get('document_quality'))
            if reviewed.get('file_hash') != job['file_hash'] or current.fingerprint != Dossier(saved['manifest']['document'],file_hash=job['file_hash'],objects_root=OBJECTS_DIR).fingerprint:
                raise ValueError('reviewed mapping changed; old upload cursor cannot resume')
            memory = memory_provider(RUNTIME_CONFIG)
            try:
                return run_mapped_upload(job=job,manifest=saved['manifest'],config=RUNTIME_CONFIG,
                    memory=memory,cancel=check_cancel,progress=progress)
            finally: memory.close()
    if not settings.SAM_FACTS_TREE and job.get("l2_committed"):
        check_cancel()
        _classify_committed_document(job, deadline=deadline)
        _rebuild_fact_projections(job, deadline=deadline)
        try:
            queue_import_guide(job["file_hash"], company_id=job["company_id"])
        except Exception:
            app.logger.exception("import guide queue failed after projection retry")
        return
    progress(stage="starting_engine", message="正在准备本地解析引擎")
    check_cancel()
    staging = StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / "map")
    fact_service: L2CompleteFactService | None = None
    prepared_facts = None
    try:
        if not _has_declared_self(job["company_id"]):
            raise ValueError("请先登记本公司名称")
        progress(stage="parsing", message="正在解析文档")
        ensure_ready = getattr(adapter, "ensure_runtime_ready", None)
        if ensure_ready is not None:
            # This probe is deliberately before parser work. A cold Gateway
            # remains a retryable runtime condition, not a document failure.
            ensure_ready(timeout=budget(90))
        # Cancellation can arrive while the worker is transitioning from
        # engine setup to the private bridge.  Re-check immediately before
        # spawning so a stopped queued job never starts a parser.
        check_cancel()
        staging_id = adapter.run_parse(job["file_hash"], job["harness_format"], timeout=budget(600))
        check_cancel()
        staged = staging.get(staging_id)
        payload = staged.get("payload", {})
        parse_status, _memory_status = parse_result_status(payload)
        summary = payload.get("parse_summary") if isinstance(payload, dict) else {}
        if parse_status != "parsed":
            error = RuntimeError(
                str((summary or {}).get("user_message") or "文档解析失败")
            )
            error.error_kind = str((summary or {}).get("error_kind") or "engine")
            raise error
        pages = payload.get("pages") or []
        total = len(pages) or None
        progress(stage="parsing", current=total or 0, total=total, message=f"解析完成，已读取 {total or 0} 页")
        check_cancel()

        progress(stage="reviewing", message="文档处理员正在核验转换质量")
        staging_id = _review_parsed_document(job, adapter, staging, staging_id, budget(240), check_cancel)
        payload = staging.get(staging_id)['payload']
        check_cancel()
        if settings.SAM_FACTS_TREE:
            from app.document_read.upload import run_mapped_upload
            # Persist the exact reviewed mapping, not a file-hash-wide latest row.
            from app.document_read.dossier import Dossier
            Dossier.from_quality(payload['document'],file_hash=job['file_hash'],objects_root=OBJECTS_DIR,
                company_id=job['company_id'],job_id=job['job_id'],quality=payload.get('document_quality'))
            with connect(MAIN_DB) as conn:
                conn.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?) ON CONFLICT(uri) DO NOTHING',
                    ('sam://kv/rt/md-upload-manifest/'+job['job_id'],json.dumps(dict(staging_id=staging_id,manifest=payload),ensure_ascii=False),'{}'))
            memory = memory_provider(RUNTIME_CONFIG)
            try:
                return run_mapped_upload(job=job,manifest=payload,config=RUNTIME_CONFIG,memory=memory,
                    cancel=check_cancel,progress=progress)
            finally: memory.close()
        employee = semantic_employee(deadline=deadline)

        # Normal uploads automatically traverse the entity bridge and the
        # finance employee.  Model work happens before the short write-only
        # critical section; only verified candidates are retained in memory.
        bridge_run = EntityBridgeService(MAIN_DB, employee=employee).run(
            company_id=job["company_id"], file_hash=job["file_hash"],
            thread_id=f"parse-job:{job['job_id']}",
        )
        fact_service = L2CompleteFactService(MAIN_DB)
        budget(600)
        prepared_facts = fact_service.prepare(
            company_id=job["company_id"], file_hash=job["file_hash"],
            bridge_run_id=int(bridge_run["id"]), use_worker=True,
            worker=EmployeeRunner(config=RUNTIME_CONFIG, deadline=deadline),
            thread_id=f"parse-job:{job['job_id']}",
        )
        check_cancel()

        # Once this short local SQLite write starts, cancellation is rejected
        # by the API.  No engine/model call is made in the critical section.
        progress(stage="consolidating", message="正在写入事实", critical=True)
        fact_result = fact_service.commit(prepared_facts, parse_job_id=job["job_id"])
        prepared_facts = None
        progress(stage="consolidating", current=total or 0, total=total,
                 message=(
                     f"事实写入完成；{len(fact_result['unresolved'])} 个事实待经理处理"
                     if fact_result.get("unresolved") else "事实写入完成"
                 ), critical_end=True)
        _classify_committed_document(job, deadline=deadline)
        _rebuild_fact_projections(job, deadline=deadline)
        try:
            queue_import_guide(job["file_hash"], company_id=job["company_id"])
        except Exception:
            # Guide generation is a follow-up; a model/runtime outage must not
            # turn a successfully parsed and consolidated upload into failed.
            app.logger.exception("import guide queue failed after parse job completion")
    except Exception as exc:
        app.logger.exception(
            "parse job worker failed job_id=%s file_hash=%s", job["job_id"], job["file_hash"]
        )
        if fact_service is not None and prepared_facts is not None:
            try:
                fact_service.abort(
                    prepared_facts, status="canceled" if cancel_event.is_set() else "failed",
                )
            except Exception:
                app.logger.exception("failed to close interrupted fact extraction run")
        if cancel_event.is_set():
            raise ParseJobCanceled("job canceled") from exc
        raise


_PARSE_ADAPTER = None if RUNTIME_CONFIG.cloud_readonly else runtime_provider(RUNTIME_CONFIG, StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / "map"))
_PARSE_MANAGER = None if RUNTIME_CONFIG.cloud_readonly else ParseJobManager(
    MAIN_DB,
    _parse_job_worker,
    cancel_parser=_PARSE_ADAPTER.cancel_parse,
    logger=app.logger,
)


def _company_id() -> str:
    return str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))


def _parse_failure_message(payload: object, status: str) -> str | None:
    """Return a safe, actionable parse failure message for upload clients."""
    if isinstance(payload, dict):
        summary = payload.get("parse_summary")
        if isinstance(summary, dict):
            message = summary.get("user_message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    if status in {"engine_unavailable", "pending_runtime", "unavailable"}:
        return "本地解析服务暂不可用，请稍后重试或联系管理员。"
    if status != "parsed":
        return "文档解析失败，请检查文件后重试。"
    return None


def _page_context(*, company_id: str) -> dict:
    if RUNTIME_CONFIG.cloud_readonly:
        from app.cloud_home import page_context
        return page_context(app)
    facts = consolidation_service().list_facts(company_id=company_id)
    todos = _scoped_todos(company_id=company_id, status="open")
    files = classification_service().list_current(company_id=company_id)
    module_counts: dict[str, int] = {}
    for item in files:
        module = item.get("module")
        if module:
            module_counts[str(module)] = module_counts.get(str(module), 0) + 1
    with connect(MAIN_DB) as conn:
        labels = {row["code"]: row["name"] for row in conn.execute("SELECT code,name FROM modules")}
    return {"company_id": company_id, "facts": facts, "todos": todos, "files": files,
            "module_counts": module_counts, "module_labels": labels,
            "visible_modules": module_definitions(include_hidden=False)}


@app.route("/")
def home_page():
    """The permanent home is the application-owned high-fidelity shell."""
    return render_home_shell(RUNTIME_CONFIG.project_root, readonly=RUNTIME_CONFIG.cloud_readonly)


@app.route("/overview")
def legacy_overview_page():
    """Preserve the previous real-data summary page under a stable legacy URL."""
    context = _page_context(company_id=_company_id())
    context["conflict_count"] = sum(t["reason"] == "conflict" for t in context["todos"])
    context["critical_count"] = sum(t["reason"] == "critical_review" for t in context["todos"])
    context["unclassified_count"] = sum(item.get("module") is None for item in context["files"])
    return render_template("home.html", **context)


@app.route("/prototype")
def prototype_page():
    """Keep the historic URL as an alias of the same owned shell."""
    return home_page()


@app.route("/bizov")
def business_overview_page():
    company_id = _company_id()
    context = _page_context(company_id=company_id)
    context["overview"] = business_overview_service().overview(company_id=company_id)
    context["dashboard"] = assemble_dashboard(
        overview=context["overview"], facts=context["facts"],
        hidden=dashboard_preferences().get(company_id=company_id),
    )
    return render_template("bizov.html", **context)


@app.route("/api/business-overview", methods=["GET"])
@app.route("/api/bizov", methods=["GET"])
def api_business_overview():
    company_id = _company_id()
    overview = business_overview_service().overview(company_id=company_id)
    return {"overview": overview, "dashboard": assemble_dashboard(
        overview=overview,
        facts=consolidation_service().list_facts(company_id=company_id),
        hidden=dashboard_preferences().get(company_id=company_id),
    )}


@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    """Stable dashboard-only API for native clients and future modules."""
    company_id = _company_id()
    overview = business_overview_service().overview(company_id=company_id)
    return {"dashboard": assemble_dashboard(
        overview=overview,
        facts=consolidation_service().list_facts(company_id=company_id),
        hidden=dashboard_preferences().get(company_id=company_id),
    )}


def _dashboard_company_id() -> str:
    payload = request.get_json(silent=True) or {}
    return str(payload.get("company_id") or request.args.get("company_id")
               or os.environ.get("SAM_COMPANY_ID", "default"))


@app.route("/api/dashboard/preferences", methods=["GET", "POST"])
def api_dashboard_preferences():
    """Read or persist company-scoped card visibility preferences."""
    if request.method == "GET":
        company_id = _company_id()
        return {"company_id": company_id, "dashboard_id": "business",
                "hidden": dashboard_preferences().get(company_id=company_id)}
    payload = request.get_json(silent=True) or {}
    card_id = payload.get("card_id")
    hidden = payload.get("hidden")
    if not isinstance(card_id, str) or not isinstance(hidden, bool):
        return {"error": "card_id and boolean hidden are required"}, 400
    return _toggle_dashboard_card(card_id, company_id=_dashboard_company_id(), hidden=hidden)


def _toggle_dashboard_card(card_id: str, *, company_id: str, hidden: bool):
    known = {card.key: card for card in REGISTRY.cards("business")}
    if card_id not in known:
        return {"error": "unknown dashboard card"}, 404
    if known[card_id].always_visible:
        return {"error": "company overview cannot be hidden"}, 400
    preference = dashboard_preferences().set(
        company_id=company_id, dashboard_id="business", card_id=card_id, hidden=hidden,
    )
    return {"preference": preference, "hidden": dashboard_preferences().get(company_id=company_id)}


@app.route("/api/dashboard/cards/<card_id>/toggle", methods=["POST"])
def api_toggle_dashboard_card(card_id: str):
    payload = request.get_json(silent=True) or {}
    hidden = payload.get("hidden")
    if not isinstance(hidden, bool):
        return {"error": "boolean hidden is required"}, 400
    return _toggle_dashboard_card(card_id, company_id=_dashboard_company_id(), hidden=hidden)


@app.route("/todos")
def todos_page():
    selected_status = str(request.args.get("status") or "open").lower()
    if selected_status not in {"open", "all", "resolved", "dismissed"}:
        selected_status = "open"
    context = _page_context(company_id=_company_id())
    context["todos"] = _scoped_todos(
        company_id=context["company_id"],
        status=None if selected_status == "all" else selected_status,
    )
    context["todo_status"] = selected_status
    context["md_conflicts"] = [t for t in context["todos"] if t.get("kind") == "md_content_conflict"]
    context["open_conflicts"] = [t for t in context["todos"] if t["reason"] == "conflict"]
    context["open_critical"] = [t for t in context["todos"] if t["reason"] == "critical_review"]
    return render_template("todos.html", **context)


@app.route("/facts")
def facts_page():
    # F-004 P03: facts.html is the tree page; nodes load from /api/facts/tree.
    # Todos only feed the base template's badge; flat facts are never read here.
    company_id = _company_id()
    return render_template("facts.html", company_id=company_id,
                           todos=_scoped_todos(company_id=company_id, status="open"))


def _knowledge_company_id() -> str:
    """Knowledge reads use host scope only, never a caller's query/body."""
    company_id = os.environ.get("SAM_COMPANY_ID", "").strip()
    if not company_id:
        abort(503, description="宿主尚未注入 SAM_COMPANY_ID，知识库不可读取。")
    return company_id


def knowledge_service() -> KnowledgeService:
    if RUNTIME_CONFIG.cloud_readonly:
        from app.cloud_home import current_snapshot
        from app.cloud_snapshot import SnapshotKnowledgeService
        return SnapshotKnowledgeService(current_snapshot(app))
    return KnowledgeService(MAIN_DB, app.extensions["sam_memory_provider"], objects_path=OBJECTS_DIR)


@app.route("/knowledge", methods=["GET"])
def knowledge_page():
    return home_page()


@app.route("/api/knowledge", methods=["GET"])
def api_knowledge():
    # Both local and cloud-readonly services go through the same redaction
    # before the payload leaves the host: mapping internals stay private.
    result = knowledge_service().read(company_id=_knowledge_company_id())
    return mapping_redacted_projection(result)


@app.route("/knowledge/sources/<file_hash>", methods=["GET"])
def knowledge_source_page(file_hash: str):
    return home_page()


@app.route("/api/knowledge/sources/<file_hash>/original", methods=["GET"])
def knowledge_original(file_hash: str):
    """Download an existing, company-scoped original; never parse or write."""
    company_id = _knowledge_company_id()
    if RUNTIME_CONFIG.cloud_readonly:
        abort(404)  # snapshots contain metadata/mappings, not binary originals
    service = knowledge_service()
    stored = next((row for row in service._catalog(company_id)
                   if row["file_hash"] == file_hash), None)
    if stored is None:
        abort(404)
    store = ArchiveFileStore(OBJECTS_DIR, MAIN_DB)
    path = store.path_for(file_hash)
    if not path.is_file():
        abort(404)
    from app.facts_tree.tree_read import blob_sha256
    if blob_sha256(path) != file_hash:
        # A corrupted or swapped blob is never served as the original.
        return {"error": "integrity_failed", "message": "原件内容校验失败，未发送文件"}, 409
    response = send_file(path, as_attachment=True, download_name=stored["original_name"],
                         mimetype="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/files")
def files_page():
    return render_template("files.html", **_page_context(company_id=_company_id()))


@app.route("/api/entity-roster", methods=["GET", "POST"])
def api_entity_roster():
    """Read or explicitly register the authenticated company's own name."""
    company_id = _company_id()
    service = EntityRosterService(MAIN_DB)
    if request.method == "GET":
        return {"company_id": company_id, "entities": service.list(company_id=company_id)}
    payload = request.get_json(silent=True) or {}
    entity_name = payload.get("entity_name")
    aliases = payload.get("aliases", [])
    if not isinstance(entity_name, str) or not entity_name.strip():
        return {"error": "entity_name is required", "message": "请填写本公司全称"}, 400
    if isinstance(aliases, str):
        aliases = [item.strip() for item in aliases.replace("，", ",").split(",") if item.strip()]
    if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
        return {"error": "aliases must be a list of strings"}, 400
    row = service.declare(company_id=company_id, entity_name=entity_name, aliases=aliases)
    return {"company_id": company_id, "entity": row}, 201


@app.route("/inbox")
def inbox_page():
    """Upload handoff queue; parsing itself remains in the worker pool."""
    company_id = _company_id()
    return render_template(
        "inbox.html", company_id=company_id, facts_tree_enabled=settings.SAM_FACTS_TREE,
        jobs=_PARSE_MANAGER.list(company_id=company_id),
    )


@app.route("/chat")
def chat_page():
    return render_template("chat.html", **_page_context(company_id=_company_id()))


def _agent_visible_text(data: object) -> str:
    """Extract visible text from both legacy and 9.6 agent JSON envelopes."""
    if not isinstance(data, dict):
        return ""
    envelopes: list[dict] = [data]
    nested = data.get("result")
    if isinstance(nested, dict):
        envelopes.insert(0, nested)
    for envelope in envelopes:
        meta = envelope.get("meta")
        if isinstance(meta, dict):
            text = meta.get("finalAssistantVisibleText")
            if isinstance(text, str) and text.strip():
                return text
        for key in ("final", "message", "text"):
            text = envelope.get(key)
            if isinstance(text, str) and text.strip():
                return text
        payloads = envelope.get("payloads")
        if isinstance(payloads, list):
            parts = [
                item.get("text", "")
                for item in payloads
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            answer = "".join(parts).strip()
            if answer:
                return answer
    return ""


@app.route("/api/chat", methods=["POST", "OPTIONS"])
def api_chat():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return {"error": "empty message"}, 400

    session_id = str(payload.get("session_id") or ("session-" + uuid.uuid4().hex))
    company_id = os.environ.get("SAM_COMPANY_ID", "default")
    if payload.get("company_id") not in (None, company_id):
        return {"error": "company differs from injected host scope"}, 400
    memory = app.extensions["sam_memory_provider"]
    if hasattr(memory, "scoped"):
        memory = memory.scoped(company_id)
    source_store = ArchiveFileStore(objects_path=OBJECTS_DIR, db_path=MAIN_DB)
    thread_id = str(payload.get("thread_id") or session_id)
    catalog = source_catalog_from_db(MAIN_DB)
    map_result = MapBuilder(memory, source_catalog=catalog).load_or_rebuild(company_id)
    map_data = map_result.map
    # The existing map stays navigation-only. ContextAssembler additionally
    # reads the employee-produced 2B L0 guide; no L2/L1 body is copied here.
    map_json = json.dumps(map_data, ensure_ascii=False, sort_keys=True)
    conversations = ConversationStore(memory)
    try:
        conversations.append(company_id, thread_id, role="user", text=question)
        conversations.fold_l1(company_id, thread_id)
        conversations.fold_l0(company_id, thread_id, summary=deterministic_summary(conversations.read_range(company_id, thread_id)))
    except Exception:
        map_data["degraded"] = True
    try:
        agent_config = ""
        try:
            sync_agent_config(memory, RUNTIME_CONFIG.harness_root)
            agent_config = "\n\n".join((RUNTIME_CONFIG.harness_root / name).read_text(encoding="utf-8") for name in CONFIG_FILES)
        except Exception:
            map_data["degraded"] = True
        assembled = ContextAssembler(memory, company_id=company_id, thread_id=thread_id, agent_config=agent_config, source_catalog=catalog).assemble(question)
        context_text = assembled["prompt"] + "\n\n经理只读工具：memory_map()、memory_read(uri)、memory_search(query)、file_get(file_hash)、conversation_read(start,end)、thread_list()、thread_open()。经理可读取全局资料，但禁止 sam_document_ingest 与 promote，不写入或提升任何事实；工具参数仍只传各自契约字段。"
        context_stats = assembled["stats"]
    except Exception:
        context_text = "公司记忆地图（仅导航，不含事实正文）：\n" + map_json[:12000] + "\n\n当前上下文 degraded。"
        context_stats = {"tokens": max(1, len(context_text) // 4), "branch_count": len(map_data.get("branches", [])), "folded_blocks": 0, "tool_calls": 0}
    tools = MemoryMapTools(memory, source_store, company_id, source_catalog=catalog)
    conversation_tools = ConversationTools(memory, company_id, thread_id)
    runtime = RuntimeWorkingMemory(memory)
    try:
        try:
            state = runtime.get_runtime(session_id, "conversation")
            if state.get("state") == "expired":
                runtime.cleanup(session_id, "conversation")
                raise KeyError(session_id)
        except (KeyError, FileNotFoundError):
            runtime.create(session_id, "conversation", goal=f"公司 {company_id} 对话", company_id=company_id)
    except Exception:
        # Runtime memory is useful but must not turn a chat into a 500.
        map_data["degraded"] = True
    adapter = runtime_provider(RUNTIME_CONFIG, StagingStore(db_path=MAIN_DB, maps_path=OBJECTS_DIR.parent / "map"))
    try:
        raw = adapter.run_agent_message(
            question, context_text=context_text, company_id=company_id,
            thread_id=thread_id, allow_promote=False, timeout=600,
            access_role="manager",
        )
        data = json.loads(raw)
    except Exception:
        # Keep the client-facing contract stable while preserving the actual
        # gateway/model failure (including malformed envelopes) in Flask logs.
        app.logger.exception("api_chat agent execution failed")
        return {"error": "agent failed"}, 502
    answer = _agent_visible_text(data)
    try:
        refs = [uri for uri in tools.navigation if uri.startswith("sam://")]
        runtime.append_event(session_id, "conversation", "action", {
            "kind": "memory_map_turn", "question": question[:1000],
            "map_uri": MapBuilder(memory).map_uri(company_id),
            "navigation": refs[:50], "answer_summary": answer[:1000],
        })
        conversations.append(company_id, thread_id, role="assistant", text=answer, tool_results=[{"navigation": refs[:50]}])
    except Exception:
        map_data["degraded"] = True
    return {
        "message": answer,
        "model": os.environ.get(RUNTIME_CONFIG.model_name_env, RUNTIME_CONFIG.model_default),
        "session_id": session_id,
        "thread_id": thread_id,
        "context": {"map_uri": MapBuilder(memory).map_uri(company_id), "degraded": bool(map_data.get("degraded")), "navigation_count": len(tools.navigation), **context_stats},
    }


@app.route("/api/upload", methods=["POST", "OPTIONS"])
def api_upload():
    """Store an immutable original and enqueue parsing without blocking HTTP."""
    if request.method == "OPTIONS":
        return ("", 204)
    company_id = str(request.form.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    if not _has_declared_self(company_id):
        return {"error": "company_not_registered", "message": "请先登记本公司名称"}, 409
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return {"error": "no file"}, 400
    file_format = detect_format(upload.filename)
    if file_format == "unknown":
        return {
            "error": "unsupported format",
            "message": "暂不支持该文件格式；请上传 PDF、Excel、PPT 或 Word 文件。",
        }, 400
    blob = upload.read()
    store = ArchiveFileStore(objects_path=OBJECTS_DIR, db_path=MAIN_DB)
    stored = store.put_bytes(
        blob,
        original_name=upload.filename,
        mime_type="application/pdf" if file_format == "pdf" else None,
        origin_zone="internal",
    )
    if file_format == "excel":
        harness_format = "xlsx"
    elif file_format == "ppt":
        harness_format = "pptx" if upload.filename.lower().endswith(".pptx") else "ppt"
    elif file_format == "doc":
        harness_format = "docx" if upload.filename.lower().endswith(".docx") else "doc"
    else:
        harness_format = file_format
    job = _PARSE_MANAGER.create(
        company_id=company_id, file_hash=stored.file_hash,
        original_name=upload.filename, file_format=file_format,
        harness_format=harness_format, size_bytes=len(blob),
    )
    return {
        "job_id": job["job_id"], "file_hash": stored.file_hash,
        "original_name": upload.filename, "format": file_format,
        "size": len(blob), "status": "queued", "stage": "queued",
    }, 202


@app.route("/api/parse-jobs", methods=["GET"])
def api_parse_jobs():
    company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    status = request.args.get("status")
    if status is not None and status not in {"all", "queued", "parsing", "done", "failed", "canceled"}:
        return {"error": "invalid parse job status"}, 400
    return {"jobs": _PARSE_MANAGER.list(company_id=company_id, status=status)}


@app.route("/api/parse-jobs/<job_id>", methods=["GET"])
def api_parse_job(job_id: str):
    try:
        job = _PARSE_MANAGER.get(job_id)
    except KeyError:
        return {"error": "parse job not found"}, 404
    company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    if job["company_id"] != company_id:
        return {"error": "parse job not found"}, 404
    return {"job": job}


@app.route("/api/parse-jobs/<job_id>/cancel", methods=["POST"])
def api_cancel_parse_job(job_id: str):
    try:
        job = _PARSE_MANAGER.get(job_id)
        payload = request.get_json(silent=True) or {}
        company_id = str(request.args.get("company_id") or request.form.get("company_id") or payload.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
        if job["company_id"] != company_id:
            return {"error": "parse job not found"}, 404
        result = _PARSE_MANAGER.cancel(job_id)
    except KeyError:
        return {"error": "parse job not found"}, 404
    except RuntimeError as exc:
        return {"error": str(exc)}, 409
    return {"job": result}


@app.route("/api/parse-jobs/<job_id>/retry", methods=["POST"])
def api_retry_parse_job(job_id: str):
    try:
        job = _PARSE_MANAGER.get(job_id)
        payload = request.get_json(silent=True) or {}
        company_id = str(request.args.get("company_id") or request.form.get("company_id") or payload.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
        if job["company_id"] != company_id:
            return {"error": "parse job not found"}, 404
        if settings.SAM_FACTS_TREE:
            result = _PARSE_MANAGER.retry(job_id,resume_unresolved=payload.get('resume_unresolved',True),
                renew_deadline_seconds=payload.get('renew_deadline_seconds'))
        else:
            result = _PARSE_MANAGER.retry(job_id)
    except KeyError:
        return {"error": "parse job not found"}, 404
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}, 409
    return {"job": result}, 202


@app.route("/api/facts", methods=["GET"])
def api_facts():
    """Read current verified document facts; superseded values are excluded."""
    company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    file_hash = request.args.get("file_hash")
    return {"facts": consolidation_service().list_facts(company_id=company_id, file_hash=file_hash)}


def _facts_tree_params():
    """API-001 parameter contract; the company comes only from the host scope."""
    body = request.get_json(silent=True)
    if ("company_id" in request.args or "company_id" in request.form
            or (isinstance(body, dict) and "company_id" in body)):
        return None, None, ({"error": "company_id_rejected",
                             "message": "公司范围只能由宿主注入，不接受 company_id 参数"}, 400)
    node = request.args.get("node", "tree")
    code = request.args.get("code")
    if (set(request.args) - {"node", "code"} or node not in {"tree", "trunk", "leaf"}
            or (node != "tree" and not code) or (node == "tree" and code is not None)):
        return None, None, ({"error": "invalid_parameter",
                             "message": "node 仅支持 tree/trunk/leaf；trunk/leaf 必须提供 code"}, 400)
    return node, code, None


@app.route("/api/facts/tree", methods=["GET"])
def api_facts_tree():
    """F-004-API-001: host-scoped read-only FactsTree tree/trunk/leaf reads."""
    node, code, error = _facts_tree_params()
    if error is not None:
        return error
    company_id = os.environ.get("SAM_COMPANY_ID", "").strip()
    if not company_id:
        return {"error": "company_scope_missing", "message": "宿主未注入公司范围"}, 503
    from app.facts_tree.tree_read import TreeReadError, TreeReadService
    try:
        with TreeReadService(MAIN_DB, app.extensions["sam_memory_provider"],
                             company_id=company_id) as service:
            if node == "trunk":
                return service.read_trunk(company_id, code)
            if node == "leaf":
                return service.read_leaf(company_id, code)
            return service.read_tree(company_id)
    except TreeReadError as exc:
        return {"error": exc.category, "message": str(exc)}, exc.status


def _facts_tree_source_params():
    """API-002 parameter contract; the company comes only from the host scope."""
    body = request.get_json(silent=True)
    if ("company_id" in request.args or "company_id" in request.form
            or (isinstance(body, dict) and "company_id" in body)):
        return None, None, ({"error": "company_id_rejected",
                             "message": "公司范围只能由宿主注入，不接受 company_id 参数"}, 400)
    leaf_code = request.args.get("leaf_code")
    ref = request.args.get("ref")
    if set(request.args) - {"leaf_code", "ref"} or not leaf_code or not ref:
        return None, None, ({"error": "invalid_parameter",
                             "message": "必须提供 leaf_code 与 ref，且不接受其他参数"}, 400)
    return leaf_code, ref, None


@app.route("/api/facts/tree/source", methods=["GET"])
def api_facts_tree_source():
    """F-004-API-002: locate one source cited by a verified current leaf."""
    leaf_code, ref, error = _facts_tree_source_params()
    if error is not None:
        return error
    company_id = os.environ.get("SAM_COMPANY_ID", "").strip()
    if not company_id:
        return {"error": "company_scope_missing", "message": "宿主未注入公司范围"}, 503
    from app.facts_tree.tree_read import TreeReadError, TreeReadService
    try:
        with TreeReadService(MAIN_DB, app.extensions["sam_memory_provider"],
                             company_id=company_id) as service:
            return service.read_leaf_source(company_id, leaf_code, ref,
                                            objects_path=OBJECTS_DIR)
    except TreeReadError as exc:
        return {"error": exc.category, "message": str(exc)}, exc.status


def _internal_manager_authorized() -> bool:
    """Require both a deployment-provided token and a loopback caller."""
    expected = os.environ.get("SAM_INTERNAL_TOKEN", "")
    supplied = request.headers.get("X-SAM-Internal-Token", "")
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        return False
    return request.remote_addr in {"127.0.0.1", "::1"}


@app.route("/api/facts/modules/<module_key>/<layer>", methods=["GET"])
def api_module_projection(module_key, layer):
    # Scope is host-owned; query/body company IDs cannot select another pocket.
    company_id = os.environ.get("SAM_COMPANY_ID", "default")
    requested = request.args.get("company_id")
    if requested is not None and requested != company_id:
        return {"error": "company scope mismatch"}, 400
    format = request.args.get("format", "md")
    try:
        result = ModuleProjectionService(MAIN_DB, app.extensions["sam_memory_provider"]).read(
            company_id=company_id, module_key=module_key, layer=layer, format=format)
    except ValueError:
        return {"error": "invalid module projection address"}, 400
    except (FileNotFoundError, MemoryUnavailable):
        result = None
    if result is None:
        return {"error": "module projection not available"}, 404
    return app.response_class(result, mimetype="application/json" if format == "json" else "text/markdown")


@app.route("/api/internal/facts/projections/rebuild", methods=["POST"])
def api_internal_projections_rebuild():
    if not _internal_manager_authorized():
        return {"error": "internal authorization required"}, 403
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return {"error": "write operation requires confirm"}, 400
    company_id = os.environ.get("SAM_COMPANY_ID", "default")
    if settings.SAM_FACTS_TREE:
        from app.facts_tree.leaf_store import LeafPublishService
        from app.facts_tree.rollup import FactsTreeRollup
        store = LeafPublishService(MAIN_DB,app.extensions['sam_memory_provider'],company_id=company_id)
        try:
            result = FactsTreeRollup(store).consume_notifications(company_id=company_id)
            return dict(company_id=company_id,**result)
        except Exception:
            app.logger.exception('mechanical facts-tree consumption failed')
            return {'error':'资料导览更新失败，待处理通知已保留'},503
        finally: store.close()
    memory = app.extensions["sam_memory_provider"]
    try:
        L2ProjectionService(MAIN_DB, memory).publish(company_id=company_id)
        from app.employee_policy import parse_total_timeout
        result = ModuleProjectionService(MAIN_DB, memory,
            worker=EmployeeRunner(config=RUNTIME_CONFIG, deadline=time.monotonic() + parse_total_timeout())).rebuild(company_id=company_id)
        return {"status": "unresolved" if result["unresolved"] else "ready",
                "published_modules": result["published_modules"],
                "unresolved_modules": [item["module_key"] for item in result["unresolved"]]}, (503 if result["unresolved"] else 200)
    except Exception:
        app.logger.exception("explicit projection rebuild failed")
        return {"error": "projection rebuild failed"}, 503


@app.route("/api/internal/facts/extract", methods=["POST"])
def api_internal_facts_extract():
    """Resident-manager entry point for the worker-backed finance path.

    This is deliberately not exposed as a CLI-to-model bypass: only the
    long-lived Flask process may invoke ``use_worker=True``.  The host owns
    company scope; request JSON cannot override it.
    """
    if not _internal_manager_authorized():
        return {"error": "internal authorization required"}, 403
    payload = request.get_json(silent=True) or {}
    file_hash = payload.get("file_hash")
    if not isinstance(file_hash, str) or not file_hash:
        return {"error": "file_hash is required"}, 400
    bridge_run_id = payload.get("bridge_run_id")
    if bridge_run_id is not None and not isinstance(bridge_run_id, int):
        return {"error": "bridge_run_id must be an integer"}, 400
    thread_id = payload.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        return {"error": "thread_id must be a string"}, 400
    try:
        if bridge_run_id is None:
            bridge_run = EntityBridgeService(MAIN_DB, employee=semantic_employee()).run(
                company_id=_company_id(), file_hash=file_hash,
                thread_id=thread_id,
            )
            bridge_run_id = int(bridge_run["id"])
        result = L2CompleteFactService(MAIN_DB).extract(
            company_id=_company_id(), file_hash=file_hash, bridge_run_id=bridge_run_id,
            use_worker=True, thread_id=thread_id,
        )
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}, 400
    return {"run": result}


@app.route("/api/todos", methods=["GET"])
def api_todos():
    company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    status = request.args.get("status", "open")
    if status not in {"open", "resolved", "dismissed", "all"}:
        return {"error": "invalid todo status"}, 400
    return {"todos": _scoped_todos(
        company_id=company_id, status=None if status == "all" else status,
    )}


def _todo_action(todo_id: int, *, dismiss: bool = False):
    payload = request.get_json(silent=True) or {}
    try:
        if dismiss:
            result = consolidation_service().dismiss(todo_id)
        else:
            choose = payload.get("choose")
            if not isinstance(choose, str) or not choose:
                return {"error": "choose is required"}, 400
            result = consolidation_service().resolve(todo_id, choose=choose)
    except PermissionError:
        # Reserved for an injected future authorization policy.
        return {"error": "authorization failed"}, 403
    except KeyError:
        return {"error": "todo not found"}, 404
    except ValueError as exc:
        return {"error": str(exc)}, 409
    return {"todo": result}


@app.route("/api/todos/<int:todo_id>/resolve", methods=["POST"])
def api_resolve_todo(todo_id: int):
    return _todo_action(todo_id)


@app.route("/api/todos/<int:todo_id>/dismiss", methods=["POST"])
def api_dismiss_todo(todo_id: int):
    return _todo_action(todo_id, dismiss=True)


@app.route("/api/files/classifications", methods=["GET", "POST", "OPTIONS"])
def api_file_classifications():
    """List effective 2A labels or append one human correction."""
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "GET":
        company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
        module = request.args.get("module")
        if module == "":
            module = None
        include_history = str(request.args.get("include_history", "")).lower() in {"1", "true", "yes"}
        return {"files": classification_service().list_current(
            company_id=company_id, module=module, include_history=include_history,
        )}

    payload = request.get_json(silent=True) or {}
    file_hash = payload.get("file_hash")
    company_id = payload.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default")
    module = payload.get("module")
    doc_type = payload.get("doc_type")
    if not isinstance(file_hash, str) or not isinstance(company_id, str):
        return {"error": "file_hash and company_id are required"}, 400
    if module is not None and not isinstance(module, str):
        return {"error": "module must be a string or null"}, 400
    if doc_type is not None and not isinstance(doc_type, str):
        return {"error": "doc_type must be a string or null"}, 400
    try:
        record = classification_service().reclassify(
            file_hash=file_hash, company_id=company_id, module=module, doc_type=doc_type,
        )
    except KeyError:
        return {"error": "file not found"}, 404
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return {"classification": record}


@app.route("/api/files/classifications/confirm", methods=["POST", "OPTIONS"])
def api_confirm_file_classification():
    """Optional review marker; automatic labels are already active without it."""
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    file_hash = payload.get("file_hash")
    company_id = payload.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default")
    if not isinstance(file_hash, str) or not isinstance(company_id, str):
        return {"error": "file_hash and company_id are required"}, 400
    try:
        record = classification_service().confirm(file_hash=file_hash, company_id=company_id)
    except KeyError:
        return {"error": "classification not found"}, 404
    return {"classification": record}


@app.route("/api/import-guide", methods=["POST", "OPTIONS"])
def api_import_guide():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "open")
    if event not in {"open", "upload", "refresh"}:
        return {"error": "invalid import guide event"}, 400
    uploaded_file_hash = payload.get("uploaded_file_hash")
    if uploaded_file_hash is not None and not isinstance(uploaded_file_hash, str):
        return {"error": "invalid uploaded_file_hash"}, 400
    company_id = str(payload.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    job = _GUIDE_COORDINATOR.get_or_start(
        _guide_key(company_id),
        lambda: _run_import_guide(event=event, uploaded_file_hash=uploaded_file_hash, company_id=company_id),
    )
    try:
        guide = _GUIDE_COORDINATOR.wait(
            # A guide may legitimately use most of the 150s worker budget.
            # The HTTP refresh path is only a short poll; clients continue via
            # the read-only status endpoint until ready/failed.
            job, timeout=float(os.environ.get("SAM_GUIDE_REFRESH_WAIT", "5"))
        )
    except ModelUnavailable as exc:
        app.logger.info("import guide unavailable: %s", getattr(exc, "reason", type(exc).__name__))
        return {"error": "import guide unavailable", "guide_status": "failed", "reason": getattr(exc, "reason", "unavailable")}, 503
    except Exception as exc:
        app.logger.info("import guide background failed: %s", type(exc).__name__)
        return {"error": "import guide unavailable", "guide_status": "failed", "reason": "unavailable"}, 503
    if guide is None:
        return {"guide_status": "generating", "retry_after_ms": 1000}, 202
    return {"guide": guide, "guide_status": "ready"}


@app.route("/api/import-guide/status", methods=["GET"])
def api_import_guide_status():
    """Read-only polling endpoint for a company guide's terminal state."""
    company_id = str(request.args.get("company_id") or os.environ.get("SAM_COMPANY_ID", "default"))
    return _GUIDE_COORDINATOR.snapshot(_guide_key(company_id))


@app.route("/files/<file_hash>")
def detail(file_hash: str):
    """Render canonical source-file metadata plus its current 2A/2B references."""
    store = ArchiveFileStore(objects_path=OBJECTS_DIR, db_path=MAIN_DB)
    try:
        stored = store.get(file_hash)
    except KeyError:
        abort(404)

    company_id = _company_id()
    classification = next(
        (item for item in classification_service().list_current(company_id=company_id)
         if item["file_hash"] == file_hash),
        None,
    )
    context = _page_context(company_id=company_id)
    context.update({
        "file": stored,
        "classification": classification,
        "file_facts": consolidation_service().list_facts(
            company_id=company_id, file_hash=file_hash,
        ),
    })
    return render_template("detail.html", **context)


if RUNTIME_CONFIG.cloud_readonly:
    from app.cloud_home import install_cloud_window
    install_cloud_window(app, RUNTIME_CONFIG, _page_context)


if __name__ == "__main__":
    import os
    if not RUNTIME_CONFIG.cloud_readonly:
        init_db()
        # Only the long-lived server owns a newly spawned gateway. Imports
        # (including pytest and readonly snapshots) never start a daemon.
        _PARSE_ADAPTER.ensure_runtime_ready()
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
