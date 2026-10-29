from __future__ import annotations

from dataclasses import replace
import json
import subprocess

import pytest

from app.db import connect, init_db
from app.employee_policy import call_timeout, openclaw_timeout_floor
from app.employees import EmployeeRunner
from app.harness.runtime.openclaw_adapter import OpenClawAdapter
from app.harness.staging import StagingStore
from app.memory.company_facts import L2CompleteFactService
from app.parse_jobs import ParseJobManager
from app.runtime_config import RuntimeConfig
from tests.runtime_install import managed_config, managed_runner
from app.runtime_errors import EmployeeTransportError, transport_exception
from tests.test_finance_worker import _setup, _candidate
from tests.test_parse_jobs import _manager, _create, _wait_for

TIMEOUT = {"status": "timeout", "timeoutPhase": "provider", "result": {
    "payloads": [{"text": "Request timed out", "isError": True}]}}


@pytest.mark.parametrize("skill,expected", [
    ("document-classifier", 240), ("finance-fact-extraction", 240),
    ("fact-brief", 300), ("fact-guide", 300),
])
def test_skill_default_and_override(monkeypatch, skill, expected):
    key = "SAM_EMPLOYEE_" + skill.upper().replace("-", "_") + "_TIMEOUT"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SAM_EMPLOYEE_TIMEOUT", raising=False)
    assert call_timeout(skill) == expected
    monkeypatch.setenv("SAM_EMPLOYEE_TIMEOUT", "480")
    assert call_timeout(skill) == 480
    monkeypatch.setenv(key, "500")
    assert call_timeout(skill) == 500
    assert call_timeout(skill, 12) == 12


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_invalid_call_budget_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SAM_EMPLOYEE_TIMEOUT", value)
    with pytest.raises(ValueError):
        call_timeout("fact-brief")


def test_provider_timeout_retries_identical_request_with_backoff(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("app.employees.worker.time.sleep", sleeps.append)
    def run(**kw):
        calls.append(kw)
        return TIMEOUT if len(calls) < 3 else {"items": [{"classification": "ambiguous", "reason": "employee decision"}]}
    result = EmployeeRunner(runner=run).run(skill="document-classifier", text="block", company_id="acme")
    assert result[0]["classification"] == "ambiguous"
    assert sleeps == [1, 2]
    assert len(calls) == 3
    assert all(call == calls[0] for call in calls)
    assert calls[0]["timeout"] == 240


@pytest.mark.parametrize("raw", [[], {"items": []}, {"items": [{"classification": "ambiguous", "reason": "timeout in source"}]}])
def test_business_decisions_are_never_transport_retried(raw):
    calls = []
    employee = EmployeeRunner(runner=lambda **kw: calls.append(kw) or raw)
    employee.run(skill="document-classifier", text="block", company_id="acme")
    assert len(calls) == 1


def test_total_deadline_caps_attempts_and_waits(monkeypatch):
    now, sleeps, calls = [0.0], [], []
    monkeypatch.setattr("app.employees.worker.time.monotonic", lambda: now[0])
    monkeypatch.setattr("app.employees.worker.time.sleep", lambda seconds: (sleeps.append(seconds), now.__setitem__(0, now[0] + seconds)))
    def run(**kw):
        calls.append(kw)
        now[0] += kw["timeout"]
        return TIMEOUT
    with pytest.raises(EmployeeTransportError, match="deadline exhausted"):
        EmployeeRunner(runner=run, total_timeout=10, deadline=7).run(
            skill="fact-brief", text="block", company_id="acme")
    assert [call["timeout"] for call in calls] == [7]
    assert sleeps == []


def test_nonretryable_transport_error_not_retried():
    calls = []
    def run(**kw):
        calls.append(kw)
        raise EmployeeTransportError("HTTP 401 unauthorized", retryable=False)
    with pytest.raises(EmployeeTransportError, match="attempts=1"):
        EmployeeRunner(runner=run).run(skill="fact-guide", text="block", company_id="acme")
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [TimeoutError(), ConnectionResetError(104, "reset"), subprocess.TimeoutExpired(["secret prompt"], 3)])
def test_network_errors_retryable_without_persisting_prompt(exc):
    failure = transport_exception(exc)
    assert failure.retryable
    assert "secret prompt" not in str(failure)


def test_openclaw_default_floor_and_cli_use_python_budget(tmp_path, monkeypatch):
    config = RuntimeConfig.from_env()
    # Every runtime-writable root (harness cache/inbox/outbox, state, config)
    # moves into tmp_path; the config template is copied along so the runtime
    # keeps the production defaults while the repo stays read-only.
    harness_root = tmp_path / "harness"
    template = config.harness_root / "state" / "openclaw.example.json"
    target = harness_root / "state" / "openclaw.example.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    config = replace(config, harness_root=harness_root, state_dir=tmp_path / "state",
                     config_path=tmp_path / "state/openclaw.json", openclaw_mode="local")
    monkeypatch.setenv("SAM_EMPLOYEE_FACT_BRIEF_TIMEOUT", "720")
    calls = []
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(TIMEOUT), "")
    config = managed_config(config, tmp_path)
    adapter = OpenClawAdapter(StagingStore(tmp_path / "db"), config=config, runner=managed_runner(run))
    adapter.prepare_runtime()
    assert json.loads(config.config_path.read_text())["agents"]["defaults"]["timeoutSeconds"] >= 720
    with pytest.raises(EmployeeTransportError, match="timeoutPhase=provider"):
        adapter.run_agent_message("block", timeout=300)
    cmd, kw = calls[0]
    assert 298 <= int(cmd[cmd.index("--timeout") + 1]) <= 300
    assert int(cmd[cmd.index("--timeout") + 1]) == kw["timeout"]
    assert openclaw_timeout_floor() >= 720


def test_timeout_exhaustion_has_terminal_job_and_no_partial_facts(tmp_path, monkeypatch):
    db, stored, bridge = _setup(tmp_path)
    # Duplicate source block: the first yields a verified candidate, the
    # second times out. No successful prefix may reach facts/todos.
    with connect(db) as conn:
        conn.execute("""INSERT INTO entity_bridge_blocks
          (run_id,company_id,file_hash,block_index,content,source_page,source_span,
           classification,subject,artifact_id,block_type,created_at)
          SELECT run_id,company_id,file_hash,block_index+1,content,source_page,source_span,
                 classification,subject,artifact_id,block_type,created_at FROM entity_bridge_blocks""")
        conn.commit()
    calls = []
    def run(**kw):
        calls.append(kw)
        return {"items": [_candidate()]} if len(calls) == 1 else TIMEOUT
    employee = EmployeeRunner(runner=run, retry_backoff=0)
    service = L2CompleteFactService(db)
    def worker(job, progress, event):
        progress(stage="parsing")
        service.extract(company_id="acme", file_hash=stored.file_hash,
                        bridge_run_id=bridge["id"], worker=employee)
    manager = ParseJobManager(db, worker, workers=1)
    try:
        job = manager.create(company_id="acme", file_hash=stored.file_hash,
                             original_name=stored.original_name, file_format="xlsx",
                             harness_format="xlsx", size_bytes=1)
        manager.executor.shutdown(wait=True)
        failed = manager.get(job["job_id"])
        assert failed["status"] == failed["stage"] == "failed"
        assert failed["error_kind"] == "employee_transport"
        assert failed["finished_at"] and len(failed["message"]) <= 1200
        assert "finance-fact-extraction" in failed["message"] and "attempts=3" in failed["message"]
        assert len(calls) == 4
        assert service.list_facts(company_id="acme") == service.list_todos(company_id="acme") == []
        with connect(db) as conn:
            assert conn.execute("SELECT status FROM fact_runs").fetchone()[0] == "failed"
    finally:
        manager.close()


def test_restart_recovers_orphaned_parsing_job(tmp_path):
    manager, digest = _manager(tmp_path, lambda *_: None)
    job = _create(manager, digest)
    manager.executor.shutdown(wait=True)
    manager._update(job["job_id"], status="parsing", stage="parsing", finished_at=None)
    manager.close()
    restarted = ParseJobManager(manager.db_path, lambda *_: None, workers=1)
    try:
        failed = restarted.get(job["job_id"])
        assert failed["status"] == "failed" and failed["error_kind"] == "interrupted"
        retried = restarted.retry(job["job_id"])
        assert _wait_for(restarted, retried["job_id"], lambda j: j["status"] == "done")["status"] == "done"
    finally:
        restarted.close()


@pytest.mark.parametrize("exc", [SystemExit("worker exited"), RuntimeError("worker failed")])
def test_uncaught_worker_exception_published_and_not_swallowed(tmp_path, exc):
    manager, digest = _manager(tmp_path, lambda *_: (_ for _ in ()).throw(exc))
    try:
        job = _create(manager, digest)
        manager.executor.shutdown(wait=True)
        assert manager.get(job["job_id"])["status"] == "failed"
        # _run still raises: terminal publication is not exception swallowing.
        manager._update(job["job_id"], status="queued")
        with pytest.raises(type(exc)):
            manager._run(job["job_id"])
    finally:
        manager.close()


def test_future_reports_exception_in_runtime_retry_handler(tmp_path, monkeypatch):
    from app.ports import RetryableRuntimeError
    monkeypatch.setenv("SAM_RUNTIME_RETRIES", "invalid")
    def worker(*_):
        raise RetryableRuntimeError("gateway not ready")
    manager, digest = _manager(tmp_path, worker)
    try:
        job = _create(manager, digest)
        manager.executor.shutdown(wait=True)
        failed = manager.get(job["job_id"])
        assert failed["status"] == "failed" and failed["finished_at"]
        assert "invalid" in failed["message"]
    finally:
        manager.close()


def test_late_failure_callback_does_not_overwrite_new_retry(tmp_path):
    from concurrent.futures import Future
    from threading import Event
    manager, digest = _manager(tmp_path, lambda *_: None)
    try:
        job = _create(manager, digest)
        manager.executor.shutdown(wait=True)
        old_generation, new_generation = Event(), Event()
        manager._events[job["job_id"]] = new_generation
        manager._update(job["job_id"], status="parsing", stage="parsing")
        future = Future()
        future.set_exception(RuntimeError("old failure"))
        manager._observe_failure(job["job_id"], future, old_generation)
        assert manager.get(job["job_id"])["status"] == "parsing"
    finally:
        manager.close()


@pytest.mark.parametrize("failed_step", ["projection", "classification"])
def test_web_worker_projection_timeout_fails_and_retry_is_idempotent(tmp_path, monkeypatch, failed_step):
    from app.ports import LocalMemoryProvider
    # This worker contract uses an injected memory provider, including assembly
    # during import; it must not require an installed HF model in isolation.
    monkeypatch.setattr("app.providers.memory_provider", lambda *_a, **_kw: LocalMemoryProvider(tmp_path / "ov"))
    import webapp.app as web
    from tests.entity_employee import FixtureEntityEmployee
    db, stored, _ = _setup(tmp_path)
    with connect(db) as conn:
        staging_id = conn.execute("SELECT id FROM parse_staging").fetchone()[0]
    class Adapter:
        def run_parse(self, *_args, **_kw):
            return staging_id
    class Classification:
        def classify_parsed(self, **_kw):
            if failed_step == "classification" and outage[0]:
                EmployeeRunner(runner=lambda **kw: calls.append("document-classifier") or {
                    "result": {"payloads": [], "stopReason": "length"}}, retry_backoff=0).run(
                        skill="document-classifier", text="source", company_id="acme")
            return {"id": 1, "_created_for_parse": False, "resource_folder": "财务"}
    outage, calls = [True], []
    def run(*, skill, text, **_kw):
        calls.append(skill)
        if skill == "finance-fact-extraction":
            return {"items": [_candidate(module_key="fin")]}
        if skill == "fact-brief":
            if outage[0] and failed_step == "projection":
                return TIMEOUT
            return {"items": [{"topic": "财务", "conclusion": f"营业收入3.26亿元{{f:{json.loads(text)['facts'][0]['fact_id']}}}",
                               "fact_ids": [fact["fact_id"] for fact in json.loads(text)["facts"]]}]}
        entry_id = json.loads(text)["l1"]["items"][0]["entry_id"]
        assert entry_id == "fin-1"
        return {"items": [{"conclusion": f"营业收入3.26亿元{{l:{entry_id}}}", "l1_entry_ids": [entry_id]}]}
    monkeypatch.setattr(web, "MAIN_DB", db)
    monkeypatch.setattr(web, "OBJECTS_DIR", tmp_path / "objects")
    monkeypatch.setattr(web, "_PARSE_ADAPTER", Adapter())
    monkeypatch.setattr(web, "_review_parsed_document", lambda job, adapter, staging, staging_id, timeout, check_cancel: staging_id)
    monkeypatch.setattr(web, "classification_service", lambda **_: Classification())
    monkeypatch.setattr(web, "semantic_employee", lambda **_: FixtureEntityEmployee())
    monkeypatch.setattr(web, "EmployeeRunner", lambda **kw: EmployeeRunner(runner=run, retry_backoff=0, **kw))
    monkeypatch.setitem(web.app.extensions, "sam_memory_provider", LocalMemoryProvider(tmp_path / "ov"))
    monkeypatch.setattr(web, "queue_import_guide", lambda *_a, **_kw: None)
    manager = ParseJobManager(db, web._parse_job_worker, workers=1)
    try:
        job = manager.create(company_id="acme", file_hash=stored.file_hash,
                             original_name=stored.original_name, file_format="xlsx",
                             harness_format="xlsx", size_bytes=1)
        assert _wait_for(manager, job["job_id"], lambda j: j["status"] == "failed")["error_kind"] == "employee_transport"
        assert calls.count("fact-brief" if failed_step == "projection" else "document-classifier") == 3
        before = L2CompleteFactService(db).list_facts(company_id="acme")
        assert len(before) == 1
        # Wait until terminal publication's finally block is complete.
        with manager._lock:
            assert job["job_id"] not in manager._events
        outage[0] = False
        manager.retry(job["job_id"])
        manager.executor.shutdown(wait=True)
        assert manager.get(job["job_id"])["status"] == "done"
        assert L2CompleteFactService(db).list_facts(company_id="acme") == before
        assert L2CompleteFactService(db).list_todos(company_id="acme") == []
    finally:
        manager.close()


@pytest.mark.parametrize('raw', [
    {'result': {'payloads': [], 'stopReason': 'length'}},
    {'status': 'error', 'error': 'model-fallback reason=format next=none; empty response'},
])
def test_classifier_empty_truncated_retries_three_attempts(raw, monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr('app.employees.worker.time.sleep', sleeps.append)
    def run(**kw):
        calls.append(kw)
        return raw if len(calls) < 3 else {'items': [{'module': 'info'}]}
    assert EmployeeRunner(runner=run).run(skill='document-classifier', text='source', company_id='acc-A')[0]['module'] == 'info'
    assert len(calls) == 3 and sleeps == [1, 2]


def test_gateway_fallback_and_output_budget_are_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv('SAM_EMPLOYEE_FALLBACK_MODEL', 'token-plan/operator-selected')
    monkeypatch.setenv('SAM_EMPLOYEE_MAX_OUTPUT_TOKENS', '24576')
    config = replace(RuntimeConfig.from_env(), state_dir=tmp_path / 'state',
                     config_path=tmp_path / 'state/openclaw.json', openclaw_mode='local')
    config = managed_config(config, tmp_path)
    OpenClawAdapter(StagingStore(tmp_path / 'db'), config=config, runner=managed_runner()).prepare_runtime()
    data = json.loads(config.config_path.read_text())
    assert data['agents']['defaults']['model']['fallbacks'] == ['token-plan/operator-selected']
    assert data['agents']['defaults']['models']['token-plan/${SAM_LEADER_MODEL}']['params']['maxTokens'] == 24576
    assert data['agents']['entries']['sam-leader']['model']['fallbacks'] == ['token-plan/operator-selected']
