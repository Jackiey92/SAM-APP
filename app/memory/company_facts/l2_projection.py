"""Storage-only SAM projection of employee-verified SQLite L2 facts."""
from __future__ import annotations

import json

from ...memory_paths import MEMORY_ROOT
from .l2_consolidation import ConsolidationService
from .l1_brief import projection_lock
from .paths import fact_uri


class L2ProjectionService:
    def __init__(self, db_path, memory):
        self.facts = ConsolidationService(db_path)
        self.memory = memory
        self.root = getattr(memory, "memory_root", MEMORY_ROOT)

    def publish(self, *, company_id):
        # A projection contains employee conclusions and provenance only, not
        # the unfiltered 2A manifest/body. SQLite remains the write authority;
        # Projection writes happen after the short local transaction has committed.
        with projection_lock(self.root, company_id):
            facts = self.facts.list_verified_facts(company_id=company_id)
            if hasattr(self.memory, "rebuild_projection"):
                self.memory.rebuild_projection(company_id)
                return len(facts)
            for fact in facts:
                self.memory.put(fact_uri(company_id, fact["id"], root=self.root, module_key=fact.get("module_key")),
                                json.dumps(fact, ensure_ascii=False, sort_keys=True),
                                metadata={"company_id": company_id, "layer": "2b_L2"})
            return len(facts)
