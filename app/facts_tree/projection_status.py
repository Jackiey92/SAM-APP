"""Deterministic projection envelopes and management gates (appendix D5 v1)."""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class IndexStatus:
    name: str
    ready: bool


@dataclass(frozen=True)
class ProjectionStatus:
    repo_id: str
    company_id: str
    head_commit: str
    snapshot_commit: Optional[str]
    generation_id: str
    serving_commit: str
    built_against_head: str
    object_format: str
    projection_pending: bool
    snapshot_current: Optional[bool]
    serving_current: bool
    management_ready: bool
    indexes: Tuple[IndexStatus, ...]
    generation_record_count: int
    generation_active_fact_count: int


class ProjectionStatusError(ValueError):
    """An envelope input violates the frozen contract."""


def _indexes(value, name, *, unique=False):
    if not isinstance(value, (tuple, list)) or (not value and not unique):
        raise ProjectionStatusError(name + " must be a nonempty tuple/list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ProjectionStatusError(name + " must contain nonempty strings")
    result = tuple(dict.fromkeys(value))
    if unique and len(result) != len(value):
        raise ProjectionStatusError(name + " must not contain duplicates")
    return result


def build_projection_status(
    head_commit: str,
    serving,
    *,
    snapshot_commit: Optional[str] = None,
    required_indexes: Tuple[str, ...] = ("canonical_records",),
) -> ProjectionStatus:
    """Compare caller-captured H/C/P; never inspect storage or infer facts."""
    names = (
        "generation_id", "db_path", "repo_id", "company_id", "projected_commit",
        "head_oid", "object_format", "render_sha256", "record_count",
        "active_fact_count", "projection_schema_version", "builder_version",
        "renderer_version", "required_indexes",
    )
    try:
        values = {name: getattr(serving, name) for name in names}
    except AttributeError as exc:
        raise ProjectionStatusError("serving is missing a required field") from exc
    object_format = values["object_format"]
    if not isinstance(object_format, str) or object_format not in ("sha1", "sha256"):
        raise ProjectionStatusError("invalid object_format")
    length = 40 if object_format == "sha1" else 64
    commits = [("head_commit", head_commit),
               ("projected_commit", values["projected_commit"]),
               ("head_oid", values["head_oid"])]
    if snapshot_commit is not None:
        commits.append(("snapshot_commit", snapshot_commit))
    for name, value in commits:
        if (not isinstance(value, str) or len(value) != length
                or any(char not in "0123456789abcdef" for char in value)):
            raise ProjectionStatusError(name + " must be a lowercase full OID")
    for name in ("repo_id", "company_id"):
        if not isinstance(values[name], str) or not values[name]:
            raise ProjectionStatusError(name + " must be a nonempty string")
    required = _indexes(required_indexes, "required_indexes")
    available = _indexes(values["required_indexes"], "serving.required_indexes", unique=True)
    indexes = tuple(IndexStatus(name, name in available) for name in required)
    current = values["projected_commit"] == head_commit
    snapshot_current = None if snapshot_commit is None else snapshot_commit == head_commit
    return ProjectionStatus(
        values["repo_id"], values["company_id"], head_commit, snapshot_commit,
        values["generation_id"], values["projected_commit"], values["head_oid"],
        object_format, not current, snapshot_current, current,
        snapshot_current is True and current and all(index.ready for index in indexes),
        indexes, values["record_count"], values["active_fact_count"],
    )
