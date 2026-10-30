"""Frozen D5 envelope contract, including the real D2/D3 serving chain."""
from contextlib import closing
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from types import SimpleNamespace

import pytest

from app.facts_tree.projection_status import (
    IndexStatus, ProjectionStatus, ProjectionStatusError, build_projection_status,
)


@dataclass(frozen=True)
class FakeServing:
    generation_id: str = "generation-test"
    db_path: str = "/must/not/be/opened.sqlite"
    repo_id: str = "repo-test"
    company_id: str = "company-test"
    projected_commit: str = "a" * 40
    head_oid: str = "b" * 40
    object_format: str = "sha1"
    render_sha256: str = "0" * 64
    record_count: int = 19
    active_fact_count: int = 3
    projection_schema_version: int = 1
    builder_version: int = 1
    renderer_version: int = 1
    required_indexes: tuple = ("canonical_records",)


def test_complete_envelope():
    serving = FakeServing()
    status = build_projection_status("a" * 40, serving, snapshot_commit="a" * 40)
    assert status == ProjectionStatus(
        "repo-test", "company-test", "a" * 40, "a" * 40, "generation-test",
        "a" * 40, "b" * 40, "sha1", False, True, True, True,
        (IndexStatus("canonical_records", True),), 19, 3)


@pytest.mark.parametrize("head,snapshot,pending,snapshot_current,current,ready", [
    ("b" * 40, "b" * 40, True, True, False, False),
    ("a" * 40, "b" * 40, False, False, True, False),
    ("a" * 40, None, False, None, True, False),
])
def test_normal_nonready_states(head, snapshot, pending, snapshot_current, current, ready):
    status = build_projection_status(head, FakeServing(), snapshot_commit=snapshot)
    assert (status.projection_pending, status.snapshot_current,
            status.serving_current, status.management_ready) == (
                pending, snapshot_current, current, ready)
    assert status.serving_commit == "a" * 40


def test_missing_indexes_and_ordered_deduplication():
    required = ["fts5", "canonical_records", "fts5"]
    status = build_projection_status("a" * 40, FakeServing(), snapshot_commit="a" * 40,
                                     required_indexes=required)
    assert status.indexes == (IndexStatus("fts5", False), IndexStatus("canonical_records", True))
    assert not status.management_ready
    assert required == ["fts5", "canonical_records", "fts5"]
    assert not build_projection_status("a" * 40, replace(FakeServing(), required_indexes=()),
                                       snapshot_commit="a" * 40).management_ready


@pytest.mark.parametrize("field", ["head_commit", "snapshot_commit", "projected_commit", "head_oid"])
@pytest.mark.parametrize("bad", ["A" * 40, "a" * 39, "a" * 41, "g" * 40, "", None, 40])
def test_bad_sha1_oids(field, bad):
    if field == "snapshot_commit" and bad is None:
        assert build_projection_status("a" * 40, FakeServing(), snapshot_commit=None).snapshot_current is None
        return
    kwargs = {"snapshot_commit": "a" * 40}
    serving, head = FakeServing(), "a" * 40
    if field in ("projected_commit", "head_oid"):
        serving = replace(serving, **{field: bad})
    elif field == "head_commit":
        head = bad
    else:
        kwargs[field] = bad
    with pytest.raises(ProjectionStatusError):
        build_projection_status(head, serving, **kwargs)


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "a" * 65, "x" * 64, ""])
@pytest.mark.parametrize("field", ["head_commit", "snapshot_commit", "projected_commit", "head_oid"])
def test_sha256_guardrails(field, bad):
    serving = replace(FakeServing(), object_format="sha256", projected_commit="a" * 64,
                      head_oid="b" * 64)
    assert build_projection_status("a" * 64, serving, snapshot_commit="a" * 64).management_ready
    kwargs, head = {"snapshot_commit": "a" * 64}, "a" * 64
    if field in ("projected_commit", "head_oid"):
        serving = replace(serving, **{field: bad})
    elif field == "head_commit":
        head = bad
    else:
        kwargs[field] = bad
    with pytest.raises(ProjectionStatusError):
        build_projection_status(head, serving, **kwargs)


@pytest.mark.parametrize("bad", ["sha512", "SHA1", "", None, [], 1])
def test_bad_object_format(bad):
    with pytest.raises(ProjectionStatusError):
        build_projection_status("a" * 40, replace(FakeServing(), object_format=bad))


@pytest.mark.parametrize("field", [field.name for field in fields(FakeServing)])
def test_every_serving_field_is_required(field):
    values = {item.name: getattr(FakeServing(), item.name) for item in fields(FakeServing)}
    del values[field]
    with pytest.raises(ProjectionStatusError):
        build_projection_status("a" * 40, SimpleNamespace(**values))


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
@pytest.mark.parametrize("bad", ["", None, 1])
def test_bad_scope(field, bad):
    with pytest.raises(ProjectionStatusError):
        build_projection_status("a" * 40, replace(FakeServing(), **{field: bad}))


@pytest.mark.parametrize("bad", [None, "canonical_records", {"canonical_records"}, 1,
                                  ("",), (1,), (None,)])
@pytest.mark.parametrize("serving_side", [False, True])
def test_bad_index_lists(bad, serving_side):
    with pytest.raises(ProjectionStatusError):
        if serving_side:
            build_projection_status("a" * 40, replace(FakeServing(), required_indexes=bad))
        else:
            build_projection_status("a" * 40, FakeServing(), required_indexes=bad)


def test_empty_required_and_duplicate_serving_indexes():
    for empty in ([], ()):
        with pytest.raises(ProjectionStatusError):
            build_projection_status("a" * 40, FakeServing(), required_indexes=empty)
    with pytest.raises(ProjectionStatusError):
        build_projection_status("a" * 40, replace(FakeServing(), required_indexes=("x", "x")))


def test_counts_determinism_and_freezing():
    serving = replace(FakeServing(), record_count=123, active_fact_count=0)
    first = build_projection_status("a" * 40, serving)
    assert first == build_projection_status("a" * 40, serving)
    assert (first.generation_record_count, first.generation_active_fact_count) == (123, 0)
    with pytest.raises(FrozenInstanceError):
        first.management_ready = True
    with pytest.raises(FrozenInstanceError):
        first.indexes[0].ready = False


def test_real_generation_build_publish_open(tmp_path):
    from app.facts_tree import git_format
    from app.facts_tree.record_tree import build_record_tree
    from app.facts_tree.projection_plan import build_projection_plan
    from app.facts_tree.projection_generation import build_projection_generation
    from app.facts_tree.projection_router import ServingGeneration, open_current, publish_generation
    from tests.facts_tree_full_fixture import SCOPE, full_tree

    payloads = full_tree()
    payloads[git_format.record_path("repository_meta")] = {
        "schema_version": 1, "format_version": 1, "installation_id": "install-test", **SCOPE}
    records = [(path, git_format.dumps_record(payload)) for path, payload in payloads.items()]
    tree = build_record_tree(records, **SCOPE, commit_oid="c" * 40, head_oid="d" * 40)
    active = frozenset(value["fact_uid"] for value in tree.ordered.values()
                       if value.get("record_type") == "fact_current" and value["status"] == "active")
    directory, root = tmp_path / "generations", tmp_path / "registry"
    built = build_projection_generation(build_projection_plan(tree), records, active, directory,
                                        expected_repo_id=SCOPE["repo_id"],
                                        expected_company_id=SCOPE["company_id"])
    published = publish_generation(built, root, directory, expected_repo_id=SCOPE["repo_id"],
                                   expected_company_id=SCOPE["company_id"])
    serving, connection = open_current(root, **SCOPE)
    with closing(connection):
        assert isinstance(serving, ServingGeneration)
        assert serving == published
        for head, ready in (("c" * 40, True), ("d" * 40, False)):
            status = build_projection_status(head, serving, snapshot_commit=head)
            assert status.management_ready is ready
            assert status.serving_current is ready
            assert status.projection_pending is (not ready)
            assert status.snapshot_current is True
            assert status == ProjectionStatus(
                serving.repo_id, serving.company_id, head, head, serving.generation_id,
                serving.projected_commit, serving.head_oid, serving.object_format,
                not ready, True, ready, ready, (IndexStatus("canonical_records", True),),
                serving.record_count, serving.active_fact_count)
