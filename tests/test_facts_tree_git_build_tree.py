"""Tests for FactsGitRepository.build_tree (complete snapshot materialization).

Every repository here is a synthetic bare repo created with Git plumbing
inside pytest tmp_path; nothing touches SAM-APP, SAM-DOCS, data/, a real
SAM_GIT_DIR, or any remote. All records are synthetic canonical JSON
bytes; no real company data is involved.
"""
import os
import subprocess

import pytest

from app.facts_tree import git_format
from app.facts_tree import git_repository
from app.facts_tree.git_repository import (
    REF_FACTS,
    BuildTreeError,
    FactsGitRepository,
)

_IDS = {
    "repo_id": "repo-test-0001",
    "installation_id": "inst-test-0001",
    "company_id": "co-test-0001",
}
_GIT_TIMEOUT = 30


def _git(repo_path, *args, input_bytes=None, check=True):
    # The plumbing fixtures must stay hermetic even when the process
    # environment is polluted, so inherited GIT_* variables are stripped
    # the same way the adapter strips them.
    full_env = {key: value for key, value in os.environ.items()
                if not key.startswith("GIT_")}
    full_env["GIT_CONFIG_NOSYSTEM"] = "1"
    full_env["GIT_CONFIG_GLOBAL"] = os.devnull
    result = subprocess.run(
        ["git", "-C", str(repo_path)] + list(args),
        input=input_bytes,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        env=full_env,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            "git %s failed: %s" % (args, result.stderr.decode("utf-8",
                                                              "replace")))
    return result


def _git_text(repo_path, *args, **kwargs):
    return _git(repo_path, *args, **kwargs).stdout.decode("utf-8")


def _init_repo(tmp_path, name="facts.git", **kwargs):
    return FactsGitRepository.initialize(tmp_path / name, **_IDS, **kwargs)


def _head(repo_path):
    return _git_text(repo_path, "rev-parse", "--verify", REF_FACTS).strip()


def _meta_bytes():
    return git_format.dumps_record({
        "schema_version": git_repository.REPOSITORY_META_SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": _IDS["repo_id"],
        "installation_id": _IDS["installation_id"],
        "company_id": _IDS["company_id"],
    })


def _snapshot():
    # A complete final snapshot as the host would assemble it: the
    # identity record matching this repository plus several nested
    # synthetic fact, source and evidence records.
    return {
        "meta/repository.json": _meta_bytes(),
        "facts/fact-0001/current.json": git_format.dumps_record(
            {"fact_uid": "fact-0001", "revision": 1}),
        "facts/fact-0001/revisions/1.json": git_format.dumps_record(
            {"fact_uid": "fact-0001", "revision": 1,
             "statement": "synthetic fact"}),
        "facts/fact-0002/current.json": git_format.dumps_record(
            {"fact_uid": "fact-0002", "revision": 1}),
        "sources/src-0001/versions/v-0001.json": git_format.dumps_record(
            {"source_id": "src-0001", "version_id": "v-0001"}),
        "evidence/ev-0001.json": git_format.dumps_record(
            {"evidence_id": "ev-0001", "excerpt": "synthetic"}),
    }


def _records(snapshot):
    return [(path, snapshot[path]) for path in sorted(snapshot)]


def _loose_object_count(repo_path):
    # Plumbing-created test objects are always loose; nothing in these
    # fixtures ever packs or runs gc. "info" and "pack" are excluded by
    # the two-character fanout directory shape.
    total = 0
    objects = repo_path / "objects"
    for child in objects.iterdir():
        if child.is_dir() and len(child.name) == 2:
            total += sum(1 for item in child.iterdir() if item.is_file())
    return total


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_build_tree_materializes_complete_snapshot(tmp_path, object_format):
    repo = _init_repo(tmp_path, object_format=object_format)
    head_before = _head(repo.path)
    snapshot = _snapshot()
    tree = repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert len(tree) == repo.oid_length
    assert _git_text(repo.path, "cat-file", "-t", tree).strip() == "tree"
    rows = [line.split(None, 3)
            for line in _git_text(repo.path, "ls-tree", "-r", tree)
            .splitlines()]
    # Every recursive entry is a regular non-executable file blob at its
    # full repository-relative path.
    assert sorted(row[3] for row in rows) == sorted(snapshot)
    assert {(row[0], row[1]) for row in rows} == {("100644", "blob")}
    # Every blob is readable and carries the exact canonical bytes.
    for row in rows:
        content = _git(repo.path, "cat-file", "blob", row[2]).stdout
        assert content == snapshot[row[3]]
    # Only Git objects were created; the authoritative ref never moved.
    assert repo.head_oid() == head_before == _head(repo.path)


def test_build_tree_directories_use_git_tree_mode(tmp_path):
    repo = _init_repo(tmp_path)
    snapshot = _snapshot()
    tree = repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    rows = [line.split(None, 3)
            for line in _git_text(repo.path, "ls-tree", tree).splitlines()]
    # ls-tree pads the stored "40000" directory mode to "040000".
    assert {(row[0], row[1]) for row in rows} == {("040000", "tree")}
    assert sorted(row[3] for row in rows) == [
        "evidence", "facts", "meta", "sources"]


def test_build_tree_identity_only_snapshot_matches_genesis_tree(tmp_path):
    # A snapshot carrying only the identity record must materialize the
    # exact tree the genesis commit already has: initialize() wrote the
    # same record through the same canonical bytes and layout.
    repo = _init_repo(tmp_path)
    snapshot = {"meta/repository.json": _meta_bytes()}
    tree = repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    genesis_tree = _git_text(
        repo.path, "rev-parse", "%s^{tree}" % repo.genesis_oid).strip()
    assert tree == genesis_tree
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_output_commits_through_create_commit_and_accept(tmp_path):
    # The returned tree is a plain Git tree object, usable by the
    # existing create_commit/accept path. This is a same-adapter object
    # flow check, not a runtime or Commit Broker integration.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    snapshot = _snapshot()
    tree = repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    commit = repo.create_commit(tree, genesis, message="facts: build tree")
    # accept() re-validates the complete physical tree against the same
    # host whitelist before the CAS.
    assert repo.accept(genesis, commit, expected_paths=sorted(snapshot)) \
        .accepted
    assert repo.head_oid() == commit
    meta_path = git_format.record_path("repository_meta")
    meta = _git(
        repo.path, "cat-file", "blob", "%s:%s" % (commit, meta_path)).stdout
    assert meta == _meta_bytes()


def test_build_tree_refuses_unbound_instance(tmp_path):
    repo = _init_repo(tmp_path)
    unbound = FactsGitRepository(repo.path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    with pytest.raises(BuildTreeError):
        unbound.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert _head(repo.path) == repo.genesis_oid


def test_build_tree_rejects_malformed_record_structure(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    records = _records(snapshot)
    paths = sorted(snapshot)
    with pytest.raises(TypeError):
        repo.build_tree("meta/repository.json", expected_paths=paths)
    with pytest.raises(TypeError):
        repo.build_tree([list(records[0])] + records[1:],
                        expected_paths=paths)
    with pytest.raises(ValueError):
        repo.build_tree([("meta/repository.json",)] + records[1:],
                        expected_paths=paths)
    with pytest.raises(TypeError):
        repo.build_tree([(123, records[0][1])] + records[1:],
                        expected_paths=paths)
    with pytest.raises(TypeError):
        repo.build_tree([(records[0][0], "not-bytes")] + records[1:],
                        expected_paths=paths)
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_duplicate_record_path(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    records = _records(snapshot)
    with pytest.raises(ValueError):
        repo.build_tree(records + [records[0]],
                        expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_path_not_encodable_as_utf8(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    bad_path = "facts/\ud800/current.json"
    records = _records(snapshot) + [
        (bad_path, git_format.dumps_record({"x": 1}))]
    with pytest.raises(ValueError):
        repo.build_tree(
            records, expected_paths=sorted(snapshot) + [bad_path])
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_file_directory_prefix_conflict(tmp_path):
    # No canonical template path can be a proper prefix of another
    # (template file names are literals ending in .json and encoded IDs
    # can never contain a literal "."), so the conflicting child path is
    # not a canonical record path at all and the canonical-template gate
    # refuses it before any object write. The structural prefix-conflict
    # boundary itself is covered by the shared validator tests in
    # tests/test_facts_tree_git_tree_validation.py.
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    head_before = _head(repo.path)
    snapshot = _snapshot()
    conflict = "facts/fact-0001/current.json/child.json"
    records = _records(snapshot) + [
        (conflict, git_format.dumps_record({"child": True}))]
    with pytest.raises(BuildTreeError):
        repo.build_tree(
            records, expected_paths=sorted(snapshot) + [conflict])
    assert _loose_object_count(repo.path) == objects_before
    assert _head(repo.path) == head_before


def test_build_tree_rejects_prefix_conflict_across_sorted_gap(tmp_path):
    # "a-b" sorts between "a" and "a/child.json" because "-" < "/", so a
    # check comparing only adjacent sorted entries never sees that "a"
    # is a proper directory prefix of "a/child.json". None of the three
    # paths is a canonical fixed-template record path, so the
    # canonical-template gate refuses the snapshot before any object is
    # written; the sorted-gap prefix-conflict structure itself stays
    # covered by the shared validator tests in
    # tests/test_facts_tree_git_tree_validation.py.
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    head_before = _head(repo.path)
    snapshot = _snapshot()
    snapshot.update({
        "a": git_format.dumps_record({"path": "a"}),
        "a-b": git_format.dumps_record({"path": "a-b"}),
        "a/child.json": git_format.dumps_record({"path": "a/child.json"}),
    })
    with pytest.raises(BuildTreeError):
        repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert _head(repo.path) == head_before


def test_build_tree_rejects_snapshot_missing_identity_record(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    del snapshot["meta/repository.json"]
    with pytest.raises(BuildTreeError):
        repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_rewritten_identity_record(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    rewritten = git_format.loads_record(snapshot["meta/repository.json"])
    rewritten["company_id"] = "co-evil"
    snapshot["meta/repository.json"] = git_format.dumps_record(rewritten)
    with pytest.raises(BuildTreeError):
        repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_non_canonical_content(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    # The same payload as the canonical record, serialized differently.
    snapshot["evidence/ev-0001.json"] = (
        b'{"evidence_id":"ev-0001","excerpt":"synthetic"}\n')
    with pytest.raises(ValueError):
        repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_non_object_json_content(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    snapshot["evidence/ev-0001.json"] = b"[1, 2, 3]\n"
    with pytest.raises(ValueError):
        repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_extra_path_outside_whitelist(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    extra = "facts/fact-0003/current.json"
    records = _records(snapshot) + [
        (extra, git_format.dumps_record({"fact_uid": "fact-0003"}))]
    with pytest.raises(ValueError):
        repo.build_tree(records, expected_paths=sorted(snapshot))
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_missing_whitelisted_path(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    expected = sorted(snapshot) + ["facts/fact-0003/current.json"]
    with pytest.raises(ValueError):
        repo.build_tree(_records(snapshot), expected_paths=expected)
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_rejects_duplicated_whitelist_path(tmp_path):
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    expected = sorted(snapshot) + ["meta/repository.json"]
    with pytest.raises(ValueError):
        repo.build_tree(_records(snapshot), expected_paths=expected)
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


@pytest.mark.parametrize("evil", [
    "../escape.json",
    "/absolute/path.json",
    "facts//empty-segment.json",
    "facts/./dot.json",
    "facts/../traversal.json",
    "back\\slash.json",
])
def test_build_tree_rejects_unsafe_paths(tmp_path, evil):
    # An unsafe path can never be a canonical fixed-template record
    # path, so the canonical-template gate refuses it with
    # BuildTreeError before the shared validator's own unsafe-path
    # ValueError (covered in tests/test_facts_tree_git_tree_validation.py)
    # would ever run, and before any object is written.
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    snapshot = _snapshot()
    records = _records(snapshot) + [
        (evil, git_format.dumps_record({"x": 1}))]
    with pytest.raises(BuildTreeError):
        repo.build_tree(records, expected_paths=sorted(snapshot) + [evil])
    assert _loose_object_count(repo.path) == objects_before
    assert repo.head_oid() == repo.genesis_oid


def test_build_tree_accepts_canonical_escaped_id_paths(tmp_path):
    # Host IDs that need escaping still produce canonical template
    # paths; the complete-tree gate must accept them and materialize
    # the exact bytes.
    repo = _init_repo(tmp_path)
    fact_uid = "公司 α/1"
    path = git_format.record_path("fact_current", fact_uid=fact_uid)
    assert "~" in path  # the escapes were actually exercised
    snapshot = _snapshot()
    snapshot[path] = git_format.dumps_record(
        {"fact_uid": fact_uid, "revision": 1})
    tree = repo.build_tree(_records(snapshot), expected_paths=sorted(snapshot))
    assert len(tree) == repo.oid_length
    content = _git(
        repo.path, "cat-file", "blob", "%s:%s" % (tree, path)).stdout
    assert content == snapshot[path]


@pytest.mark.parametrize("unregistered", [
    "notes/freeform.json",
    "facts/fact-0001/current.json.bak",
    "facts/fact-0001/current.json/child.json",
    "taxonomy/v2/leaves/L-07",
])
def test_build_tree_rejects_safe_path_outside_fixed_templates(
        tmp_path, unregistered):
    # Each path is clean, safe and carries canonical JSON, but no fixed
    # record template produces it; the snapshot must be refused before
    # any Git object is written.
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    head_before = _head(repo.path)
    snapshot = _snapshot()
    records = _records(snapshot) + [
        (unregistered, git_format.dumps_record({"x": 1}))]
    with pytest.raises(BuildTreeError, match="fixed record template"):
        repo.build_tree(
            records, expected_paths=sorted(snapshot) + [unregistered])
    assert _loose_object_count(repo.path) == objects_before
    assert _head(repo.path) == head_before


@pytest.mark.parametrize("alias", [
    "facts/f~2D1/current.json",        # "~2D" decodes to the safe "-"
    "facts/~66act-0001/current.json",  # "~66" decodes to the safe "f"
    "facts/f~2d1/current.json",        # lowercase hex is not canonical
])
def test_build_tree_rejects_decodable_noncanonical_escape_alias(
        tmp_path, alias):
    # The alias decodes to the legitimate fact uid fact-0001 (or fails
    # decoding outright), but it is not the encode_id round-trip of that
    # ID, so it must never reach the object store.
    repo = _init_repo(tmp_path)
    objects_before = _loose_object_count(repo.path)
    head_before = _head(repo.path)
    snapshot = _snapshot()
    records = _records(snapshot) + [
        (alias, git_format.dumps_record({"fact_uid": "fact-0001"}))]
    with pytest.raises(BuildTreeError, match="canonical record_path"):
        repo.build_tree(records, expected_paths=sorted(snapshot) + [alias])
    assert _loose_object_count(repo.path) == objects_before
    assert _head(repo.path) == head_before
