"""Persist employee decisions, not host attribution vocabulary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3

import pytest

from app.db import connect, init_db
from app.employees import EmployeeRunner, SemanticEmployee
from app.entities import EntityBridgeService, EntityRosterService
from app.harness.staging import StagingStore
from app.memory.company_facts import L2CompleteFactService
from app.storage import ArchiveFileStore


def setup(tmp_path, *, classification="ambiguous", needs_review=True, consolidated=None, relation=None):
    db = tmp_path / "app.db"
    init_db(db)
    archive = ArchiveFileStore(tmp_path / "2a/bin", db)
    source = archive.put_bytes(b"unresolved test", original_name="附件.xlsx")
    StagingStore(db).save_manifest({
        "file_hash": source.file_hash, "filename": source.original_name, "format": "xlsx",
        "parse_summary": {"status": "parsed"},
        "pages": [{"page_no": 3, "text_items": [{"text": "987.65", "source_loc": {"locator": "sheet/附件/C2"}}]}],
    })
    EntityRosterService(db).declare(company_id="acc-review", entity_name="谷斗科技（上海）有限公司")
    worker = EmployeeRunner(runner=lambda **_: {"candidates": [{
        "block_id": 0, "classification": classification, "needs_review": needs_review,
        "subject": None if classification == "ambiguous" else "谷斗科技（上海）有限公司",
        "consolidated": consolidated, "relation": relation, "reason": "只有裸数字，无法确定主体",
    }]})
    bridge = EntityBridgeService(db, employee=SemanticEmployee(worker)).run(
        company_id="acc-review", file_hash=source.file_hash)
    return db, source, bridge


class Forbidden:
    def run(self, **_):
        pytest.fail("unresolved attribution must not enter fact extraction")


@pytest.mark.parametrize("classification", ["ambiguous", "self", "related"])
def test_employee_ambiguous_or_needs_review_persist_reason_text_coordinate_and_placeholder(tmp_path, classification):
    db, source, bridge = setup(tmp_path, classification=classification)
    service = L2CompleteFactService(db)
    prepared = service.prepare(company_id="acc-review", file_hash=source.file_hash,
                               bridge_run_id=bridge["id"], worker=Forbidden())
    assert service.list_todos(company_id="acc-review") == []  # preparation has no final writes
    result = service.commit(prepared)
    assert result["fact_count"] == 0 and result["todo_count"] == 1
    todo = service.list_todos(company_id="acc-review")[0]
    assert todo["reason"] == "只有裸数字，无法确定主体"
    assert todo["source_file"] == source.file_hash and todo["source_page"] == 3
    assert todo["source_span"] == "page=3; locator=sheet/附件/C2"
    assert todo["metric"] == todo["candidate_value"] == ""  # no invented metric/value
    handoff = json.loads(todo["handoff_json"])
    assert handoff["status"] == "unresolved" and handoff["source"]["text"] == "987.65"
    assert handoff["attribution"]["classification"] == classification
    assert handoff["attribution"]["attribution_id"] is None
    assert handoff["attribution"]["artifact_id"] == bridge["artifact_id"]
    with pytest.raises(ValueError, match="re-attribution"):
        service.decide(company_id="acc-review", todo_id=todo["id"], choose="candidate", confirm=True)
    with pytest.raises(KeyError):
        service.decide(company_id="other", todo_id=todo["id"], choose="candidate", confirm=True)


def test_unresolved_retry_and_concurrent_commit_are_idempotent(tmp_path):
    db, source, bridge = setup(tmp_path)
    service = L2CompleteFactService(db)
    batches = [service.prepare(company_id="acc-review", file_hash=source.file_hash,
                              bridge_run_id=bridge["id"], worker=Forbidden()) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.commit, batches))
    assert sorted(result["todo_count"] for result in results) == [0, 1]
    assert len(service.list_todos(company_id="acc-review")) == 1
    with pytest.raises(ValueError, match="not running"):
        service.commit(batches[0])


def test_failed_batch_rolls_back_fact_todo_and_run_status(tmp_path):
    db, source, bridge = setup(tmp_path, classification="self", needs_review=False)
    worker = EmployeeRunner(runner=lambda **_: {"candidates": [
        {"metric": "员工结论", "value": "987.65", "quote": "987.65", "entity": "谷斗科技（上海）有限公司"},
    ]})
    service = L2CompleteFactService(db)
    batch = service.prepare(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"], worker=worker)
    bad = {"status": "unresolved", "reason": "review", "blocking": "review",
           "source": {"file_hash": "a" * 64}}
    with pytest.raises(sqlite3.IntegrityError):
        service.commit(replace(batch, unresolved=(bad,)))
    assert service.list_facts(company_id="acc-review") == service.list_todos(company_id="acc-review") == []
    with connect(db) as conn:
        assert conn.execute("SELECT status FROM fact_runs WHERE id=?", (batch.run_id,)).fetchone()[0] == "running"


def test_cancelled_preparation_writes_no_todos(tmp_path):
    db, source, bridge = setup(tmp_path)
    service = L2CompleteFactService(db)
    batch = service.prepare(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"], worker=Forbidden())
    service.abort(batch)
    with pytest.raises(ValueError):
        service.commit(batch)
    assert service.list_todos(company_id="acc-review") == []


def test_secondary_outbox_failure_cannot_lose_committed_todo(tmp_path, monkeypatch):
    db, source, bridge = setup(tmp_path)
    service = L2CompleteFactService(db)
    monkeypatch.setattr(service, "_write_unresolved_handoff", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    result = service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"], worker=Forbidden())
    assert result["status"] == "completed" and result["todo_count"] == 1
    assert service.list_todos(company_id="acc-review")[0]["handoff_json"]


@pytest.mark.parametrize("relation,consolidated,count", [("并表子公司", False, 0), ("员工自定义关系用语", True, 1)])
def test_consolidation_uses_employee_flag_not_relation_wordlist(tmp_path, relation, consolidated, count):
    db, source, bridge = setup(tmp_path, classification="related", needs_review=False,
                               relation=relation, consolidated=consolidated)
    worker = EmployeeRunner(runner=lambda **_: {"candidates": [
        {"metric": "指标", "value": "987.65", "quote": "987.65", "entity": "谷斗科技（上海）有限公司"},
    ]})
    result = L2CompleteFactService(db).extract(company_id="acc-review", file_hash=source.file_hash,
                                             bridge_run_id=bridge["id"], worker=worker)
    assert result["fact_count"] == count


def test_existing_todo_schema_upgrade_is_idempotent_and_preserves_rows(tmp_path):
    db, source, bridge = setup(tmp_path)
    service = L2CompleteFactService(db)
    service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"], worker=Forbidden())
    with connect(db) as conn:
        conn.execute("DROP INDEX idx_todos_handoff")
        conn.execute("ALTER TABLE todos DROP COLUMN handoff_key")
        conn.execute("ALTER TABLE todos DROP COLUMN handoff_json")
        conn.execute("ALTER TABLE entity_bridge_blocks DROP COLUMN consolidated")
    init_db(db)
    init_db(db)
    assert service.list_todos(company_id="acc-review")[0]["reason"] == "只有裸数字，无法确定主体"
    with connect(db) as conn:
        assert "consolidated" in {row["name"] for row in conn.execute("PRAGMA table_info(entity_bridge_blocks)")}


def test_fact_employee_review_candidate_is_unresolved_not_an_empty_fact(tmp_path):
    db, source, bridge = setup(tmp_path, classification="self", needs_review=False)
    worker = EmployeeRunner(runner=lambda **_: {"runId": "actual-review-run", "result": {
        "payloads": [{"text": json.dumps({"candidates": [{
            "classification": "ambiguous", "needs_review": True,
            "reason": "提炼员工无法确认口径", "quote": "987.65",
        }]}, ensure_ascii=False)}],
    }})
    service = L2CompleteFactService(db)
    result = service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"], worker=worker)
    assert result["fact_count"] == 0 and result["todo_count"] == 1
    todo = service.list_todos(company_id="acc-review")[0]
    handoff = json.loads(todo["handoff_json"])
    assert todo["reason"] == "提炼员工无法确认口径"
    assert handoff["source"]["text"] == "987.65" and handoff["candidate"]["classification"] == "ambiguous"
    assert handoff["attribution"]["attribution_id"] == "actual-review-run"


@pytest.mark.parametrize("flag", ["classification", "needs_review", "review_reason"])
@pytest.mark.parametrize("number", ["987.65", "432.10"])
def test_fact_review_flags_with_valid_metric_never_become_conflict(tmp_path, flag, number):
    db, source, bridge = setup(tmp_path, classification="self", needs_review=False)
    with connect(db) as conn:
        conn.execute("UPDATE entity_bridge_blocks SET content=? WHERE run_id=?", (number, bridge["id"]))
    service = L2CompleteFactService(db)
    ordinary = {"metric": "员工指标", "value": "prior", "quote": number}
    service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"],
                    worker=EmployeeRunner(runner=lambda **_: {"candidates": [ordinary]}))
    review = dict(ordinary, value=number, reason="员工不能确定主体", blocking="请补充主体和指标")
    review[flag] = {"classification": "ambiguous", "needs_review": True,
                    "review_reason": "员工不能确定主体"}[flag]
    worker = EmployeeRunner(runner=lambda **_: {"candidates": [review]})
    results = [service.extract(company_id="acc-review", file_hash=source.file_hash,
                               bridge_run_id=bridge["id"], worker=worker) for _ in range(2)]
    assert [result["todo_count"] for result in results] == [1, 0]
    todo, = service.list_todos(company_id="acc-review")
    handoff = json.loads(todo["handoff_json"])
    assert todo["reason"] == handoff["reason"] == "员工不能确定主体"
    assert handoff["status"] == "unresolved" and handoff["blocking"] == "请补充主体和指标"
    assert handoff["source"] == {
        "file_hash": source.file_hash, "page": 3, "span": "page=3; locator=sheet/附件/C2",
        "text": number, "entity": "谷斗科技（上海）有限公司", "classification": "self",
    }
    assert "attribution_id" in handoff["attribution"]
    assert service.list_facts(company_id="acc-review")[0]["value"] == "prior"


@pytest.mark.parametrize("wrapper", ["direct", "payloads", "content"])
def test_top_level_employee_unresolved_reason_blocking_survive_transport(tmp_path, wrapper):
    db, source, bridge = setup(tmp_path, classification="self", needs_review=False)
    payload = {"candidates": [], "unresolved": {"reason": "员工未能归属", "blocking": "需范范补原件"}}
    if wrapper == "payloads":
        payload = {"runId": "review-run", "result": {"payloads": [{"text": json.dumps(payload)}]}}
    elif wrapper == "content":
        payload = {"runId": "review-run", "content": [{"type": "text", "text": json.dumps(payload)}]}
    calls = []
    worker = EmployeeRunner(runner=lambda **kw: calls.append(kw) or payload)
    service = L2CompleteFactService(db)
    result = service.extract(company_id="acc-review", file_hash=source.file_hash,
                             bridge_run_id=bridge["id"], worker=worker)
    assert len(calls) == 1 and result["fact_count"] == 0 and result["todo_count"] == 1
    todo, = service.list_todos(company_id="acc-review")
    handoff = json.loads(todo["handoff_json"])
    assert handoff["reason"] == todo["reason"] == "员工未能归属"
    assert handoff["blocking"] == "需范范补原件" and handoff["source"]["text"] == "987.65"
    assert handoff["attribution"]["attribution_id"] == (None if wrapper == "direct" else "review-run")


def test_conflicts_keep_verbatim_block_employee_reason_and_both_values(tmp_path):
    db, source, bridge = setup(tmp_path, classification="self", needs_review=False)
    text = "客户清单：星河制造、青禾设备；待确认版本：海川物流。"
    with connect(db) as conn:
        conn.execute("UPDATE entity_bridge_blocks SET content=? WHERE run_id=?", (text, bridge["id"]))
    service = L2CompleteFactService(db)
    def employee(value):
        return EmployeeRunner(runner=lambda **_: {"runId": "fact-run", "result": {"candidates": [{
            "metric": "客户清单", "value": value, "value_type": "string", "quote": value,
            "reason": "员工逐字初筛，不决定冲突取舍", "blocking": "需回查客户确认版",
        }]}})
    service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"],
                    worker=employee("星河制造"))
    batches = [service.prepare(company_id="acc-review", file_hash=source.file_hash,
                               bridge_run_id=bridge["id"], worker=employee("青禾设备")) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.commit, batches))
    assert sorted(result["todo_count"] for result in results) == [0, 1]
    service.extract(company_id="acc-review", file_hash=source.file_hash, bridge_run_id=bridge["id"],
                    worker=employee("海川物流"))
    todos = service.list_todos(company_id="acc-review")
    assert len(todos) == 2 and len({row["handoff_key"] for row in todos}) == 2
    for todo in todos:
        handoff = json.loads(todo["handoff_json"])
        assert todo["reason"] == handoff["system_reason"] == handoff["status"] == "conflict"
        assert handoff["reason"] == "员工逐字初筛，不决定冲突取舍"
        assert handoff["blocking"] == "需回查客户确认版"
        assert handoff["source"]["text"] == text
        assert (handoff["source"]["file_hash"], handoff["source"]["page"], handoff["source"]["span"]) == (
            source.file_hash, 3, "page=3; locator=sheet/附件/C2")
        assert handoff["existing"]["value"] == "星河制造"
        assert handoff["candidate_value"] == handoff["candidate"]["value"] == todo["candidate_value"]
        assert handoff["attribution"]["attribution_id"] == "fact-run"
        assert "先保留" not in todo["suggestion"]
    # A structured conflict packet is not an unresolved placeholder: humans
    # can still resolve it through the existing authorization/confirmation gate.
    with pytest.raises(PermissionError):
        service.decide(company_id="acc-review", todo_id=todos[0]["id"], choose="candidate")
    service.decide(company_id="acc-review", todo_id=todos[0]["id"], choose="existing", confirm=True)
    repeated = service.extract(company_id="acc-review", file_hash=source.file_hash,
                                bridge_run_id=bridge["id"], worker=employee("青禾设备"))
    assert repeated["todo_count"] == 0
    assert len(service.list_todos(company_id="acc-review", status=None)) == 2
