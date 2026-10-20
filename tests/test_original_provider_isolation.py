"""Physical archive boundary and local-only cleanup safety regression tests."""
import ast
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROTECTED = ("app/storage", "app/memory/archive")
FORBIDDEN = re.compile(r"MemoryProvider|ExternalMemory|memory_paths|add_resource_to|memory\.put")


def assert_external_only(source, filename):
    assert not FORBIDDEN.search(source), f"memory dependency in {filename}"
    for node in ast.walk(ast.parse(source, filename=str(filename))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "", *(alias.name for alias in node.names)]
        else:
            continue
        assert not any(
            module.split(".")[-1] in {"ports", "memory_navigation", "memory_paths"}
            or "external_memory_vendor" in module.lower() or "memoryprovider" in module.lower()
            for module in modules
        ), f"memory import in {filename}"


def test_archive_storage_has_no_provider_dependency():
    for directory in PROTECTED:
        for path in (ROOT / directory).rglob("*.py"):
            assert_external_only(path.read_text(encoding="utf-8"), path)


@pytest.mark.parametrize("source", [
    "from app.ports import MemoryProvider",
    "from ...ports import LocalMemoryProvider as Store",
    "import app.memory_paths as paths",
    "from app import memory_paths",
    "import external_memory_vendor",
    "store.add_resource_to('source', 'uri')",
    "memory.put('uri', 'content')",
])
def test_physical_guard_rejects_injected_dependency(tmp_path, source):
    # Exercise the very same scanner on an actual protected-directory fixture.
    path = tmp_path / "app" / "storage" / "archive_store.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError):
        assert_external_only(path.read_text(encoding="utf-8"), path)


def test_repository_has_no_deleted_visual_module_import():
    excluded = {".git", ".sam-isolated", ".venv", "venv", "node_modules", "__pycache__"}
    deleted = "app.memory." + "visual_extraction"
    for path in ROOT.rglob("*.py"):
        if excluded.intersection(path.relative_to(ROOT).parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                assert all(alias.name != deleted for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module != deleted, path
                assert not (module == "app.memory" and any(
                    alias.name == deleted.rsplit(".", 1)[1] for alias in node.names
                )), path
    assert not (ROOT / "app/memory" / (deleted.rsplit(".", 1)[1] + ".py")).exists()
