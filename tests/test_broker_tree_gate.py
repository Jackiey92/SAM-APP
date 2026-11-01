"""B3 v2: byte-only Broker gate, separate from storage acceptance."""
from copy import deepcopy
from types import MappingProxyType

import pytest

from app.facts_tree import git_format, record_tree
from app.facts_tree.broker_tree_gate import BrokerTreeGateError, validate_broker_tree
from app.facts_tree.record_schema import RecordSchemaClosureError, SCHEMA_BUILDERS
from tests.facts_tree_full_fixture import full_tree, SCOPE


def path(family, **ids):
    return git_format.record_path(family, **ids)


FACT = path("fact_revision", fact_uid="fact-1", revision=1)
CURRENT = path("fact_current", fact_uid="fact-1")
PLACEMENT = path("placement_current", fact_uid="fact-1")
INPUT = path("input_candidate", candidate_uid="candidate-一", candidate_revision=1)
OP = path("operation", operation_id="operation-1")
CONSUMED = path("consumed_candidate", candidate_uid="candidate-一", candidate_revision=1)
EVENT = path("event", event_id="event-1")


def _contents(tree):
    return {p: git_format.dumps_record(value) for p, value in tree.items()}


def _validate(contents, **kwargs):
    return validate_broker_tree(contents, **{**SCOPE, "commit_oid": "c" * 40,
                                            "head_oid": "d" * 40, **kwargs})


# Reuse v1 mutations with the same schema/closure diagnostics.
CASES = [
    ("missing_business", "missing_field"),
    ("missing_envelope", "missing_field"),
    ("schema_version", "unsupported_version"),
    ("format_version", "unsupported_version"),
    ("record_type", "record_type"),
    ("repo_scope", "scope_mismatch"),
    ("company_scope", "scope_mismatch"),
    ("closure1", "unresolved_typed_ref"),
    ("closure2", "current_dangling"),
    ("closure3", "located_text_binding"),
    ("closure4", "active_fact_without_primary_placement"),
    ("multiple_operations", "multiple_operations"),
    ("missing_consumed", "missing_consumed"),
    ("orphan_consumed", "orphan_consumed"),
    ("nonterminal_consumed", "nonterminal_consumed"),
    ("consumed_result_mismatch", "consumed_result_mismatch"),
]


def _mutate(tree, case):
    if case == "missing_business":
        del tree[FACT]["canonical_value"]
    elif case == "missing_envelope":
        del tree[FACT]["schema_version"]
    elif case in ("schema_version", "format_version"):
        tree[FACT][case] = 2
    elif case == "record_type":
        tree[FACT]["record_type"] = "entity_revision"
    elif case in ("repo_scope", "company_scope"):
        tree[FACT]["repo_id" if case == "repo_scope" else "company_id"] = "foreign"
    elif case == "closure1":
        tree[FACT]["entity_ref"]["stable_id"] = "absent"
    elif case == "closure2":
        tree[CURRENT]["revision"] = 99
    elif case == "closure3":
        tree[FACT]["qualifiers"]["entity"]["text"] = None
    elif case == "closure4":
        del tree[PLACEMENT]
    elif case == "multiple_operations":
        other = deepcopy(tree[OP])
        other.update(operation_id="operation-2", event_ids=[])
        tree[path("operation", operation_id="operation-2")] = other
    elif case == "missing_consumed":
        del tree[CONSUMED]
    elif case == "orphan_consumed":
        del tree[INPUT]
        tree[OP]["candidate_refs"] = []
        tree[OP]["disposition_result"] = []
    elif case == "nonterminal_consumed":
        tree[OP]["disposition_result"][0]["terminal"] = False
    elif case == "consumed_result_mismatch":
        tree[CONSUMED]["disposition_result"]["result_refs"] = [
            {"record_type": "evidence", "stable_id": "evidence-1"}]
    else:
        raise AssertionError(case)


def _tree(object_format="sha1"):
    tree = full_tree()
    tree[OP]["base_commit"] = tree[EVENT]["base_commit"] = "b" * (
        40 if object_format == "sha1" else 64)
    return tree


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("stage", ["genesis", "complete_unit", "append", "current_advance"])
def test_valid_complete_snapshots_and_context(stage, object_format):
    tree = _tree(object_format)
    if stage == "genesis":
        tree = {path("repository_meta"): {
            "schema_version": 1, "format_version": 1,
            "installation_id": "installation-test", **SCOPE}}
    elif stage in ("append", "current_advance"):
        entity = SCHEMA_BUILDERS["entity_revision"](entity_uid="entity-2", revision=1)
        entity.update(SCOPE)
        tree[path("entity_revision", entity_uid="entity-2", revision=1)] = entity
        if stage == "current_advance":
            revision = SCHEMA_BUILDERS["entity_revision"](entity_uid="entity-1", revision=3)
            revision.update(SCOPE, predecessor_ref={"record_type": "entity_revision",
                                                   "stable_id": "entity-1", "revision": 2})
            tree[path("entity_revision", entity_uid="entity-1", revision=3)] = revision
            tree[path("entity_current", entity_uid="entity-1")]["revision"] = 3
    contents = _contents(tree)
    before = deepcopy(contents)
    length = 40 if object_format == "sha1" else 64
    kwargs = {"object_format": object_format, "commit_oid": "c" * length,
              "head_oid": "d" * length}
    result = _validate(contents, **kwargs)
    assert isinstance(result, record_tree.ValidatedRecordTree)
    assert result == _validate(contents, **kwargs)
    assert contents == before
    assert dict(result.ordered) == tree
    assert result.record_count == len(tree)
    assert set(result.families) == {git_format.canonical_record_type(p) for p in tree}
    assert {p for paths in result.families.values() for p in paths} == set(tree)
    assert (result.repo_id, result.company_id, result.object_format) == (
        SCOPE["repo_id"], SCOPE["company_id"], object_format)
    assert result.commit_oid == kwargs["commit_oid"]
    assert result.head_oid == kwargs["head_oid"]


@pytest.mark.parametrize("case,code", CASES, ids=[case for case, _ in CASES])
def test_invalid_snapshots_fail_closed_with_original_code(case, code):
    tree = _tree()
    _mutate(tree, case)
    contents = _contents(tree)
    before = deepcopy(contents)
    for _ in range(2):
        with pytest.raises(BrokerTreeGateError) as caught:
            _validate(contents)
        assert isinstance(caught.value, ValueError)
        cause = caught.value.__cause__
        assert isinstance(cause, RecordSchemaClosureError)
        assert cause.code == code
        assert cause.path in tree
        assert cause.field
        assert str(cause) in str(caught.value)
        assert contents == before


@pytest.mark.parametrize("readonly", [False, True])
def test_exact_bytes_by_identity_and_zero_io(monkeypatch, readonly):
    import builtins
    import io
    import os
    import socket
    import subprocess
    from app.facts_tree.git_repository import FactsGitRepository

    contents = _contents(_tree())
    before = deepcopy(contents)
    supplied = MappingProxyType(contents) if readonly else contents
    real_build = record_tree.build_record_tree
    real_load = record_tree.loads_schema_record
    build_calls, loaded = [], []

    def observe_build(records, **kwargs):
        pairs = list(records)
        assert len(pairs) == len(contents)
        assert all(raw is supplied[p] for p, raw in pairs)
        build_calls.append(kwargs)
        return real_build(pairs, **kwargs)

    def observe_load(data, *, path, **kwargs):
        assert data is supplied[path]
        loaded.append(path)
        return real_load(data, path=path, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure gate attempted IO")

    monkeypatch.setattr(record_tree, "build_record_tree", observe_build)
    monkeypatch.setattr(record_tree, "loads_schema_record", observe_load)
    with monkeypatch.context() as guard:
        for obj, name in ((builtins, "open"), (io, "open"), (os, "open"),
                          (os, "mkdir"), (os, "system"), (socket, "socket"),
                          (socket, "create_connection"), (subprocess, "Popen"),
                          (FactsGitRepository, "_git"),
                          (FactsGitRepository, "_git_bytes"),
                          (FactsGitRepository, "_ref_value"),
                          (FactsGitRepository, "read_snapshot_at"),
                          (FactsGitRepository, "_read_commit_tree_entries")):
            guard.setattr(obj, name, forbidden)
        result = _validate(supplied)
        assert result == _validate(supplied)
    assert len(build_calls) == 2
    assert loaded == sorted(contents) * 2
    assert contents == before


@pytest.mark.parametrize("exception", [TypeError, ValueError])
def test_assembly_errors_preserve_cause_identity(monkeypatch, exception):
    failure = exception("synthetic assembly failure")

    def reject(*args, **kwargs):
        raise failure

    monkeypatch.setattr(record_tree, "build_record_tree", reject)
    with pytest.raises(BrokerTreeGateError, match="synthetic assembly failure") as caught:
        _validate({})
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("contents", [None, [], "bytes", 1])
def test_nonmapping_input_fails_closed(contents):
    with pytest.raises(BrokerTreeGateError, match="must be a mapping") as caught:
        _validate(contents)
    assert isinstance(caught.value.__cause__, TypeError)


@pytest.mark.parametrize("raw", ["{}\n", bytearray(b'{}\n'), None])
def test_nonbytes_values_fail_closed(raw):
    contents = {path("repository_meta"): raw}
    before = deepcopy(contents)
    with pytest.raises(BrokerTreeGateError, match="content must be bytes") as caught:
        _validate(contents)
    assert isinstance(caught.value.__cause__, TypeError)
    assert contents == before
