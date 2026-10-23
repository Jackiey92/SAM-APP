import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.memory.company_facts.paths import fact_uri
from app.memory_map import MapBuilder, MemoryMapTools, ROOT, source_map_uri
from app.ports import LocalMemoryProvider
from app.tool_bridge import ToolBridge, handle_request
from tests.test_facts_tree_leaves import kernel, publish, COMPANY
from tests.test_facts_tree_tree_read import attribute, db_path
from tests.test_memory_map import LedgerFiles, employee_tools, source_setup


def test_empty_map_and_tool_construction_never_touch_ov(tmp_path, monkeypatch):
    memory = LocalMemoryProvider(tmp_path / "ov")
    for name in ("query", "read", "put", "list_facts", "search"):
        monkeypatch.setattr(memory, name, lambda *a, **kw: pytest.fail("navigation called OV"))
    bridge = ToolBridge(memory, object(), company_id="acc-empty", thread_id="map")
    result = handle_request(bridge, {"id": 1, "method": "sam_memory_map", "params": {}})
    assert result["ok"] is True and len(result["result"]["branches"]) == 2


@pytest.mark.parametrize("global_read_only", [False, True])
def test_removed_namespace_is_denied_without_provider_call(tmp_path, monkeypatch, global_read_only):
    memory = LocalMemoryProvider(tmp_path)
    monkeypatch.setattr(memory, "read", lambda *_: pytest.fail("removed 2A URI reached OV"))
    tools = MemoryMapTools(memory, object(), "acme", global_read_only=global_read_only)
    with pytest.raises(PermissionError):
        tools.memory_read(f"{ROOT}/2a_extraction/acme/source/L2/manifest.json")


def test_scoped_external_source_read_uses_corrected_source_map_and_not_ov(tmp_path, monkeypatch):
    memory, files, maps, catalog = source_setup(tmp_path)
    owner, digest = catalog[0]
    maps.replace(file_hash=digest, company_id=owner, target_locator="page=1; locator=text/0",
                 replacement_text="经理校正正文", confirm=True)
    monkeypatch.setattr(memory, "read", lambda *_: pytest.fail("source read reached OV"))
    bridge = ToolBridge(memory, files, company_id=owner, thread_id="map", source_catalog=catalog)
    result = bridge.dispatch("sam_memory_read", {"uri": source_map_uri(owner, digest)})
    assert "经理校正正文" in result and "4820" not in result


def test_l2_fact_drilldown_uses_exact_scoped_ledger_coordinate(kernel):
    # Decision 13-A: F<ID>.md drill-down is served only through the verified
    # TreeReadService chain (company scope, identity, unique placement, formal
    # leaf reference) — never a direct SQLite facts row read. The kernel
    # fixture keeps SAM_FACTS_TREE enabled only inside this test.
    published = publish(kernel)
    attribute(kernel)
    kernel.db.execute("UPDATE facts SET source_page=2, source_span='sheet/财务/C2' WHERE id=1")
    kernel.db.commit()
    tools = employee_tools(kernel)
    card = json.loads(tools.memory_read(fact_uri(COMPANY, 1, module_key="bizov")))
    assert card["company_id"] == COMPANY and card["fact"]["fact_id"] == 1
    assert card["fact"]["source"]["page"] == 2 and card["fact"]["source"]["span"] == "sheet/财务/C2"
    assert card["leaf"]["code"] == "L-bizov-08" and card["leaf"]["uri"] == published["uri"]
    with pytest.raises(PermissionError):
        tools.memory_read(fact_uri("acc-other", 1, module_key="bizov"))
    global_tools = MemoryMapTools(kernel.memory, LedgerFiles(db_path(kernel)), COMPANY,
                                  global_read_only=True)
    with pytest.raises(FileNotFoundError):
        global_tools.memory_read(fact_uri("acc-other", 1, module_key="bizov"))  # ID cannot override URI company
    with pytest.raises(FileNotFoundError):
        global_tools.memory_read(fact_uri(COMPANY, 1, module_key="bizov"))
    with pytest.raises(PermissionError):
        tools.memory_read(ROOT + "/2b/acme/../other/L1/brief.md")


def test_real_python_tool_bridge_map_finishes_without_connector_or_rebuild(tmp_path):
    # The actual child interpreter/import/config/JSON-RPC path used by Node.
    # No model or memory transport is mocked; they must never be invoked.
    env = dict(os.environ, SAM_MAIN_DB=str(tmp_path / "absent.db"), SAM_THREAD_ID="test-map",
               SAM_COMPANY_ID="acc-map", SAM_MEMORY_BACKEND="localvector")
    result = subprocess.run([sys.executable, "-m", "app.tool_bridge"],
                            input='{"id":1,"method":"sam_memory_map","params":{}}\n',
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["ok"] is True and response["result"]["company_id"] == "acc-map"
    assert "2a_extraction" not in result.stdout
    assert not (tmp_path / "absent.db").exists()


def test_no_runtime_code_generates_retired_2a_addresses():
    root = Path(__file__).resolve().parents[1]
    for directory in ("app", "webapp", "skills", "harness-openclaw/plugins", "harness-openclaw/skill-registry", "harness-openclaw/tool-registry"):
        for path in (root / directory).rglob("*"):
            if path.suffix in {".py", ".js", ".json", ".md"}:
                assert "2a_extraction" not in path.read_text(), path
