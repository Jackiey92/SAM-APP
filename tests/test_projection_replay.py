"""D7c pure replay invariants and source boundary."""
import ast
from copy import deepcopy
from pathlib import Path

import pytest

from app.facts_tree import projection_replay
from app.facts_tree.projection_delta import (
    ProjectionDeltaError, SnapshotRecord, tree_delta,
)
from app.facts_tree.projection_replay import active_fact_uids_of, replay_records
from tests.test_projection_generation import _input


@pytest.mark.parametrize('base', [[], [('z', b'2'), ('a', b'1')],
                                  [SnapshotRecord('b', b'3')]])
def test_empty_deltas_normalize(base):
    before = deepcopy(base)
    expected = tuple(sorted((r if isinstance(r, SnapshotRecord) else SnapshotRecord(*r)
                             for r in base), key=lambda r: r.path))
    assert replay_records(iter(base), iter(())) == expected
    assert base == before


def test_multihop_roundtrip_empty_delta_and_no_mutation():
    base = [('z', b'0'), ('a', b'1')]
    middle = [('z', b'2'), ('b', b'3')]
    end = [('b', b'4')]
    deltas = [tree_delta(base, middle), tree_delta(middle, middle), tree_delta(middle, end)]
    before = deepcopy((base, deltas))
    result = replay_records(base, iter(deltas))
    assert result == (SnapshotRecord('b', b'4'),)
    assert replay_records(result, [tree_delta(end, base)]) == replay_records(base, [])
    assert (base, deltas) == before


def test_missing_hop_fails_without_generation(tmp_path):
    a, b, c, d = [[('p', value)] for value in (b'a', b'b', b'c', b'd')]
    with pytest.raises(ProjectionDeltaError):
        replay_records(a, [tree_delta(a, b), tree_delta(c, d)])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('base', [None, [('a', b'1'), ('a', b'2')], [('a', 'bad')]])
def test_empty_deltas_still_validate_base(base):
    with pytest.raises(ProjectionDeltaError):
        replay_records(base, [])


@pytest.mark.parametrize('bad', [None, {}, object()])
def test_active_set_exact_type_guard(bad):
    with pytest.raises(TypeError):
        active_fact_uids_of(bad)


def test_active_set_matches_existing_plan():
    from app.facts_tree.record_tree import build_record_tree, ValidatedRecordTree
    plan, records, active = _input()
    tree = build_record_tree(records, repo_id=plan.repo_id, company_id=plan.company_id,
                             commit_oid=plan.commit_oid, head_oid=plan.head_oid)
    assert active_fact_uids_of(tree) == active
    assert len(active) == plan.active_fact_count
    class Derived(ValidatedRecordTree):
        pass
    with pytest.raises(TypeError):
        active_fact_uids_of(Derived(**tree.__dict__))


def test_pure_kernel_source_guard():
    source = Path(projection_replay.__file__).read_text()
    parsed = ast.parse(source)
    imports = [n.module for n in ast.walk(parsed) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(parsed) if isinstance(n, ast.Import) for a in n.names]
    assert set(imports) == {'app.facts_tree.projection_delta', 'app.facts_tree.record_tree'}
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == 'open' for n in ast.walk(parsed))
    # The two required payload selectors are the only business type/status literals.
    from app.facts_tree import git_format
    literals = {n.value for n in ast.walk(parsed) if isinstance(n, ast.Constant)
                and isinstance(n.value, str)}
    assert literals.intersection(git_format.RECORD_TYPES) <= {'fact_current'}
