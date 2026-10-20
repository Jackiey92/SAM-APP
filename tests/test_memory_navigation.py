from app.memory_navigation import MemoryNavigationService
from app.ports import LocalMemoryProvider


def routes():
    return {"resource_uri": "sam://original/acc-test/sample.md", "sidecar_uris": {
        "abstract_uri": "sam://facts/acc-test/bizov/sample/L0/abstract.md",
        "overview_uri": "sam://facts/acc-test/bizov/sample/L1/overview.md",
    }}


def test_reads_exact_sam_coordinates(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    manifest = routes()
    for key, uri in manifest["sidecar_uris"].items():
        memory.put(uri, key)
    result = MemoryNavigationService(memory=memory).read_sidecars(manifest, company_id="acc-test")
    assert result["abstract"] == "abstract_uri"
    assert result["overview"] == "overview_uri"
    assert result["provenance"] == "sam.navigation"


def test_no_fallback_to_unregistered_sidecars(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    manifest = routes()
    resource = manifest["resource_uri"]
    memory.put(resource + "/.abstract.md", "OV")
    memory.put(resource + "/.overview.md", "OV")
    service = MemoryNavigationService(memory=memory)
    assert service.read_sidecars(manifest, company_id="acc-test") is None
    assert service.read_sidecars({"resource_uri": resource}, company_id="acc-test") is None
    manifest["sidecar_uris"] = {
        "abstract_uri": resource + "/.abstract.md",
        "overview_uri": resource + "/.overview.md",
    }
    assert service.read_sidecars(manifest, company_id="acc-test") is None


def test_sidecars_are_not_ready_until_both_files_exist(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    manifest = routes()
    memory.put(manifest["sidecar_uris"]["abstract_uri"], "摘要")
    assert MemoryNavigationService(memory=memory).read_sidecars(manifest, company_id="acc-test") is None
