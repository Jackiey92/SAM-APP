"""Pure ordered replay of canonical record differences."""
from app.facts_tree.projection_delta import (
    SnapshotRecord, TreeDelta, apply_tree_delta,
)
from app.facts_tree.record_tree import ValidatedRecordTree


class ProjectionReplayError(ValueError):
    """Invalid replay input."""


def replay_records(base_records, deltas) -> tuple[SnapshotRecord, ...]:
    """Normalize the base and apply each delta without mutating caller input."""
    records = apply_tree_delta(base_records, TreeDelta((), (), ()))
    for delta in deltas:
        records = apply_tree_delta(records, delta)
    return records


def active_fact_uids_of(tree) -> frozenset[str]:
    """Derive the active UID set from a validated tree."""
    if type(tree) is not ValidatedRecordTree:
        raise TypeError("tree must be a ValidatedRecordTree")
    return frozenset(payload["fact_uid"] for payload in tree.ordered.values()
                     if payload.get("record_type") == "fact_current"
                     and payload["status"] == "active")
