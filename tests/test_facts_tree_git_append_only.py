"""Pinned checks for the append-only retention validator.

Covers the approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f
sections 10.2.2 and 14.2: history revisions, evidence, operations, consumed
candidates and event files are append-only, old versions are never rewritten
in place or deleted, and current pointers may change. The validator is a
pure function over caller-supplied (path, oid) entries; repository_meta is
outside its append-only set because the accept() gate already pins the
repository identity record separately.
"""
import pytest

from app.facts_tree import git_append_only
from app.facts_tree import git_format

_OID_A = "a" * 40
_OID_B = "b" * 40

_APPEND_ONLY_PATHS = {
    "entity_revision": git_format.record_path(
        "entity_revision", entity_uid="ent-1", revision="r1"),
    "fact_revision": git_format.record_path(
        "fact_revision", fact_uid="fact-1", revision="r1"),
    "source_version": git_format.record_path(
        "source_version", source_id="src-1", version_id="v1"),
    "source_lifecycle": git_format.record_path(
        "source_lifecycle", source_id="src-1", revision="r1"),
    "evidence": git_format.record_path("evidence", evidence_id="ev-1"),
    "link_revision": git_format.record_path(
        "link_revision", link_id="link-1", revision="r1"),
    "placement_revision": git_format.record_path(
        "placement_revision", fact_uid="fact-1", revision="r1"),
    "issue_event": git_format.record_path(
        "issue_event", issue_uid="issue-1", event_id="ev-1"),
    "proposal_event": git_format.record_path(
        "proposal_event", proposal_uid="prop-1", event_id="ev-1"),
    "input_candidate": git_format.record_path(
        "input_candidate", candidate_uid="cand-1", candidate_revision="r1"),
    "operation": git_format.record_path("operation", operation_id="op-1"),
    "consumed_candidate": git_format.record_path(
        "consumed_candidate", candidate_uid="cand-1",
        candidate_revision="r1"),
    "event": git_format.record_path("event", event_id="ev-1"),
}

_MUTABLE_PATHS = {
    "taxonomy_manifest": git_format.record_path(
        "taxonomy_manifest", pack_version="pack-1"),
    "taxonomy_leaf": git_format.record_path(
        "taxonomy_leaf", pack_version="pack-1", leaf_id="leaf-1"),
    "bindings_current": git_format.record_path("bindings_current"),
    "entity_current": git_format.record_path(
        "entity_current", entity_uid="ent-1"),
    "fact_current": git_format.record_path(
        "fact_current", fact_uid="fact-1"),
    "placement_current": git_format.record_path(
        "placement_current", fact_uid="fact-1"),
}


def test_append_only_family_set_is_pinned():
    assert git_append_only.APPEND_ONLY_RECORD_TYPES == tuple(sorted(
        _APPEND_ONLY_PATHS))
    for record_type, path in _APPEND_ONLY_PATHS.items():
        assert git_format.canonical_record_type(path) == record_type


def test_empty_and_identical_snapshots_pass():
    assert git_append_only.validate_append_only_retention([], []) is None
    entries = [(path, _OID_A) for path in _APPEND_ONLY_PATHS.values()]
    assert git_append_only.validate_append_only_retention(
        entries, list(entries)) is None


def test_new_append_only_paths_are_legal():
    base = [(_APPEND_ONLY_PATHS["event"], _OID_A)]
    new = base + [
        (git_format.record_path("event", event_id="ev-2"), _OID_B),
        (git_format.record_path(
            "fact_revision", fact_uid="fact-1", revision="r2"), _OID_B),
    ]
    assert git_append_only.validate_append_only_retention(base, new) is None


def test_mutable_and_unknown_paths_may_change_or_disappear():
    base = [(path, _OID_A) for path in _MUTABLE_PATHS.values()]
    base.append(("notes/free-form.json", _OID_A))
    # Every mutable or unknown path is rewritten or deleted; none of them
    # is covered by this validator.
    new = [(_MUTABLE_PATHS["fact_current"], _OID_B)]
    assert git_append_only.validate_append_only_retention(base, new) is None


def test_non_canonical_alias_path_gets_no_retention_guarantee():
    # "ev~2D1" decodes to "ev-1" but is not the canonical encoding, so
    # canonical_record_type returns None and the path is not append-only.
    canonical = _APPEND_ONLY_PATHS["evidence"]
    alias = canonical.replace("ev-1", "ev~2D1")
    assert git_format.canonical_record_type(alias) is None
    assert git_append_only.validate_append_only_retention(
        [(alias, _OID_A)], []) is None


@pytest.mark.parametrize("record_type", sorted(_APPEND_ONLY_PATHS))
def test_deleted_append_only_record_is_rejected(record_type):
    path = _APPEND_ONLY_PATHS[record_type]
    with pytest.raises(ValueError, match="was deleted"):
        git_append_only.validate_append_only_retention(
            [(path, _OID_A)], [])


@pytest.mark.parametrize("record_type", sorted(_APPEND_ONLY_PATHS))
def test_rewritten_append_only_record_is_rejected(record_type):
    path = _APPEND_ONLY_PATHS[record_type]
    with pytest.raises(ValueError, match="rewritten in place"):
        git_append_only.validate_append_only_retention(
            [(path, _OID_A)], [(path, _OID_B)])


def test_violation_report_is_stable_in_sorted_path_order():
    deleted = sorted(_APPEND_ONLY_PATHS.values())
    base = [(path, _OID_A) for path in reversed(deleted)]
    with pytest.raises(ValueError) as excinfo:
        git_append_only.validate_append_only_retention(base, [])
    assert repr(deleted[0]) in str(excinfo.value)
    # Reordering the input does not change the reported path.
    with pytest.raises(ValueError) as excinfo_reordered:
        git_append_only.validate_append_only_retention(
            list(reversed(base)), [])
    assert str(excinfo.value) == str(excinfo_reordered.value)


@pytest.mark.parametrize("position", ["base", "new"])
def test_duplicate_paths_are_rejected(position):
    path = _APPEND_ONLY_PATHS["event"]
    pair = [(path, _OID_A), (path, _OID_B)]
    args = {"base": [], "new": []}
    args[position] = pair
    with pytest.raises(ValueError, match="duplicated"):
        git_append_only.validate_append_only_retention(
            args["base"], args["new"])


def test_wrong_container_types_are_rejected():
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention("events/x.json", [])
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention([], b"entries")
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention(None, [])


def test_wrong_entry_shapes_are_rejected():
    entry = (_APPEND_ONLY_PATHS["event"], _OID_A)
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention(
            [list(entry)], [])
    with pytest.raises(ValueError):
        git_append_only.validate_append_only_retention(
            [(entry[0],)], [])
    with pytest.raises(ValueError):
        git_append_only.validate_append_only_retention(
            [], [(entry[0], entry[1], "extra")])


def test_wrong_field_types_are_rejected():
    path = _APPEND_ONLY_PATHS["event"]
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention([(b"bytes", _OID_A)], [])
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention([(path, 42)], [])
    with pytest.raises(TypeError):
        git_append_only.validate_append_only_retention([(path, None)], [])


def test_inputs_are_not_mutated():
    base = [(_APPEND_ONLY_PATHS["event"], _OID_A)]
    new = [
        (_APPEND_ONLY_PATHS["event"], _OID_A),
        (_MUTABLE_PATHS["fact_current"], _OID_B),
    ]
    base_before = list(base)
    new_before = list(new)
    assert git_append_only.validate_append_only_retention(base, new) is None
    assert base == base_before
    assert new == new_before


def test_generators_are_accepted():
    path = _APPEND_ONLY_PATHS["operation"]
    base = (entry for entry in [(path, _OID_A)])
    new = (entry for entry in [(path, _OID_A)])
    assert git_append_only.validate_append_only_retention(base, new) is None
