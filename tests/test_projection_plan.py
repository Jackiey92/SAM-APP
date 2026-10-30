"""Appendix D v1.1: pure plans from schema/closure-validated records."""
import ast
import builtins
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import pytest

from app.facts_tree import git_format, projection_plan, record_tree
from app.facts_tree.projection_plan import (
    ProjectionDigest, ProjectionPlan, build_projection_plan, projection_digest,
)
from app.facts_tree.record_schema.entity_fact import FACT_CURRENT_STATUSES
from app.facts_tree.record_tree import ValidatedRecordTree, build_record_tree
from tests.facts_tree_full_fixture import SCOPE, full_tree
from tests.test_facts_tree_record_schema_closure import binding_tree


META = git_format.record_path("repository_meta")
CURRENT = git_format.record_path("fact_current", fact_uid="fact-1")
FACT = git_format.record_path("fact_revision", fact_uid="fact-1", revision=1)


def _records():
    records = full_tree()
    records[META] = {"schema_version": 1, "format_version": 1,
                     "installation_id": "install-test", **SCOPE}
    return records


def _build(records=None, **overrides):
    if records is None:
        records = _records()
    return build_record_tree(
        [(path, git_format.dumps_record(payload))
         for path, payload in records.items()],
        **{**SCOPE, "commit_oid": "c" * 40, "head_oid": "d" * 40, **overrides})


def _reference_bytes(records):
    # Independent accumulator: no projection implementation helpers or output.
    output = bytearray()
    for path, payload in sorted(records.items()):
        if output:
            output.extend(b"\x1e")
        output.extend(git_format.canonical_record_type(path).encode("utf-8"))
        output.extend(b"\x1f")
        output.extend(path.encode("utf-8"))
        output.extend(b"\x1f")
        output.extend(git_format.dumps_record(payload))
    return bytes(output)


def test_full_tree_counts_current_identity_and_independent_render_digest():
    records = _records()
    tree = _build(records)
    plan = build_projection_plan(tree)
    digest = projection_digest(plan)
    assert type(plan) is ProjectionPlan
    assert type(digest) is ProjectionDigest
    assert dict(plan.family_counts) == {
        family: len(paths) for family, paths in tree.families.items()}
    assert plan.total_records == tree.record_count == sum(plan.family_counts.values()) == 19
    assert plan.active_fact_count == digest.active_fact_count == 1  # hand count
    assert dict(plan.current_heads) == {"fact-1": 2} == {
        records[path]["fact_uid"]: records[path]["revision"]
        for path in tree.families["fact_current"]}
    assert dict(plan.primary_placement) == {"fact-1": 1} == {
        records[path]["fact_uid"]: records[path]["revision"]
        for path in tree.families["placement_current"]}
    for attribute in ("commit_oid", "head_oid", "repo_id", "company_id", "object_format"):
        assert getattr(plan, attribute) == getattr(tree, attribute)
    expected = _reference_bytes(records)
    assert type(plan.render_bytes) is bytes
    assert plan.render_bytes == expected
    assert not plan.render_bytes.endswith(b"\x1e")
    assert digest.digest == plan.render_sha256 == sha256(expected).hexdigest()
    assert len(plan.render_sha256) == 64
    assert set(plan.render_sha256) <= set("0123456789abcdef")
    for attribute in ("family_counts", "current_heads", "primary_placement", "total_records"):
        assert getattr(digest, attribute) == getattr(plan, attribute)


@pytest.mark.parametrize("status,expected", [
    ("active", 1), ("superseded", 0), ("retracted", 0),
])
def test_only_active_fact_current_counts_without_filtering_heads_or_placement(status, expected):
    assert status in FACT_CURRENT_STATUSES
    records = _records()
    records[CURRENT]["status"] = status
    # entity_current remains active, and fact_revision kind remains supersede.
    tree = _build(records)
    plan = build_projection_plan(tree)
    assert plan.active_fact_count == projection_digest(plan).active_fact_count == expected
    assert dict(plan.current_heads) == {"fact-1": 2}
    assert dict(plan.primary_placement) == {"fact-1": 1}


def test_mixed_current_statuses_count_only_active_and_keep_all_pointers():
    records = _records()
    for uid, status in (("fact-2", "superseded"), ("fact-3", "retracted")):
        for family in ("fact_revision", "fact_current", "placement_revision", "placement_current"):
            for path, payload in list(records.items()):
                if (git_format.canonical_record_type(path) != family
                        or payload["fact_uid"] != "fact-1"):
                    continue
                copy = deepcopy(payload)
                copy["fact_uid"] = uid
                if family == "fact_current":
                    copy["status"] = status
                if family == "fact_revision" and copy["predecessor_ref"] is not None:
                    copy["predecessor_ref"]["stable_id"] = uid
                if family == "placement_revision":
                    copy["fact_ref"]["stable_id"] = uid
                ids = {"fact_uid": uid}
                if family.endswith("_revision"):
                    ids["revision"] = copy["revision"]
                records[git_format.record_path(family, **ids)] = copy
    plan = build_projection_plan(_build(records))
    assert plan.active_fact_count == projection_digest(plan).active_fact_count == 1
    assert dict(plan.current_heads) == {"fact-1": 2, "fact-2": 2, "fact-3": 2}
    assert dict(plan.primary_placement) == {"fact-1": 1, "fact-2": 1, "fact-3": 1}


def test_unregistered_families_are_counted_including_bindings_current():
    records, _, _ = binding_tree()
    records[META] = _records()[META]
    records[git_format.record_path("taxonomy_manifest", pack_version="pack")] = {"version": 7}
    records[git_format.record_path("taxonomy_leaf", pack_version="pack", leaf_id="leaf")] = {
        "code": "leaf", "title": "标题"}
    tree = _build(records)
    plan = build_projection_plan(tree)
    assert {"repository_meta", "taxonomy_manifest", "taxonomy_leaf", "bindings_current"} <= set(
        plan.family_counts)
    assert dict(plan.family_counts) == {
        family: len(paths) for family, paths in tree.families.items()}
    assert plan.total_records == tree.record_count == sum(plan.family_counts.values())
    assert plan.active_fact_count == 0
    assert dict(plan.current_heads) == dict(plan.primary_placement) == {}
    assert plan.render_bytes == _reference_bytes(records)


def test_determinism_and_tree_preserved_even_if_ordered_iteration_is_reversed():
    tree = _build()
    names = ("ordered", "index", "families")
    views = {name: getattr(tree, name) for name in names}
    before = {name: deepcopy(dict(view)) for name, view in views.items()}
    plan = build_projection_plan(tree)
    assert plan == build_projection_plan(tree)
    reversed_tree = replace(tree, ordered=MappingProxyType(dict(reversed(list(tree.ordered.items())))))
    assert plan == build_projection_plan(reversed_tree)
    for name, view in views.items():
        assert getattr(tree, name) is view
        assert type(view) is MappingProxyType
        assert dict(view) == before[name]


@pytest.mark.parametrize("attribute", ["family_counts", "current_heads", "primary_placement"])
def test_plan_and_digest_mapping_views_are_readonly(attribute):
    plan = build_projection_plan(_build())
    for result in (plan, projection_digest(plan)):
        view = getattr(result, attribute)
        assert type(view) is MappingProxyType
        with pytest.raises(TypeError):
            view["new"] = 1
        with pytest.raises(TypeError):
            del view[next(iter(view))]
        with pytest.raises(FrozenInstanceError):
            result.active_fact_count = 99


@pytest.mark.parametrize("records", [{}, {META: {**SCOPE}}])
def test_empty_and_metadata_only_trees(records):
    plan = build_projection_plan(_build(records))
    assert plan.total_records == len(records)
    assert plan.active_fact_count == 0
    assert dict(plan.current_heads) == dict(plan.primary_placement) == {}
    assert plan.render_bytes == _reference_bytes(records)
    assert projection_digest(plan).digest == sha256(plan.render_bytes).hexdigest()


def test_render_preserves_decimals_text_controls_and_encoded_paths():
    records = _records()
    records[FACT]["canonical_value"] = {
        "amount": Decimal("123456789.00100"), "original": "未收款\n原文\x1f\x1e",
        "list": [None, False, {"unmodified": "字面值"}],
    }
    path = git_format.record_path("taxonomy_leaf", pack_version="pack", leaf_id="a\x1fb\x1ec")
    records[path] = {"code": "a\x1fb\x1ec", "title": "原始文本"}
    assert "~1F" in path and "~1E" in path
    assert "\x1f" not in path and "\x1e" not in path
    plan = build_projection_plan(_build(records))
    reference = _reference_bytes(records)
    assert plan.render_bytes == reference
    assert b"123456789.00100" in reference
    assert plan.render_sha256 == sha256(reference).hexdigest()
    records[FACT]["canonical_value"]["original"] += "！"
    assert build_projection_plan(_build(records)).render_sha256 != plan.render_sha256


@pytest.mark.parametrize("value", [{}, None])
def test_wrong_tree_and_plan_types_raise_builtin_type_error(value):
    for function in (build_projection_plan, projection_digest):
        with pytest.raises(TypeError) as caught:
            function(value)
        assert type(caught.value) is TypeError


def test_tree_subclasses_are_not_accepted():
    class DerivedTree(ValidatedRecordTree):
        pass

    tree = _build()
    derived = DerivedTree(**{field.name: getattr(tree, field.name) for field in fields(tree)})
    with pytest.raises(TypeError):
        build_projection_plan(derived)


@pytest.mark.parametrize("damage", ["record_count", "families", "ordered"])
def test_inconsistent_assembly_counts_raise_builtin_value_error(damage):
    tree = _build()
    if damage == "record_count":
        tree = replace(tree, record_count=tree.record_count + 1)
    else:
        mapping = dict(getattr(tree, damage))
        del mapping[next(iter(mapping))]
        tree = replace(tree, **{damage: MappingProxyType(mapping)})
    with pytest.raises(ValueError, match="counts") as caught:
        build_projection_plan(tree)
    assert type(caught.value) is ValueError


@pytest.mark.parametrize("family", ["fact_current", "placement_current"])
def test_duplicate_current_uid_is_an_assembly_value_error(family):
    tree = _build()
    source = tree.families[family][0]
    path = git_format.record_path(family, fact_uid="different-path-same-payload-uid")
    ordered = {**tree.ordered, path: deepcopy(tree.ordered[source])}
    families = {**tree.families, family: (*tree.families[family], path)}
    broken = replace(tree, ordered=MappingProxyType(ordered),
                     families=MappingProxyType(families), record_count=tree.record_count + 1)
    with pytest.raises(ValueError, match="duplicate") as caught:
        build_projection_plan(broken)
    assert type(caught.value) is ValueError


def test_pure_build_does_not_revalidate_and_digest_only_uses_plan(monkeypatch):
    tree = _build()

    def forbidden(*args, **kwargs):
        raise AssertionError("projection must not perform IO, closure validation or render parsing")

    with monkeypatch.context() as patch:
        patch.setattr(record_tree, "validate_record_schema_closure", forbidden)
        patch.setattr(git_format, "loads_record", forbidden)
        patch.setattr(builtins, "open", forbidden)
        plan = build_projection_plan(tree)
        # A deliberately opaque render proves digest does not parse or rehash it.
        detached = replace(plan, active_fact_count=7, render_bytes=b"opaque",
                           render_sha256="f" * 64)
        digest = projection_digest(detached)
    assert digest.active_fact_count == 7
    assert digest.digest == "f" * 64


def test_projection_imports_are_limited_to_pure_derivation_dependencies():
    source = Path(projection_plan.__file__).read_text(encoding="utf-8")
    parsed = ast.parse(source)
    allowed = {
        "collections.abc", "dataclasses", "hashlib", "types", "app.facts_tree",
        "app.facts_tree.record_tree", "app.facts_tree.record_schema.entity_fact",
    }
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed
            if node.module == "app.facts_tree":
                assert [alias.name for alias in node.names] == ["git_format"]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "open", "eval", "exec"}
