"""B3 v2 layer boundary: low-level accept remains a Git storage primitive."""
import pytest

from app.facts_tree import git_format, record_tree
from app.facts_tree import broker_tree_gate
from tests.test_facts_tree_git_repository import _init_repo, _META_PATH, _meta_payload


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_storage_accept_does_not_call_business_tree_gate(tmp_path, monkeypatch, object_format):
    def forbidden(*args, **kwargs):
        raise AssertionError("storage accept invoked business schema validation")

    monkeypatch.setattr(record_tree, "build_record_tree", forbidden)
    monkeypatch.setattr(broker_tree_gate, "validate_broker_tree", forbidden)
    repo = _init_repo(tmp_path, object_format=object_format)
    current = git_format.record_path("fact_current", fact_uid="fact-1")
    # Deliberately minimal record: exercise storage, not business semantics.
    records = [(_META_PATH, git_format.dumps_record(_meta_payload())),
               (current, git_format.dumps_record({"fact": 1}))]
    tree = repo.build_tree(records, expected_paths=[_META_PATH, current])
    commit = repo.create_commit(tree, repo.genesis_oid)
    assert repo.accept(repo.genesis_oid, commit,
                       expected_paths=[_META_PATH, current]).accepted
    assert repo.head_oid() == commit
