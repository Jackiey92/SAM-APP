"""Read-only controlled embedding catalog; selection never probes a provider."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path


class EmbeddingCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingSpec:
    resource_id: str
    provider_kind: str
    embedder: str
    model_version: str
    dim: int
    base_url: str | None
    api_key: str | None = field(repr=False)
    status: str


@dataclass(frozen=True)
class Record:
    """Recursively immutable JSON object with attribute and key access."""
    _items: tuple

    def __getitem__(self, key):
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    default_resource_id: str
    resources: tuple[Record, ...]


_ENTRY_KEYS = set("resource_id display_name provider_kind modality status is_default connection capabilities limits pricing evidence".split())
_SUB_KEYS = {
    "connection": set("base_url api_key_env model_id protocol".split()),
    "capabilities": set("dim_default dims_allowed context_tokens sparse input_modes text_type_param".split()),
    "limits": {"rpm", "tpm"},
    "pricing": {"currency", "per_million_input", "per_million_image"},
    "evidence": {"source_url", "reviewed_at", "notes"},
}
_STATUSES = {"registered", "pending_verification", "enabled", "suspended", "quarantined", "retired"}


def _require(condition):
    if not condition:
        # Do not echo untrusted catalog contents (which could contain a secret).
        raise EmbeddingCatalogError("invalid embedding catalog")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _keys(value, expected):
    _require(isinstance(value, dict) and set(value) == expected)


def _freeze(value):
    if isinstance(value, dict):
        return Record(tuple((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def load_embedding_catalog(path=None) -> Catalog:
    path = Path(path) if path is not None else Path(__file__).resolve().parent / "embeddings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise EmbeddingCatalogError("cannot read embedding catalog") from None
    _keys(data, {"schema_version", "default_resource_id", "resources"})
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    _require(_text(data["default_resource_id"]))
    resources = data["resources"]
    _require(isinstance(resources, list) and bool(resources))
    ids, defaults = set(), []
    for entry in resources:
        _keys(entry, _ENTRY_KEYS)
        for name, keys in _SUB_KEYS.items():
            _keys(entry[name], keys)
        rid = entry["resource_id"]
        _require(_text(rid) and rid not in ids)
        ids.add(rid)
        _require(_text(entry["display_name"]))
        _require(isinstance(entry["status"], str) and entry["status"] in _STATUSES)
        _require(isinstance(entry["provider_kind"], str) and entry["provider_kind"] in {"local-bge", "openai-compatible"})
        _require(entry["modality"] == ["text"] and type(entry["is_default"]) is bool)
        if entry["is_default"]:
            _require(entry["status"] == "enabled")
            defaults.append(rid)
        connection = entry["connection"]
        _require(_text(connection["model_id"]))
        if entry["provider_kind"] == "local-bge":
            _require(connection["protocol"] == "local" and connection["base_url"] is None and connection["api_key_env"] is None)
        else:
            _require(_text(connection["base_url"]) and connection["api_key_env"] == "SAM_EMBEDDING_API_KEY")
            _require(connection["protocol"] == "openai-embeddings-v1")
        caps = entry["capabilities"]
        dims = caps["dims_allowed"]
        _require(isinstance(dims, list) and bool(dims) and all(type(d) is int and d > 0 for d in dims))
        _require(type(caps["dim_default"]) is int and caps["dim_default"] in dims)
        _require(type(caps["context_tokens"]) is int and caps["context_tokens"] > 0)
        _require(type(caps["sparse"]) is bool and caps["input_modes"] == ["text"] and caps["text_type_param"] is False)
        _require(all(v is None or type(v) is int for v in entry["limits"].values()))
        pricing = entry["pricing"]
        _require(_text(pricing["currency"]))
        _require(all(v is None or (type(v) in (int, float) and float('-inf') < v < float('inf')) for k, v in pricing.items() if k != "currency"))
        _require(all(isinstance(v, str) for v in entry["evidence"].values()))
    _require(defaults == [data["default_resource_id"]])
    return Catalog(data["schema_version"], data["default_resource_id"], tuple(_freeze(e) for e in resources))


def resolve_embedding_spec(*, catalog=None, env=None) -> EmbeddingSpec:
    catalog = load_embedding_catalog() if catalog is None else catalog
    values = os.environ if env is None else env
    # Optional SAM_EMBEDDING_MODEL selects a controlled resource; default is local.
    requested = (values.get("SAM_EMBEDDING_MODEL") or "").strip() or catalog.default_resource_id
    entry = next((e for e in catalog.resources if e.resource_id == requested), None)
    if entry is None:
        raise EmbeddingCatalogError("unknown embedding resource_id")
    if entry.status != "enabled":
        raise EmbeddingCatalogError(f"{entry.resource_id}: {entry.status}；需在 embeddings.json 经宿主受控改为 enabled")
    key, base_url = None, None
    local = entry.provider_kind == "local-bge"
    if not local:
        # Credentials come only from SAM_EMBEDDING_API_KEY, never the catalog.
        key = (values.get(entry.connection.api_key_env) or "").strip()
        if not key:
            raise EmbeddingCatalogError(f"embedding 凭据未配置：请设置环境变量 {entry.connection.api_key_env}")
        base_url = entry.connection.base_url
    return EmbeddingSpec(entry.resource_id, entry.provider_kind,
                         "local-bge" if local else entry.resource_id,
                         entry.connection.model_id, entry.capabilities.dim_default,
                         base_url, key, entry.status)


def resolve_embedding(*, catalog=None, env=None) -> "EmbeddingProvider":
    spec = resolve_embedding_spec(catalog=catalog, env=env)
    from ..memory.embedding import LocalBgeProvider, RemoteOpenAICompatibleProvider
    if spec.provider_kind == "local-bge":
        return LocalBgeProvider()
    return RemoteOpenAICompatibleProvider(
        enabled=True, endpoint=spec.base_url, api_key=spec.api_key,
        model=spec.model_version, dim=spec.dim, timeout=30.0,
        embedder=spec.resource_id,
    )
