"""Backend orchestration contracts with scripted employees; no live models."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
from threading import Event

import pytest

from app.context_assembler import ContextAssembler
from app.db import connect, init_db
from app.employees.worker import EmployeeRunner, WorkerUnavailable
from app.memory.company_facts import ConsolidationService, L1BriefService, L0GuideService, L2CompleteFactService
from app.memory.company_facts.paths import (
    brief_json_uri, brief_md_uri, company_uri, guide_json_uri, guide_md_uri, l2_uri,
)
from app.memory_map import MemoryMapTools
from app.ports import LocalMemoryProvider, MemoryUnavailable
from app.storage import ArchiveFileStore
from tests.fact_candidates import fact_candidate

COMPANY = "acc-gudou"


@pytest.fixture
def pipeline(tmp_path):
    db = tmp_path / "app.db"
    init_db(db)
    archive = ArchiveFileStore(tmp_path / "objects", db)
    consolidation = ConsolidationService(db)
    ids = []
    for label, value in [("营业收入", "3.26"), ("净利润", "0.58")]:
        source = archive.put_bytes(label.encode(), original_name=label + ".pdf")
        ids.extend(consolidation.consolidate_candidates([
            fact_candidate(source.file_hash, label, value, company_id=COMPANY, period="2024", unit="亿元"),
        ]).verified_fact_ids)
    memory = LocalMemoryProvider(tmp_path / "ov")
    return db, memory, ids, archive


def brief_employee(ids, *, conclusion=None, run_id=None, session_id=None):
    if conclusion is None and not ids:
        conclusion = "无事实"
    if conclusion is None:
        conclusion = f"2024年{{f:{ids[0]}}}营业收入3.26亿元{{f:{ids[0]}}}；净利润0.58亿元{{f:{ids[1]}}}"
    def transport(**kwargs):
        assert kwargs["skill"] == "fact-brief"
        assert kwargs["scope"]["company_id"] == COMPANY
        assert "fact_ids" in kwargs["skill_text"]
        facts = json.loads(kwargs["text"])["facts"]
        assert [item["fact_id"] for item in facts] == ids
        return {"runId": run_id, "result": {
            "meta": {"agentMeta": {"sessionId": session_id}},
            "payloads": [{"text": json.dumps({"items": [
                {"topic": "财务", "conclusion": conclusion, "fact_ids": ids},
            ]}, ensure_ascii=False)}],
        }}
    return EmployeeRunner(runner=transport, retries=0)


def guide_employee(*, items=None):
    def transport(**kwargs):
        assert kwargs["skill"] == "fact-guide"
        payload = json.loads(kwargs["text"])
        assert set(payload) == {"l1", "token_budget"}
        assert "facts" not in payload and "source_block" not in payload
        assert "l1_entry_ids" in kwargs["skill_text"]
        return {"items": items if items is not None else [
            {"conclusion": "营业收入3.26亿元{l:brief-1}", "l1_entry_ids": ["brief-1"]},
        ]}
    return EmployeeRunner(runner=transport, retries=0)


def build_brief(pipeline, **kwargs):
    db, memory, ids, _ = pipeline
    return L1BriefService(db, memory, worker=brief_employee(ids, **kwargs)).rebuild(company_id=COMPANY)


def file_snapshot(memory):
    return {item["uri"]: item["content"] for item in memory.query(prefix="sam://")}


def test_l1_full_company_verified_snapshot_and_numeric_provenance(pipeline):
    db, memory, ids, archive = pipeline
    source = archive.put_bytes(b"excluded", original_name="excluded.pdf")
    with connect(db) as conn:
        for status, company in [("claimed", COMPANY), ("inferred", COMPANY), ("verified", "acc-other")]:
            conn.execute("""INSERT INTO facts(company_id,entity,attribute,value,source_file,status,created_at)
                            VALUES (?,?,?,?,?,?,?)""",
                         (company, company, status, "99", source.file_hash, status, "fixture"))
        conn.execute("""INSERT INTO facts(company_id,entity,attribute,value,source_file,status,valid_to,created_at)
                        VALUES (?,?,?,?,?,'verified','closed','fixture')""",
                     (COMPANY, COMPANY, "expired", "88", source.file_hash))
        conn.execute("""INSERT INTO facts(company_id,entity,attribute,value,source_file,status,superseded_by,created_at)
                        VALUES (?,?,?,?,?,'verified',?,'fixture')""",
                     (COMPANY, COMPANY, "old", "77", source.file_hash, ids[0]))
    brief = build_brief(pipeline)
    facts = {fact["id"]: fact for fact in ConsolidationService(db).list_verified_facts(company_id=COMPANY)}
    assert len({fact["source_file"] for fact in facts.values()}) == 2
    for item in brief["items"]:
        assert item["fact_ids"] and set(item["fact_ids"]) <= facts.keys()
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", " ".join(
            facts[ref]["period"] + " " + facts[ref]["value"] for ref in item["fact_ids"])))
        from app.memory.company_facts.numeric_guard import strip_marks
        assert set(re.findall(r"\d+(?:\.\d+)?", strip_marks(item["conclusion"]))) <= source_numbers
        assert item["attribution_id"] is None
        assert all(facts[ref]["source_file"] and facts[ref]["source_span"] for ref in item["fact_ids"])
    assert json.loads(memory.read(brief_json_uri(COMPANY))) == brief
    assert "L2 fact_id:" in memory.read(brief_md_uri(COMPANY))


def test_full_l1_l0_rebuild_is_idempotent(pipeline):
    _, memory, _, _ = pipeline
    brief = build_brief(pipeline)
    guide = L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)
    previous = file_snapshot(memory)
    assert build_brief(pipeline) == brief
    assert L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY) == guide
    assert file_snapshot(memory) == previous
    assert len(previous) == 4
    assert guide["items"][0]["l1_uris"] == [brief_md_uri(COMPANY) + "#brief-1"]
    from app.memory.company_facts.numeric_guard import strip_marks
    assert strip_marks(guide["items"][0]["conclusion"]) in strip_marks(brief["items"][0]["conclusion"])


def test_empty_l2_writes_no_projection_or_placeholder(tmp_path):
    db = tmp_path / "empty.db"
    init_db(db)
    memory = LocalMemoryProvider(tmp_path / "ov")
    class Forbidden:
        def run(self, **_):
            pytest.fail("empty state invoked an employee")
    assert L1BriefService(db, memory, worker=Forbidden()).rebuild(company_id=COMPANY) is None
    assert L0GuideService(memory, worker=Forbidden()).rebuild(company_id=COMPANY) is None
    assert file_snapshot(memory) == {}


@pytest.mark.parametrize("raw", [None, "", "  ", {"company_id": COMPANY, "items": []}])
def test_l0_missing_or_empty_l1_never_reads_l2(pipeline, monkeypatch, raw):
    _, memory, _, _ = pipeline
    if raw is not None:
        memory.put(brief_json_uri(COMPANY), json.dumps(raw) if isinstance(raw, dict) else raw)
    original = file_snapshot(memory)
    reads = []
    real_read = memory.read
    def read(uri):
        reads.append(uri)
        assert uri in {brief_json_uri(COMPANY), guide_json_uri(COMPANY), guide_md_uri(COMPANY)}
        return real_read(uri)
    monkeypatch.setattr(memory, "read", read)
    class Forbidden:
        def run(self, **_):
            pytest.fail("L0 invoked an employee without L1")
    assert L0GuideService(memory, worker=Forbidden()).rebuild(company_id=COMPANY) is None
    assert reads[0] == brief_json_uri(COMPANY)
    assert file_snapshot(memory) == original


@pytest.mark.parametrize("refs", [[], [99999], [True], ["1"], None])
def test_l1_rejects_missing_or_unknown_fact_ids_without_replacing_old(pipeline, refs):
    db, memory, _, _ = pipeline
    build_brief(pipeline)
    previous = file_snapshot(memory)
    worker = EmployeeRunner(runner=lambda **_: {"items": [{"topic": "财务", "conclusion": "无法回查", "fact_ids": refs}]})
    with pytest.raises(WorkerUnavailable):
        L1BriefService(db, memory, worker=worker).rebuild(company_id=COMPANY)
    assert file_snapshot(memory) == previous


@pytest.mark.parametrize("refs", [[], ["absent"], [1], None])
def test_l0_rejects_unknown_l1_pointers_without_files(pipeline, refs):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    previous = file_snapshot(memory)
    with pytest.raises(WorkerUnavailable):
        L0GuideService(memory, worker=guide_employee(items=[{"conclusion": "入口", "l1_entry_ids": refs}])).rebuild(company_id=COMPANY)
    assert file_snapshot(memory) == previous


def test_l0_never_queries_l2_even_when_generating(pipeline, monkeypatch):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    def forbidden(*_, **__):
        pytest.fail("L0 directly queried L2")
    monkeypatch.setattr(ConsolidationService, "list_facts", forbidden)
    monkeypatch.setattr(ConsolidationService, "list_verified_facts", forbidden)
    monkeypatch.setattr(memory, "list_facts", forbidden)
    real_read = memory.read
    def read(uri):
        assert "/L2/" not in uri and "/2b_facts/" not in uri
        return real_read(uri)
    monkeypatch.setattr(memory, "read", read)
    assert L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)["items"]


@pytest.mark.parametrize("stage", ["l1", "l0"])
def test_empty_employee_keeps_previous_version(pipeline, stage):
    db, memory, _, _ = pipeline
    build_brief(pipeline)
    L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)
    previous = file_snapshot(memory)
    worker = EmployeeRunner(runner=lambda **_: {"items": []})
    service = L1BriefService(db, memory, worker=worker) if stage == "l1" else L0GuideService(memory, worker=worker)
    assert service.rebuild(company_id=COMPANY) is None
    assert file_snapshot(memory) == previous


@pytest.mark.parametrize("stage", ["l1", "l0"])
def test_pair_write_failure_restores_previous_version(pipeline, monkeypatch, stage):
    db, memory, ids, _ = pipeline
    build_brief(pipeline)
    L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)
    previous = file_snapshot(memory)
    real_put = memory.put
    target = brief_json_uri(COMPANY) if stage == "l1" else guide_json_uri(COMPANY)
    failed = False
    def fail_once(uri, content, **kwargs):
        nonlocal failed
        real_put(uri, content, **kwargs)
        if uri == target and not failed:
            failed = True
            raise MemoryUnavailable("fixture write outage after mutation")
    monkeypatch.setattr(memory, "put", fail_once)
    service = (L1BriefService(db, memory, worker=brief_employee(ids, conclusion="不同员工措辞"))
               if stage == "l1" else L0GuideService(memory, worker=guide_employee(items=[
                   {"conclusion": "简短入口", "l1_entry_ids": ["brief-1"]},
               ])))
    with pytest.raises(MemoryUnavailable):
        service.rebuild(company_id=COMPANY)
    assert file_snapshot(memory) == previous


def test_l0_budget_crops_only_whole_entries_not_numbers_or_pointers(pipeline):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    first = {"conclusion": "营业收入3.26亿元{l:brief-1}", "l1_entry_ids": ["brief-1"]}
    last = {"conclusion": "净利润0.58亿元{l:brief-1}", "l1_entry_ids": ["brief-1"]}
    size = len((first["conclusion"].replace("{l:brief-1}", "") + "\n[L1](" + brief_md_uri(COMPANY) + "#brief-1)\n\n").encode())
    result = L0GuideService(memory, worker=guide_employee(items=[first, last]), token_budget=size + 1).rebuild(company_id=COMPANY)
    assert len(result["items"]) == 1
    markdown = memory.read(guide_md_uri(COMPANY))
    assert len(markdown.encode()) <= size + 1
    assert "3.26" in markdown and markdown.rstrip().endswith("#brief-1)")


@pytest.mark.parametrize("budget", [0, 1])
def test_l0_unusable_length_budget_writes_no_placeholder(pipeline, budget):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    before = file_snapshot(memory)
    assert L0GuideService(memory, worker=guide_employee(), token_budget=budget).rebuild(company_id=COMPANY) is None
    assert file_snapshot(memory) == before


@pytest.mark.parametrize("run_id,session_id", [("real-run", "real-session"), (None, "real-session"), (None, None)])
def test_transport_attribution_is_nullable_and_not_invented(pipeline, run_id, session_id):
    _, memory, _, _ = pipeline
    brief = build_brief(pipeline, run_id=run_id, session_id=session_id)
    assert brief["run_id"] == run_id and brief["session_id"] == session_id
    assert brief["items"][0]["attribution_id"] == (run_id or session_id)
    guide = L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)
    assert guide["items"][0]["attribution_id"] is None  # not copied from L1


def test_forged_candidate_replay_ids_are_not_transport_metadata():
    raw = {"items": [{"conclusion": "x", "runId": "fake", "sessionId": "fake"}]}
    runner = EmployeeRunner(runner=lambda **_: raw)
    result = runner.run(skill="fact-guide", text="fixture", company_id=COMPANY)
    assert isinstance(result, list) and result == raw["items"]
    assert result.attribution_id is None


def test_guide_is_resident_via_existing_context_and_l1_is_readable(pipeline):
    _, memory, _, archive = pipeline
    build_brief(pipeline)
    service = L0GuideService(memory, worker=guide_employee())
    service.rebuild(company_id=COMPANY)
    assembled = ContextAssembler(memory, company_id=COMPANY, thread_id="fixture").assemble("公司情况")
    assert "[memory_map]" in assembled["prompt"] and "[fact_guide]" in assembled["prompt"]
    assert service.read_guide(company_id=COMPANY) == assembled["layers"]["fact_guide"]
    assert "L2 fact_id:" not in assembled["prompt"]
    tools = MemoryMapTools(memory, archive, COMPANY)
    assert "3.26" in tools.memory_read(brief_md_uri(COMPANY))
    assert any("/brief" in uri for uri in tools.memory_search("3.26"))
    with pytest.raises(PermissionError):
        tools.memory_read(brief_md_uri("acc-other"))


def test_context_budget_drops_whole_guide_entries(pipeline):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    L0GuideService(memory, worker=guide_employee()).rebuild(company_id=COMPANY)
    assembler = ContextAssembler(memory, company_id=COMPANY, thread_id="fixture", budget=50)
    result = assembler.assemble("公司情况")
    assert not result["layers"].get("fact_guide")


def test_stale_l0_is_not_injected_after_l1_changes(pipeline):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    guide = L0GuideService(memory, worker=guide_employee())
    guide.rebuild(company_id=COMPANY)
    build_brief(pipeline, conclusion="最新员工措辞")
    assert guide.read_guide(company_id=COMPANY) == ""
    assert "[fact_guide]" not in ContextAssembler(memory, company_id=COMPANY, thread_id="fixture").assemble("情况")["prompt"]


def test_company_projection_workers_are_serialized(pipeline):
    db, memory, ids, _ = pipeline
    entered, release, second = Event(), Event(), Event()
    def slow(**_):
        entered.set()
        assert release.wait(5)
        return {"items": [{"topic": "财务", "conclusion": "先构建", "fact_ids": ids}]}
    def fast(**_):
        second.set()
        return {"items": [{"topic": "财务", "conclusion": "后构建", "fact_ids": ids}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(L1BriefService(db, memory, worker=EmployeeRunner(runner=slow)).rebuild, company_id=COMPANY)
        assert entered.wait(5)
        later = pool.submit(L1BriefService(db, memory, worker=EmployeeRunner(runner=fast)).rebuild, company_id=COMPANY)
        assert not second.wait(0.1)
        release.set()
        first.result(timeout=5)
        later.result(timeout=5)
    assert json.loads(memory.read(brief_json_uri(COMPANY)))["items"][0]["conclusion"] == "后构建"


def test_legacy_extraction_cannot_be_selected(pipeline):
    db, _, _, _ = pipeline
    with pytest.raises(ValueError, match="code-side extraction removed"):
        L2CompleteFactService(db).prepare(company_id=COMPANY, file_hash="none", use_worker=False)


def test_pure_paths_obey_injected_root_and_company_scope():
    assert l2_uri(COMPANY) == f"sam://facts/{COMPANY}"
    assert company_uri(COMPANY, root="sam://resources/sam") == f"sam://facts/{COMPANY}"
    with pytest.raises(ValueError):
        brief_json_uri("a/b")
    with pytest.raises(ValueError):
        guide_md_uri("..")


def test_rebuilding_after_source_becomes_empty_removes_only_generated_files(pipeline):
    db, memory, _, _ = pipeline
    build_brief(pipeline)
    guide = L0GuideService(memory, worker=guide_employee())
    guide.rebuild(company_id=COMPANY)
    unrelated = guide_md_uri("acc-other")
    memory.put(unrelated, "other company stays")
    with connect(db) as conn:
        conn.execute("UPDATE facts SET valid_to='closed' WHERE company_id=?", (COMPANY,))
    assert L1BriefService(db, memory, worker=brief_employee([])).rebuild(company_id=COMPANY) is None
    assert guide.rebuild(company_id=COMPANY) is None
    assert file_snapshot(memory) == {unrelated: "other company stays"}


@pytest.mark.parametrize("raw", [
    {"company_id": "acc-other", "items": [{"entry_id": "brief-1"}]},
    {"company_id": COMPANY, "items": [{"entry_id": "brief-1"}, {"entry_id": "brief-1"}]},
    {"company_id": COMPANY, "items": [{}]},
    {"company_id": COMPANY, "items": ["malformed"]},
])
def test_l0_rejects_malformed_l1_before_employee_execution(pipeline, raw):
    _, memory, _, _ = pipeline
    memory.put(brief_json_uri(COMPANY), json.dumps(raw))
    class Forbidden:
        def run(self, **_):
            pytest.fail("malformed L1 reached the employee")
    with pytest.raises(WorkerUnavailable):
        L0GuideService(memory, worker=Forbidden()).rebuild(company_id=COMPANY)
    assert len(file_snapshot(memory)) == 1


def test_l0_transport_run_and_session_reach_attribution_fields(pipeline):
    _, memory, _, _ = pipeline
    build_brief(pipeline, run_id="different-l1-run")
    worker = EmployeeRunner(runner=lambda **_: json.dumps({
        "runId": "real-l0-run", "result": {
            "meta": {"agentMeta": {"sessionId": "real-l0-session"}},
            "payloads": [{"text": json.dumps({"items": [
                {"conclusion": "营业收入3.26亿元{l:brief-1}", "l1_entry_ids": ["brief-1"], "attribution_id": "forged"},
            ]})}],
        },
    }))
    guide = L0GuideService(memory, worker=worker).rebuild(company_id=COMPANY)
    assert guide["run_id"] == "real-l0-run"
    assert guide["session_id"] == "real-l0-session"
    assert guide["items"][0]["attribution_id"] == "real-l0-run"


def test_employee_replay_metadata_comes_from_successful_attempt_only():
    attempts = iter([
        {"runId": "failed-run", "result": {"payloads": [{"text": "broken"}]}},
        {"runId": "actual-run", "result": {"payloads": [{"text": '{"items":[]}'}]}},
    ])
    worker = EmployeeRunner(runner=lambda **_: next(attempts))
    result = worker.run(skill="fact-brief", text="fixture", company_id=COMPANY)
    assert result == [] and result.run_id == "actual-run"


@pytest.mark.parametrize("stage", ["l1", "l0"])
def test_first_projection_write_failure_leaves_no_half_pair(pipeline, monkeypatch, stage):
    db, memory, ids, _ = pipeline
    if stage == "l0":
        build_brief(pipeline)
    before = file_snapshot(memory)
    target = brief_json_uri(COMPANY) if stage == "l1" else guide_json_uri(COMPANY)
    real_put = memory.put
    def failed(uri, text, **kwargs):
        if uri == target:
            raise MemoryUnavailable("fixture first write failure")
        real_put(uri, text, **kwargs)
    monkeypatch.setattr(memory, "put", failed)
    service = (L1BriefService(db, memory, worker=brief_employee(ids)) if stage == "l1"
               else L0GuideService(memory, worker=guide_employee()))
    with pytest.raises(MemoryUnavailable):
        service.rebuild(company_id=COMPANY)
    assert file_snapshot(memory) == before


def test_ov_read_outage_is_not_an_empty_l1_and_does_not_erase_guide(pipeline, monkeypatch):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    guide = L0GuideService(memory, worker=guide_employee())
    guide.rebuild(company_id=COMPANY)
    before = file_snapshot(memory)
    def failed(*_):
        raise MemoryUnavailable("fixture read outage")
    monkeypatch.setattr(memory, "read", failed)
    with pytest.raises(MemoryUnavailable):
        guide.rebuild(company_id=COMPANY)
    assert file_snapshot(memory) == before


def test_l0_contract_forbids_multiline_entries_that_break_budget_boundaries(pipeline):
    _, memory, _, _ = pipeline
    build_brief(pipeline)
    with pytest.raises(WorkerUnavailable):
        L0GuideService(memory, worker=guide_employee(items=[
            {"conclusion": "入口\n\n营业收入3.26亿元", "l1_entry_ids": ["brief-1"]},
        ])).rebuild(company_id=COMPANY)
    assert len(file_snapshot(memory)) == 2


def test_l2_ov_projection_is_storage_only_verified_employee_facts(pipeline):
    from app.memory.company_facts import L2ProjectionService
    from app.memory.company_facts.paths import fact_uri
    db, memory, ids, archive = pipeline
    before = ConsolidationService(db).list_verified_facts(company_id=COMPANY)
    assert L2ProjectionService(db, memory).publish(company_id=COMPANY) == 2
    for fact in before:
        assert json.loads(memory.read(fact_uri(COMPANY, fact['id']))) == fact
    assert len(file_snapshot(memory)) == 2
    assert all('/待归类/F' in uri for uri in file_snapshot(memory))
    # Rebuilding has no semantic worker, no original/manifest copy, no new
    # facts or todos, and exact idempotent JSON values.
    snapshot = file_snapshot(memory)
    assert L2ProjectionService(db, memory).publish(company_id=COMPANY) == 2
    assert file_snapshot(memory) == snapshot
    assert ConsolidationService(db).list_verified_facts(company_id=COMPANY) == before


@pytest.mark.parametrize("persist_before_failure", [False, True])
def test_l2_projection_retries_without_duplicate_or_missing_facts(pipeline, tmp_path, monkeypatch,
                                                                persist_before_failure):
    from tests.memory_setup import migrated_memory
    from app.memory.provider import ProjectionError
    from app.memory.company_facts.l2_projection import L2ProjectionService
    from tests.test_memory_kv import Embedding
    db, _, ids, _ = pipeline
    memory = migrated_memory(db, embedding=Embedding(), company_id=COMPANY)
    service = L2ProjectionService(db, memory)
    original = memory._project
    failed = False
    def project(row):
        nonlocal failed
        if row['fact_key'] == str(ids[1]) and not failed:
            failed = True
            if persist_before_failure:
                original(row)
            raise RuntimeError('synthetic interrupted projection')
        original(row)
    monkeypatch.setattr(memory, '_project', project)
    try:
        with pytest.raises(ProjectionError): service.publish(company_id=COMPANY)
        facts = ConsolidationService(db).list_verified_facts(company_id=COMPANY)
        assert len(facts) == len(ids)
        assert service.publish(company_id=COMPANY) == len(ids)
        assert len(memory.list_facts(COMPANY)) == len(ids)
        assert memory.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == len(ids)
        assert service.publish(company_id=COMPANY) == len(ids)
        assert memory.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == len(ids)
    finally:
        memory.close()
