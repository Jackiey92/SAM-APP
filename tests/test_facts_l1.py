from __future__ import annotations

from pathlib import Path

import pytest

from app.db import connect, init_db
from app.entities import EntityBridgeService, EntityRosterService
from app.memory.company_facts import L2CompleteFactService
from app.memory.company_facts.l2_fact_verifier import verify_candidates
from tests.fact_candidates import FixtureFinanceEmployee, decision
from app.harness.staging import StagingStore
from app.storage import ArchiveFileStore
from tests.entity_employee import FixtureEntityEmployee


def _manifest(file_hash: str, text: list[str]) -> dict:
    return {
        "file_hash": file_hash,
        "filename": "年报2025.xlsx",
        "format": "xlsx",
        "parse_summary": {"status": "parsed"},
        "pages": [{
            "page_no": 1,
            "text_items": [
                {"text": value, "source_loc": {"locator": f"text/{index}"}}
                for index, value in enumerate(text)
            ],
            "tables": [],
        }],
    }


def _pipeline(tmp_path: Path, text: list[str], name="年报2025.xlsx"):
    db = tmp_path / "app.db"
    init_db(db)
    store = ArchiveFileStore(tmp_path / "objects", db)
    stored = store.put_bytes(b"file-" + str(len(text)).encode(), original_name=name)
    manifest = _manifest(stored.file_hash, text)
    StagingStore(db).save_manifest(manifest)
    EntityRosterService(db).declare(company_id="acme", entity_name="主公司有限公司")
    bridge = EntityBridgeService(db, employee=FixtureEntityEmployee()).run(company_id="acme", file_hash=stored.file_hash)
    return db, stored, bridge


def test_l2_reads_only_self_blocks_and_preserves_literal_digits(tmp_path: Path):
    db, stored, bridge = _pipeline(tmp_path, [
        "主公司有限公司营业收入 98,765.43",
        "远方科技有限公司营业收入 12345.67",
        "营业收入较上年同期增长15.30%",
    ])
    service = L2CompleteFactService(db)
    run = service.extract(company_id="acme", file_hash=stored.file_hash, bridge_run_id=bridge["id"], worker=FixtureFinanceEmployee(
        decision("营业收入", "98,765.43", "营业收入 98,765.43"),
    ))
    facts = service.list_facts(company_id="acme")
    assert run["candidate_count"] == 1
    assert [(fact["attribute"], fact["value"]) for fact in facts] == [("营业收入", "98,765.43")]
    todos = service.list_todos(company_id="acme")
    assert len(todos) == 1 and todos[0]["handoff_json"]
    assert todos[0]["source_span"] and "15.30%" in todos[0]["handoff_json"]


def test_critical_first_occurrence_is_todo_and_decide_requires_confirm(tmp_path: Path):
    db, stored, bridge = _pipeline(tmp_path, ["主公司有限公司注册资本 5000000"])
    service = L2CompleteFactService(db)
    run = service.extract(company_id="acme", file_hash=stored.file_hash, bridge_run_id=bridge["id"], worker=FixtureFinanceEmployee(
        decision("注册资本", "5000000", "注册资本 5000000", critical=True),
    ))
    assert run["fact_count"] == 0 and run["todo_count"] == 1
    todo = service.list_todos(company_id="acme")[0]
    assert todo["reason"] == "critical_review" and todo["suggestion"]
    with pytest.raises(PermissionError):
        service.decide(company_id="acme", todo_id=todo["id"], choose="candidate")


def test_l2_is_idempotent_and_tracks_fact_run_counts(tmp_path: Path):
    db, stored, bridge = _pipeline(tmp_path, ["主公司有限公司净利润 1200"])
    service = L2CompleteFactService(db)
    first = service.extract(company_id="acme", file_hash=stored.file_hash, bridge_run_id=bridge["id"], worker=FixtureFinanceEmployee(
        decision("净利润", "1200", "净利润 1200"),
    ))
    second = service.extract(company_id="acme", file_hash=stored.file_hash, bridge_run_id=bridge["id"], worker=FixtureFinanceEmployee(
        decision("净利润", "1200", "净利润 1200"),
    ))
    assert first["fact_count"] == 1
    assert second["fact_count"] == 0
    assert len(service.list_facts(company_id="acme")) == 1
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_runs").fetchone()[0] == 2


def test_fact_extraction_requires_host_company_scope(tmp_path: Path):
    db, stored, bridge = _pipeline(tmp_path, ["主公司有限公司营业收入 10"])
    with pytest.raises(ValueError):
        L2CompleteFactService(db).extract(company_id="", file_hash=stored.file_hash, bridge_run_id=bridge["id"])


@pytest.mark.parametrize("text,value,unit", [
    ("实现收入0.92亿元", "0.92", "亿元"),
    ("营业收入3.26亿元", "3.26", "亿元"),
    ("收入51000.00万元", "51000.00", "万元"),
])
def test_employee_numbers_and_units_are_preserved(text, value, unit):
    fact = verify_candidates([decision("营业收入", value, text, unit=unit)], [{
        "content": text, "file_hash": "a" * 64, "source_page": 1,
        "source_span": "fixture", "subject": "本公司",
    }], company_id="acme")[0]
    assert (fact.metric, fact.value, fact.unit) == ("营业收入", value, unit)


def test_employee_period_and_entity_are_not_inferred_from_filename():
    text = "2024 年经营活动产生的现金流量净额为 0.36 亿元"
    fact = verify_candidates([decision("经营现金流", "0.36", text, unit="亿元")], [{
        "content": text, "file_hash": "a" * 64, "source_page": 1,
        "source_span": "fixture", "subject": "本公司",
    }], company_id="acme")[0]
    assert (fact.metric, fact.value, fact.unit, fact.entity, fact.period) == (
        "经营现金流", "0.36", "亿元", "本公司", "2024",
    )


def test_subsidiary_self_blocks_use_subject_dimension_without_false_conflict(tmp_path: Path):
    db, parent_file, parent_bridge = _pipeline(tmp_path, ["主公司有限公司营业收入3.26亿元"])
    store = ArchiveFileStore(tmp_path / "objects", db)
    child = store.put_bytes(b"child", original_name="子公司2024.xlsx")
    StagingStore(db).save_manifest(_manifest(child.file_hash, [
        "主公司有限公司全资子公司常州未蓝新能源有限公司收入0.92亿元",
    ]))
    child_bridge = EntityBridgeService(db, employee=FixtureEntityEmployee()).run(company_id="acme", file_hash=child.file_hash)
    service = L2CompleteFactService(db)
    service.extract(company_id="acme", file_hash=parent_file.file_hash, bridge_run_id=parent_bridge["id"], worker=FixtureFinanceEmployee(
        decision("营业收入", "3.26", "营业收入3.26亿元", unit="亿元"),
    ))
    child_run = service.extract(company_id="acme", file_hash=child.file_hash, bridge_run_id=child_bridge["id"], worker=FixtureFinanceEmployee(
        decision("营业收入", "0.92", "收入0.92亿元", unit="亿元"),
    ))
    assert child_run["todo_count"] == 0
    facts = service.list_facts(company_id="acme")
    assert {(fact["entity"], fact["attribute"], fact["value"], fact["unit"]) for fact in facts} == {
        ("主公司有限公司", "营业收入", "3.26", "亿元"),
        ("常州未蓝新能源有限公司", "营业收入", "0.92", "亿元"),
    }
