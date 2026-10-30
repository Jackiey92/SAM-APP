"""R2-CL4: appendix B v1.1 §5, rules 4.1 (reject) and 4.4 (retain)."""
from copy import deepcopy

import pytest

from app.facts_tree import git_format
from app.facts_tree.record_schema import (
    CLOSURE_VALIDATORS, RecordSchemaClosureError, SCHEMA_BUILDERS,
    validate_record_schema_closure,
)
from app.facts_tree.record_schema.closure_04_cardinality import _closure_4
from app.facts_tree.record_schema.entity_fact import FACT_CURRENT_STATUSES
from app.facts_tree.record_schema.source_evidence import SOURCE_LIFECYCLE_STATUSES
from tests.facts_tree_full_fixture import SCOPE, full_tree


FACT_CURRENT = git_format.record_path("fact_current", fact_uid="fact-1")
PLACEMENT_CURRENT = git_format.record_path("placement_current", fact_uid="fact-1")
PLACEMENT_REVISION = git_format.record_path(
    "placement_revision", fact_uid="fact-1", revision=1)


def validate(tree):
    return validate_record_schema_closure(tree, **SCOPE)


def reject(tree, fact_uid="fact-1"):
    before = deepcopy(tree)
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate(tree)
    error = caught.value
    assert error.code == "active_fact_without_primary_placement"
    assert error.family == "fact_current"
    assert error.path == git_format.record_path("fact_current", fact_uid=fact_uid)
    assert error.field == "fact_uid"
    assert tree == before


def add_fact(tree, fact_uid):
    for family in ("fact_revision", "fact_current", "placement_revision", "placement_current"):
        ids = {"fact_uid": fact_uid}
        if family.endswith("_revision"):
            ids["revision"] = 1
        payload = SCHEMA_BUILDERS[family](**ids)
        if family == "fact_revision":
            payload["entity_ref"] = {
                "record_type": "entity_revision", "stable_id": "entity-1", "revision": 1}
        elif family == "placement_revision":
            payload["fact_ref"] = {
                "record_type": "fact_revision", "stable_id": fact_uid, "revision": 1}
        tree[git_format.record_path(family, **ids)] = payload


def test_registered_and_full_tree_passes_without_mutation():
    assert CLOSURE_VALIDATORS[4] is _closure_4
    tree = full_tree()
    before = deepcopy(tree)
    assert tree[FACT_CURRENT]["status"] == "active"
    assert tree[PLACEMENT_CURRENT]["revision"] == tree[PLACEMENT_REVISION]["revision"] == 1
    assert validate(tree) is None
    assert tree == before


def test_active_fact_without_placement_current_is_rejected_despite_history():
    tree = full_tree()
    del tree[PLACEMENT_CURRENT]
    assert PLACEMENT_REVISION in tree
    reject(tree)


def test_active_fact_with_dangling_placement_current_is_rejected():
    tree = full_tree()
    tree[PLACEMENT_CURRENT]["revision"] = 99
    reject(tree)


def test_active_fact_without_pointed_placement_revision_is_rejected():
    tree = full_tree()
    del tree[PLACEMENT_REVISION]
    reject(tree)


@pytest.mark.parametrize("status", sorted(s for s in FACT_CURRENT_STATUSES if s != "active"))
@pytest.mark.parametrize("remove_history", [False, True])
def test_inactive_fact_needs_no_primary_placement(status, remove_history):
    tree = full_tree()
    tree[FACT_CURRENT]["status"] = status
    del tree[PLACEMENT_CURRENT]
    if remove_history:
        del tree[PLACEMENT_REVISION]
    assert validate(tree) is None


@pytest.mark.parametrize("status", sorted(s for s in SOURCE_LIFECYCLE_STATUSES if s != "active"))
def test_source_exit_preserves_fact_references_to_historical_evidence(status):
    tree = full_tree()
    lifecycle_path = git_format.record_path("source_lifecycle", source_id="src-1", revision=1)
    evidence_path = git_format.record_path("evidence", evidence_id="evidence-1")
    before_evidence = deepcopy(tree[evidence_path])
    tree[lifecycle_path]["status"] = status
    for revision in (1, 2):
        fact_path = git_format.record_path("fact_revision", fact_uid="fact-1", revision=revision)
        assert {"record_type": "evidence", "stable_id": "evidence-1"} in (
            tree[fact_path]["primary_evidence_refs"])
    assert tree[evidence_path]["source_version_ref"] == tree[lifecycle_path]["version_scope"]["version_ref"]
    assert validate(tree) is None
    assert tree[evidence_path] == before_evidence


def test_empty_tree_passes():
    assert validate({}) is None


def test_no_fact_current_needs_no_placement_current():
    tree = full_tree()
    del tree[FACT_CURRENT]
    del tree[PLACEMENT_CURRENT]
    assert validate(tree) is None


@pytest.mark.parametrize("missing", ["current", "revision"])
def test_other_facts_placements_do_not_satisfy_active_fact(missing):
    tree = full_tree()
    add_fact(tree, "another-fact")
    if missing == "current":
        del tree[PLACEMENT_CURRENT]
    else:
        del tree[PLACEMENT_REVISION]
    reject(tree)


def test_every_active_fact_is_checked():
    tree = full_tree()
    fact_uid = "zz-second-fact"
    add_fact(tree, fact_uid)
    del tree[git_format.record_path("placement_current", fact_uid=fact_uid)]
    reject(tree, fact_uid)


def test_opaque_fact_uid_uses_canonical_encoded_paths():
    tree = full_tree()
    fact_uid = "fact/甲\\~current.json"
    add_fact(tree, fact_uid)
    assert validate(tree) is None
    del tree[git_format.record_path("placement_current", fact_uid=fact_uid)]
    reject(tree, fact_uid)


def test_placement_revision_is_independent_of_fact_revision_and_not_fixed_at_one():
    tree = full_tree()
    placement = tree.pop(PLACEMENT_REVISION)
    placement["revision"] = 7
    tree[git_format.record_path("placement_revision", fact_uid="fact-1", revision=7)] = placement
    tree[PLACEMENT_CURRENT]["revision"] = 7
    assert tree[FACT_CURRENT]["revision"] == 2
    assert validate(tree) is None
