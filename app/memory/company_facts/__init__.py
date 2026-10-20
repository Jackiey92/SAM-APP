"""Rebuildable, source-backed 2b fact layer."""

from .l2_fact_schema import FactRecord, SourceRef
from .l2_cap_table import CapTableEvent, CapTableStore, replay_cap_table
from .l2_derivation import FactDerivation
from .l2_fact_extractor import ExtractedFact
from .l2_complete_facts import L2CompleteFactService
from .l2_consolidation import ConsolidationResult, ConsolidationService
from .l1_brief import L1BriefService
from .l0_guide import L0GuideService
from .l2_projection import L2ProjectionService

__all__ = [
    "FactRecord", "SourceRef", "CapTableEvent", "CapTableStore",
    "replay_cap_table", "FactDerivation",
    "ExtractedFact", "ConsolidationResult", "L1BriefService", "L0GuideService",
    "L2CompleteFactService",
    "ConsolidationService",
    "L2ProjectionService",
]
