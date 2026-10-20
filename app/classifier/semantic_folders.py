"""Semantic resource-folder boundary owned by the file-processing employee."""
from __future__ import annotations

from pathlib import Path

from ..employees import SemanticDecisionUnavailable, SemanticEmployee
from ..module_taxonomy import MODULES, validated_module

# Storage paths are protocol constants. Their meaning belongs to the skill.
FOLDERS = tuple(m.directory for m in MODULES)


def classify(*, text: str, employee: SemanticEmployee | None = None,
             company_id: str = "default", thread_id: str | None = None,
             skills_root: str | Path = "skills") -> str:
    """Ask the employee for a folder; fail closed when no decision exists."""
    del skills_root
    if employee is None:
        return "unclassified"
    try:
        result = employee.classify_document(
            text=text, company_id=company_id, thread_id=thread_id,
        )
    except (SemanticDecisionUnavailable, OSError, TypeError, ValueError):
        return "unclassified"
    module = validated_module(result.get("module")) if isinstance(result, dict) else None
    # Key -> directory is structural addressing of an employee choice, not inference.
    return next((m.directory for m in MODULES if m.key == module), "unclassified")
