"""Source-backed employee candidate data; no code-side fact extraction."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedFact:
    company_id: str
    metric: str
    period: str
    value: str
    value_type: str
    unit: str | None
    source_file: str
    source_page: int | None
    source_span: str | None
    confidence: float
    critical: bool
    review_reason: str | None = None
    entity: str | None = None
    # Host-owned source/coordinates plus verbatim employee decision. Kept
    # through verification so review/conflict todos never lose the evidence.
    handoff: dict | None = None
    module_key: str | None = None

    @property
    def coordinate(self) -> tuple[str, str, str]:
        return self.company_id, self.metric, self.period

    @property
    def entity_coordinate(self) -> tuple[str, str, str, str]:
        return self.company_id, self.entity or self.company_id, self.metric, self.period
