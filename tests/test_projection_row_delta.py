"""D12-2 pure row guards and real wide/FTS readback against a set oracle."""
import ast
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree import projection_row_delta
from app.facts_tree.projection_row_delta import (
    RowChanges, RowDeltaError, diff_rows, fts_row_changes, wide_row_changes,
)
from app.facts_tree.projection_advance import build_standard_generation_for_commit
from app.facts_tree.projection_wide import build_projection_wide
from app.facts_tree.projection_fts import build_projection_fts
from tests.test_projection_incremental import EXPECTED, chain  # noqa: F401


@pytest.mark.parametrize('parent,child,expected', [
    ([], [('z', 2), ('a', 1)], RowChanges((('a', 1), ('z', 2)), (), ())),
    ([('z', 2), ('a', 1)], [], RowChanges((), (), (('a', 1), ('z', 2)))),
    ([('z', 0), ('a', 0)], [('z', 2), ('a', 1)],
     RowChanges((), (('a', 1), ('z', 2)), ())),
    ([('a', 1)], [('a', 1)], RowChanges((), (), ())),
    ([], [], RowChanges((), (), ())),
    ([('z', 0), ('b', 1), ('r', 4), ('a', 0), ('q', 3)],
     [('z', 2), ('d', 4), ('b', 1), ('a', 1), ('c', 3)],
     RowChanges((('c', 3), ('d', 4)), (('a', 1), ('z', 2)), (('q', 3), ('r', 4)))),
])
def test_diff_branches_sorted_complete_rows_without_mutation(parent, child, expected):
    before = deepcopy((parent, child))
    result = diff_rows(parent, child, lambda row: row[0])
    assert result == expected
    assert result.is_empty() == (not any((expected.added, expected.modified, expected.removed)))
    assert (parent, child) == before
    assert diff_rows(iter(parent), iter(child), lambda row: row[0]) == expected


def test_changes_frozen_fields():
    changes = RowChanges((), (), ())
    assert [f.name for f in fields(changes)] == ['added', 'modified', 'removed']
    with pytest.raises(FrozenInstanceError):
        changes.added = (('a',),)


@pytest.mark.parametrize('side', ['parent', 'child'])
def test_duplicate_keys_rejected_on_either_side(side):
    rows = [('same', 1), ('same', 2)]
    with pytest.raises(RowDeltaError, match='^duplicate row key: same$'):
        diff_rows(rows if side == 'parent' else [], rows if side == 'child' else [],
                  lambda row: row[0])


@pytest.mark.parametrize('side', ['parent', 'child'])
@pytest.mark.parametrize('bad', [None, 1, False])
def test_noniterable_rows_rejected(side, bad):
    with pytest.raises(RowDeltaError, match='iterable'):
        diff_rows(bad if side == 'parent' else [], bad if side == 'child' else [],
                  lambda row: row[0])


@pytest.mark.parametrize('bad', [['a'], 'a', 1, None, {'a': 1}])
@pytest.mark.parametrize('side', ['parent', 'child'])
def test_non_tuple_row_rejected(bad, side):
    with pytest.raises(RowDeltaError, match='tuple'):
        diff_rows([bad] if side == 'parent' else [], [bad] if side == 'child' else [],
                  lambda row: row[0])


@pytest.mark.parametrize('value', [None, '', 1, False, b'a', [], {}])
def test_invalid_key_values(value):
    with pytest.raises(RowDeltaError, match='nonempty string'):
        diff_rows([('a',)], [], lambda row: value)


def test_invalid_key_callable_and_short_rows():
    with pytest.raises(RowDeltaError, match='callable'):
        diff_rows([], [], None)
    with pytest.raises(RowDeltaError, match='extract row key'):
        fts_row_changes([()], [])
    with pytest.raises(RowDeltaError, match='extract row key'):
        wide_row_changes({'entity': [('short',)]}, {})


def test_wide_multifamily_ownership_uses_fourth_last_path():
    # Five ownership columns precede the four system columns, even on long rows.
    old = ('fact-1', 2, 'active', 'trunk', 'leaf-old', 1, 'pack', 3,
           'facts/one', '{}', 'digest', 2)
    new = (*old[:4], 'leaf-new', *old[5:])
    entity_old = ('old entity', 'entities/old', '{}', 'old-digest', 2)
    entity_new = ('new entity', 'entities/new', '{}', 'new-digest', 2)
    parent = {'fact_current': [old], 'entity': [entity_old], 'parent_only': [entity_old],
              'empty': []}
    child = {'fact_current': [new], 'entity': [entity_new], 'child_only': [entity_new],
             'empty': []}
    before = deepcopy((parent, child))
    delta = wide_row_changes(parent, child)
    assert list(delta) == sorted(parent.keys() | child.keys())
    assert delta['fact_current'] == RowChanges((), (new,), ())
    assert delta['entity'] == RowChanges((entity_new,), (), (entity_old,))
    assert delta['parent_only'] == RowChanges((), (), (entity_old,))
    assert delta['child_only'] == RowChanges((entity_new,), (), ())
    assert delta['empty'].is_empty()
    assert all(change.is_empty() for change in wide_row_changes(parent, parent).values())
    assert wide_row_changes({}, {}) == {}
    assert (parent, child) == before


def test_fts_content_change_requires_full_rebuild_and_unchanged_skips_writes():
    parent = [('b', 'family', 'old text'), ('d', 'family', 'removed')]
    child = [('a', 'family', 'added'), ('b', 'family', 'new text')]
    assert fts_row_changes(parent, child) == RowChanges((child[0],), (child[1],), (parent[1],))
    assert fts_row_changes(child, list(reversed(child))).is_empty()
    assert fts_row_changes([], []).is_empty()


def _oracle(parent, child, offset):
    left = {row[offset]: row for row in parent}
    right = {row[offset]: row for row in child}
    removed_keys = set(left).difference(right)
    added_keys = set(right).difference(left)
    modified_keys = {k for k in set(left).intersection(right) if left[k] != right[k]}
    return RowChanges(tuple(right[k] for k in sorted(added_keys)),
                      tuple(right[k] for k in sorted(modified_keys)),
                      tuple(left[k] for k in sorted(removed_keys)))


def _read_derived(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dw_%' ORDER BY name")]
        wide = {table[3:]: conn.execute('SELECT * FROM ' + table + ' ORDER BY path').fetchall()
                for table in tables}
        fts = conn.execute('SELECT path, family, record_text FROM fts_records ORDER BY path').fetchall()
    return wide, fts


def test_real_adjacent_generations_wide_fts_readback_matches_set_oracle(chain, tmp_path):
    repo, commits = chain
    generations = []
    for i, commit in enumerate(commits):
        directory = tmp_path / str(i)
        std = build_standard_generation_for_commit(repo, commit, directory, **EXPECTED)
        wide = build_projection_wide(std.db_path, directory=directory, **EXPECTED)
        fts = build_projection_fts(std.db_path, directory=directory, **EXPECTED)
        assert fts.db_path == wide.db_path
        generations.append(_read_derived(fts.db_path))
    for parent, child in zip(generations, generations[1:]):
        pw, pf = parent
        cw, cf = child
        actual = wide_row_changes(pw, cw)
        assert len(actual) == 16
        assert actual == {family: _oracle(pw[family], cw[family], -4) for family in sorted(pw)}
        assert fts_row_changes(pf, cf) == _oracle(pf, cf, 0)
    assert any(not d.is_empty() for d in wide_row_changes(generations[1][0], generations[2][0]).values())
    assert not fts_row_changes(generations[1][1], generations[2][1]).is_empty()
    # c2 -> c3 only changes nonsearchable status and a skipped taxonomy record.
    assert fts_row_changes(generations[2][1], generations[3][1]).is_empty()
    for wide, fts in generations:
        assert all(d.is_empty() for d in wide_row_changes(wide, wide).values())
        assert fts_row_changes(fts, fts).is_empty()


def test_source_imports_only_pure_standard_library():
    source = ast.parse(Path(projection_row_delta.__file__).read_text())
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            assert all(alias.name == 'dataclasses' for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module == 'dataclasses'
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'open', 'exec', 'eval', '__import__'}
