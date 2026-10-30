"""Appendix B v1.1 section 3: mutations through the public closure entry."""

from copy import deepcopy
from hashlib import sha256

import pytest

from app.facts_tree import git_format
from app.facts_tree.record_schema import (
    CLOSURE_VALIDATORS, RecordSchemaClosureError, validate_record_schema_closure,
)
from app.facts_tree.record_schema._primitives import _target_index
from tests.facts_tree_full_fixture import SCOPE, full_tree, with_dangling_fact_current


# Test identities, never a second implementation of the path vocabulary.
IDENTITIES = {
    "entity_revision": ("entity_uid", "entity-1", "revision", 1),
    "fact_revision": ("fact_uid", "fact-1", "revision", 1),
    "link_revision": ("link_id", "link-1", "revision", 1),
    "placement_revision": ("fact_uid", "fact-1", "revision", 1),
    "source_lifecycle": ("source_id", "src-1", "revision", 1),
    "source_version": ("source_id", "src-1", "version_id", "opaque-version"),
    "issue_event": ("issue_uid", "issue-1", "event_id", "issue-event-1"),
    "proposal_event": ("proposal_uid", "proposal-1", "event_id", "proposal-event-1"),
}
REVISIONS = tuple(family for family in IDENTITIES if family.endswith("revision")) + (
    "source_lifecycle",
)
EVENTS = ("issue_event", "proposal_event")
CURRENTS = {
    "entity_revision": "entity_current",
    "fact_revision": "fact_current",
    "placement_revision": "placement_current",
}


def _path(family, identity=None, uid=None):
    uid_field, default_uid, identity_field, default_identity = IDENTITIES[family]
    return git_format.record_path(family, **{
        uid_field: default_uid if uid is None else uid,
        identity_field: default_identity if identity is None else identity,
    })


def _ref(family, identity=None, uid=None):
    _, default_uid, _, default_identity = IDENTITIES[family]
    return {"record_type": family, "stable_id": default_uid if uid is None else uid,
            "revision": default_identity if identity is None else identity}


def _add(tree, family, identity, uid=None, **changes):
    uid_field, default_uid, identity_field, _ = IDENTITIES[family]
    payload = deepcopy(tree[_path(family)])
    payload.update({uid_field: default_uid if uid is None else uid,
                    identity_field: identity, **changes})
    path = _path(family, identity, uid)
    tree[path] = payload
    return path


def _revisions(family, numbers=(1, 2)):
    tree = full_tree()
    for i, revision in enumerate(numbers):
        changes = {"predecessor_ref": None if i == 0 else _ref(family, numbers[i - 1])}
        if family == "fact_revision":
            changes["revision_kind"] = "initial" if i == 0 else "correct"
        _add(tree, family, revision, **changes)
    if family in CURRENTS:
        uid_field, uid, _, _ = IDENTITIES[family]
        current_path = git_format.record_path(CURRENTS[family], **{uid_field: uid})
        tree[current_path]["revision"] = max(numbers)
    return tree


def _check(tree):
    # Mutating the original issue also changes its broker event digest. Keep
    # that unrelated closure-1 guard valid so closure 2 receives the mutation.
    supplied = deepcopy(tree)
    event = supplied[git_format.record_path("event", event_id="event-1")]
    event["after"] = [sha256(git_format.dumps_record(
        supplied[_path("issue_event")])).hexdigest()]
    return validate_record_schema_closure(supplied, **SCOPE)


def _reject(tree, family, path, code, field="predecessor_ref"):
    with pytest.raises(RecordSchemaClosureError) as caught:
        _check(tree)
    error = caught.value
    assert (error.family, error.path, error.code, error.field) == (
        family, path, code, field)


def test_full_fixture_and_empty_tree_pass_without_mutation():
    tree = full_tree()
    original = deepcopy(tree)
    assert validate_record_schema_closure(tree, **SCOPE) is None
    assert tree == original
    assert validate_record_schema_closure({}, **SCOPE) is None
    assert 2 in CLOSURE_VALIDATORS


@pytest.mark.parametrize("family", REVISIONS)
def test_2_1_wrong_predecessor_family(family):
    tree = _revisions(family)
    path = _path(family, 2)
    other = "fact_revision" if family == "entity_revision" else "entity_revision"
    tree[path]["predecessor_ref"] = _ref(other)
    _reject(tree, family, path, "predecessor_wrong_family")


@pytest.mark.parametrize("family", REVISIONS)
def test_2_1_and_2_4_cross_identity(family):
    tree = _revisions(family)
    _add(tree, family, 1, uid="other-uid", predecessor_ref=None)
    path = _path(family, 2)
    tree[path]["predecessor_ref"] = _ref(family, 1, "other-uid")
    _reject(tree, family, path, "predecessor_cross_identity")


@pytest.mark.parametrize("family", REVISIONS)
def test_2_1_predecessor_must_be_earlier(family):
    tree = _revisions(family, (1, 2, 3))
    # A forward edge without a cycle isolates the ordering rule.
    tree[_path(family, 3)]["predecessor_ref"] = None
    path = _path(family, 2)
    tree[path]["predecessor_ref"] = _ref(family, 3)
    _reject(tree, family, path, "predecessor_revision_not_earlier")


@pytest.mark.parametrize("family", REVISIONS)
def test_2_1_missing_target_is_still_owned_by_closure_1(family):
    tree = _revisions(family)
    path = _path(family, 2)
    tree[path]["predecessor_ref"] = _ref(family, 99)
    _reject(tree, family, path, "unresolved_typed_ref", "$.predecessor_ref")


@pytest.mark.parametrize("family", REVISIONS)
def test_2_2_revision_fork(family):
    tree = _revisions(family, (1, 2, 3))
    path = _path(family, 3)
    tree[path]["predecessor_ref"] = _ref(family, 1)
    _reject(tree, family, path, "revision_chain_fork")


@pytest.mark.parametrize("family", REVISIONS)
@pytest.mark.parametrize("self_loop", (False, True))
def test_2_3_revision_cycle(family, self_loop):
    tree = _revisions(family)
    # Remove initial semantics so it cannot mask the cycle diagnostic.
    if family == "fact_revision":
        tree[_path(family, 1)]["revision_kind"] = "correct"
    if self_loop:
        tree[_path(family, 2)]["predecessor_ref"] = _ref(family, 2)
        path = _path(family, 2)
    else:
        tree[_path(family, 1)]["predecessor_ref"] = _ref(family, 2)
        path = _path(family, 1)
    _reject(tree, family, path, "revision_chain_cycle")


@pytest.mark.parametrize("family", REVISIONS)
def test_2_5_revision_gaps_are_allowed(family):
    tree = _revisions(family, (1, 2, 11))
    assert _check(tree) is None


def test_2_6_initial_with_predecessor():
    tree = full_tree()
    path = _path("fact_revision", 2)
    tree[path]["revision_kind"] = "initial"
    _reject(tree, "fact_revision", path, "initial_with_predecessor")


def test_2_6_imported_noninitial_root_is_allowed():
    tree = full_tree()
    tree[_path("fact_revision")]["revision_kind"] = "correct"
    assert _check(tree) is None


@pytest.mark.parametrize("fault,code", (
    ("family", "predecessor_wrong_family"),
    ("identity", "predecessor_cross_identity"),
    ("fork", "source_version_chain_fork"),
    ("cycle", "revision_chain_cycle"),
    ("self_loop", "revision_chain_cycle"),
))
def test_2_7_source_version_chains(fault, code):
    family = "source_version"
    field = "prior_version_ref"
    tree = full_tree()
    path = _add(tree, family, "version-next", seq=2,
                prior_version_ref=_ref(family))
    if fault == "family":
        tree[path][field] = _ref("entity_revision")
    elif fault == "identity":
        _add(tree, family, "other-version", uid="other-source", prior_version_ref=None)
        tree[path][field] = _ref(family, "other-version", "other-source")
    elif fault == "fork":
        path = _add(tree, family, "zz-fork", seq=3, prior_version_ref=_ref(family))
    elif fault == "self_loop":
        tree[path][field] = _ref(family, "version-next")
    else:
        tree[_path(family)][field] = _ref(family, "version-next")
        path = _path(family)
    _reject(tree, family, path, code, field)


@pytest.mark.parametrize("seq", (1, 2))
def test_2_8_source_seq_must_increase(seq):
    tree = full_tree()
    tree[_path("source_version")]["seq"] = 2
    path = _add(tree, "source_version", "not-a-number", seq=seq,
                prior_version_ref=_ref("source_version"))
    _reject(tree, "source_version", path, "source_seq_not_earlier", "prior_version_ref")


def test_source_version_ids_are_opaque_and_seq_need_not_be_contiguous():
    tree = full_tree()
    _add(tree, "source_version", "001/旧版", seq=19,
         prior_version_ref=_ref("source_version"))
    _add(tree, "source_version", "000/新版", seq=71,
         prior_version_ref=_ref("source_version", "001/旧版"))
    assert _check(tree) is None


@pytest.mark.parametrize("revision_family,current_family", CURRENTS.items())
@pytest.mark.parametrize("fault", ("absent_revision", "other_uid_only", "no_revisions"))
def test_2_9_current_must_resolve_same_uid(revision_family, current_family, fault):
    tree = full_tree()
    uid_field, uid, _, _ = IDENTITIES[revision_family]
    path = git_format.record_path(current_family, **{uid_field: uid})
    if fault == "absent_revision":
        tree[path]["revision"] = 99
    elif fault == "other_uid_only":
        _add(tree, revision_family, 99, uid="other-uid", predecessor_ref=None)
        tree[path]["revision"] = 99
    else:
        # A fresh current UID with no revision directory; existing refs stay valid.
        path = git_format.record_path(current_family, **{uid_field: "current-only"})
        tree[path] = {**tree[git_format.record_path(current_family, **{uid_field: uid})],
                      uid_field: "current-only"}
    _reject(tree, current_family, path, "current_dangling", "revision")


def test_dangling_current_fixture_helper():
    tree = full_tree()
    original = deepcopy(tree)
    mutated = with_dangling_fact_current(tree)
    _reject(mutated, "fact_current", git_format.record_path(
        "fact_current", fact_uid="fact-1"), "current_dangling", "revision")
    assert tree == original


@pytest.mark.parametrize("revision_family,current_family", CURRENTS.items())
def test_2_10_current_must_point_to_numeric_maximum(revision_family, current_family):
    tree = _revisions(revision_family, (1, 2, 11))
    uid_field, uid, _, _ = IDENTITIES[revision_family]
    path = git_format.record_path(current_family, **{uid_field: uid})
    tree[path]["revision"] = 2
    _reject(tree, current_family, path, "current_not_head", "revision")


def test_placement_current_resolves_placements_not_fact_revisions():
    tree = full_tree()
    # fact-1/2 exists but placement fact-1/2 does not.
    path = git_format.record_path("placement_current", fact_uid="fact-1")
    tree[path]["revision"] = 2
    _reject(tree, "placement_current", path, "current_dangling", "revision")


def test_2_11_lifecycle_double_tail():
    tree = full_tree()
    path = _add(tree, "source_lifecycle", 2, predecessor_ref=None)
    _reject(tree, "source_lifecycle", path, "lifecycle_double_tail")


@pytest.mark.parametrize("family", EVENTS)
@pytest.mark.parametrize("fault,code", (
    ("family", "predecessor_wrong_family"),
    ("identity", "predecessor_cross_identity"),
    ("fork", "event_chain_fork"),
    ("two_roots", "event_chain_fork"),
    ("cycle", "event_chain_cycle"),
    ("self_loop", "event_chain_cycle"),
))
def test_2_12_event_chain(family, fault, code):
    field = "predecessor_event_ref"
    tree = full_tree()
    path = _add(tree, family, "next-event", ordinal=2,
                predecessor_event_ref=_ref(family))
    if fault == "family":
        other = "proposal_event" if family == "issue_event" else "issue_event"
        tree[path][field] = _ref(other)
    elif fault == "identity":
        _add(tree, family, "other-event", uid="other-uid", predecessor_event_ref=None)
        tree[path][field] = _ref(family, "other-event", "other-uid")
    elif fault == "fork":
        path = _add(tree, family, "zz-fork", ordinal=3,
                    predecessor_event_ref=_ref(family))
    elif fault == "two_roots":
        tree[path][field] = None
        path = max(path, _path(family))
    elif fault == "cycle":
        tree[_path(family)][field] = _ref(family, "next-event")
        path = min(path, _path(family))
    else:
        tree[path][field] = _ref(family, "next-event")
    _reject(tree, family, path, code, field)


@pytest.mark.parametrize("family", EVENTS)
def test_2_13_ordinal_gaps_and_unfrozen_status_arcs_are_allowed(family):
    tree = full_tree()
    _add(tree, family, "next-event", ordinal=71, status="open",
         predecessor_event_ref=_ref(family))
    assert _check(tree) is None


@pytest.mark.parametrize("family", EVENTS)
def test_long_event_history_needs_no_recursion(family):
    tree = full_tree()
    predecessor = _ref(family)
    for i in range(1200):
        identity = f"event-{i:04d}"
        _add(tree, family, identity, ordinal=i + 2,
             predecessor_event_ref=predecessor)
        predecessor = _ref(family, identity)
    # Reverse lexical traversal follows the entire history on its first walk.
    ordered = dict(sorted(tree.items(), reverse=True))
    original = deepcopy(ordered)
    assert CLOSURE_VALIDATORS[2](ordered, _target_index(ordered)) is None
    assert ordered == original


@pytest.mark.parametrize("family", tuple(IDENTITIES))
def test_independent_uids_do_not_share_successors(family):
    tree = full_tree()
    _, _, identity_field, _ = IDENTITIES[family]
    second = 2 if identity_field == "revision" else "next-id"
    field = ("prior_version_ref" if family == "source_version" else
             "predecessor_event_ref" if family in EVENTS else "predecessor_ref")
    for uid in (None, "other-uid"):
        if uid is not None:
            _add(tree, family, IDENTITIES[family][3], uid=uid, **{field: None})
        changes = {field: _ref(family, uid=uid)}
        if family == "source_version":
            changes["seq"] = 2
        elif family in EVENTS:
            changes["ordinal"] = 2
        elif family == "fact_revision":
            changes["revision_kind"] = "correct"
        _add(tree, family, second, uid=uid, **changes)
    if family in CURRENTS:
        uid_field, uid, _, _ = IDENTITIES[family]
        tree[git_format.record_path(CURRENTS[family], **{uid_field: uid})]["revision"] = 2
    assert _check(tree) is None
