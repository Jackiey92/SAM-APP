"""Four-layer resident prompt assembler with conservative budgets."""
from __future__ import annotations

import json
import os
from typing import Any

from .memory.user_memory.l2_conversation import ConversationStore, partition_turns
from .memory_map import MapBuilder
from .memory.company_facts.l0_guide import L0GuideService
from .thread_manager import ThreadManager

KEEP_FIRST = 2
KEEP_LAST = 6


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


class ContextAssembler:
    def __init__(self, memory, *, company_id: str, thread_id: str, agent_config: str = "", budget: int | None = None, source_catalog=()):
        self.memory, self.company_id, self.thread_id = memory, company_id, thread_id
        self.agent_config = agent_config
        self.budget = budget if budget is not None else int(os.environ.get("SAM_CTX_RESIDENT_TOKEN_BUDGET", "6000"))
        self.maps = MapBuilder(memory, source_catalog=source_catalog)
        self.conversations = ConversationStore(memory)

    def assemble(self, question: str, *, tool_calls: int = 0) -> dict[str, Any]:
        result = self.maps.load_or_rebuild(self.company_id)
        branches = result.map.get("branches", [])
        # Lazy map loading: select at most two topic branches; a map itself is
        # always resident, branch bodies are never copied here.
        words = set(question.lower().split())
        selected = [b for b in branches if any(word in str(b.get("title", "")).lower() for word in words)][:2]
        if not selected:
            selected = branches[:2]
        map_view = {**result.map, "branches": selected, "lazy": True}
        # Fixed taxonomy is navigation metadata, not resident branch bodies.
        map_view.pop("modules", None)
        map_view.pop("special_directories", None)
        folded = partition_turns(self.conversations.read_range(self.company_id, self.thread_id), keep_first=KEEP_FIRST, keep_last=KEEP_LAST)
        layers = {"agent_config": self.agent_config, "memory_map": map_view, "conversation": folded, "question": question}
        try:
            guide = L0GuideService(self.memory).read_guide(company_id=self.company_id)
        except Exception:
            guide = ""
            map_view["degraded"] = True
        if guide:
            layers["fact_guide"] = guide
        prompt = self.render(layers)
        folded_count = folded.get("folded_count", 0)
        while estimate_tokens(prompt) > self.budget and folded.get("pointers"):
            folded["pointers"] = folded["pointers"][1:]
            prompt = self.render(layers)
        while estimate_tokens(prompt) > self.budget and map_view.get("branches"):
            map_view["branches"] = map_view["branches"][:-1]
            prompt = self.render(layers)
        while estimate_tokens(prompt) > self.budget and layers.get("fact_guide"):
            # Drop only a trailing complete L0 entry, not half a number/link.
            parts = layers["fact_guide"].rstrip().rsplit("\n\n", 1)
            layers["fact_guide"] = parts[0] + "\n\n" if len(parts) > 1 else ""
            if not layers["fact_guide"]:
                layers.pop("fact_guide")
            prompt = self.render(layers)
        return {"prompt": prompt, "layers": layers, "stats": {"tokens": estimate_tokens(prompt), "branch_count": len(map_view.get("branches", [])), "folded_blocks": folded_count, "tool_calls": tool_calls}}

    @staticmethod
    def render(layers: dict[str, Any]) -> str:
        return "\n\n".join((
            "[agent_config]\n" + layers.get("agent_config", ""),
            "[memory_map]\n" + json.dumps(layers.get("memory_map", {}), ensure_ascii=False, sort_keys=True),
            *(["[fact_guide]\n" + layers["fact_guide"]] if layers.get("fact_guide") else []),
            "[conversation]\n" + json.dumps(layers.get("conversation", {}), ensure_ascii=False, sort_keys=True),
            "[current_question]\n" + str(layers.get("question", "")),
        ))
