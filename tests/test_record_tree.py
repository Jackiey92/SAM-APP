"""Appendix C v1.2: pure assembly and synthetic tmp_path Git integration."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest

from app.facts_tree import git_format, record_tree
from app.facts_tree.git_repository import (
    FactsGitRepository, SnapshotReadError, SnapshotRecord,
)
from app.facts_tree.record_schema import (
    RecordSchemaClosureError, SCHEMA_BUILDERS, SCHEMA_FIELDS,
    loads_schema_record, validate_record_schema_closure,
)
from app.facts_tree.record_tree import build_record_tree, read_validated_record_tree
from tests.facts_tree_full_fixture import SCOPE, full_tree
from tests.test_facts_tree_git_repository import (
    _IDS, _META_PATH, _all_object_oids, _git_text, _head, _init_repo,
    _meta_payload,
)
from tests.test_facts_tree_record_schema_closure import binding_tree


OIDS = {"commit_oid": "c" * 40, "head_oid": "d" * 40}
META = git_format.record_path("repository_meta")
FACT_CURRENT = git_format.record_path("fact_current", fact_uid="fact-1")
FACT = git_format.record_path("fact_revision", fact_uid="fact-1", revision=1)


def _tree():
    tree = full_tree()
    tree[META] = {"schema_version": 1, "format_version": 1,
                  "installation_id": "install-test", **SCOPE}
    return tree


def _records(tree):
    return [(path, git_format.dumps_record(payload)) for path, payload in tree.items()]


def _build(records, **overrides):
    return build_record_tree(records, **{**SCOPE, **OIDS, **overrides})


def _coordinates(error):
    return error.family, error.path, error.code, error.field


def test_full_tree_lossless_sorted_index_determinism_and_input_unchanged():
    tree = _tree()
    # Exact decimal precision, original text and arbitrary JSON survive assembly.
    tree[FACT]["canonical_value"] = {"amount": Decimal("123456789.00100"),
                                      "text": "尚未收款\n原文", "array": [False, None]}
    records = _records(tree)
    before = deepcopy(records)
    result = _build(iter(reversed(records)))
    assert result == _build(records)
    assert records == before
    assert dict(result.ordered) == tree
    assert list(result.ordered) == sorted(tree)
    assert result.commit_oid == OIDS["commit_oid"]
    assert result.head_oid == OIDS["head_oid"]
    assert (result.repo_id, result.company_id, result.object_format) == (
        SCOPE["repo_id"], SCOPE["company_id"], "sha1")
    assert result.record_count == len(tree) == sum(map(len, result.families.values()))
    assert set(result.families) == set(SCHEMA_FIELDS) | {"repository_meta"}
    for family, paths in result.families.items():
        assert paths == tuple(sorted(p for p in tree
                                     if git_format.canonical_record_type(p) == family))
    expected = {
        ("entity_revision", "entity-1", 1), ("entity_revision", "entity-1", 2),
        ("fact_revision", "fact-1", 1), ("fact_revision", "fact-1", 2),
        ("source_version", "src-1", "opaque-version"),
        ("source_lifecycle", "src-1", 1), ("evidence", "evidence-1", None),
        ("link_revision", "link-1", 1), ("placement_revision", "fact-1", 1),
        ("issue_event", "issue-1", "issue-event-1"),
        ("proposal_event", "proposal-1", "proposal-event-1"),
        ("input_candidate", "candidate-一", 1),
        ("consumed_candidate", "candidate-一", 1),
        ("operation", "operation-1", None), ("event", "event-1", None),
    }
    assert set(result.index) == expected
    for key, path in result.index.items():
        assert git_format.canonical_record_type(path) == key[0]
        assert tuple(git_format._match_template(key[0], path).values()) == (
            key[1:2] if key[2] is None else key[1:])
    assert validate_record_schema_closure(dict(result.ordered), **SCOPE) is None
    for path, raw in records:
        assert git_format.dumps_record(result.ordered[path]) == raw
    # The two independently parsed results do not share payload containers.
    other = _build(records)
    other.ordered[FACT]["canonical_value"]["array"].append("local")
    assert result.ordered[FACT]["canonical_value"]["array"] == [False, None]
    assert records == before


def test_snapshot_records_and_public_closure_called_once_before_views(monkeypatch):
    real_validate = record_tree.validate_record_schema_closure
    calls = []

    def validate(tree, **kwargs):
        assert type(tree) is dict
        calls.append(deepcopy(tree))
        return real_validate(tree, **kwargs)

    monkeypatch.setattr(record_tree, "validate_record_schema_closure", validate)
    tree = _tree()
    result = _build(SnapshotRecord(p, raw) for p, raw in _records(tree))
    assert calls == [tree]
    assert isinstance(result.ordered, MappingProxyType)


def test_all_unregistered_families_and_binding_event_logical_index():
    tree, paths, current = binding_tree()
    tree[META] = {**SCOPE}
    manifest = git_format.record_path("taxonomy_manifest", pack_version="opaque-pack")
    leaf = git_format.record_path("taxonomy_leaf", pack_version="opaque-pack", leaf_id="leaf-1")
    tree[manifest] = {"pack": "generic", "version": 7}
    tree[leaf] = {"code": "leaf-1", "title": "标题"}
    assert validate_record_schema_closure(tree, **SCOPE) is None
    result = _build(_records(tree))
    assert {"repository_meta", "taxonomy_manifest", "taxonomy_leaf",
            "bindings_current"} <= set(result.families)
    assert result.index[("bindings_current", "current", 1)] == paths[3]
    assert result.index[("event", "event-1", None)] == paths[3]
    assert result.index[("taxonomy_manifest", "opaque-pack", None)] == manifest
    assert result.index[("taxonomy_leaf", "opaque-pack", "leaf-1")] == leaf
    assert current not in result.index.values()
    assert META not in result.index.values()


@pytest.mark.parametrize("records", [[], [(META, git_format.dumps_record(SCOPE))]])
def test_empty_and_metadata_only_trees(records):
    result = _build(records)
    assert result.record_count == len(records)
    assert dict(result.index) == {}
    assert validate_record_schema_closure(dict(result.ordered), **SCOPE) is None
    assert result == _build(records)


@pytest.mark.parametrize("attribute", ["ordered", "index", "families"])
def test_mapping_views_are_readonly(attribute):
    result = _build(_records(_tree()))
    view = getattr(result, attribute)
    with pytest.raises(TypeError):
        view["new"] = "value"
    with pytest.raises(TypeError):
        del view[next(iter(view))]
    with pytest.raises(FrozenInstanceError):
        result.record_count = 0


@pytest.mark.parametrize("raw,code", [
    (b'{"repo_id":"repo-test","company_id":"company-test"}', "noncanonical_encoding"),
    (b'{\n  "repo_id": "repo-test",\n  "company_id": "company-test"\n}\n',
     "noncanonical_encoding"),
    (b'{"repo_id":"repo-test","repo_id":"repo-test"}', "record_encoding"),
    (b'\xff', "record_encoding"), (b'not-json', "record_encoding"),
])
def test_encoding_errors_match_public_loader(raw, code):
    with pytest.raises(RecordSchemaClosureError) as direct:
        loads_schema_record(raw, path=META, **SCOPE)
    with pytest.raises(RecordSchemaClosureError) as resolved:
        _build([(META, raw)])
    assert _coordinates(resolved.value) == _coordinates(direct.value)
    assert resolved.value.code == code


@pytest.mark.parametrize("path", ["random/stranger.json", "../meta/repository.json",
                                  "entities/entity-1/revisions/01.json"])
def test_noncanonical_paths_match_public_entry(path):
    tree = {path: SCOPE}
    with pytest.raises(RecordSchemaClosureError) as direct:
        validate_record_schema_closure(tree, **SCOPE)
    with pytest.raises(RecordSchemaClosureError) as resolved:
        _build(_records(tree))
    assert _coordinates(resolved.value) == _coordinates(direct.value)
    assert resolved.value.code == "record_path"


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
def test_metadata_scope_mismatch_matches_public_entry(field):
    tree = _tree()
    tree[META][field] = "foreign"
    with pytest.raises(RecordSchemaClosureError) as direct:
        validate_record_schema_closure(tree, **SCOPE)
    with pytest.raises(RecordSchemaClosureError) as resolved:
        _build(_records(tree))
    assert _coordinates(resolved.value) == _coordinates(direct.value)
    assert resolved.value.code == "scope_mismatch"


@pytest.mark.parametrize("case,code", [
    ("schema", "missing_field"), ("closure1", "unresolved_typed_ref"),
    ("closure2", "current_dangling"), ("closure3", "located_text_binding"),
    ("closure4", "active_fact_without_primary_placement"),
    ("closure5", "missing_consumed"),
])
def test_schema_and_each_closure_reject_with_public_coordinates(case, code):
    tree = _tree()
    if case == "schema":
        del tree[FACT]["schema_version"]
    elif case == "closure1":
        tree[FACT]["entity_ref"]["stable_id"] = "missing-entity"
    elif case == "closure2":
        tree[FACT_CURRENT]["revision"] = 99
    elif case == "closure3":
        tree[FACT]["qualifiers"]["entity"]["text"] = None
    elif case == "closure4":
        del tree[git_format.record_path("placement_current", fact_uid="fact-1")]
    elif case == "closure5":
        del tree[git_format.record_path("consumed_candidate", candidate_uid="candidate-一",
                                       candidate_revision=1)]
    before = deepcopy(tree)
    with pytest.raises(RecordSchemaClosureError) as direct:
        validate_record_schema_closure(tree, **SCOPE)
    with pytest.raises(RecordSchemaClosureError) as resolved:
        _build(_records(tree))
    assert _coordinates(resolved.value) == _coordinates(direct.value)
    assert resolved.value.code == code
    assert tree == before


def test_duplicate_path_is_assembly_value_error():
    record = (META, git_format.dumps_record(SCOPE))
    with pytest.raises(ValueError, match="duplicate record path") as caught:
        _build([record, record])
    assert type(caught.value) is ValueError


@pytest.mark.parametrize("content", ["{}\n", bytearray(b'{}\n'), memoryview(b'{}\n'), None, 1])
def test_nonbytes_is_assembly_type_error(content):
    with pytest.raises(TypeError, match="content must be bytes"):
        _build([(META, content)])


@pytest.mark.parametrize("path", [None, 1, b'meta/repository.json'])
def test_nonstr_path_is_assembly_type_error(path):
    with pytest.raises(TypeError, match="path must be a str"):
        _build([(path, b'{}\n')])


@pytest.mark.parametrize("object_format", ["sha512", None, 1])
def test_invalid_object_format_including_empty_tree(object_format):
    with pytest.raises(RecordSchemaClosureError) as caught:
        _build([], object_format=object_format)
    assert _coordinates(caught.value) == ("tree", "<tree>", "object_format", "object_format")


def test_schema_and_storage_exceptions_pass_through_by_identity(monkeypatch):
    error = RecordSchemaClosureError("tree", META, "record_encoding", "$")

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(record_tree, "loads_schema_record", reject)
    with pytest.raises(RecordSchemaClosureError) as caught:
        _build([(META, b'{}\n')])
    assert caught.value is error
    for failure in (SnapshotReadError("synthetic refusal"), OSError("synthetic IO")):
        error = failure
        repository = SimpleNamespace(read_snapshot_at=reject)
        with pytest.raises(type(error)) as caught:
            read_validated_record_tree(repository, OIDS["commit_oid"])
        assert caught.value is error


def _repo_state(repo):
    return (_head(repo.path),
            _git_text(repo.path, "for-each-ref", "--format=%(refname) %(objectname)"),
            _all_object_oids(repo.path), (repo.path / "config").read_bytes())


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_read_fixed_commit_and_genesis_is_repeatable_and_readonly(tmp_path, object_format):
    repo = _init_repo(tmp_path, object_format=object_format)
    genesis = repo.genesis_oid
    scope = {key: _IDS[key] for key in SCOPE}
    tree = {_META_PATH: _meta_payload()}
    for family in ("entity_revision", "entity_current"):
        ids = {"entity_uid": "entity-1"}
        if family == "entity_revision":
            ids["revision"] = 1
        payload = SCHEMA_BUILDERS[family](**ids)
        payload.update(scope)
        tree[git_format.record_path(family, **ids)] = payload
    tree_oid = repo.build_tree(_records(tree), expected_paths=list(tree))
    commit = repo.create_commit(tree_oid, genesis)
    assert repo.accept(genesis, commit, expected_paths=list(tree)).accepted
    before = _repo_state(repo)
    for oid in (genesis, commit):
        result = read_validated_record_tree(repo, oid)
        assert result == read_validated_record_tree(repo, oid)
        assert result.commit_oid == oid
        assert result.head_oid == commit
        assert result.repo_id == _IDS["repo_id"]
        assert result.company_id == _IDS["company_id"]
        assert result.object_format == object_format
        assert result.families["repository_meta"] == (_META_PATH,)
        assert validate_record_schema_closure(dict(result.ordered), **scope,
                                              object_format=object_format) is None
        assert result.record_count == (1 if oid == genesis else 3)
    assert _repo_state(repo) == before


def test_unbound_repository_uses_existing_snapshot_refusal(tmp_path):
    repo = _init_repo(tmp_path)
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(SnapshotReadError) as direct:
        unbound.read_snapshot_at(repo.genesis_oid)
    with pytest.raises(SnapshotReadError) as resolved:
        read_validated_record_tree(unbound, repo.genesis_oid)
    assert str(resolved.value) == str(direct.value)
    assert "identity" in str(resolved.value)


def test_committed_dangling_current_is_rejected_without_repository_changes(tmp_path):
    repo = _init_repo(tmp_path)
    scope = {key: _IDS[key] for key in SCOPE}
    current = SCHEMA_BUILDERS["entity_current"](entity_uid="entity-1")
    current.update(scope, revision=99)
    path = git_format.record_path("entity_current", entity_uid="entity-1")
    tree = {_META_PATH: _meta_payload(), path: current}
    # The storage acceptance gate does not yet enforce business closures 1-5.
    # This accepted synthetic C isolates the resolver's additional validation.
    tree_oid = repo.build_tree(_records(tree), expected_paths=list(tree))
    commit = repo.create_commit(tree_oid, repo.genesis_oid)
    assert repo.accept(repo.genesis_oid, commit, expected_paths=list(tree)).accepted
    before = _repo_state(repo)
    with pytest.raises(RecordSchemaClosureError) as direct:
        validate_record_schema_closure(tree, **scope)
    with pytest.raises(RecordSchemaClosureError) as resolved:
        read_validated_record_tree(repo, commit)
    assert _coordinates(resolved.value) == _coordinates(direct.value)
    assert _coordinates(resolved.value) == ("entity_current", path, "current_dangling", "revision")
    assert _repo_state(repo) == before


def test_wrapper_reads_once_uses_captured_oids_and_bound_scope():
    calls = []
    snapshot = SimpleNamespace(records=[SnapshotRecord(META, git_format.dumps_record(SCOPE))],
                               **OIDS)

    def read(oid):
        calls.append(oid)
        return snapshot

    repository = SimpleNamespace(read_snapshot_at=read, _expected_meta=dict(SCOPE),
                                 object_format="sha1")
    assert read_validated_record_tree(repository, "requested", object_format="sha256") == (
        _build(snapshot.records, object_format="sha256"))
    assert calls == ["requested"]
    for field in SCOPE:
        with pytest.raises(TypeError):
            read_validated_record_tree(repository, "requested", **{field: "foreign"})
    assert calls == ["requested"]
