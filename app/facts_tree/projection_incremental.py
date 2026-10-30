"""Read an authoritative chain and build its replayed standard generation."""
from app.facts_tree.projection_delta import tree_delta
from app.facts_tree.projection_generation import build_projection_generation
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.projection_replay import active_fact_uids_of, replay_records
from app.facts_tree.record_tree import build_record_tree


class IncrementalProjectionError(Exception):
    """Invalid incremental projection assembly."""


def read_chain_snapshots(repository, commits) -> tuple:
    """Read each pinned commit in order; repository failures pass through."""
    return tuple(repository.read_snapshot_at(oid) for oid in commits)


def build_replayed_generation(repository, from_commit, to_commit, directory, *,
                              expected_repo_id: str, expected_company_id: str):
    """Replay adjacent snapshots through the shared full-generation builders."""
    commits = repository.list_commit_chain(from_commit, to_commit)
    snaps = read_chain_snapshots(repository, commits)
    deltas = [tree_delta(parent.records, child.records)
              for parent, child in zip(snaps, snaps[1:])]
    records = replay_records(snaps[0].records, deltas)
    tree = build_record_tree(
        records, repo_id=expected_repo_id, company_id=expected_company_id,
        commit_oid=to_commit, head_oid=snaps[-1].head_oid,
        object_format=repository.object_format)
    plan = build_projection_plan(tree)
    active = active_fact_uids_of(tree)
    return build_projection_generation(
        plan, records, active, directory, expected_repo_id=expected_repo_id,
        expected_company_id=expected_company_id)
