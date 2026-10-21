"""External SourceMap navigation contracts, with no OV 2A legacy fixtures."""
from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from app.db import init_db
from app.facts_tree.tree_read import TreeReadService
from app.facts_tree.uris import leaf_uri as tree_leaf_uri, tree_prefix
from app.harness.staging import StagingStore
from app.memory.company_facts.paths import fact_uri
from app.memory_map import MapBuilder, MemoryMapTools, ROOT, source_map_uri
from app import settings
from app.ports import LocalMemoryProvider, MemoryUnavailable
from app.storage import ArchiveFileStore
from app.storage.mapping_store import SourceMapService
from tests.test_facts_tree_leaves import kernel, artifact, publish, COMPANY
from tests.test_facts_tree_tree_read import GENERIC, attribute, db_path, indexed, vector_kernel


@dataclass
class FakeFile:
    file_hash: str
    original_name: str
    mime_type: str
    size_bytes: int
    storage_path: str


class FakeFiles:
    def __init__(self):
        self.rows = {digest * 64: FakeFile(digest * 64, name, "application/pdf", 10, "blob")
                     for digest, name in [("a", "report.pdf"), ("b", "brief.pdf")]}

    def get(self, file_hash):
        return self.rows[file_hash]


def source_setup(tmp_path):
    db = tmp_path / "app.db"
    init_db(db)
    files = ArchiveFileStore(tmp_path / "2a/bin", db)
    catalog = []
    for company, name in [("acme", "report.pdf"), ("other", "brief.pdf")]:
        stored = files.put_bytes(name.encode(), original_name=name)
        StagingStore(db, maps_path=tmp_path / "2a/map").save_manifest({
            "source_id": stored.file_hash, "file_hash": stored.file_hash, "filename": name,
            "format": "pdf", "parse_summary": {"status": "parsed"},
            "pages": [{"page_no": 1, "text_items": [{"text": "正文 4820 万元", "source_loc": {"locator": "text/0"}}]}],
        })
        catalog.append((company, stored.file_hash))
    memory = LocalMemoryProvider(tmp_path / "ov")
    memory.put(f"{ROOT}/acme/brief.md", "report 4820 万元")
    memory.put(f"{ROOT}/other/brief.md", "brief 320 万元")
    return memory, files, SourceMapService(db, objects_path=files.objects_path), tuple(catalog)


def test_map_is_navigation_only_and_rebuild_is_deterministic(tmp_path):
    memory, _, _, catalog = source_setup(tmp_path)
    builder = MapBuilder(memory, source_catalog=catalog)
    before = memory.query(prefix=ROOT)
    first = builder.rebuild_map("acme").map
    assert first["branches"] == builder.rebuild_map("acme").map["branches"]
    assert all(set(item) >= {"title", "uri", "kind"} for item in first["branches"])
    assert "4820" not in str(first)
    assert memory.query(prefix=ROOT) == before  # navigation is read-only


def test_map_marks_dangling_branch(tmp_path):
    # No provider probe is required to discover an external mapping. A deleted
    # local projection is rebuilt in memory by SourceMap, never mirrored to OV.
    memory, files, maps, catalog = source_setup(tmp_path)
    maps.map_path(catalog[0][1]).unlink()
    result = MapBuilder(memory, source_catalog=catalog).rebuild_map("acme").map
    branch = next(item for item in result["branches"] if item["kind"] == "2a")
    assert branch["external"] is True
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    assert "4820" in tools.memory_read(branch["uri"])
    assert not maps.map_path(catalog[0][1]).exists()  # read has no writes


def test_manager_can_read_parser_body_from_map_branch(tmp_path):
    memory, files, _, catalog = source_setup(tmp_path)
    branch = MapBuilder(memory, source_catalog=catalog).rebuild_map("acme").map["branches"][0]
    assert branch["uri"].endswith("/mapping.md")
    body = MemoryMapTools(memory, files, "acme", source_catalog=catalog).memory_read(branch["uri"])
    assert "source_id" in body and "page=1" in body and "4820" in body


def test_cached_map_refreshes_when_parallel_import_adds_a_source(tmp_path):
    memory, _, _, catalog = source_setup(tmp_path)
    stale = {"company_id": "acme", "branches": [{"uri": "sam://stale/removed", "kind": "2a"}]}
    memory.put(MapBuilder(memory).map_uri("acme"), json.dumps(stale))
    first = MapBuilder(memory, source_catalog=catalog[:1]).load_or_rebuild("acme").map
    new_catalog = (catalog[0], ("acme", catalog[1][1]))
    refreshed = MapBuilder(memory, source_catalog=new_catalog).load_or_rebuild("acme").map
    assert len([item for item in first["branches"] if item["kind"] == "2a"]) == 1
    assert {item["file_hash"] for item in refreshed["branches"] if item["kind"] == "2a"} == {digest for _, digest in catalog}
    assert "stale" not in str(refreshed)


def test_tools_enforce_company_scope_and_record_navigation(tmp_path):
    memory, files, _, catalog = source_setup(tmp_path)
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    uri = source_map_uri(*catalog[0])
    assert "source_id" in tools.memory_read(uri)
    assert tools.memory_search("report") == [f"{ROOT}/acme/brief.md"]
    assert tools.file_get(catalog[0][1])["original_name"] == "report.pdf"
    with pytest.raises(PermissionError):
        tools.memory_read(source_map_uri(*catalog[1]))
    with pytest.raises(PermissionError):
        tools.file_get(catalog[1][1])
    with pytest.raises(ValueError):
        tools.file_get("not-a-hash")
    assert all(item.startswith("sam://") for item in tools.navigation)


def test_tools_allow_only_company_scoped_pack_tree_uris(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)
    memory, files, _, catalog = source_setup(tmp_path)
    uri = "sam://facts/co/acme/tree/generic-v2/1/leaves/L-finance-01-income.md"
    memory.put(uri, "pack-scoped finance content")
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    assert tools._check_uri(uri) == uri
    with pytest.raises(MemoryUnavailable):  # employee search is the verified tree
        tools.memory_search("pack-scoped finance")  # read; planted content never leaks
    with pytest.raises(PermissionError):
        MemoryMapTools(memory, files, "other").memory_read(uri)


def test_tools_do_not_search_pack_tree_namespace_while_feature_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)
    memory, files, _, catalog = source_setup(tmp_path)
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    assert tools.company_prefixes == (f"{ROOT}/acme/",)


def test_manager_global_read_scope_reads_other_company_without_writing_map(tmp_path):
    memory, files, _, catalog = source_setup(tmp_path)
    tools = MemoryMapTools(memory, files, "acme", global_read_only=True, source_catalog=catalog)
    assert "brief.pdf" in tools.memory_read(source_map_uri(*catalog[1]))
    assert f"{ROOT}/other/brief.md" in tools.memory_search("brief")
    assert tools.file_get(catalog[1][1])["original_name"] == "brief.pdf"
    before = memory.query(prefix=ROOT)
    global_map = MapBuilder(memory).global_read_map(source_catalog=catalog)
    assert global_map["scope"] == "global_read_only" and global_map["read_only"] is True
    assert {item["company_id"] for item in global_map["branches"] if item["kind"] == "2a"} == {"acme", "other"}
    assert memory.query(prefix=ROOT) == before
    with pytest.raises(PermissionError):
        tools.memory_read("sam://untrusted/outside")


# -- Employee reads of current FactsTree leaf URIs go through the read-only
#    TreeReadService ownership verification; failures never fall back. --

class LedgerFiles:
    """Only the ledger path is exposed; no archive objects are served here."""

    def __init__(self, path):
        self._db_path = path
        self.objects_path = None


def employee_tools(kernel, company=COMPANY):
    return MemoryMapTools(kernel.memory, LedgerFiles(db_path(kernel)), company)


def canonical_uri(company, code='L-bizov-08'):
    return tree_leaf_uri(company, GENERIC, GENERIC.leaf_for(code))


def test_employee_leaf_read_goes_through_tree_read_service(kernel, monkeypatch):
    published = publish(kernel)
    attribute(kernel)
    calls = []
    real = TreeReadService.read_leaf

    def spied(self, company_id, code):
        calls.append((company_id, code))
        return real(self, company_id, code)

    monkeypatch.setattr(TreeReadService, 'read_leaf', spied)
    tools = employee_tools(kernel)
    result = tools.memory_read(published['uri'])
    assert result.startswith(artifact())  # leaf body verbatim
    assert '系统读取信息（动态账本元数据，不属于叶正文）' in result  # ledger note appended
    assert calls == [(COMPANY, 'L-bizov-08')]
    assert published['uri'] in tools.navigation


def test_unbound_company_leaf_read_fails_closed(kernel):
    uri = canonical_uri('acc-unbound')
    kernel.memory.put(uri, '未绑定公司的残留内容', metadata={})
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel, 'acc-unbound').memory_read(uri)


def test_not_generated_leaf_read_fails_closed_without_fallback(kernel):
    uri = canonical_uri(COMPANY)  # bound pack, but the leaf was never published
    kernel.memory.put(uri, '未生成叶子的旧内容', metadata={})
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(uri)


def test_unknown_leaf_read_fails_closed(kernel):
    publish(kernel)
    attribute(kernel)
    uri = f"{ROOT}/{COMPANY}/{GENERIC.trunk_directory('bizov')}/leaves/L-bizov-99-不存在.md"
    kernel.memory.put(uri, '未知叶子的残留内容', metadata={})
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(uri)


@pytest.mark.parametrize('pack,version', [('generic', 2), ('generic-v2', 1)])
def test_wrong_pack_version_leaf_uri_fails_closed(kernel, pack, version):
    published = publish(kernel)
    attribute(kernel)
    uri = f"{tree_prefix(COMPANY)}{pack}/{version}/leaves/L-bizov-08-测试叶子.md"
    kernel.memory.put(uri, '过期地址的残留内容', metadata={})
    tools = employee_tools(kernel)
    with pytest.raises(FileNotFoundError):  # not the canonical bound-pack URI
        tools.memory_read(uri)
    assert tools.memory_read(published['uri']).startswith(artifact())  # canonical still served


def test_leaf_read_company_scope_cannot_be_chosen(kernel):
    publish(kernel)
    attribute(kernel)
    tools = employee_tools(kernel)  # scoped to the injected company only
    with pytest.raises(PermissionError):
        tools.memory_read(canonical_uri('acc-other'))
    with pytest.raises(PermissionError):
        tools.memory_read(f"{tree_prefix('acc-other')}generic/1/leaves/L-bizov-08-测试叶子.md")


def test_leaf_read_fails_closed_when_feature_gate_closes(kernel, monkeypatch):
    published = publish(kernel)
    attribute(kernel)
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    kernel.memory.put(published['uri'], '功能门关闭后的残留内容', metadata={})
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(published['uri'])
    with pytest.raises(PermissionError):  # the tree prefix leaves the scope too
        employee_tools(kernel).memory_read(
            f"{tree_prefix(COMPANY)}generic/1/leaves/L-bizov-08-测试叶子.md")


def test_unplaced_fact_reference_fails_closed(kernel):
    published = publish(kernel)  # the leaf cites fact 1, which has no placement
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(published['uri'])


def test_damaged_leaf_projection_fails_closed(kernel):
    published = publish(kernel)
    attribute(kernel)
    kernel.memory.put(published['uri'], artifact(title='篡改标题'), metadata={})
    with pytest.raises(FileNotFoundError):  # the tampered body is never served
        employee_tools(kernel).memory_read(published['uri'])


def test_leaf_read_failures_never_call_plain_memory_read(kernel, monkeypatch):
    published = publish(kernel)
    attribute(kernel)
    tools = employee_tools(kernel)
    reads = []
    real_read = kernel.memory.read

    def spied(uri):
        reads.append(uri)
        return real_read(uri)

    monkeypatch.setattr(kernel.memory, 'read', spied)
    assert tools.memory_read(published['uri']).startswith(artifact())
    reads.clear()
    with pytest.raises(FileNotFoundError):
        tools.memory_read(canonical_uri(COMPANY, 'L-bizov-07'))  # not generated
    assert reads == []  # a failed claim never touches plain memory.read


def test_leaf_reads_never_write(kernel):
    published = publish(kernel)
    attribute(kernel)
    ledger = Path(db_path(kernel)).read_bytes()
    writes = kernel.memory.writes
    tools = employee_tools(kernel)
    assert tools.memory_read(published['uri']).startswith(artifact())
    with pytest.raises(FileNotFoundError):
        tools.memory_read(canonical_uri(COMPANY, 'L-bizov-07'))
    with pytest.raises(FileNotFoundError):
        tools.memory_read(f"{tree_prefix(COMPANY)}generic/2/leaves/L-bizov-08-测试叶子.md")
    with pytest.raises(PermissionError):
        tools.memory_read(canonical_uri('acc-other'))
    assert kernel.memory.writes == writes
    assert Path(db_path(kernel)).read_bytes() == ledger


# -- 决策13-A: 员工 memory_read 在叶正文后附加系统读取信息（动态账本元数据） --

def test_employee_leaf_read_appends_ledger_note(kernel):
    published = publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET status='claimed', source_page=5, source_span='text/0' WHERE id=1")
    kernel.db.commit()
    result = employee_tools(kernel).memory_read(published['uri'])
    assert isinstance(result, str)  # return type stays text
    body, note = result.split('系统读取信息（动态账本元数据，不属于叶正文）')
    assert body.startswith(artifact())  # leaf body verbatim, note only appended
    assert 'F1' in note and '状态：claimed' in note
    assert '来源：sample.docx' in note and '页码：5' in note and '坐标：text/0' in note


def test_employee_leaf_read_note_never_fabricates_source(kernel):
    published = publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET source_file=NULL, source_page=NULL, source_span=NULL WHERE id=1")
    kernel.db.commit()
    result = employee_tools(kernel).memory_read(published['uri'])
    _, note = result.split('系统读取信息（动态账本元数据，不属于叶正文）')
    assert '来源：无来源记录' in note  # reported as missing, never fabricated
    assert '页码' not in note and '坐标' not in note  # no invented coordinates


def empty_data_leaf(body='无数字正文。[^one]'):
    """Zero formal fact references on a data-shaped (shape s) generic leaf.

    L-bizov-08 requires the 数据明细 section even when the leaf cites no
    facts, so the fixture keeps the required section order with a legal
    empty data table (header only, no rows) instead of dropping the section.
    """
    markdown = artifact(body=body)
    row = '| F1 | 测试指标 | 2025-01 | 9 | 个 | [^one] |\n'
    assert row in markdown  # the shared fixture's only data row
    return markdown.replace(row, '')


def test_employee_leaf_read_note_handles_leaf_without_facts(kernel):
    markdown = empty_data_leaf()
    published = publish(kernel, markdown, fact_ids=[])
    result = employee_tools(kernel).memory_read(published['uri'])
    assert result.startswith(markdown)
    _, note = result.split('系统读取信息（动态账本元数据，不属于叶正文）')
    assert '本叶当前无正式引用的事实' in note


# -- 决策13-A: 员工 memory_search 只走当前 TreeReadService.search_leaves --

def vector_tools(vector_kernel, company=COMPANY):
    return MemoryMapTools(vector_kernel.memory, LedgerFiles(db_path(vector_kernel)), company)


def test_employee_search_returns_only_formal_leaf_uris(vector_kernel):
    uri = indexed(vector_kernel)
    tools = vector_tools(vector_kernel)
    assert tools.memory_search('测试') == [uri]
    assert uri in tools.navigation


def test_employee_search_filters_are_exact_and_unknown_rejected(vector_kernel):
    uri = indexed(vector_kernel)
    tools = vector_tools(vector_kernel)
    assert tools.memory_search('测试', filters={'trunk_code': 'bizov'}) == [uri]
    assert tools.memory_search('测试', filters={'leaf_code': 'L-bizov-08'}) == [uri]
    assert tools.memory_search('测试', filters={'entity': '临时主体'}) == [uri]
    assert tools.memory_search('测试', filters={'entity': '不匹配'}) == []
    assert tools.memory_search('测试', filters={'company_id': COMPANY}) == [uri]
    with pytest.raises(ValueError):
        tools.memory_search('测试', filters={'module_key': 'bizov'})
    with pytest.raises(ValueError):
        tools.memory_search('测试', filters={'unknown': 'x'})
    with pytest.raises(ValueError):
        tools.memory_search('测试', filters={'company_id': 'acc-other'})


@pytest.mark.parametrize('code_filter', [{'trunk_code': 'bizov'}, {'leaf_code': 'L-bizov-08'}])
def test_employee_search_filters_tree_codes_refused_when_gate_closed(tmp_path, monkeypatch, code_filter):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    memory, files, _, catalog = source_setup(tmp_path)
    calls = []
    real_search = memory.search

    def spied(*args, **kwargs):
        calls.append((args, kwargs))
        return real_search(*args, **kwargs)

    monkeypatch.setattr(memory, 'search', spied)
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    with pytest.raises(MemoryUnavailable):  # explicit refusal, never a wider legacy search
        tools.memory_search('report', filters=code_filter)
    assert calls == []  # the closed gate never falls back to plain memory.search
    assert tools.navigation == []


def test_employee_search_filters_absent_keeps_legacy_search_when_gate_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    memory, files, _, catalog = source_setup(tmp_path)
    tools = MemoryMapTools(memory, files, "acme", source_catalog=catalog)
    assert tools.memory_search('report') == [f"{ROOT}/acme/brief.md"]


def test_employee_search_fact_filters_match_claimed_and_exclude_expired(vector_kernel):
    uri = indexed(vector_kernel)
    vector_kernel.db.execute("UPDATE facts SET status='claimed' WHERE id=1")
    vector_kernel.db.commit()
    tools = vector_tools(vector_kernel)
    assert tools.memory_search('测试', filters={'entity': '临时主体'}) == [uri]
    vector_kernel.db.execute("UPDATE facts SET valid_to='2025-12' WHERE id=1")
    vector_kernel.db.commit()
    assert tools.memory_search('测试', filters={'entity': '临时主体'}) == []


def test_employee_search_never_falls_back_to_plain_memory(kernel, monkeypatch):
    publish(kernel)
    attribute(kernel)

    def forbidden(*args, **kwargs):
        raise AssertionError('employee tree search must not touch plain memory.search')

    monkeypatch.setattr(kernel.memory, 'search', forbidden, raising=False)
    tools = employee_tools(kernel)  # DocumentPort memory cannot join the snapshot
    with pytest.raises(MemoryUnavailable):  # storage conflict reported, never a fallback
        tools.memory_search('测试')


def test_global_read_scope_keeps_legacy_search_with_gate_open(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)
    memory, files, _, catalog = source_setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError('global read-only search must not use the single-company tree read')

    monkeypatch.setattr(TreeReadService, 'search_leaves', forbidden)
    tools = MemoryMapTools(memory, files, "acme", global_read_only=True, source_catalog=catalog)
    assert f"{ROOT}/other/brief.md" in tools.memory_search("brief")


# -- 决策13-A: F<ID>.md 直接读取改由 TreeReadService 按绑定/身份/归属核验 --

def test_employee_fact_read_goes_through_tree_verification(kernel):
    published = publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET source_page=2, source_span='text/9' WHERE id=1")
    kernel.db.commit()
    result = employee_tools(kernel).memory_read(fact_uri(COMPANY, 1, module_key='bizov'))
    assert isinstance(result, str)  # return type stays text
    card = json.loads(result)
    assert card['fact']['fact_id'] == 1 and card['fact']['status'] == 'verified'
    assert card['fact']['source']['original_name'] == 'sample.docx'
    assert card['fact']['source']['page'] == 2 and card['fact']['source']['span'] == 'text/9'
    assert card['leaf']['code'] == 'L-bizov-08' and card['leaf']['uri'] == published['uri']


def test_employee_fact_read_allows_claimed(kernel):
    publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET status='claimed' WHERE id=1")
    kernel.db.commit()
    card = json.loads(employee_tools(kernel).memory_read(fact_uri(COMPANY, 1, module_key='bizov')))
    assert card['fact']['status'] == 'claimed'


def test_employee_fact_read_ignores_legacy_module_key(kernel):
    publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET module_key='hr' WHERE id=1")  # stale legacy hint
    kernel.db.commit()
    uri = f"sam://facts/{COMPANY}/bizov/F1.md"  # ownership comes from the placement
    card = json.loads(employee_tools(kernel).memory_read(uri))
    assert card['fact']['fact_id'] == 1 and card['leaf']['code'] == 'L-bizov-08'


@pytest.mark.parametrize('damage', [
    "UPDATE facts SET status='inferred' WHERE id=1",
    "UPDATE facts SET valid_to='2025-12' WHERE id=1",
    "UPDATE facts SET superseded_by=1 WHERE id=1",
])
def test_employee_fact_read_rejects_unreadable(kernel, damage):
    publish(kernel)
    attribute(kernel)
    kernel.db.execute(damage)
    kernel.db.commit()
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(fact_uri(COMPANY, 1, module_key='bizov'))


def test_employee_fact_read_requires_verified_placement(kernel):
    publish(kernel)  # fact 1 has no identity/placement at all
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(fact_uri(COMPANY, 1, module_key='bizov'))
    with pytest.raises(FileNotFoundError):  # unknown fact id
        employee_tools(kernel).memory_read(fact_uri(COMPANY, 99, module_key='bizov'))


def test_employee_fact_read_fails_closed_when_gate_closes(kernel, monkeypatch):
    publish(kernel)
    attribute(kernel)
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(FileNotFoundError):
        employee_tools(kernel).memory_read(fact_uri(COMPANY, 1, module_key='bizov'))


def test_employee_fact_read_never_crosses_company(kernel):
    publish(kernel)
    attribute(kernel)
    tools = employee_tools(kernel)
    with pytest.raises(PermissionError):  # foreign company URI is outside the scope
        tools.memory_read(fact_uri('acc-other', 1, module_key='bizov'))
    global_tools = MemoryMapTools(kernel.memory, LedgerFiles(db_path(kernel)), COMPANY,
                                  global_read_only=True)
    with pytest.raises(FileNotFoundError):  # the global exception stays isolated
        global_tools.memory_read(fact_uri('acc-other', 1, module_key='bizov'))
    with pytest.raises(FileNotFoundError):
        global_tools.memory_read(fact_uri(COMPANY, 1, module_key='bizov'))
