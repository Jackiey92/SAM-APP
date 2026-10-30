"""Real isolated Git chains, shared builders, and all-row SQLite equality."""
from copy import deepcopy
from pathlib import Path

import pytest

from app.facts_tree import git_format
from app.facts_tree.git_repository import FactsGitRepository, CommitChainError
from app.facts_tree.projection_delta import tree_delta
from app.facts_tree.projection_incremental import (
    build_replayed_generation, read_chain_snapshots,
)
from app.facts_tree.projection_generation import build_projection_generation
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.projection_replay import active_fact_uids_of, replay_records
from app.facts_tree.record_tree import read_validated_record_tree
from tests.facts_tree_full_fixture import full_tree, SCOPE
from tests.test_projection_generation import _read

EXPECTED = {'expected_repo_id': SCOPE['repo_id'],
            'expected_company_id': SCOPE['company_id']}
META = git_format.record_path('repository_meta')
CURRENT = git_format.record_path('fact_current', fact_uid='fact-1')
PLACEMENT = git_format.record_path('placement_current', fact_uid='fact-1')


def _append(repo, payloads):
    records = [(p, git_format.dumps_record(v)) for p, v in payloads.items()]
    tree = repo.build_tree(records, expected_paths=list(payloads))
    parent = repo.head_oid()
    commit = repo.create_commit(tree, parent)
    assert repo.accept(parent, commit, expected_paths=list(payloads)).accepted
    return commit


def _full(repo, commit, directory):
    tree = read_validated_record_tree(repo, commit)
    return build_projection_generation(
        build_projection_plan(tree), repo.read_snapshot_at(commit).records,
        active_fact_uids_of(tree), directory, **EXPECTED)


@pytest.fixture
def chain(tmp_path):
    repo = FactsGitRepository.initialize(tmp_path / 'facts.git',
                                         installation_id='install-test', **SCOPE)
    genesis = repo.head_oid()
    payloads = full_tree()
    for payload in payloads.values():
        if payload.get('record_type') in {'operation', 'event'}:
            payload['base_commit'] = genesis
    payloads[META] = git_format.loads_record(repo.read_snapshot_at(genesis).records[0].content)
    removable = git_format.record_path('entity_current', entity_uid='entity-1')
    c1 = _append(repo, payloads)
    payloads[CURRENT]['status'] = 'retracted'
    revision = deepcopy(payloads[git_format.record_path(
        'placement_revision', fact_uid='fact-1', revision=1)])
    revision.update(revision=2, leaf_code='next-leaf', predecessor_ref={
        'record_type': 'placement_revision', 'stable_id': 'fact-1', 'revision': 1})
    payloads[git_format.record_path('placement_revision', fact_uid='fact-1', revision=2)] = revision
    payloads[PLACEMENT]['revision'] = 2
    del payloads[removable]
    c2 = _append(repo, payloads)
    payloads[CURRENT]['status'] = 'active'
    payloads[git_format.record_path('taxonomy_leaf', pack_version='test', leaf_id='final')] = {'code': 'final'}
    c3 = _append(repo, payloads)
    return repo, (genesis, c1, c2, c3)


def test_each_commit_equals_full_and_all_rows(chain, tmp_path):
    repo, commits = chain
    snaps = read_chain_snapshots(repo, commits)
    assert tuple(s.commit_oid for s in snaps) == commits
    assert all(s.head_oid == commits[-1] for s in snaps)
    assert len(snaps[0].records) == 1
    delta = tree_delta(snaps[1].records, snaps[2].records)
    assert delta.added and delta.modified and delta.removed
    for i, commit in enumerate(commits):
        full = _full(repo, commit, tmp_path / ('full-%d' % i))
        inc = build_replayed_generation(repo, repo.zero_oid, commit,
                                        tmp_path / ('inc-%d' % i), **EXPECTED)
        assert inc.generation_id == full.generation_id
        assert inc.render_sha256 == full.render_sha256
        assert _read(inc.db_path) == _read(full.db_path)
        assert dict(_read(inc.db_path)['generation_meta'])['head_oid'] == commits[-1]
        assert replay_records(snaps[0].records, [tree_delta(a.records, b.records)
                              for a, b in zip(snaps[:i], snaps[1:i+1])]) == snaps[i].records
    assert _read(_full(repo, commits[1], tmp_path / 'active').db_path)['fact_current_view'] == [('fact-1', 2, 1)]
    assert _read(_full(repo, commits[2], tmp_path / 'inactive').db_path)['fact_current_view'] == [('fact-1', 2, 0)]
    assert _read(_full(repo, commits[2], tmp_path / 'placed').db_path)['placement_current_view'] == [('fact-1', 2)]


@pytest.mark.parametrize('start_index', [0, 1, 2, 3])
def test_split_start_and_zero_hop(chain, tmp_path, start_index):
    repo, commits = chain
    target = commits[-1]
    full = _full(repo, target, tmp_path / 'full')
    inc = build_replayed_generation(repo, commits[start_index], target, tmp_path / 'inc', **EXPECTED)
    assert inc.generation_id == full.generation_id
    assert _read(inc.db_path) == _read(full.db_path)


def test_repeat_trigger_single_file(chain, tmp_path):
    repo, commits = chain
    directory = tmp_path / 'generations'
    first = build_replayed_generation(repo, repo.zero_oid, commits[-1], directory, **EXPECTED)
    path = Path(first.db_path)
    before = (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
    assert build_replayed_generation(repo, repo.zero_oid, commits[-1], directory, **EXPECTED) == first
    assert list(directory.iterdir()) == [path]
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) == before


@pytest.mark.parametrize('failure', ['reverse', 'unreachable'])
def test_chain_errors_pass_through_and_no_output(chain, tmp_path, failure):
    repo, commits = chain
    start, target = commits[-1], commits[1]
    if failure == 'unreachable':
        start = repo.zero_oid
        target = repo.create_commit(repo.build_tree(
            [(r.path, r.content) for r in repo.read_snapshot_at(commits[0]).records],
            expected_paths=[META]), commits[-1])
    directory = tmp_path / 'output'
    with pytest.raises(CommitChainError):
        build_replayed_generation(repo, start, target, directory, **EXPECTED)
    assert not directory.exists()


def test_identity_mismatch_fails_before_output(chain, tmp_path):
    repo, commits = chain
    directory = tmp_path / 'output'
    with pytest.raises(ValueError):
        build_replayed_generation(repo, repo.zero_oid, commits[-1], directory,
                                  **{**EXPECTED, 'expected_repo_id': 'wrong'})
    assert not directory.exists()


def test_gid_stable_when_head_advances(tmp_path):
    repo = FactsGitRepository.initialize(tmp_path / 'facts.git',
                                         installation_id='install-test', **SCOPE)
    genesis = repo.head_oid()
    before = _full(repo, genesis, tmp_path / 'before')
    payloads = {META: git_format.loads_record(repo.read_snapshot_at(genesis).records[0].content)}
    payloads[git_format.record_path('taxonomy_leaf', pack_version='p', leaf_id='a')] = {'code': 'a'}
    head = _append(repo, payloads)
    after = build_replayed_generation(repo, repo.zero_oid, genesis, tmp_path / 'after', **EXPECTED)
    assert before.generation_id == after.generation_id
    assert dict(_read(before.db_path)['generation_meta'])['head_oid'] == genesis
    assert dict(_read(after.db_path)['generation_meta'])['head_oid'] == head
