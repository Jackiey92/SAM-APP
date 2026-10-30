"""Byte-level delta invariants; no repository or database fixtures required."""
import ast
from dataclasses import FrozenInstanceError
from itertools import product
from pathlib import Path

import pytest

from app.facts_tree import projection_delta as module
from app.facts_tree.git_repository import SnapshotRecord
from app.facts_tree.projection_delta import (
    ProjectionDeltaError, RecordChange, TreeDelta, apply_tree_delta, tree_delta,
)


def snapshot(records):
    return tuple(SnapshotRecord(*item) for item in sorted(records))


STATES = [tuple((path, value) for path, value in zip(("z", "a"), values)
                if value is not None)
          for values in product((None, b"", b"\x00\xff"), repeat=2)]


@pytest.mark.parametrize("parent", STATES)
@pytest.mark.parametrize("child", STATES)
def test_roundtrip_reverse_and_empty(parent, child):
    before, after = snapshot(parent), snapshot(child)
    forward, reverse = tree_delta(parent, child), tree_delta(child, parent)
    assert apply_tree_delta(parent, forward) == after
    assert apply_tree_delta(child, reverse) == before
    assert reverse.added == tuple(RecordChange(c.path, None, c.before)
                                  for c in forward.removed)
    assert reverse.removed == tuple(RecordChange(c.path, c.after, None)
                                    for c in forward.added)
    assert reverse.modified == tuple(RecordChange(c.path, c.after, c.before)
                                     for c in forward.modified)
    empty = tree_delta(parent, parent)
    assert empty.is_empty()
    assert apply_tree_delta(apply_tree_delta(parent, empty), empty) == before
    assert forward.is_empty() == (before == after)
    assert snapshot(parent) == before


def test_chain_replay_and_mixed_inputs():
    chain = [(), (("z", b"bad JSON"), ("a", b"")),
             (("a", b"\x00"), ("b", b"\xff")), (("b", b"\xff"),), ()]
    replay = ()
    for parent, child in zip(chain, chain[1:]):
        mixed = [SnapshotRecord(*item) if i % 2 else item
                 for i, item in enumerate(parent)]
        delta = tree_delta(mixed, iter(snapshot(child)))
        assert delta == tree_delta(parent, child)
        replay = apply_tree_delta(replay, delta)
        assert replay == snapshot(child)
    assert replay == snapshot(chain[-1])


def test_three_states_sorted_and_equal_bytes():
    parent = [("keep", b"same"), ("mod", b"old"), ("z", b""), ("y", b"x")]
    child = [("b", b"2"), ("mod", b"new"), ("a", b"1"),
             ("keep", bytes(bytearray(b"same")))]
    delta = tree_delta(parent, child)
    assert delta == TreeDelta(
        (RecordChange("a", None, b"1"), RecordChange("b", None, b"2")),
        (RecordChange("mod", b"old", b"new"),),
        (RecordChange("y", b"x", None), RecordChange("z", b"", None)))
    assert apply_tree_delta(parent, delta) == snapshot(child)
    with pytest.raises(FrozenInstanceError):
        delta.added = ()
    with pytest.raises(FrozenInstanceError):
        delta.modified[0].after = b"other"


@pytest.mark.parametrize("records", [None, 1, [None], ["ab"], [["x", b"x"]],
    [("x",)], [("x", b"x", 3)], [("", b"x")], [(1, b"x")],
    [("x", "x")], [("x", bytearray(b"x"))], [SnapshotRecord("", b"")],
    [SnapshotRecord("x", None)], [("x", b"1"), SnapshotRecord("x", b"2")]])
def test_invalid_records_fail_closed(records):
    for call in (lambda: tree_delta(records, ()), lambda: tree_delta((), records),
                 lambda: apply_tree_delta(records, TreeDelta((), (), ()))):
        with pytest.raises(ProjectionDeltaError):
            call()


@pytest.mark.parametrize("kind,change,base", [
    ("added", RecordChange("x", None, b"new"), [("x", b"old")]),
    ("added", RecordChange("x", b"old", b"new"), []),
    ("added", RecordChange("x", None, None), []),
    ("added", RecordChange("x", None, "new"), []),
    ("modified", RecordChange("x", b"old", b"new"), []),
    ("modified", RecordChange("x", b"wrong", b"new"), [("x", b"old")]),
    ("modified", RecordChange("x", None, b"new"), [("x", b"old")]),
    ("modified", RecordChange("x", b"old", None), [("x", b"old")]),
    ("modified", RecordChange("x", b"old", b"old"), [("x", b"old")]),
    ("modified", RecordChange("x", "old", b"new"), [("x", b"old")]),
    ("removed", RecordChange("x", b"old", None), []),
    ("removed", RecordChange("x", b"wrong", None), [("x", b"old")]),
    ("removed", RecordChange("x", b"old", b"new"), [("x", b"old")]),
    ("removed", RecordChange("x", None, None), [("x", b"old")]),
    ("removed", RecordChange("x", "old", None), [("x", b"old")]),
])
def test_apply_conflicts(kind, change, base):
    groups = dict(added=(), modified=(), removed=())
    groups[kind] = (change,)
    original = base.copy()
    with pytest.raises(ProjectionDeltaError):
        apply_tree_delta(base, TreeDelta(**groups))
    assert base == original


@pytest.mark.parametrize("delta", [None, (), TreeDelta([], (), ()),
    TreeDelta((None,), (), ()), TreeDelta((RecordChange("", None, b""),), (), ()),
    TreeDelta((RecordChange(1, None, b""),), (), ()),
    TreeDelta((RecordChange("x", None, b""),) * 2, (), ()),
    TreeDelta((RecordChange("x", None, b""),), (), (RecordChange("x", b"", None),)),
    TreeDelta((RecordChange("z", None, b""), RecordChange("a", None, b"")), (), ())])
def test_malformed_delta(delta):
    with pytest.raises(ProjectionDeltaError):
        apply_tree_delta((), delta)


def test_late_conflict_preserves_inputs():
    base = [("z", b"old")]
    delta = TreeDelta((RecordChange("a", None, b"new"),), (),
                      (RecordChange("z", b"wrong", None),))
    with pytest.raises(ProjectionDeltaError):
        apply_tree_delta(base, delta)
    assert base == [("z", b"old")]
    assert delta.added == (RecordChange("a", None, b"new"),)


def test_import_and_source_guard():
    source = Path(module.__file__).read_text()
    parsed = ast.parse(source)
    imports = [node for node in ast.walk(parsed)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(node, ast.ImportFrom) for node in imports)
    assert [(node.module, [a.name for a in node.names]) for node in imports] == [
        ("dataclasses", ["dataclass"]),
        ("app.facts_tree.git_repository", ["SnapshotRecord"])]
    # The sole adapter import supplies the existing value object, never an IO API.
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {"open", "exec", "eval", "__import__"}
                   for node in ast.walk(parsed))
    for forbidden in ("sqlite", "subprocess", "socket", "requests", "pathlib",
                      "json", "append_only", "fact_revision", "entity_current",
                      "placement_revision", "repository_meta"):
        assert forbidden not in source.lower()
