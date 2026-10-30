"""Real, isolated Git authority chains; sha1 and sha256, no Git/repo mocks.

Cycles cannot be manufactured with content-addressed Git objects without a
hash fixed point; the walk's revisit guard is additionally checked structurally.
"""
import ast
import inspect
import textwrap

import pytest

from app.facts_tree import git_format
from app.facts_tree.git_repository import (
    CommitChainError, FactsGitRepository, GitCommandError, REF_FACTS,
)
from tests.test_facts_tree_git_repository import _git, _git_text, _IDS


@pytest.fixture(params=['sha1', 'sha256'])
def chain(tmp_path, request):
    repo = FactsGitRepository.initialize(
        tmp_path / 'facts.git', **_IDS, object_format=request.param)
    commits = [repo.head_oid()]
    records = {'meta/repository.json': git_format.dumps_record(repo._expected_meta)}
    for number in range(1, 4):
        records['facts/fact-%d/current.json' % number] = git_format.dumps_record(
            {'fact_uid': 'fact-%d' % number, 'revision': 1})
        tree = repo.build_tree(list(records.items()), expected_paths=sorted(records))
        commit = repo.create_commit(tree, commits[-1], message='chain %d' % number)
        assert repo.accept(commits[-1], commit,
                           expected_paths=sorted(records)).accepted
        commits.append(commit)
    return repo, tuple(commits)


def _snapshot(repo):
    # Includes config, refs, all loose/packed objects and reflogs. Compare bytes
    # and metadata as well as directory entries to catch any incidental writes.
    return {str(p.relative_to(repo.path)):
            (p.stat().st_mode, p.stat().st_mtime_ns,
             p.read_bytes() if p.is_file() else None)
            for p in repo.path.rglob('*')}


def _raw_commit(repo, parents, tree=None):
    tree = tree or _git_text(repo.path, 'rev-parse', parents[0] + '^{tree}').strip()
    args = ['commit-tree', tree]
    for parent in parents:
        args += ['-p', parent]
    return _git_text(repo.path, *args, '-m', 'synthetic raw commit', env={
        'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@localhost',
        'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@localhost',
    }).strip()


def test_intervals_order_unique_and_deterministic_read_only(chain):
    repo, (g, c1, c2, c3) = chain
    before = _snapshot(repo)
    cases = [(repo.zero_oid, None, (g, c1, c2, c3)),
             (c1, None, (c1, c2, c3)), (c2, c3, (c2, c3)),
             (c3, c3, (c3,)), (c1, c2, (c1, c2)),
             (repo.zero_oid, g, (g,)), (g, g, (g,))]
    for start, end, expected in cases:
        assert [repo.list_commit_chain(start, end) for _ in range(3)] == [expected] * 3
        assert len(set(expected)) == len(expected)
    assert repo.list_commit_chain(c1) == repo.list_commit_chain(c1, c3)
    assert all(len(c) == repo.oid_length for c in (g, c1, c2, c3))
    assert _snapshot(repo) == before
    assert repo.head_oid() == c3


@pytest.mark.parametrize('target', ['from', 'to'])
@pytest.mark.parametrize('bad', [None, 1, [], {}, b'ab', 'abc', 'f' * 39,
                                'z' * 40, 'HEAD', 'refs/heads/facts'])
def test_malformed_is_wrapped(chain, target, bad):
    repo, commits = chain
    if target == 'to' and bad is None:
        assert repo.list_commit_chain(commits[0], bad) == commits
        return
    args = (bad, commits[-1]) if target == 'from' else (commits[0], bad)
    with pytest.raises(CommitChainError):
        repo.list_commit_chain(*args)


def test_zero_to_and_noncommit_objects(chain):
    repo, commits = chain
    tree = _git_text(repo.path, 'rev-parse', commits[-1] + '^{tree}').strip()
    blob = _git_text(repo.path, 'hash-object', '-w', '--stdin',
                     input_bytes=b'synthetic').strip()
    for start, end in [(commits[0], repo.zero_oid), (tree, None),
                       (blob, None), (commits[0], tree), (commits[0], blob)]:
        with pytest.raises(CommitChainError):
            repo.list_commit_chain(start, end)


def test_reverse_missing_and_unreachable_objects(chain, tmp_path):
    repo, (g, c1, c2, c3) = chain
    orphan = _raw_commit(repo, [g])
    other = FactsGitRepository.initialize(tmp_path / 'other.git', **{
        **_IDS, 'repo_id': 'unrelated'}, object_format=repo.object_format)
    before = _snapshot(repo)
    for start, end, message in [
            (c3, c1, 'from is not an ancestor'),
            (orphan, c3, 'from is not an ancestor'),
            (g, orphan, 'to is not on the authoritative chain'),
            (other.head_oid(), c3, 'unknown object'),
            ('f' * repo.oid_length, c3, 'unknown object'),
            (g, 'f' * repo.oid_length, 'unknown object')]:
        with pytest.raises(CommitChainError, match=message):
            repo.list_commit_chain(start, end)
    assert _snapshot(repo) == before


def test_unbound_precedes_malformed(chain):
    repo, _ = chain
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(CommitChainError, match='identity is not bound'):
        unbound.list_commit_chain([], {})


@pytest.mark.parametrize('defect', ['merge', 'identity', 'missing'])
def test_entire_authority_gate_precedes_interval_return(chain, tmp_path, defect):
    repo, (g, c1, c2, c3) = chain
    if defect == 'merge':
        bad = _raw_commit(repo, [c3, c1])
        message = 'single-parent'
    elif defect == 'identity':
        foreign = FactsGitRepository.initialize(
            tmp_path / 'foreign.git', **{**_IDS, 'company_id': 'foreign'},
            object_format=repo.object_format)
        # Transfer only synthetic objects, locally, to build identity drift.
        _git(repo.path, 'fetch', str(foreign.path), REF_FACTS)
        tree = _git_text(repo.path, 'rev-parse', foreign.head_oid() + '^{tree}').strip()
        bad = _raw_commit(repo, [c3], tree)
        message = 'identity'
    else:
        bad = c3
        (repo.path / 'objects' / c2[:2] / c2[2:]).unlink()
        message = 'unknown object|unreadable'
    _git(repo.path, 'update-ref', REF_FACTS, bad)
    before = _snapshot(repo)
    # Even this old, zero-length interval must verify defects AFTER its end.
    with pytest.raises(CommitChainError, match=message):
        repo.list_commit_chain(g, g)
    assert _snapshot(repo) == before


def test_missing_authority_and_input_failure_order(chain):
    repo, commits = chain
    _git(repo.path, 'update-ref', '-d', REF_FACTS)
    with pytest.raises(CommitChainError, match='to_commit'):
        repo.list_commit_chain([], 'bad')
    with pytest.raises(CommitChainError, match='missing'):
        repo.list_commit_chain([], None)
    with pytest.raises(CommitChainError, match='missing'):
        repo.list_commit_chain(commits[0], commits[-1])


def test_replacement_refs_are_ignored(chain):
    repo, commits = chain
    replacement = _raw_commit(repo, [commits[0]])
    _git(repo.path, 'replace', commits[-1], replacement)
    before = _snapshot(repo)
    assert repo.list_commit_chain(repo.zero_oid) == commits
    assert _snapshot(repo) == before


def test_walk_has_revisit_guard_before_recording_entry():
    # Pure walk-level structural assertion, without mocking the repo or Git.
    source = textwrap.dedent(inspect.getsource(FactsGitRepository.list_commit_chain))
    node = ast.parse(source)
    walk = next(n for n in ast.walk(node) if isinstance(n, ast.While))
    guard = walk.body[0]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == 'current in seen'
    assert any(isinstance(n, ast.Raise) for n in ast.walk(guard))
    assert ast.unparse(walk.body[1]) == 'seen.add(current)'
