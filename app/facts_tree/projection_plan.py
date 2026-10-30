"""Pure projection summaries for a validated fixed tree (appendix D v1.1)."""
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from app.facts_tree import git_format
from app.facts_tree.record_schema.entity_fact import FACT_CURRENT_STATUSES
from app.facts_tree.record_tree import ValidatedRecordTree


_ACTIVE_FACT_STATUS = next(status for status in FACT_CURRENT_STATUSES
                           if status == "active")


@dataclass(frozen=True)
class ProjectionDigest:
    family_counts: Mapping[str, int]
    total_records: int
    active_fact_count: int
    current_heads: Mapping[str, int]
    primary_placement: Mapping[str, int]
    digest: str


@dataclass(frozen=True)
class ProjectionPlan:
    commit_oid: str
    head_oid: str
    repo_id: str
    company_id: str
    object_format: str
    family_counts: Mapping[str, int]
    total_records: int
    active_fact_count: int
    current_heads: Mapping[str, int]
    primary_placement: Mapping[str, int]
    render_bytes: bytes
    render_sha256: str


def build_projection_plan(tree: ValidatedRecordTree) -> ProjectionPlan:
    """Derive read-only counts, current pointers and canonical render bytes.

    The caller supplies a schema/closure-validated tree. Only assembly
    invariants are checked here; no storage or closure validation is invoked.
    """
    if type(tree) is not ValidatedRecordTree:
        raise TypeError("tree must be a ValidatedRecordTree")

    family_counts = {family: len(tree.families[family])
                     for family in sorted(tree.families)}
    total_records = sum(family_counts.values())
    if total_records != tree.record_count or total_records != len(tree.ordered):
        raise ValueError("tree record counts do not match")

    current_heads, primary_placement = {}, {}
    active_fact_count = 0
    segments = []
    for path in sorted(tree.ordered):
        payload = tree.ordered[path]
        family = git_format.canonical_record_type(path)
        if family == "fact_current":
            fact_uid = payload["fact_uid"]
            if fact_uid in current_heads:
                raise ValueError("duplicate fact_current fact_uid")
            current_heads[fact_uid] = int(payload["revision"])
            if payload["status"] == _ACTIVE_FACT_STATUS:
                active_fact_count += 1
        elif family == "placement_current":
            fact_uid = payload["fact_uid"]
            if fact_uid in primary_placement:
                raise ValueError("duplicate placement_current fact_uid")
            primary_placement[fact_uid] = int(payload["revision"])
        segments.append(family.encode("utf-8") + b"\x1f"
                        + path.encode("utf-8") + b"\x1f"
                        + git_format.dumps_record(payload))

    render_bytes = b"\x1e".join(segments)
    return ProjectionPlan(
        commit_oid=tree.commit_oid, head_oid=tree.head_oid,
        repo_id=tree.repo_id, company_id=tree.company_id,
        object_format=tree.object_format,
        family_counts=MappingProxyType(family_counts),
        total_records=total_records, active_fact_count=active_fact_count,
        current_heads=MappingProxyType(current_heads),
        primary_placement=MappingProxyType(primary_placement),
        render_bytes=render_bytes, render_sha256=sha256(render_bytes).hexdigest())


def projection_digest(plan: ProjectionPlan) -> ProjectionDigest:
    """Expose the already computed summary without revisiting tree or render."""
    if type(plan) is not ProjectionPlan:
        raise TypeError("plan must be a ProjectionPlan")
    return ProjectionDigest(
        family_counts=plan.family_counts, total_records=plan.total_records,
        active_fact_count=plan.active_fact_count,
        current_heads=plan.current_heads, primary_placement=plan.primary_placement,
        digest=plan.render_sha256)
