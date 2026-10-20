from app.memory_map import MapBuilder, source_map_uri
from app.ports import LocalMemoryProvider


class LaggyListingMemory(LocalMemoryProvider):
    def query(self, **_):
        raise AssertionError("navigation must never enumerate OV")


def test_known_source_is_found_by_exact_read_while_ls_lags(tmp_path):
    memory = LaggyListingMemory(tmp_path)
    catalog = (("acme", "b" * 64),)
    result = MapBuilder(memory, source_catalog=catalog).rebuild_map("acme", source_ids=("b" * 64,))
    assert [item["uri"] for item in result.map["branches"] if item["kind"] == "2a"] == [source_map_uri(*catalog[0])]
    again = MapBuilder(memory, source_catalog=catalog).load_or_rebuild("acme")
    assert again.map["branches"] == result.map["branches"]
    assert result.map["branches"][0]["external"] is True
