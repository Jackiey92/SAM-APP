"""Small capability seams for replaceable runtime, model, and memory backends.

The product layer depends on these protocols rather than on OpenClaw, a model
vendor, or a memory vendor.  Concrete implementations may be local, remote, or
test doubles without changing the business code.
"""
from __future__ import annotations

from pathlib import Path
import json
import shutil
from collections.abc import Sequence
from typing import Any, Protocol
from .memory_paths import MEMORY_ROOT



class RuntimeProvider(Protocol):
    def supports(self, format: str) -> bool:
        """Whether a configured ingest skill handles this format."""

    def run_parse(self, file_hash: str, format: str, timeout: int = 600) -> int:
        """Run the configured parser and return a staging record id."""

    def run_agent_message(self, message: str, *, context_text: str | None = None,
                          company_id: str | None = None, thread_id: str | None = None,
                          allow_promote: bool = False, timeout: int = 600,
                          access_role: str = "employee") -> str:
        """Run one agent turn and return its serialized result."""


class ModelProvider(Protocol):
    def complete_json(self, *, system_prompt: str, payload: dict[str, Any],
                      image_urls: list[str] | None = None) -> dict[str, Any]:
        """Return a JSON object from a configured model endpoint."""


class ModelUnavailable(RuntimeError):
    """The configured model provider could not return a valid response."""


class RetryableRuntimeError(RuntimeError):
    """A runtime dependency is not ready yet and the job may be retried."""

    error_kind = "runtime"


class MemoryProvider(Protocol):
    def add_resource(self, path: str, *, parent: str, wait: bool = True) -> None:
        """Import a source file into the configured semantic folder."""

    def add_resource_to(self, path: str, target_uri: str, *, wait: bool = True,
                        timeout: int = 600) -> Any:
        """Import a source file to an exact, caller-owned URI."""

    def ensure_directory(self, uri: str) -> None:
        """Ensure a semantic parent directory exists."""

    def wait_for_resource(self, uri: str, *, timeout: int = 600, interval: float = 2.0) -> None:
        """Poll asynchronous resource processing until semantic navigation is ready."""

    def put(self, uri: str, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        """Persist a memory document at a provider-specific URI."""

    def read(self, uri: str) -> str:
        """Read one memory document."""

    def query(self, *, prefix: str, query: str | None = None) -> list[dict[str, Any]]:
        """Query memory without exposing provider-specific internals."""

    def search(self, query: str, *, prefix: str = "sam://") -> list[dict[str, Any]]:
        """Search L0/L1/L2 memory through a provider-neutral operation."""

    def put_fact(self, company_id: str, fact_key: str, fact: dict[str, Any]) -> None:
        """Write a verified 2b fact under a company-scoped key."""

    def get_fact(self, company_id: str, fact_key: str) -> dict[str, Any] | None:
        """Read one 2b fact by deterministic company/key address."""

    # Legacy adapter signature retained; new providers use sam://facts/ and get_fact.
    def get_2b(self, company_id: str, key: str) -> dict[str, Any] | None:
        """Read one verified 2b value; pending/conflicting data is excluded."""

    def delete(self, uri: str, *, recursive: bool = False) -> None:
        """Delete an explicitly scoped runtime-memory resource."""

    def list_facts(self, company_id: str, *, fact_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
        """List verified 2b facts for one company."""


class LocalMemoryProvider:
    """Minimal filesystem reference implementation for local development.

    It is a test/reference adapter, not the production vector store.
    """

    def __init__(self, root: str | Path, *, memory_root: str = MEMORY_ROOT):
        self.root = Path(root)
        self.memory_root = memory_root.rstrip("/")

    def _path(self, uri: str) -> Path:
        if not isinstance(uri, str) or not uri.startswith("sam://"):
            raise ValueError("memory URI must use the SAM logical scheme")
        relative = uri.removeprefix("sam://").lstrip("/")
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError("memory URI escapes local memory root")
        return path

    def add_resource(self, path: str, *, parent: str, wait: bool = True) -> None:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(path)
        target = self._path(parent.rstrip("/") + "/" + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def add_resource_to(self, path: str, target_uri: str, *, wait: bool = True,
                        timeout: int = 600) -> None:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(path)
        target = self._path(target_uri)
        if target.exists():
            raise FileExistsError(target_uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def ensure_directory(self, uri: str) -> None:
        path = self._path(uri)
        path.mkdir(parents=True, exist_ok=True)

    def wait_for_resource(self, uri: str, *, timeout: int = 600, interval: float = 2.0) -> None:
        return None

    def put(self, uri: str, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        path = self._path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, uri: str) -> str:
        return self._path(uri).read_text(encoding="utf-8")

    def query(self, *, prefix: str, query: str | None = None) -> list[dict[str, Any]]:
        root = self._path(prefix)
        if not root.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in root.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if query is None or query.lower() in text.lower():
                    results.append({"uri": "sam://" + str(path.relative_to(self.root)), "content": text})
        return results

    def put_fact(self, company_id: str, fact_key: str, fact: dict[str, Any]) -> None:
        if fact.get("status") != "verified":
            raise ValueError("only verified facts may enter the 2b memory provider")
        self.put(
            f"{self.memory_root}/2b_facts/{company_id}/{fact_key}.json",
            json.dumps(fact, ensure_ascii=False, sort_keys=True),
            metadata={"layer": "2b", "company_id": company_id, "fact_key": fact_key},
        )

    def get_fact(self, company_id: str, fact_key: str) -> dict[str, Any] | None:
        uri = f"{self.memory_root}/2b_facts/{company_id}/{fact_key}.json"
        try:
            return json.loads(self.read(uri))
        except FileNotFoundError:
            return None

    def search(self, query: str, *, prefix: str = "sam://") -> list[dict[str, Any]]:
        return self.query(prefix=prefix, query=query)

    def get_2b(self, company_id: str, key: str) -> dict[str, Any] | None:
        return self.get_fact(company_id, key)

    def delete(self, uri: str, *, recursive: bool = False) -> None:
        path = self._path(uri)
        if path.is_dir():
            if not recursive:
                raise ValueError("refusing to delete a directory without recursive=True")
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def list_facts(self, company_id: str, *, fact_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
        prefix = f"{self.memory_root}/2b_facts/{company_id}"
        facts = []
        seen: set[str] = set()
        for key in fact_keys:
            try:
                fact = self.get_fact(company_id, str(key))
            except Exception:
                fact = None
            if isinstance(fact, dict) and fact.get("status") == "verified":
                fact.setdefault("fact_key", str(key))
                fact.setdefault("uri", f"{prefix}/{key}.json")
                facts.append(fact)
                seen.add(str(key))
        for item in self.query(prefix=prefix):
            uri = item.get("uri", "")
            if not uri.endswith(".json"):
                continue
            try:
                fact = json.loads(item.get("content", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(fact, dict) and fact.get("status") == "verified":
                fact.setdefault("fact_key", uri.rsplit("/", 1)[-1][:-5])
                fact.setdefault("uri", uri)
                if fact["fact_key"] not in seen:
                    facts.append(fact)
                    seen.add(fact["fact_key"])
        return sorted(facts, key=lambda value: str(value.get("fact_key", "")))


class RerankProvider(Protocol):
    """Optional candidate ordering only. No external provider is enabled by default."""
    external: bool

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ...


class MemoryUnavailable(RuntimeError):
    """Raised by a memory adapter when its configured endpoint is unavailable."""

    def __init__(self, detail: str, *, retryable: bool = False):
        super().__init__(detail)
        self.retryable = retryable
