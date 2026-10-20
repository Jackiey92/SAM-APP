"""Exact, host-scoped SAM navigation; no inferred resource fallbacks."""
from typing import Any
from .memory import paths
from .ports import MemoryProvider, MemoryUnavailable

class MemoryNavigationService:
    def __init__(self, db_path=None, memory: MemoryProvider | None = None):
        self.db_path, self.memory = db_path, memory

    def read_sidecars(self, manifest: dict[str, Any], *, company_id: str) -> dict[str, Any] | None:
        routes = manifest.get("sidecar_uris")
        if self.memory is None or not isinstance(routes, dict):
            return None
        abstract_uri, overview_uri = routes.get("abstract_uri"), routes.get("overview_uri")
        try:
            abstract = paths.parse(abstract_uri, company_id=company_id)
            overview = paths.parse(overview_uri, company_id=company_id)
        except (TypeError, ValueError):
            return None
        if (abstract.area != "facts" or overview.area != "facts"
                or not abstract.uri.endswith("/L0/abstract.md")
                or overview.uri != abstract.uri.removesuffix("/L0/abstract.md") + "/L1/overview.md"):
            return None
        try:
            abstract_text = self.memory.read(abstract.uri)
            overview_text = self.memory.read(overview.uri)
        except (FileNotFoundError, MemoryUnavailable, OSError):
            return None
        return {"resource_uri": manifest.get("resource_uri"), "abstract_uri": abstract.uri,
                "overview_uri": overview.uri, "abstract": abstract_text,
                "overview": overview_text, "provenance": "sam.navigation"}
