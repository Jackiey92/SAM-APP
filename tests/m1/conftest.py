"""Collect M1's hyphen-named tests without changing existing pytest settings."""
import pytest


def pytest_collect_file(file_path, parent):
    if file_path.name == "test-memory.py":
        return pytest.Module.from_parent(parent, path=file_path)
