"""Pure advance windows and single-snapshot standard-generation assembly."""
from dataclasses import dataclass

from app.facts_tree.projection_generation import (
    BuiltProjectionGeneration, build_projection_generation,
)
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.projection_replay import active_fact_uids_of
from app.facts_tree.record_tree import build_record_tree


class ProjectionAdvanceError(ValueError):
    """Invalid projection advance window."""


@dataclass(frozen=True)
class AdvanceWindow:
    target_commit: str
    base_commit: str | None
    commits: tuple[str, ...]
    is_bootstrap: bool
    is_up_to_date: bool


def evaluate_advance_window(current_commit, target_commit, commit_chain) -> AdvanceWindow:
    """Slice a caller-validated oldest-first genesis-to-target chain."""
    oids = (target_commit,) if current_commit is None else (current_commit, target_commit)
    if not isinstance(commit_chain, tuple):
        raise ProjectionAdvanceError("commit_chain must be a tuple")
    if any(not isinstance(oid, str) or not oid or '\x00' in oid
           for oid in (*oids, *commit_chain)):
        raise ProjectionAdvanceError("commits must be nonempty strings without NUL")
    # Git ancestry is the caller's responsibility; reject structural ambiguity.
    if (not commit_chain or commit_chain[-1] != target_commit
            or len(set(commit_chain)) != len(commit_chain)):
        raise ProjectionAdvanceError("commit_chain must end at target without duplicates")
    if current_commit is None:
        return AdvanceWindow(target_commit, None, commit_chain, True, False)
    if current_commit not in commit_chain:
        raise ProjectionAdvanceError("serving commit is not an ancestor of target")
    commits = commit_chain[commit_chain.index(current_commit) + 1:]
    return AdvanceWindow(target_commit, current_commit, commits, False,
                         current_commit == target_commit)


def next_commit(window: AdvanceWindow) -> str | None:
    """Return the first pending commit, or None for an empty window."""
    return window.commits[0] if window.commits else None


def build_standard_generation_for_commit(repository, commit_oid, directory, *,
                                         expected_repo_id, expected_company_id
                                         ) -> BuiltProjectionGeneration:
    """Build only the standard generation from one pinned historical snapshot."""
    snapshot = repository.read_snapshot_at(commit_oid)
    tree = build_record_tree(
        snapshot.records, repo_id=expected_repo_id, company_id=expected_company_id,
        commit_oid=snapshot.commit_oid, head_oid=snapshot.head_oid,
        object_format=repository.object_format)
    plan = build_projection_plan(tree)
    active = active_fact_uids_of(tree)
    return build_projection_generation(
        plan, snapshot.records, active, directory, expected_repo_id=expected_repo_id,
        expected_company_id=expected_company_id)
