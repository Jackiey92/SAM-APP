import json

from app.memory.user_memory.l2_conversation import ConversationStore
from app.ports import LocalMemoryProvider, MemoryUnavailable


def test_append_repairs_provider_that_strips_trailing_newline(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    store = ConversationStore(memory)
    store.append("acme", "t", role="user", text="one", turn_id="1")
    uri = store.turns_uri("acme", "t")
    memory.put(uri, memory.read(uri).rstrip("\n"))
    store.append("acme", "t", role="assistant", text="two", turn_id="2")
    assert [row["turn_id"] for row in store.read_range("acme", "t")] == ["1", "2"]


def test_missing_conversation_is_empty(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    assert ConversationStore(memory).read_range("acc-gudou", "missing") == []


def test_unavailable_provider_is_not_empty(tmp_path):
    class Unavailable:
        def read(self, uri): raise MemoryUnavailable("synthetic failure")
    import pytest
    with pytest.raises(MemoryUnavailable):
        ConversationStore(Unavailable()).read_range("acc-gudou", "missing")
