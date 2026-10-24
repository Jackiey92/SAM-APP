"""Tests for app.facts_tree.git_repository.

Every repository here is a synthetic bare repo created with Git
plumbing inside pytest tmp_path; nothing touches SAM-APP, SAM-DOCS,
data/, a real SAM_GIT_DIR, or any remote. The production adapter only
ever receives OIDs; tests may additionally drive plumbing directly to
prepare synthetic trees and competing commits.
"""
import hashlib
import os
import subprocess
import zlib

import pytest

from app.facts_tree import git_format
from app.facts_tree import git_repository
from app.facts_tree.git_repository import (
    REF_FACTS,
    CasResult,
    FactsGitRepository,
    GitRepositoryError,
    IdentityMismatchError,
    InitializeError,
    IntegrityError,
    IntegrityReport,
    OpenError,
    SnapshotReadError,
    StaleBaseError,
)

_IDS = {
    "repo_id": "repo-test-0001",
    "installation_id": "inst-test-0001",
    "company_id": "co-test-0001",
}
_GIT_TIMEOUT = 30
_META_PATH = git_format.record_path("repository_meta")


def _accept_paths(*extra_paths):
    # The host-generated exact whitelist accept() re-validates against:
    # the identity record plus every record path the synthetic complete
    # snapshot carries.
    return [_META_PATH] + sorted(extra_paths)


def _git(repo_path, *args, input_bytes=None, env=None, check=True):
    # The plumbing fixtures must stay hermetic even when a test pollutes
    # the process environment on purpose, so inherited GIT_* variables
    # are stripped the same way the adapter strips them.
    full_env = {key: value for key, value in os.environ.items()
                if not key.startswith("GIT_")}
    full_env["GIT_CONFIG_NOSYSTEM"] = "1"
    full_env["GIT_CONFIG_GLOBAL"] = os.devnull
    if env:
        full_env.update(env)
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


# Design section 10.9.1: the exact local durability configuration the
# facts authority repo must carry. Tests assert these literal strings
# so a drift in the production constant is caught, not echoed.
_FSYNC_EXPECTED = {
    "core.fsync": "objects,reference",
    "core.fsyncMethod": "fsync",
}


def _local_config_values(repo_path, key):
    # The effective local configuration as Git itself parses it, with
    # duplicated entries exposed (--get-all), never the write command's
    # word. Exit code 1 means the key is absent.
    result = _git(
        repo_path, "config", "--local", "--get-all", key, check=False)
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise AssertionError(
            "git config --get-all %s failed: %s"
            % (key, result.stderr.decode("utf-8", "replace")))
    return result.stdout.decode("utf-8").splitlines()


def _init_repo(tmp_path, name="facts.git", **kwargs):
    return FactsGitRepository.initialize(tmp_path / name, **_IDS, **kwargs)


def _commit_parents(repo_path, oid):
    out = _git_text(repo_path, "cat-file", "commit", oid)
    return [line.split(" ", 1)[1]
            for line in out.splitlines() if line.startswith("parent ")]


def _write_blob(repo_path, data):
    return _git_text(
        repo_path, "hash-object", "-w", "--stdin", input_bytes=data).strip()


def _tree_with_mode_entry(repo_path, base_tree, mode, path, oid, tmp_path,
                          index_name):
    # cacheinfo registers an entry with any Git mode (regular file,
    # executable, symlink, gitlink) without the adapter's own path or
    # mode rules getting in the way, so hostile entry types can be
    # expressed exactly as a raw tree would carry them.
    env = {"GIT_INDEX_FILE": str(tmp_path / index_name)}
    _git(repo_path, "read-tree", base_tree, env=env)
    _git(repo_path, "update-index", "--add", "--cacheinfo",
         "%s,%s,%s" % (mode, oid, path), env=env)
    return _git_text(repo_path, "write-tree", env=env).strip()


def _tree_with_added_file(repo_path, base_tree, path, blob_oid, tmp_path,
                          index_name):
    return _tree_with_mode_entry(
        repo_path, base_tree, "100644", path, blob_oid, tmp_path,
        index_name)


def _tree_with_removed_file(repo_path, base_tree, path):
    # Dropping one path from an existing tree is the only way to
    # express a deleted record in a raw tree. read-tree/update-index
    # would need a work tree, which a bare repo does not have, so the
    # tree is rebuilt layer by layer with ls-tree + mktree instead.
    # Filtering ls-tree output keeps the entries in tree order, which
    # is exactly what mktree requires. Like a real Git commit, a
    # directory left empty by the removal is pruned, so the rest of
    # the tree stays well-formed.
    parts = path.split("/")

    def rebuild(tree_oid, remaining):
        # Returns the rebuilt tree OID, or None when nothing is left.
        lines = _git_text(repo_path, "ls-tree", tree_oid).splitlines()
        head = remaining[0]
        out = []
        for line in lines:
            meta, name = line.split("\t", 1)
            if name != head:
                out.append(line)
            elif len(remaining) == 1:
                continue  # this is the deleted record: drop the entry
            else:
                mode, kind, oid = meta.split(" ")
                if kind != "tree":
                    raise AssertionError(
                        "%s: %s is not a directory" % (path, head))
                sub = rebuild(oid, remaining[1:])
                if sub is not None:
                    out.append("%s tree %s\t%s" % (mode, sub, name))
        if not out:
            return None
        return _git_text(
            repo_path, "mktree",
            input_bytes=("\n".join(out) + "\n").encode("utf-8")).strip()

    rebuilt = rebuild(base_tree, parts)
    if rebuilt is None:
        # Only possible when the base tree held just the one record;
        # mktree with empty input yields the canonical empty tree.
        rebuilt = _git_text(
            repo_path, "mktree", input_bytes=b"").strip()
    return rebuilt


def _tree_with_empty_directory(repo_path, commit_oid, name="facts"):
    # An index can never hold an empty subtree, so this tree is built
    # directly with mktree: the commit's current meta/ tree plus one
    # directory that holds no record at all.
    empty = _git_text(
        repo_path, "hash-object", "-t", "tree", "-w", "--stdin",
        input_bytes=b"").strip()
    meta_tree = _git_text(
        repo_path, "rev-parse", "%s:meta" % commit_oid).strip()
    listing = "040000 tree %s\t%s\n040000 tree %s\tmeta\n" % (
        empty, name, meta_tree)
    return _git_text(
        repo_path, "mktree", input_bytes=listing.encode("utf-8")).strip()


def _tree_without_meta(repo_path, tmp_path, index_name):
    # A synthetic tree with a business record but no meta/ directory at
    # all, built from an empty index instead of the genesis tree.
    env = {"GIT_INDEX_FILE": str(tmp_path / index_name)}
    _git(repo_path, "read-tree", "--empty", env=env)
    blob = _write_blob(repo_path, b'{"fact": 1}\n')
    _git(repo_path, "update-index", "--add", "--cacheinfo",
         "100644,%s,%s" % (blob, "facts/fact-1/current.json"), env=env)
    return _git_text(repo_path, "write-tree", env=env).strip()


def _meta_payload(**overrides):
    payload = {
        "schema_version": git_repository.REPOSITORY_META_SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": _IDS["repo_id"],
        "installation_id": _IDS["installation_id"],
        "company_id": _IDS["company_id"],
    }
    payload.update(overrides)
    return payload


def _commit_tree(repo_path, tree, parents, message):
    env = {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test-author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "test-committer@example.invalid",
    }
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return _git_text(repo_path, *args, env=env).strip()


def _head(repo_path):
    return _git_text(repo_path, "rev-parse", "--verify", REF_FACTS).strip()


def _tree_oid(repo_path, commit_oid):
    return _git_text(
        repo_path, "rev-parse", "%s^{tree}" % commit_oid).strip()


def test_initialize_creates_bare_repo_with_facts_head_and_genesis(tmp_path):
    repo = _init_repo(tmp_path)
    assert _git_text(
        repo.path, "rev-parse", "--is-bare-repository").strip() == "true"
    assert _git_text(repo.path, "symbolic-ref", "HEAD").strip() == REF_FACTS
    assert _git_text(repo.path, "remote").strip() == ""
    head = _head(repo.path)
    assert head == repo.genesis_oid
    assert head == repo.head_oid()
    # The genesis snapshot has zero parents.
    assert _commit_parents(repo.path, head) == []
    # Identity record lives at the fixed record_path location and is the
    # exact canonical dumps_record output.
    meta_path = git_format.record_path("repository_meta")
    assert meta_path == "meta/repository.json"
    meta_bytes = _git(
        repo.path, "cat-file", "blob", "%s:%s" % (head, meta_path)).stdout
    expected = git_format.dumps_record({
        "schema_version": git_repository.REPOSITORY_META_SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": _IDS["repo_id"],
        "installation_id": _IDS["installation_id"],
        "company_id": _IDS["company_id"],
    })
    assert meta_bytes == expected
    record = git_format.loads_record(meta_bytes)
    assert record["repo_id"] == _IDS["repo_id"]
    assert record["installation_id"] == _IDS["installation_id"]
    assert record["company_id"] == _IDS["company_id"]


def test_initialize_refuses_existing_file(tmp_path):
    target = tmp_path / "facts.git"
    target.write_bytes(b"precious")
    with pytest.raises(InitializeError):
        FactsGitRepository.initialize(target, **_IDS)
    assert target.read_bytes() == b"precious"


def test_initialize_refuses_non_empty_directory(tmp_path):
    target = tmp_path / "facts.git"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(InitializeError):
        FactsGitRepository.initialize(target, **_IDS)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_initialize_refuses_existing_repository(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(InitializeError):
        FactsGitRepository.initialize(repo.path, **_IDS)
    assert _head(repo.path) == repo.genesis_oid


def test_initialize_uses_existing_empty_directory(tmp_path):
    target = tmp_path / "facts.git"
    target.mkdir()
    repo = FactsGitRepository.initialize(target, **_IDS)
    assert repo.head_oid() == repo.genesis_oid


def test_initialize_refuses_symlink_target(tmp_path):
    # A symlink is refused before the link target's contents are ever
    # inspected; the external empty directory must stay untouched.
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "facts.git"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip("platform cannot create directory symlinks: %s" % exc)
    with pytest.raises(InitializeError):
        FactsGitRepository.initialize(link, **_IDS)
    assert link.is_symlink()
    assert list(outside.iterdir()) == []


def test_initialize_rejects_empty_identity_ids(tmp_path):
    for field in ("repo_id", "installation_id", "company_id"):
        bad = dict(_IDS)
        bad[field] = ""
        with pytest.raises(ValueError):
            FactsGitRepository.initialize(tmp_path / field, **bad)


def test_inherited_git_dir_cannot_hijack_repository(tmp_path, monkeypatch):
    # The reviewer reproduced this with GIT_DIR pointing at a second
    # legitimate bare repo; the adapter must keep acting on its own
    # fixed path no matter what GIT_* the process inherited.
    # The genesis commit is deterministic (fixed identity, same-second
    # timestamp), so give the second repository a distinct message to
    # guarantee the two heads differ; otherwise a hijack that returns
    # the second repository's head could pass unnoticed.
    first = _init_repo(tmp_path, name="first.git")
    second = _init_repo(
        tmp_path, name="second.git", message="facts: genesis second")
    assert first.genesis_oid != second.genesis_oid
    monkeypatch.setenv("GIT_DIR", str(second.path))
    # Reads stay on the first repository's fixed path and HEAD.
    assert first.head_oid() == first.genesis_oid
    assert second.head_oid() == second.genesis_oid
    # Writes (object creation plus the CAS) also stay on the first
    # repository; the second repository never moves.
    blob = _write_blob(first.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_added_file(
        first.path, _tree_oid(first.path, first.genesis_oid),
        "facts/fact-1/current.json", blob, tmp_path, "index-env")
    commit = first.create_commit(tree, first.genesis_oid)
    assert first.accept(
        first.genesis_oid, commit,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    assert first.head_oid() == commit
    assert _head(first.path) == commit
    assert _head(second.path) == second.genesis_oid


def test_open_verifies_identity_and_returns_same_head(tmp_path):
    repo = _init_repo(tmp_path)
    reopened = FactsGitRepository.open(repo.path, **_IDS)
    assert reopened.head_oid() == repo.genesis_oid
    assert reopened.object_format == repo.object_format


@pytest.mark.parametrize(
    "field", ["repo_id", "installation_id", "company_id"])
def test_open_rejects_identity_mismatch_without_modifying(tmp_path, field):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    wrong = dict(_IDS)
    wrong[field] = "wrong-" + field
    with pytest.raises(IdentityMismatchError):
        FactsGitRepository.open(repo.path, **wrong)
    assert _head(repo.path) == before


def test_open_rejects_missing_path_and_non_bare_repo(tmp_path):
    with pytest.raises(OpenError):
        FactsGitRepository.open(tmp_path / "absent", **_IDS)
    plain = tmp_path / "plain"
    subprocess.run(
        ["git", "init", str(plain)],
        capture_output=True, timeout=_GIT_TIMEOUT, check=True,
    )
    with pytest.raises(OpenError):
        FactsGitRepository.open(plain, **_IDS)


def test_open_rejects_moved_head_symref(tmp_path):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    _git(repo.path, "symbolic-ref", "HEAD", "refs/heads/other")
    with pytest.raises(OpenError):
        FactsGitRepository.open(repo.path, **_IDS)
    assert _head(repo.path) == before


def test_open_rejects_configured_remote(tmp_path):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    # Only configuration; nothing is ever fetched or pushed.
    _git(repo.path, "remote", "add", "origin",
         "https://invalid.example/never-touched.git")
    with pytest.raises(OpenError):
        FactsGitRepository.open(repo.path, **_IDS)
    assert _head(repo.path) == before


def test_initialize_sets_exact_fsync_policy(tmp_path):
    # Section 10.9.1: the new repository must carry the exact local
    # fsync configuration. The values are read back through Git
    # plumbing, not assumed from the adapter's write. This is a
    # configuration-value check only: it does not prove the running
    # Git or filesystem actually performs fsync, and it is no
    # substitute for power-loss or backup validation.
    repo = _init_repo(tmp_path)
    for key, expected in _FSYNC_EXPECTED.items():
        assert _local_config_values(repo.path, key) == [expected]
    assert repo.head_oid() == repo.genesis_oid


@pytest.mark.parametrize("key", sorted(_FSYNC_EXPECTED))
def test_open_rejects_missing_fsync_configuration(tmp_path, key):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    _git(repo.path, "config", "--local", "--unset", key)
    with pytest.raises(OpenError, match="missing"):
        FactsGitRepository.open(repo.path, **_IDS)
    # The refusal is read-only: nothing was repaired or rewritten.
    assert _head(repo.path) == before
    assert _local_config_values(repo.path, key) == []


@pytest.mark.parametrize(
    "key,drifted",
    [("core.fsync", "objects"),
     ("core.fsync", "none"),
     ("core.fsyncMethod", "writeout-only"),
     ("core.fsyncMethod", "batch")])
def test_open_rejects_drifted_fsync_configuration(tmp_path, key, drifted):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    _git(repo.path, "config", "--local", key, drifted)
    with pytest.raises(OpenError, match="fsync policy"):
        FactsGitRepository.open(repo.path, **_IDS)
    assert _head(repo.path) == before
    assert _local_config_values(repo.path, key) == [drifted]


@pytest.mark.parametrize("key", sorted(_FSYNC_EXPECTED))
def test_open_rejects_duplicated_fsync_configuration(tmp_path, key):
    # A duplicated entry is ambiguous even when every copy carries the
    # right value: fail closed instead of letting Git collapse it.
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    _git(repo.path, "config", "--local", "--add", key,
         _FSYNC_EXPECTED[key])
    assert len(_local_config_values(repo.path, key)) == 2
    with pytest.raises(OpenError, match="exactly one"):
        FactsGitRepository.open(repo.path, **_IDS)
    assert _head(repo.path) == before


@pytest.mark.parametrize("key", sorted(_FSYNC_EXPECTED))
@pytest.mark.parametrize("mode", ["missing", "drifted"])
def test_accept_rejects_fsync_violation_and_ref_keeps_value(
        tmp_path, key, mode):
    # accept() re-verifies the section 10.9.1 configuration immediately
    # before the authoritative ref CAS: a violation found after open()
    # refuses the acceptance and the ref keeps its previous value.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    blob = _write_blob(repo.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path, blob, tmp_path,
        "index-fsync-%s-%s" % (key.replace(".", "-"), mode))
    commit = repo.create_commit(tree, genesis)
    # Break the policy only after the commit object already exists, so
    # the pre-CAS re-verification is the last guard that can catch it.
    if mode == "missing":
        _git(repo.path, "config", "--local", "--unset", key)
    else:
        _git(repo.path, "config", "--local", key, "none")
    with pytest.raises(GitRepositoryError, match="fsync policy"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(path))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    # The refusal was the configuration check, not the commit: once the
    # policy is restored the very same acceptance succeeds.
    _git(repo.path, "config", "--local", key, _FSYNC_EXPECTED[key])
    assert repo.accept(
        genesis, commit, expected_paths=_accept_paths(path)).accepted
    assert repo.head_oid() == commit


def test_child_commit_single_parent_and_cas_advances_ref(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    blob = _write_blob(repo.path, git_format.dumps_record({"fact": 1}))
    new_tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis),
        "facts/fact-1/current.json", blob, tmp_path, "index-1")
    commit = repo.create_commit(new_tree, genesis, message="facts: add 1")
    # Exactly one parent, equal to the expected-old tip it was built on.
    assert _commit_parents(repo.path, commit) == [genesis]
    # The object exists but is not part of the tree before the CAS.
    assert repo.head_oid() == genesis
    assert _git_text(repo.path, "rev-list", REF_FACTS).split() == [genesis]
    result = repo.accept(
        genesis, commit,
        expected_paths=_accept_paths("facts/fact-1/current.json"))
    assert result == CasResult(
        accepted=True, current_oid=commit,
        expected_old=genesis, new_oid=commit)
    assert repo.head_oid() == commit
    assert _git_text(repo.path, "rev-list", REF_FACTS).split() == [
        commit, genesis]


def test_create_commit_rejects_parent_that_is_not_current_head(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    blob = _write_blob(repo.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis),
        "facts/fact-1/current.json", blob, tmp_path, "index-1")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    with pytest.raises(StaleBaseError):
        repo.create_commit(tree, genesis)
    # No merge/rebase escape hatch: the ref still points at the child.
    assert repo.head_oid() == commit


def test_stale_cas_reports_not_accepted_and_ref_keeps_competitor(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    base_tree = _tree_oid(repo.path, genesis)
    # Competitor commits on the genesis base and wins the CAS.
    tree1 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-1/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-1")
    competitor = repo.create_commit(tree1, genesis)
    assert repo.accept(
        genesis, competitor,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    # A second commit object prepared against the same old base (as raw
    # plumbing, like a loser prepared before the competitor moved F).
    tree2 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-2/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 2})),
        tmp_path, "index-2")
    loser = _commit_tree(repo.path, tree2, [genesis], "facts: stale")
    result = repo.accept(
        genesis, loser,
        expected_paths=_accept_paths("facts/fact-2/current.json"))
    assert not result.accepted
    assert result.current_oid == competitor
    # F keeps the competitor value; the loser object exists but is not
    # accepted into the tree.
    assert repo.head_oid() == competitor
    assert _git_text(repo.path, "cat-file", "-t", loser).strip() == "commit"
    assert _commit_parents(repo.path, loser) == [genesis]
    assert _git_text(repo.path, "rev-list", REF_FACTS).split() == [
        competitor, genesis]


def test_accept_rejects_commit_whose_parent_is_not_expected_old(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    base_tree = _tree_oid(repo.path, genesis)
    tree1 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-1/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-1")
    tip = repo.create_commit(tree1, genesis)
    assert repo.accept(
        genesis, tip,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    tree2 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-2/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 2})),
        tmp_path, "index-2")
    # A commit whose single parent is not the claimed expected-old.
    wrong_parent = _commit_tree(repo.path, tree2, [genesis], "facts: x")
    with pytest.raises(GitRepositoryError):
        repo.accept(
            tip, wrong_parent,
            expected_paths=_accept_paths("facts/fact-2/current.json"))
    assert repo.head_oid() == tip
    # A zero-parent commit cannot be accepted on a non-zero base, and a
    # parented commit cannot be accepted as a genesis.
    orphan = _commit_tree(repo.path, tree2, [], "facts: orphan")
    with pytest.raises(GitRepositoryError):
        repo.accept(
            tip, orphan,
            expected_paths=_accept_paths("facts/fact-2/current.json"))
    with pytest.raises(GitRepositoryError):
        repo.accept(
            repo.zero_oid, wrong_parent,
            expected_paths=_accept_paths("facts/fact-2/current.json"))
    assert repo.head_oid() == tip


def test_accept_rejects_commit_missing_identity_record(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    tree = _tree_without_meta(repo.path, tmp_path, "index-nometa")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError):
        repo.accept(
            genesis, commit,
            expected_paths=_accept_paths("facts/fact-1/current.json"))
    # The refusal happens before the CAS, so the ref never moved and
    # the repository still opens cleanly.
    assert repo.head_oid() == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


@pytest.mark.parametrize(
    "field", ["repo_id", "installation_id", "company_id"])
def test_accept_rejects_commit_with_rewritten_identity(tmp_path, field):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    meta_path = git_format.record_path("repository_meta")
    rewritten = _meta_payload(**{field: "evil-" + field})
    blob = _write_blob(repo.path, git_format.dumps_record(rewritten))
    env = {"GIT_INDEX_FILE": str(tmp_path / "index-rewrite")}
    _git(repo.path, "read-tree", _tree_oid(repo.path, genesis), env=env)
    _git(repo.path, "update-index", "--add", "--cacheinfo",
         "100644,%s,%s" % (blob, meta_path), env=env)
    tree = _git_text(repo.path, "write-tree", env=env).strip()
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError):
        repo.accept(genesis, commit, expected_paths=_accept_paths())
    assert repo.head_oid() == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_revalidates_tree_and_rejects_noncanonical_blob(tmp_path):
    # Raw plumbing can assemble a commit whose blob bytes build_tree()
    # would never have accepted; only the accept-time revalidation of
    # the complete physical tree keeps them off the authoritative ref.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    blob = _write_blob(repo.path, b'{"fact": 1}\n')
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path, blob, tmp_path,
        "index-noncanonical")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="canonical"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(path))
    assert repo.head_oid() == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_rejects_extra_and_missing_tree_paths(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    other = "facts/fact-2/current.json"
    blob = _write_blob(repo.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path, blob, tmp_path,
        "index-paths")
    commit = repo.create_commit(tree, genesis)
    # A whitelist missing a record the tree carries is refused.
    with pytest.raises(GitRepositoryError, match="whitelist"):
        repo.accept(genesis, commit, expected_paths=_accept_paths())
    # A whitelist asking for a record the tree does not carry is refused.
    with pytest.raises(GitRepositoryError, match="missing"):
        repo.accept(
            genesis, commit, expected_paths=_accept_paths(path, other))
    assert repo.head_oid() == genesis


def test_accept_rejects_tree_path_outside_fixed_templates(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "notes/freeform.json"
    blob = _write_blob(repo.path, git_format.dumps_record({"note": "x"}))
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path, blob, tmp_path,
        "index-freeform")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="fixed record template"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(path))
    assert repo.head_oid() == genesis


@pytest.mark.parametrize("mode", ["100755", "120000"])
def test_accept_rejects_executable_and_symlink_tree_entries(tmp_path, mode):
    # Executables and symlinks can never be records; the entry mode is
    # refused before the CAS even when the path and bytes are canonical.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    blob = _write_blob(repo.path, git_format.dumps_record({"fact": 1}))
    tree = _tree_with_mode_entry(
        repo.path, _tree_oid(repo.path, genesis), mode, path, blob,
        tmp_path, "index-mode-%s" % mode)
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="mode"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(path))
    assert repo.head_oid() == genesis


def test_accept_rejects_gitlink_tree_entry(tmp_path):
    # A Git submodule reference (mode 160000, object type commit) can
    # never be a record and must be refused before the CAS.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-9"
    tree = _tree_with_mode_entry(
        repo.path, _tree_oid(repo.path, genesis), "160000", path, genesis,
        tmp_path, "index-gitlink")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="commit"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(path))
    assert repo.head_oid() == genesis


def test_accept_rejects_empty_directory_outside_expected_paths(tmp_path):
    # A directory that holds no whitelisted record (for example an
    # empty subtree) is not part of the host snapshot and is refused.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    tree = _tree_with_empty_directory(repo.path, genesis)
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="empty"):
        repo.accept(genesis, commit, expected_paths=_accept_paths())
    assert repo.head_oid() == genesis


_FACT1_REV1 = git_format.record_path(
    "fact_revision", fact_uid="fact-1", revision=1)
_FACT1_CURRENT = git_format.record_path("fact_current", fact_uid="fact-1")


def _init_repo_with_fact_history(tmp_path, name="facts-history.git"):
    # Genesis plus one accepted commit carrying fact-1's revision 1
    # record and its current pointer: the append-only base every
    # retention test below extends or attacks.
    repo = _init_repo(tmp_path, name=name)
    genesis = repo.head_oid()
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), _FACT1_REV1,
        _write_blob(repo.path, git_format.dumps_record(
            {"fact_uid": "fact-1", "revision": 1})),
        tmp_path, "index-rev1")
    tree = _tree_with_added_file(
        repo.path, tree, _FACT1_CURRENT,
        _write_blob(repo.path, git_format.dumps_record(
            {"fact_uid": "fact-1", "revision": 1})),
        tmp_path, "index-current")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths(_FACT1_REV1, _FACT1_CURRENT)).accepted
    return repo, commit


def test_accept_allows_appending_a_new_fact_revision(tmp_path):
    # Section 10.2.2: history revisions are append-only, so adding
    # revision 2 next to an untouched revision 1 is the legal shape of a
    # new version.
    repo, tip = _init_repo_with_fact_history(tmp_path)
    rev2 = git_format.record_path(
        "fact_revision", fact_uid="fact-1", revision=2)
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, tip), rev2,
        _write_blob(repo.path, git_format.dumps_record(
            {"fact_uid": "fact-1", "revision": 2})),
        tmp_path, "index-rev2")
    commit = repo.create_commit(tree, tip)
    assert repo.accept(
        tip, commit,
        expected_paths=_accept_paths(
            _FACT1_REV1, _FACT1_CURRENT, rev2)).accepted
    assert repo.head_oid() == commit
    # The old revision blob rides into the new tree unchanged.
    assert _git_text(
        repo.path, "rev-parse", "%s:%s" % (commit, _FACT1_REV1)).strip() == \
        _git_text(
            repo.path, "rev-parse", "%s:%s" % (tip, _FACT1_REV1)).strip()


def test_accept_allows_current_pointer_advance(tmp_path):
    # The *_current pointers are mutable by design: rewriting the
    # pointer blob while keeping the append-only records is legal.
    repo, tip = _init_repo_with_fact_history(tmp_path)
    old_pointer = _git_text(
        repo.path, "rev-parse", "%s:%s" % (tip, _FACT1_CURRENT)).strip()
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, tip), _FACT1_CURRENT,
        _write_blob(repo.path, git_format.dumps_record(
            {"fact_uid": "fact-1", "revision": 2})),
        tmp_path, "index-current-rev2")
    commit = repo.create_commit(tree, tip)
    assert repo.accept(
        tip, commit,
        expected_paths=_accept_paths(_FACT1_REV1, _FACT1_CURRENT)).accepted
    assert repo.head_oid() == commit
    new_pointer = _git_text(
        repo.path, "rev-parse", "%s:%s" % (commit, _FACT1_CURRENT)).strip()
    assert new_pointer != old_pointer


def test_accept_rejects_deleted_append_only_record_and_keeps_ref(
        tmp_path):
    repo, tip = _init_repo_with_fact_history(tmp_path)
    tree = _tree_with_removed_file(
        repo.path, _tree_oid(repo.path, tip), _FACT1_REV1)
    commit = repo.create_commit(tree, tip)
    # The tree is otherwise well-formed and the whitelist matches its
    # remaining paths, so only the retention check can refuse it.
    with pytest.raises(GitRepositoryError, match="deleted"):
        repo.accept(tip, commit,
                    expected_paths=_accept_paths(_FACT1_CURRENT))
    assert repo.head_oid() == tip
    assert _head(repo.path) == tip
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == tip


def test_accept_rejects_rewritten_append_only_record_and_keeps_ref(
        tmp_path):
    repo, tip = _init_repo_with_fact_history(tmp_path)
    # Same path set, same canonical JSON shape, different bytes: an
    # in-place rewrite of an old version, refused before the CAS.
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, tip), _FACT1_REV1,
        _write_blob(repo.path, git_format.dumps_record(
            {"fact_uid": "fact-1", "revision": 1, "tampered": True})),
        tmp_path, "index-rewrite-rev1")
    commit = repo.create_commit(tree, tip)
    with pytest.raises(GitRepositoryError, match="rewritten in place"):
        repo.accept(
            tip, commit,
            expected_paths=_accept_paths(_FACT1_REV1, _FACT1_CURRENT))
    assert repo.head_oid() == tip
    assert _head(repo.path) == tip
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == tip


def test_accept_with_zero_expected_old_has_empty_retention_base(tmp_path):
    # Genesis boundary: with the all-zero expected_old there is no
    # accepted history, so the retention base is empty and a
    # well-formed zero-parent tree passes that check; only the CAS
    # decides. The ref already points at genesis here, so the same
    # acceptance reports a lost race instead of raising.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path,
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-zero-base")
    zero_parent = _commit_tree(repo.path, tree, [], "facts: zero base")
    result = repo.accept(
        repo.zero_oid, zero_parent, expected_paths=_accept_paths(path))
    assert result == CasResult(
        accepted=False, current_oid=genesis,
        expected_old=repo.zero_oid, new_oid=zero_parent)
    assert repo.head_oid() == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_oid_length_and_zero_oid_follow_object_format(tmp_path):
    repo = _init_repo(tmp_path)
    assert repo.object_format == _git_text(
        repo.path, "rev-parse", "--show-object-format").strip()
    # Nothing is pinned to 40 characters: lengths come from the repo.
    assert len(repo.genesis_oid) == repo.oid_length
    assert repo.zero_oid == "0" * repo.oid_length


def test_sha256_repository_uses_longer_oids_and_working_cas(tmp_path):
    repo = _init_repo(tmp_path, name="facts256.git", object_format="sha256")
    assert repo.object_format == "sha256"
    assert repo.oid_length == 64
    assert repo.zero_oid == "0" * 64
    assert len(repo.genesis_oid) == 64
    genesis = repo.head_oid()
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis),
        "facts/fact-1/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-1")
    commit = repo.create_commit(tree, genesis)
    assert len(commit) == 64
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    assert repo.head_oid() == commit


def test_oid_inputs_must_match_repository_object_format(tmp_path):
    repo = _init_repo(tmp_path)
    head = repo.head_oid()
    with pytest.raises(ValueError):
        repo.create_commit("123", head)
    with pytest.raises(ValueError):
        repo.create_commit("A" * repo.oid_length, head)
    with pytest.raises(ValueError):
        repo.accept(repo.zero_oid, "zz" + "0" * (repo.oid_length - 2),
                    expected_paths=_accept_paths())
    with pytest.raises(ValueError):
        repo.accept(head, repo.zero_oid, expected_paths=_accept_paths())


def _append_commit(repo, tmp_path, fact_name, index_name):
    head = repo.head_oid()
    blob = _write_blob(
        repo.path, git_format.dumps_record({"fact": fact_name}))
    path = "facts/%s/current.json" % fact_name
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, head), path, blob, tmp_path,
        index_name)
    commit = repo.create_commit(tree, head)
    # The host whitelist for the full snapshot: every record path the
    # current head tree already carries plus the one new path.
    existing = _git_text(
        repo.path, "ls-tree", "-r", "--name-only", head).split()
    assert repo.accept(
        head, commit, expected_paths=existing + [path]).accepted
    return commit


def _loose_object_path(repo_path, oid):
    # Plumbing-created test objects are always loose; nothing in these
    # fixtures ever packs or runs gc.
    return repo_path / "objects" / oid[:2] / oid[2:]


def _overwrite_loose_object(repo_path, oid, data):
    # Git stores loose objects read-only, so grant write permission
    # explicitly before damaging the fixture file.
    path = _loose_object_path(repo_path, oid)
    path.chmod(0o644)
    path.write_bytes(data)


def _point_ref_at(repo_path, oid, ref=REF_FACTS):
    # A damaged loose ref written directly: update-ref validates that a
    # branch target is a commit, so it cannot express this corruption.
    # Fixture refs are always loose; nothing ever packs them.
    (repo_path / ref).write_text(oid + "\n", encoding="ascii")


def _commit_object_raw(repo_path, body):
    # hash-object/commit-tree validate commit contents before writing
    # and refuse the malformed fixtures, so place the raw bytes as a
    # loose object directly: the OID is the repository object-format
    # hash of the canonical "commit <len>\0" header plus payload, and
    # the object is stored zlib-compressed under objects/xx/ like Git's
    # own loose-object layout.
    object_format = _git_text(
        repo_path, "rev-parse", "--show-object-format").strip()
    try:
        hasher = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}[
            object_format]
    except KeyError:
        raise AssertionError(
            "unexpected repository object format: %r" % object_format)
    stored = b"commit " + str(len(body)).encode("ascii") + b"\0" + body
    oid = hasher(stored).hexdigest()
    path = _loose_object_path(repo_path, oid)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(zlib.compress(stored))
        # Loose objects are read-only, matching Git's own layout.
        path.chmod(0o444)
    # The fixture must be addressable at the OID Git itself computes.
    assert _git_text(repo_path, "cat-file", "-t", oid).strip() == "commit"
    return oid


def _commit_object_with_parent(repo_path, tree, parent_hex):
    # A syntactically valid commit whose parent may point at an object
    # that does not exist.
    body = (
        b"tree " + tree.encode("ascii") + b"\n"
        b"parent " + parent_hex.encode("ascii") + b"\n"
        b"author Fixture <fixture@example.invalid> 1700000000 +0000\n"
        b"committer Fixture <fixture@example.invalid> 1700000000 +0000\n"
        b"\nfacts: fixture commit\n")
    return _commit_object_raw(repo_path, body)


def _malformed_parent_commit(repo_path, tree, parent_oid):
    # A commit whose parent header carries a non-ASCII byte where the
    # OID hex should be. Git itself rejects the header when parsing,
    # which is exactly the corruption the integrity check must convert
    # into IntegrityError.
    body = (
        b"tree " + tree.encode("ascii") + b"\n"
        b"parent " + parent_oid.encode("ascii")[:-1] + b"\xff\n"
        b"author Fixture <fixture@example.invalid> 1700000000 +0000\n"
        b"committer Fixture <fixture@example.invalid> 1700000000 +0000\n"
        b"\nfacts: malformed parent\n")
    return _commit_object_raw(repo_path, body)


def test_verify_integrity_genesis_only(tmp_path):
    repo = _init_repo(tmp_path)
    report = repo.verify_integrity()
    assert report == IntegrityReport(
        head_oid=repo.genesis_oid, genesis_oid=repo.genesis_oid,
        commit_count=1, object_format=repo.object_format)
    assert repo.verify_integrity(
        expected_head=repo.genesis_oid) == report
    assert _head(repo.path) == repo.genesis_oid


def test_verify_integrity_single_parent_chain(tmp_path):
    repo = _init_repo(tmp_path)
    commits = [
        _append_commit(repo, tmp_path, "fact-%d" % n, "index-%d" % n)
        for n in range(1, 4)]
    tip = commits[-1]
    report = repo.verify_integrity(expected_head=tip)
    assert report.head_oid == tip
    assert report.genesis_oid == repo.genesis_oid
    assert report.commit_count == 4
    assert report.ref == REF_FACTS
    assert _head(repo.path) == tip


def test_verify_integrity_rejects_expected_head_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    before = _head(repo.path)
    # The genesis is a real commit but no longer the ref value.
    with pytest.raises(IntegrityError):
        repo.verify_integrity(expected_head=repo.genesis_oid)
    # A commit from an unrelated repository is refused just the same.
    other = _init_repo(tmp_path, name="other.git", message="facts: other")
    with pytest.raises(IntegrityError):
        repo.verify_integrity(expected_head=other.genesis_oid)
    assert _head(repo.path) == before == tip
    # Malformed or all-zero targets are rejected as invalid input.
    with pytest.raises(ValueError):
        repo.verify_integrity(expected_head="123")
    with pytest.raises(ValueError):
        repo.verify_integrity(expected_head=repo.zero_oid)
    assert _head(repo.path) == before


def test_verify_integrity_fails_when_authoritative_ref_is_missing(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo.path, "update-ref", "-d", REF_FACTS)
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    # Nothing was repaired: the ref is still missing afterwards.
    result = _git(
        repo.path, "rev-parse", "--verify", REF_FACTS, check=False)
    assert result.returncode != 0


def test_verify_integrity_fails_when_ref_points_at_non_commit(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    tree = _tree_oid(repo.path, tip)
    _point_ref_at(repo.path, tree)
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    # The damaged ref value survives the check: nothing was repaired.
    assert _head(repo.path) == tree


def test_verify_integrity_fails_on_missing_ancestor(tmp_path):
    repo = _init_repo(tmp_path)
    middle = _append_commit(repo, tmp_path, "fact-1", "index-1")
    tip = _append_commit(repo, tmp_path, "fact-2", "index-2")
    before = _head(repo.path)
    _loose_object_path(repo.path, middle).unlink()
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    assert _head(repo.path) == before == tip


def test_verify_integrity_fails_on_corrupt_reachable_blob(tmp_path):
    # The chain walk never reads fact blobs, so only the fsck object
    # integrity leg can catch this corruption.
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    before = _head(repo.path)
    blob = _git_text(
        repo.path, "rev-parse", "%s:facts/fact-1/current.json" % tip
    ).strip()
    _overwrite_loose_object(repo.path, blob, b"not a zlib stream")
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    assert _head(repo.path) == before == tip


def test_verify_integrity_checks_only_authoritative_history(tmp_path):
    # fsck uses the captured H as its only reachability root with
    # reflogs disabled: a broken commit parked on another ref or in a
    # reflog entry is outside the authoritative history and must not
    # fail (or be scanned into) this check.
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    missing = "f" * repo.oid_length
    broken = _commit_object_with_parent(
        repo.path, _tree_oid(repo.path, tip), missing)
    _point_ref_at(repo.path, broken, ref="refs/heads/other")
    reflog = repo.path / "logs" / REF_FACTS
    reflog.parent.mkdir(parents=True, exist_ok=True)
    reflog.write_text(
        "%s %s Fixture <fixture@example.invalid> 1700000000 +0000\t"
        "fixture\n" % (tip, broken), encoding="ascii")
    # The corruption is real: with default roots (every ref and every
    # reflog) fsck does fail on the broken link; the explicit root and
    # --no-reflogs are what keep it out of the verification.
    default_fsck = _git(
        repo.path, "fsck", "--no-dangling", "--strict", check=False)
    assert default_fsck.returncode != 0
    report = repo.verify_integrity(expected_head=tip)
    assert report.head_oid == tip
    assert report.genesis_oid == repo.genesis_oid
    assert report.commit_count == 2
    assert _head(repo.path) == tip


def test_verify_integrity_ignores_replace_refs(tmp_path):
    # A replace ref must never substitute object contents during
    # verification: the check validates the real history.
    repo = _init_repo(tmp_path)
    middle = _append_commit(repo, tmp_path, "fact-1", "index-1")
    tip = _append_commit(repo, tmp_path, "fact-2", "index-2")
    real_middle = _git(repo.path, "cat-file", "commit", middle).stdout
    _git(repo.path, "replace", middle, repo.genesis_oid)
    # The replacement is active for plain plumbing: the middle commit
    # suddenly reads as the zero-parent genesis.
    assert _git(repo.path, "cat-file", "commit", middle).stdout != \
        real_middle
    report = repo.verify_integrity(expected_head=tip)
    assert report.head_oid == tip
    assert report.genesis_oid == repo.genesis_oid
    assert report.commit_count == 3
    assert _head(repo.path) == tip


def test_verify_integrity_fails_on_malformed_parent_header(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    malformed = _malformed_parent_commit(
        repo.path, _tree_oid(repo.path, tip), tip)
    _point_ref_at(repo.path, malformed)
    with pytest.raises(IntegrityError) as excinfo:
        repo.verify_integrity()
    # The malformed commit itself is what the chain walk chokes on, so
    # the fixture bytes really entered the verify path.
    assert malformed in str(excinfo.value)
    # The damaged ref value survives the check: nothing was repaired.
    assert _head(repo.path) == malformed


def test_commit_parents_decode_error_fails_closed(tmp_path):
    # The raw parent-header decode boundary: Git plumbing refuses to
    # parse such a commit at all, but the adapter parser must still
    # fail closed with its own error type instead of leaking a
    # UnicodeDecodeError if these bytes are ever handed to it.
    repo = _init_repo(tmp_path)
    malformed = _malformed_parent_commit(
        repo.path, _tree_oid(repo.path, repo.genesis_oid),
        repo.genesis_oid)
    # The malformed-parent-header message proves cat-file returned the
    # raw fixture payload and the adapter's own parser rejected it.
    with pytest.raises(GitRepositoryError, match="malformed parent header"):
        repo._commit_parents(malformed)


def test_verify_integrity_fails_on_multi_parent_commit(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    merge = _commit_tree(
        repo.path, _tree_oid(repo.path, tip),
        [tip, repo.genesis_oid], "facts: synthetic merge")
    _git(repo.path, "update-ref", REF_FACTS, merge)
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    assert _head(repo.path) == merge


def test_verify_integrity_fails_when_identity_record_changes_on_chain(
        tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    meta_path = git_format.record_path("repository_meta")
    rewritten = _meta_payload(company_id="co-evil")
    blob = _write_blob(repo.path, git_format.dumps_record(rewritten))
    env = {"GIT_INDEX_FILE": str(tmp_path / "index-evil-meta")}
    _git(repo.path, "read-tree", _tree_oid(repo.path, tip), env=env)
    _git(repo.path, "update-index", "--add", "--cacheinfo",
         "100644,%s,%s" % (blob, meta_path), env=env)
    tree = _git_text(repo.path, "write-tree", env=env).strip()
    commit = _commit_tree(repo.path, tree, [tip], "facts: rewrite meta")
    _git(repo.path, "update-ref", REF_FACTS, commit)
    # The bound instance compares against the host-issued identity.
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    # An unbound instance has no host identity to compare with, yet the
    # chain itself is inconsistent: the tip record differs from the
    # genesis record.
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(IntegrityError):
        unbound.verify_integrity()
    assert _head(repo.path) == commit


def test_verify_integrity_fails_when_identity_record_missing_on_chain(
        tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-1")
    tree = _tree_without_meta(repo.path, tmp_path, "index-nometa-tip")
    commit = _commit_tree(repo.path, tree, [tip], "facts: drop meta")
    _git(repo.path, "update-ref", REF_FACTS, commit)
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    assert _head(repo.path) == commit


def test_verify_integrity_sha256_chain_and_missing_ancestor(tmp_path):
    repo = _init_repo(tmp_path, name="facts256.git", object_format="sha256")
    middle = _append_commit(repo, tmp_path, "fact-1", "index-1")
    tip = _append_commit(repo, tmp_path, "fact-2", "index-2")
    report = repo.verify_integrity(expected_head=tip)
    assert report.head_oid == tip
    assert report.genesis_oid == repo.genesis_oid
    assert report.commit_count == 3
    assert report.object_format == "sha256"
    _loose_object_path(repo.path, middle).unlink()
    with pytest.raises(IntegrityError):
        repo.verify_integrity()
    assert _head(repo.path) == tip


def _snapshot_map(snapshot):
    return {record.path: record.content for record in snapshot.records}


def _all_object_oids(repo_path):
    # Every object the repository currently holds; read_snapshot_at()
    # must never add to this set.
    out = _git_text(
        repo_path, "cat-file", "--batch-all-objects",
        "--batch-check=%(objectname)")
    return set(out.split())


def test_read_snapshot_at_genesis_returns_identity_record(tmp_path):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    snapshot = repo.read_snapshot_at(repo.genesis_oid)
    assert snapshot.commit_oid == repo.genesis_oid
    assert snapshot.head_oid == repo.genesis_oid
    assert snapshot.ref == REF_FACTS
    # The genesis snapshot carries exactly the identity record, as the
    # raw canonical bytes written at initialize() time.
    assert _snapshot_map(snapshot) == {
        _META_PATH: git_format.dumps_record(_meta_payload()),
    }
    assert _head(repo.path) == before


def test_read_snapshot_at_pins_historical_and_current_commits(tmp_path):
    repo = _init_repo(tmp_path)
    first = _append_commit(repo, tmp_path, "fact-1", "index-hist-1")
    second = _append_commit(repo, tmp_path, "fact-2", "index-hist-2")
    expected_meta = git_format.dumps_record(_meta_payload())
    # A historical commit stays readable through the later head, and
    # carries only the records accepted at that commit.
    historical = repo.read_snapshot_at(first)
    assert historical.commit_oid == first
    assert historical.head_oid == second
    assert _snapshot_map(historical) == {
        _META_PATH: expected_meta,
        "facts/fact-1/current.json":
            git_format.dumps_record({"fact": "fact-1"}),
    }
    # The current snapshot of the tip adds the second fact record.
    current = repo.read_snapshot_at(second)
    assert current.commit_oid == second
    assert current.head_oid == second
    assert _snapshot_map(current) == {
        _META_PATH: expected_meta,
        "facts/fact-1/current.json":
            git_format.dumps_record({"fact": "fact-1"}),
        "facts/fact-2/current.json":
            git_format.dumps_record({"fact": "fact-2"}),
    }
    paths = [record.path for record in current.records]
    assert paths == sorted(paths)
    # A later move of H never splices newer records into the pinned C:
    # the same historical read returns the same bytes, with only the
    # reachability-proof head advancing.
    third = _append_commit(repo, tmp_path, "fact-3", "index-hist-3")
    again = repo.read_snapshot_at(first)
    assert again.commit_oid == first
    assert again.head_oid == third
    assert again.records == historical.records
    assert _head(repo.path) == third


def test_read_snapshot_at_rejects_unreachable_commit(tmp_path):
    # A commit object that exists in the store but lost its CAS race is
    # not part of the authoritative history and must be refused.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    base_tree = _tree_oid(repo.path, genesis)
    tree1 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-1/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-unreach-1")
    competitor = repo.create_commit(tree1, genesis)
    assert repo.accept(
        genesis, competitor,
        expected_paths=_accept_paths("facts/fact-1/current.json")).accepted
    tree2 = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-2/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 2})),
        tmp_path, "index-unreach-2")
    loser = _commit_tree(repo.path, tree2, [genesis], "facts: stale")
    assert _git_text(repo.path, "cat-file", "-t", loser).strip() == "commit"
    before = _head(repo.path)
    with pytest.raises(SnapshotReadError, match="not reachable"):
        repo.read_snapshot_at(loser)
    assert _head(repo.path) == before == competitor


def test_read_snapshot_at_rejects_unknown_object(tmp_path):
    repo = _init_repo(tmp_path)
    before = _head(repo.path)
    with pytest.raises(SnapshotReadError, match="not a readable object"):
        repo.read_snapshot_at("f" * repo.oid_length)
    assert _head(repo.path) == before


def test_read_snapshot_at_rejects_non_commit_oid(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-noncommit")
    tree = _tree_oid(repo.path, tip)
    blob = _git_text(
        repo.path, "rev-parse", "%s:facts/fact-1/current.json" % tip
    ).strip()
    before = _head(repo.path)
    for oid in (tree, blob):
        with pytest.raises(SnapshotReadError, match="not a commit"):
            repo.read_snapshot_at(oid)
    assert _head(repo.path) == before


def test_read_snapshot_at_rejects_malformed_oid_input(tmp_path):
    repo = _init_repo(tmp_path)
    for bad in ("123", "A" * repo.oid_length, repo.zero_oid, 42):
        with pytest.raises(ValueError):
            repo.read_snapshot_at(bad)
    assert _head(repo.path) == repo.head_oid()


def test_read_snapshot_at_requires_bound_identity(tmp_path):
    repo = _init_repo(tmp_path)
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(SnapshotReadError, match="not bound"):
        unbound.read_snapshot_at(repo.genesis_oid)
    assert _head(repo.path) == repo.genesis_oid


def test_read_snapshot_at_fails_when_authoritative_head_is_unusable(
        tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-headbad")
    tree = _tree_oid(repo.path, tip)
    _point_ref_at(repo.path, tree)
    with pytest.raises(SnapshotReadError, match="does not point to a "
                                                "commit"):
        repo.read_snapshot_at(tip)
    assert _head(repo.path) == tree
    _git(repo.path, "update-ref", "-d", REF_FACTS)
    with pytest.raises(SnapshotReadError, match="missing"):
        repo.read_snapshot_at(tip)
    # Nothing was repaired: the ref is still missing afterwards.
    result = _git(
        repo.path, "rev-parse", "--verify", REF_FACTS, check=False)
    assert result.returncode != 0


@pytest.mark.parametrize("case", ["noncanonical_blob", "freeform_path"])
def test_read_snapshot_at_rejects_anomalous_tree(tmp_path, case):
    # Corruption on the authoritative chain (planted by raw plumbing and
    # a forced ref, the way verify_integrity fixtures do it): the read
    # must fail closed instead of returning the anomalous bytes.
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-anom-%s" % case)
    if case == "noncanonical_blob":
        blob = _write_blob(repo.path, b'{"fact": 2}\n')
        path = "facts/fact-2/current.json"
    else:
        blob = _write_blob(repo.path, git_format.dumps_record({"note": "x"}))
        path = "notes/freeform.json"
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, tip), path, blob, tmp_path,
        "index-anom-tree-%s" % case)
    commit = _commit_tree(repo.path, tree, [tip], "facts: anomalous")
    _git(repo.path, "update-ref", REF_FACTS, commit)
    with pytest.raises(SnapshotReadError, match="failed validation"):
        repo.read_snapshot_at(commit)
    # The earlier, well-formed commit on the same chain still reads:
    # the refusal is scoped to the anomalous C, not the whole history.
    assert "facts/fact-1/current.json" in _snapshot_map(
        repo.read_snapshot_at(tip))
    assert _head(repo.path) == commit


def test_read_snapshot_at_rejects_unreadable_object(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-corrupt-read")
    blob = _git_text(
        repo.path, "rev-parse", "%s:facts/fact-1/current.json" % tip
    ).strip()
    _overwrite_loose_object(repo.path, blob, b"not a zlib stream")
    with pytest.raises(SnapshotReadError, match="unreadable"):
        repo.read_snapshot_at(tip)
    assert _head(repo.path) == tip


def test_read_snapshot_at_rejects_identity_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-idm-read")
    rewritten = _meta_payload(company_id="co-evil")
    blob = _write_blob(repo.path, git_format.dumps_record(rewritten))
    env = {"GIT_INDEX_FILE": str(tmp_path / "index-evil-meta-read")}
    _git(repo.path, "read-tree", _tree_oid(repo.path, tip), env=env)
    _git(repo.path, "update-index", "--add", "--cacheinfo",
         "100644,%s,%s" % (blob, _META_PATH), env=env)
    tree = _git_text(repo.path, "write-tree", env=env).strip()
    commit = _commit_tree(repo.path, tree, [tip], "facts: rewrite meta")
    _git(repo.path, "update-ref", REF_FACTS, commit)
    with pytest.raises(SnapshotReadError, match="identity"):
        repo.read_snapshot_at(commit)
    assert _head(repo.path) == commit


def test_read_snapshot_at_creates_no_objects_and_moves_no_ref(tmp_path):
    repo = _init_repo(tmp_path)
    first = _append_commit(repo, tmp_path, "fact-1", "index-ro-1")
    tip = _append_commit(repo, tmp_path, "fact-2", "index-ro-2")
    objects_before = _all_object_oids(repo.path)
    repo.read_snapshot_at(first)
    repo.read_snapshot_at(tip)
    assert _all_object_oids(repo.path) == objects_before
    assert _head(repo.path) == tip
