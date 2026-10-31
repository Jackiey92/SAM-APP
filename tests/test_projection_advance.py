"""D12-1 windows and real-chain single/full/replayed standard equivalence."""
import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from app.facts_tree import projection_advance
from app.facts_tree.projection_advance import (
    AdvanceWindow, ProjectionAdvanceError, build_standard_generation_for_commit,
    evaluate_advance_window, next_commit,
)
from app.facts_tree.projection_incremental import build_replayed_generation
from tests.test_projection_generation import TABLES, _read
from tests.test_projection_incremental import EXPECTED, _full, chain  # noqa: F401


COMMITS = tuple(char * 40 for char in 'abcde')


@pytest.mark.parametrize('base_index', [None, 0, 1, 3, 4])
def test_window_and_next_commit(base_index):
    current = None if base_index is None else COMMITS[base_index]
    pending = COMMITS if current is None else COMMITS[base_index + 1:]
    window = evaluate_advance_window(current, COMMITS[-1], COMMITS)
    assert window == AdvanceWindow(COMMITS[-1], current, pending,
                                   current is None, current == COMMITS[-1])
    assert next_commit(window) == (pending[0] if pending else None)
    assert [field.name for field in fields(window)] == [
        'target_commit', 'base_commit', 'commits', 'is_bootstrap', 'is_up_to_date']
    with pytest.raises(FrozenInstanceError):
        window.commits = ()


def test_non_ancestor_fails_closed():
    with pytest.raises(ProjectionAdvanceError, match='^serving commit is not an ancestor of target$'):
        evaluate_advance_window('f' * 40, COMMITS[-1], COMMITS)


@pytest.mark.parametrize('bad', [0, False, b'abc', '', 'a\x00b', [], {}])
@pytest.mark.parametrize('position', ['current', 'target', 'element'])
def test_invalid_oid_shapes(bad, position):
    current, target, commits = COMMITS[0], COMMITS[-1], COMMITS
    if position == 'current':
        current = bad
    elif position == 'target':
        target = bad
    else:
        commits = (bad, *COMMITS)
    with pytest.raises(ProjectionAdvanceError):
        evaluate_advance_window(current, target, commits)


@pytest.mark.parametrize('target,commits', [
    (None, COMMITS), (COMMITS[-1], (None, *COMMITS)),
    (COMMITS[-1], list(COMMITS)), (COMMITS[-1], None),
    (COMMITS[-1], ()), (COMMITS[-1], COMMITS[:-1]),
    (COMMITS[-1], tuple(reversed(COMMITS))),
    (COMMITS[-1], (COMMITS[0], *COMMITS)),
])
def test_invalid_chain_structure(target, commits):
    with pytest.raises(ProjectionAdvanceError):
        evaluate_advance_window(None, target, commits)


def test_sha256_chain_window():
    commits = ('a' * 64, 'b' * 64)
    assert evaluate_advance_window(commits[0], commits[1], commits).commits == commits[1:]


@pytest.mark.parametrize('index', range(4))
def test_each_commit_single_full_replay_equivalence_and_idempotence(chain, tmp_path, index):
    repo, commits = chain
    commit = commits[index]
    directory = tmp_path / 'single'
    single = build_standard_generation_for_commit(repo, commit, directory, **EXPECTED)
    full = _full(repo, commit, tmp_path / 'full')
    replay = build_replayed_generation(repo, repo.zero_oid, commit,
                                       tmp_path / 'replay', **EXPECTED)
    rows = _read(single.db_path)
    assert set(rows) == TABLES
    for other in (full, replay):
        assert single.generation_id == other.generation_id
        assert single.render_sha256 == other.render_sha256
        assert rows == _read(other.db_path)
    path = Path(single.db_path)
    before = (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
    assert build_standard_generation_for_commit(repo, commit, directory, **EXPECTED) == single
    assert before == (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
    assert list(directory.iterdir()) == [path]
    assert path.name == 'gen-' + single.generation_id + '.sqlite'
    assert not list(tmp_path.rglob('derived-*'))


@pytest.mark.parametrize('bad', [None, 1, '', 'a\x00b', 'bad', '0' * 40, 'f' * 40])
def test_invalid_commit_passes_through_without_output(chain, tmp_path, bad):
    repo, _ = chain
    directory = tmp_path / 'output'
    # The wrapper preserves the repository's original exception type and message.
    with pytest.raises(Exception) as original:
        repo.read_snapshot_at(bad)
    with pytest.raises(type(original.value)) as wrapped:
        build_standard_generation_for_commit(repo, bad, directory, **EXPECTED)
    assert str(wrapped.value) == str(original.value)
    assert not directory.exists()


@pytest.mark.parametrize('key', ['expected_repo_id', 'expected_company_id'])
def test_identity_mismatch_without_partial_output(chain, tmp_path, key):
    repo, commits = chain
    directory = tmp_path / 'output'
    with pytest.raises(ValueError):
        build_standard_generation_for_commit(repo, commits[-1], directory,
                                             **{**EXPECTED, key: 'wrong'})
    assert not directory.exists()


def test_source_import_boundary():
    source = ast.parse(Path(projection_advance.__file__).read_text())
    allowed = {'dataclasses', 'app.facts_tree.projection_generation',
               'app.facts_tree.projection_plan', 'app.facts_tree.projection_replay',
               'app.facts_tree.record_tree'}
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
