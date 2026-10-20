"""Compatibility adapter for employee-owned document classification."""
from __future__ import annotations

from dataclasses import dataclass

from app.employees import SemanticDecisionUnavailable, SemanticEmployee
from app.module_taxonomy import validated_module
from app.db.schema import DOC_TYPES


@dataclass(frozen=True)
class ClassInfo:
    module: str | None
    doc_type: str | None
    confidence: float


def classify(filename: str, *, employee: SemanticEmployee | None = None,
             company_id: str = "default") -> ClassInfo:
    """Classify parsed content through the employee, never a filename hint."""
    del filename
    if employee is None:
        return ClassInfo(None, None, 0.0)
    try:
        result = employee.classify_document(text="", company_id=company_id)
    except (SemanticDecisionUnavailable, OSError, TypeError, ValueError):
        return ClassInfo(None, None, 0.0)
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    doc_type = result.get("doc_type")
    if not isinstance(doc_type, str) or doc_type not in {code for code, _, _ in DOC_TYPES}:
        doc_type = None
    return ClassInfo(validated_module(result.get("module")), doc_type, confidence)
