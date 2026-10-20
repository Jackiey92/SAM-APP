"""External SourceMap navigation contracts, with no OV 2A legacy fixtures."""
from dataclasses import dataclass
import json

import pytest

from app.db import init_db
from app.harness.staging import StagingStore
from app.memory_map import MapBuilder, MemoryMapTools, ROOT, source_map_uri
from app import settings
from app.ports import LocalMemoryProvider
from app.storage import ArchiveFileStore
from app.storage.mapping_store import SourceMapService


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
    assert uri in tools.memory_search("pack-scoped finance")
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
