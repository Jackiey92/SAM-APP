"""Tests for app.facts_tree.operation_acceptance.

Every repository here is a synthetic bare repo created with Git
plumbing inside pytest tmp_path; nothing touches SAM-APP, SAM-DOCS,
data/, a real SAM_GIT_DIR, or any remote. The anomalies accept() would
refuse (deleted, re-added or rewritten operation records, mistyped or
foreign-identity payloads, non-canonical bytes, merges, missing or
damaged objects) are planted as raw plumbing commits and direct
update-ref moves, strictly inside these throwaway repositories, exactly
like the history contract matrix tests do. Synthetic plumbing helpers
are reused from tests/test_facts_tree_git_repository.py rather than
copied.
"""
import dataclasses

import pytest

from app.facts_tree import git_format
from app.facts_tree.git_repository import (
    REF_FACTS,
    FactsGitRepository,
    GitRepositoryError,
)
from app.facts_tree.operation_acceptance import (
    OperationAcceptanceError,
    OperationAcceptanceOrigin,
    locate_operation_acceptance,
)
from tests.test_facts_tree_git_repository import (
    _IDS,
    _META_PATH,
    _all_object_oids,
    _append_commit,
    _commit_tree,
    _git,
    _git_text,
    _head,
    _init_repo,
    _local_config_values,
    _loose_object_path,
    _overwrite_loose_object,
    _raw_refs_under,
    _tree_oid,
    _tree_with_added_file,
    _tree_with_removed_file,
    _write_blob,
)

_FSYNC_KEYS = ("core.fsync", "core.fsyncMethod")


def _operation_path(operation_id="op-1"):
    return git_format.record_path("operation", operation_id=operation_id)


def _operation_payload(operation_id="op-1", base_commit=None, **overrides):
    # The minimal operation record accept() closes inside one commit:
    # no candidate references and no events, bound to the accepting
    # commit's own parent through base_commit.
    payload = {
        "schema_version": 1,
        "record_type": "operation",
        "operation_id": operation_id,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
        "candidate_refs": [],
        "base_commit": base_commit,
        "event_ids": [],
    }
    payload.update(overrides)
    return payload


def _accept_operation(repo, tmp_path, operation_id="op-1",
                      index_name="index-op-accept"):
    # One legitimately accepted commit carrying exactly one new
    # operation record on top of the current head.
    head = repo.head_oid()
    path = _operation_path(operation_id)
    blob = _write_blob(repo.path, git_format.dumps_record(
        _operation_payload(operation_id, base_commit=head)))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, head), path, blob, tmp_path,
        index_name)
    commit = repo.create_commit(tree, head)
    existing = _git_text(
        repo.path, "ls-tree", "-r", "--name-only", head).split()
    assert repo.accept(
        head, commit, expected_paths=existing + [path]).accepted
    return commit


def _force_tip(repo, tree, parent, message):
    # A raw child commit moved onto the authoritative ref directly: the
    # only way to plant the append-only anomalies accept() itself
    # refuses. Confined to the synthetic tmp_path repository.
    commit = _commit_tree(repo.path, tree, [parent], message)
    _git(repo.path, "update-ref", REF_FACTS, commit)
    return commit


def _fsync_config(repo_path):
    return {key: _local_config_values(repo_path, key)
            for key in _FSYNC_KEYS}


def test_first_acceptance_at_the_querying_commit(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c1)
    assert origin == OperationAcceptanceOrigin(
        operation_id="op-1", commit_oid=c1, head_oid=c1,
        accepted_commit_oid=c1)
    # The result is frozen evidence, not a mutable record carrier.
    assert dataclasses.is_dataclass(origin)
    with pytest.raises(dataclasses.FrozenInstanceError):
        origin.accepted_commit_oid = None
    assert issubclass(OperationAcceptanceError, GitRepositoryError)


def test_first_acceptance_inherited_through_later_generations(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    c2 = _append_commit(repo, tmp_path, "fact-2", "index-later-2")
    c3 = _append_commit(repo, tmp_path, "fact-3", "index-later-3")
    # Querying the fixed later commit proves the same first acceptance.
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c3)
    assert origin == OperationAcceptanceOrigin(
        operation_id="op-1", commit_oid=c3, head_oid=c3,
        accepted_commit_oid=c1)
    # Querying the old C after H legitimately advanced keeps the proof
    # of the fixed C: accepted_commit_oid is still C1, nothing accepted
    # after C is mapped back into it, and head_oid is simply the head
    # this call captured as the reachability proof.
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c1)
    assert origin.commit_oid == c1
    assert origin.head_oid == c3
    assert origin.accepted_commit_oid == c1
    assert _head(repo.path) == c3


def test_head_advancing_mid_call_keeps_captured_proof(tmp_path,
                                                      monkeypatch):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    c2 = _append_commit(repo, tmp_path, "fact-2", "index-race-2")
    real_read = repo.read_snapshot_at
    calls = []

    def advancing_read(commit_oid):
        # The real method runs first and captures the real head; only
        # afterwards does a legitimate accept advance H, mid-call. The
        # snapshot returned to the locator is the real one, unmodified.
        snapshot = real_read(commit_oid)
        calls.append(commit_oid)
        _append_commit(repo, tmp_path, "fact-mid", "index-race-mid")
        return snapshot

    monkeypatch.setattr(repo, "read_snapshot_at", advancing_read)
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c1)
    # The snapshot was read exactly once, against the head captured at
    # the start of the call; the result keeps that old head as the
    # reachability proof and does not chase the new one.
    assert calls == [c1]
    c3 = repo.head_oid()
    assert c3 != c2
    assert origin == OperationAcceptanceOrigin(
        operation_id="op-1", commit_oid=c1, head_oid=c2,
        accepted_commit_oid=c1)
    assert _head(repo.path) == c3


def test_fixed_commit_without_the_operation_returns_none(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    c1 = _append_commit(repo, tmp_path, "fact-1", "index-none-1")
    c2 = _accept_operation(repo, tmp_path, index_name="index-none-op")
    # The operation joined only at c2: a fixed earlier C reports None,
    # with the current head captured as the reachability proof. None
    # never claims the operation was never executed.
    for target in (genesis, c1):
        origin = locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=target)
        assert origin.commit_oid == target
        assert origin.head_oid == c2
        assert origin.accepted_commit_oid is None
    # An operation that appears nowhere on the fixed chain is a clean
    # None too, never an error.
    origin = locate_operation_acceptance(
        repo, operation_id="op-unknown", commit_oid=c2)
    assert origin.accepted_commit_oid is None


def test_dangling_commit_is_never_an_acceptance(tmp_path):
    repo = _init_repo(tmp_path)
    head = repo.head_oid()
    # A commit that lost its CAS race: it exists in the object store
    # and carries the operation, but sits on no authoritative history.
    blob = _write_blob(repo.path, git_format.dumps_record(
        _operation_payload("op-1", base_commit=head)))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, head), _operation_path("op-1"),
        blob, tmp_path, "index-dangling")
    dangling = _commit_tree(repo.path, tree, [head], "facts: lost cas")
    # Naming the dangling commit itself is refused as unreachable.
    with pytest.raises(OperationAcceptanceError, match="not reachable"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=dangling)
    # A query on the legitimate head never adopts the dangling content.
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=head)
    assert origin.accepted_commit_oid is None
    assert _head(repo.path) == head


def test_deleted_operation_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    tree = _tree_with_removed_file(
        repo.path, _tree_oid(repo.path, c1), _operation_path("op-1"))
    c2 = _force_tip(repo, tree, c1, "facts: synthetic deletion")
    # C lacks the record while an ancestor carried it: an illegal
    # deletion, never a None result.
    with pytest.raises(OperationAcceptanceError, match="never deleted"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c2)
    # The continuity range is the fixed C and its ancestors only: the
    # older C still proves its own acceptance under the later head.
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c1)
    assert origin.accepted_commit_oid == c1
    assert origin.head_oid == c2


def test_delete_then_readd_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    path = _operation_path("op-1")
    tree = _tree_with_removed_file(
        repo.path, _tree_oid(repo.path, c1), path)
    c2 = _force_tip(repo, tree, c1, "facts: synthetic deletion")
    # Re-add the record afterwards reusing the exact blob c1 carried,
    # so the re-added bytes cannot differ from the original ones.
    original_blob = _git_text(
        repo.path, "rev-parse", "%s:%s" % (c1, path)).strip()
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, c2), path, original_blob,
        tmp_path, "index-readd")
    c3 = _force_tip(repo, tree, c2, "facts: synthetic re-add")
    original_bytes = _git(
        repo.path, "cat-file", "blob", "%s:%s" % (c1, path)).stdout
    readded_bytes = _git(
        repo.path, "cat-file", "blob", "%s:%s" % (c3, path)).stdout
    assert original_bytes == readded_bytes
    # Byte-identical re-addition does not heal the deletion in between.
    with pytest.raises(OperationAcceptanceError, match="never deleted"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c3)


def test_rewritten_operation_bytes_are_refused(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    altered = git_format.dumps_record(
        _operation_payload("op-1", base_commit=c1, schema_version=2))
    assert altered != git_format.dumps_record(
        _operation_payload("op-1", base_commit=c1))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, c1), _operation_path("op-1"),
        _write_blob(repo.path, altered), tmp_path, "index-rewrite")
    c2 = _force_tip(repo, tree, c1, "facts: synthetic rewrite")
    with pytest.raises(OperationAcceptanceError, match="never rewritten"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c2)


@pytest.mark.parametrize("field", [
    "record_type",
    "operation_id",
    "repo_id",
    "company_id",
    "missing_company_id",
])
def test_mistyped_or_foreign_operation_identity_is_refused(tmp_path,
                                                           field):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    payload = _operation_payload("op-1", base_commit=genesis)
    if field == "record_type":
        payload["record_type"] = "event"
    elif field == "operation_id":
        payload["operation_id"] = "op-other"
    elif field == "repo_id":
        payload["repo_id"] = "repo-evil"
    elif field == "company_id":
        payload["company_id"] = "co-evil"
    else:
        del payload["company_id"]
    blob = _write_blob(repo.path, git_format.dumps_record(payload))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), _operation_path("op-1"),
        blob, tmp_path, "index-identity-%s" % field)
    c1 = _force_tip(repo, tree, genesis, "facts: synthetic identity drift")
    match = "company_id" if field == "missing_company_id" else field
    with pytest.raises(OperationAcceptanceError, match=match):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1)


def test_non_object_operation_record_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    blob = _write_blob(repo.path, b'["not", "an", "object"]\n')
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), _operation_path("op-1"),
        blob, tmp_path, "index-non-object")
    c1 = _force_tip(repo, tree, genesis, "facts: synthetic non-object")
    with pytest.raises(
            OperationAcceptanceError, match="not a readable record"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1)


def test_noncanonical_operation_bytes_are_refused(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    # Well-formed JSON, but not the canonical dumps_record() bytes.
    blob = _write_blob(repo.path, b'{\n    "record_type": "operation"\n}\n')
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), _operation_path("op-1"),
        blob, tmp_path, "index-noncanonical")
    c1 = _force_tip(repo, tree, genesis, "facts: synthetic non-canonical")
    with pytest.raises(
            OperationAcceptanceError,
            match="not the canonical serialization"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1)


def test_noncanonical_tree_in_an_ancestor_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    evidence_path = git_format.record_path("evidence", evidence_id="ev-1")
    # An ancestor of C carries non-canonical JSON at an unrelated but
    # template-legal path. Every Git object is intact, so neither the
    # identity/fsck history gate nor C's own tree validation can see
    # it: only a full physical tree read of the ancestor can.
    bad_blob = _write_blob(repo.path, b'{\n    "fact": 1\n}\n')
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), evidence_path,
        bad_blob, tmp_path, "index-ancestor-bad")
    c1 = _force_tip(
        repo, tree, genesis, "facts: synthetic non-canonical ancestor")
    # The descendant C fixes that path to canonical bytes and adds the
    # operation: C's own snapshot is completely healthy, proven here
    # with the real read_snapshot_at().
    fixed_blob = _write_blob(
        repo.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, c1), evidence_path, fixed_blob,
        tmp_path, "index-ancestor-fix")
    op_blob = _write_blob(repo.path, git_format.dumps_record(
        _operation_payload("op-1", base_commit=c1)))
    tree = _tree_with_added_file(
        repo.path, tree, _operation_path("op-1"), op_blob, tmp_path,
        "index-ancestor-op")
    c2 = _force_tip(repo, tree, c1, "facts: canonical tip with operation")
    snapshot = repo.read_snapshot_at(c2)
    assert {record.path for record in snapshot.records} == {
        _META_PATH, evidence_path, _operation_path("op-1")}
    # The locator must still refuse: the fixed history behind C is not
    # canonical throughout, even though C itself is.
    with pytest.raises(
            OperationAcceptanceError,
            match="not the canonical serialization"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c2)


@pytest.mark.parametrize("kind", [
    "merge",
    "missing_ancestor",
    "damaged_object",
])
def test_history_anomaly_fails_closed(tmp_path, kind):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    c1 = _accept_operation(repo, tmp_path)
    if kind == "merge":
        other_blob = _write_blob(
            repo.path, git_format.dumps_record({"fact": "other"}))
        other_tree = _tree_with_added_file(
            repo.path, _tree_oid(repo.path, genesis),
            "facts/other/current.json", other_blob, tmp_path,
            "index-merge-other")
        other = _commit_tree(
            repo.path, other_tree, [genesis], "other line")
        tip = _commit_tree(
            repo.path, _tree_oid(repo.path, c1), [c1, other],
            "facts: synthetic merge")
        _git(repo.path, "update-ref", REF_FACTS, tip)
        fragment = "single-parent"
    elif kind == "missing_ancestor":
        _loose_object_path(repo.path, genesis).unlink()
        fragment = "is unreadable"
    else:
        blob = _git_text(
            repo.path, "rev-parse", "%s:%s" % (c1, _operation_path("op-1"))
        ).strip()
        _overwrite_loose_object(repo.path, blob, b"not a zlib stream")
        fragment = "failed integrity checks"
    # Even querying the healthy C fails closed: the captured head's
    # whole chain is gated before any location work.
    with pytest.raises(OperationAcceptanceError, match=fragment):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1)


def test_non_commit_and_unknown_targets_are_refused(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    tree = _tree_oid(repo.path, c1)
    with pytest.raises(OperationAcceptanceError, match="not a commit"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=tree)
    unknown = "f" * repo.oid_length
    with pytest.raises(
            OperationAcceptanceError, match="not a readable object"):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=unknown)


def test_malformed_inputs_are_rejected_without_touching_repository(
        tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path)
    refs_before = _raw_refs_under(repo.path, "refs")
    objects_before = _all_object_oids(repo.path)
    config_before = _fsync_config(repo.path)

    # The repository argument must be a real bound FactsGitRepository.
    with pytest.raises(TypeError):
        locate_operation_acceptance(
            object(), operation_id="op-1", commit_oid=c1)
    with pytest.raises(TypeError):
        locate_operation_acceptance(
            str(repo.path), operation_id="op-1", commit_oid=c1)

    # operation_id follows the git_format ID rules.
    with pytest.raises(TypeError):
        locate_operation_acceptance(repo, operation_id=123, commit_oid=c1)
    with pytest.raises(ValueError):
        locate_operation_acceptance(repo, operation_id="", commit_oid=c1)
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="x" * 200, commit_oid=c1)

    # commit_oid must be a full, lowercase, non-zero OID of the
    # repository object format: never a symbolic name, a prefix, an
    # uppercase spelling or the all-zero value.
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid="HEAD")
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=repo.zero_oid)
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1[:12])
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=c1.upper())
    with pytest.raises(ValueError):
        locate_operation_acceptance(
            repo, operation_id="op-1", commit_oid=None)

    # An instance never bound through initialize()/open() is refused.
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(OperationAcceptanceError, match="not bound"):
        locate_operation_acceptance(
            unbound, operation_id="op-1", commit_oid=c1)

    # None of the rejections wrote an object, moved a ref or touched
    # the fsync configuration.
    assert _head(repo.path) == c1
    assert _raw_refs_under(repo.path, "refs") == refs_before
    assert _all_object_oids(repo.path) == objects_before
    assert _fsync_config(repo.path) == config_before


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_both_object_formats_are_supported(tmp_path, object_format):
    repo = _init_repo(tmp_path, object_format=object_format)
    assert repo.object_format == object_format
    c1 = _accept_operation(repo, tmp_path, index_name="index-fmt-op")
    c2 = _append_commit(repo, tmp_path, "fact-later", "index-fmt-later")
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c2)
    assert origin == OperationAcceptanceOrigin(
        operation_id="op-1", commit_oid=c2, head_oid=c2,
        accepted_commit_oid=c1)
    # OID length follows the actual object format, never a hardcoded 40.
    assert len(origin.head_oid) == repo.oid_length
    assert len(origin.accepted_commit_oid) == repo.oid_length


def test_successful_locate_has_no_side_effects(tmp_path):
    repo = _init_repo(tmp_path)
    c1 = _accept_operation(repo, tmp_path, index_name="index-side-op")
    c2 = _append_commit(repo, tmp_path, "fact-later", "index-side-later")
    refs_before = _raw_refs_under(repo.path, "refs")
    objects_before = _all_object_oids(repo.path)
    config_before = _fsync_config(repo.path)
    tree_before = _git_text(repo.path, "ls-tree", "-r", c2)
    origin = locate_operation_acceptance(
        repo, operation_id="op-1", commit_oid=c1)
    assert origin.accepted_commit_oid == c1
    assert origin.head_oid == c2
    # A read-only proof: no ref moved, no object appeared, the tree and
    # the fsync configuration are exactly as before.
    assert _head(repo.path) == c2
    assert _raw_refs_under(repo.path, "refs") == refs_before
    assert _all_object_oids(repo.path) == objects_before
    assert _fsync_config(repo.path) == config_before
    assert _git_text(repo.path, "ls-tree", "-r", c2) == tree_before
