"""Tests for app.facts_tree.git_repository.

Every repository here is a synthetic bare repo created with Git
plumbing inside pytest tmp_path; nothing touches SAM-APP, SAM-DOCS,
data/, a real SAM_GIT_DIR, or any remote. The production adapter only
ever receives OIDs; tests may additionally drive plumbing directly to
prepare synthetic trees and competing commits. The persistent
maintenance pin tests additionally force refs and run
``git gc --prune=now``, strictly confined to these synthetic tmp_path
bare repositories.
"""
import hashlib
import os
import subprocess
import zlib

import pytest

from app.facts_tree import git_format
from app.facts_tree import git_repository
from app.facts_tree.git_repository import (
    PIN_REF_NAMESPACE,
    REF_FACTS,
    CasResult,
    FactsGitRepository,
    GitRepositoryError,
    IdentityMismatchError,
    InitializeError,
    IntegrityError,
    IntegrityReport,
    MaintenancePin,
    MaintenancePinError,
    OpenError,
    PinConflictError,
    PinCreationResult,
    PinHeadMismatchError,
    PinTargetError,
    SnapshotReadError,
    StaleBaseError,
    maintenance_pin_ref,
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


# Design 97 §4 items 1 (路径身份) and 5 (操作闭合): the candidate
# path-identity and reference closure enforced on accept(). The fixture
# payloads carry only the fields the closure validator needs plus
# schema_version/record_type as permitted extra fields; this card is not
# the full business schema gate.
_CAND1_UID = "cand-1"
_CAND1_REVISION = 1
# A legal 64-character lowercase hexadecimal SHA-256 digest shape; the
# closure validator compares digests for equality and never recomputes
# them.
_CAND1_DIGEST = "a" * 64
_CAND1_REF = {
    "candidate_uid": _CAND1_UID,
    "candidate_revision": _CAND1_REVISION,
    "payload_digest": _CAND1_DIGEST,
}


def _candidate_input_path(candidate_uid=_CAND1_UID,
                          candidate_revision=_CAND1_REVISION):
    return git_format.record_path(
        "input_candidate", candidate_uid=candidate_uid,
        candidate_revision=candidate_revision)


def _candidate_operation_path(operation_id="op-1"):
    return git_format.record_path("operation", operation_id=operation_id)


def _candidate_consumed_path(candidate_uid=_CAND1_UID,
                             candidate_revision=_CAND1_REVISION):
    return git_format.record_path(
        "consumed_candidate", candidate_uid=candidate_uid,
        candidate_revision=candidate_revision)


def _candidate_input_payload(candidate_uid=_CAND1_UID,
                             candidate_revision=_CAND1_REVISION,
                             digest=_CAND1_DIGEST, **overrides):
    payload = {
        "schema_version": 1,
        "record_type": "input_candidate",
        "candidate_uid": candidate_uid,
        "candidate_revision": candidate_revision,
        "payload_digest": digest,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
    }
    payload.update(overrides)
    return payload


def _candidate_operation_payload(operation_id="op-1", refs=(_CAND1_REF,),
                                 base_commit=None, event_ids=(), **overrides):
    # The accept-time operation/event closure leg additionally requires
    # base_commit and event_ids on every operation, so candidate-closure
    # fixtures carry them: base_commit is the parent the synthetic
    # accepting commit is built on (almost always genesis), and these
    # candidate scenarios list no events.
    payload = {
        "schema_version": 1,
        "record_type": "operation",
        "operation_id": operation_id,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
        "candidate_refs": list(refs),
        "base_commit": base_commit,
        "event_ids": list(event_ids),
    }
    payload.update(overrides)
    return payload


def _candidate_consumed_payload(candidate_uid=_CAND1_UID,
                                candidate_revision=_CAND1_REVISION,
                                digest=_CAND1_DIGEST, operation_id="op-1",
                                **overrides):
    payload = {
        "schema_version": 1,
        "record_type": "consumed_candidate",
        "candidate_uid": candidate_uid,
        "candidate_revision": candidate_revision,
        "payload_digest": digest,
        "operation_id": operation_id,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
    }
    payload.update(overrides)
    return payload


def _closed_candidate_records(base_commit):
    # One of each family, mutually closed inside one commit: the input
    # is referenced by the operation and terminally consumed through
    # that same operation's typed reference. The operation binds itself
    # to the accepting commit's parent (base_commit) and lists no events.
    return [
        (_candidate_input_path(), _candidate_input_payload()),
        (_candidate_operation_path(),
         _candidate_operation_payload(base_commit=base_commit)),
        (_candidate_consumed_path(), _candidate_consumed_payload()),
    ]


def _tree_with_records(repo_path, base_tree, records, tmp_path, index_name):
    # Layer each (path, payload) record onto the tree with its own index
    # file, exactly as the append-only fixtures do.
    tree = base_tree
    for number, (path, payload) in enumerate(records):
        tree = _tree_with_added_file(
            repo_path, tree, path,
            _write_blob(repo_path, git_format.dumps_record(payload)),
            tmp_path, "%s-%d" % (index_name, number))
    return tree


def test_accept_closes_candidate_records_and_advances_ref(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = _closed_candidate_records(genesis)
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-cand-ok")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths(*paths)).accepted
    assert repo.head_oid() == commit
    assert _head(repo.path) == commit
    # The closure gate lives on the accept path only: the storage-only
    # read still returns every record of the pinned commit as raw bytes.
    snapshot = repo.read_snapshot_at(commit)
    assert {record.path for record in snapshot.records} == \
        set(paths) | {_META_PATH}
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == commit


def test_accept_rejects_candidate_path_payload_identity_mismatch(tmp_path):
    # The input payload identifies cand-1 rev 1, but the blob is parked
    # at cand-2's canonical path. The physical tree is well-formed
    # (canonical path, canonical JSON, matching whitelist), so only the
    # accept-time path-identity leg can refuse it, before the CAS.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    planted_path = _candidate_input_path(candidate_uid="cand-2")
    records = [
        (planted_path, _candidate_input_payload()),
        (_candidate_operation_path(),
         _candidate_operation_payload(base_commit=genesis)),
        (_candidate_consumed_path(), _candidate_consumed_payload()),
    ]
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-cand-pathmismatch")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(
            GitRepositoryError,
            match="candidate path/reference closure"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


@pytest.mark.parametrize("case,refs,reason", [
    ("dangling", [{
        "candidate_uid": "cand-9",
        "candidate_revision": 1,
        "payload_digest": _CAND1_DIGEST,
    }], "dangling reference"),
    ("digest", [{
        "candidate_uid": _CAND1_UID,
        "candidate_revision": _CAND1_REVISION,
        "payload_digest": "b" * 64,
    }], "digest mismatch"),
])
def test_accept_rejects_unclosed_candidate_reference_without_moving_ref(
        tmp_path, case, refs, reason):
    # Path identity matches for all three families; the operation's
    # typed candidate_refs either name an input this commit does not
    # carry or disagree with the input digest. The refusal must happen
    # before update-ref.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = [
        (_candidate_input_path(), _candidate_input_payload()),
        (_candidate_operation_path(),
         _candidate_operation_payload(refs=refs, base_commit=genesis)),
        (_candidate_consumed_path(), _candidate_consumed_payload()),
    ]
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-cand-ref-%s" % case)
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match=reason):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_rejects_candidate_record_claiming_foreign_scope(tmp_path):
    # The expected scope is the bound repository identity, never the
    # scope a candidate record self-reports: three mutually consistent
    # records under another company still cannot close.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = [
        (_candidate_input_path(),
         _candidate_input_payload(company_id="co-other")),
        (_candidate_operation_path(),
         _candidate_operation_payload(base_commit=genesis,
                                      company_id="co-other")),
        (_candidate_consumed_path(),
         _candidate_consumed_payload(company_id="co-other")),
    ]
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-cand-scope")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="expected company"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_with_no_candidate_families_passes_closure_leg(tmp_path):
    # Three empty candidate collections must stay legal: an ordinary
    # snapshot carrying no candidate-family records accepts exactly as
    # before, and so does the genesis acceptance exercised by
    # initialize() in every fixture above.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-1/current.json"
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path,
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-cand-empty")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit, expected_paths=_accept_paths(path)).accepted
    assert repo.head_oid() == commit
    assert _head(repo.path) == commit


def test_accept_closure_consumes_validated_tree_bytes_without_reread(
        tmp_path, monkeypatch):
    # The closure leg must consume the exact blob bytes the physical
    # tree-validation leg already read and validated, never issue its
    # own second ``cat-file blob`` by OID. Count bare-OID blob reads
    # during one successful accept: the candidate tree holds four blobs
    # (repository_meta plus the three candidate records), so the total
    # must be exactly four, each OID read once. The meta read earlier in
    # accept uses the <commit>:<path> form and is not a bare-OID read.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = _closed_candidate_records(genesis)
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-cand-noreread")
    commit = repo.create_commit(tree, genesis)

    blob_reads = []
    real_git_bytes = repo._git_bytes

    def counting_git_bytes(args, env_extra=None):
        if len(args) == 3 and args[:2] == ["cat-file", "blob"] \
                and ":" not in args[2]:
            blob_reads.append(args[2])
        return real_git_bytes(args, env_extra=env_extra)

    monkeypatch.setattr(repo, "_git_bytes", counting_git_bytes)
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths(*paths)).accepted
    assert len(blob_reads) == 4
    assert len(set(blob_reads)) == len(blob_reads)
    assert repo.head_oid() == commit


# Design 97 §3, §4 item 5 (操作闭合) and §5 (事件契约): the
# operation/event cross-record closure and the new-operation parent
# binding enforced on accept(). Fixture operations carry candidate_refs
# as well (the candidate closure leg runs on the same commit) but list
# no candidates here; payloads carry only closure fields plus harmless
# record_type/schema_version extras, not the full business schema.
_OE_OP_ID = "op-evt-1"
_OE_EVENT_ID = "evt-1"


def _oe_operation_path(operation_id=_OE_OP_ID):
    return git_format.record_path("operation", operation_id=operation_id)


def _oe_event_path(event_id=_OE_EVENT_ID):
    return git_format.record_path("event", event_id=event_id)


def _oe_operation_payload(operation_id=_OE_OP_ID, *, base_commit,
                          event_ids=(_OE_EVENT_ID,), **overrides):
    payload = {
        "schema_version": 1,
        "record_type": "operation",
        "operation_id": operation_id,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
        "candidate_refs": [],
        "base_commit": base_commit,
        "event_ids": list(event_ids),
    }
    payload.update(overrides)
    return payload


def _oe_event_payload(event_id=_OE_EVENT_ID, operation_id=_OE_OP_ID, *,
                      base_commit, ordinal=0, **overrides):
    payload = {
        "schema_version": 1,
        "record_type": "event",
        "event_id": event_id,
        "operation_id": operation_id,
        "repo_id": _IDS["repo_id"],
        "company_id": _IDS["company_id"],
        "base_commit": base_commit,
        "ordinal": ordinal,
        "event_type": "fact_revision_committed",
    }
    payload.update(overrides)
    return payload


def _closed_operation_event_records(base_commit, operation_id=_OE_OP_ID,
                                    event_id=_OE_EVENT_ID, ordinal=0):
    # One new operation plus its one event, mutually closed inside one
    # commit and both bound to the same parent base_commit.
    return [
        (_oe_operation_path(operation_id),
         _oe_operation_payload(
             operation_id=operation_id, base_commit=base_commit,
             event_ids=[event_id])),
        (_oe_event_path(event_id),
         _oe_event_payload(
             event_id=event_id, operation_id=operation_id,
             base_commit=base_commit, ordinal=ordinal)),
    ]


def test_accept_closes_operation_event_pair_and_advances_ref(tmp_path):
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = _closed_operation_event_records(genesis)
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-oe-ok")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit,
        expected_paths=_accept_paths(*paths)).accepted
    assert repo.head_oid() == commit
    assert _head(repo.path) == commit
    snapshot = repo.read_snapshot_at(commit)
    assert {record.path for record in snapshot.records} == \
        set(paths) | {_META_PATH}
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == commit


def test_accept_rejects_new_operation_bound_to_wrong_base_before_cas(
        tmp_path):
    # The operation and its event agree with each other, so the generic
    # whole-commit closure passes, but both bind to a base_commit that is
    # not this commit's parent H. Only the new-operation parent binding
    # can refuse this, and it must happen before the CAS.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    wrong_base = "f" * 40
    records = [
        (_oe_operation_path(),
         _oe_operation_payload(base_commit=wrong_base)),
        (_oe_event_path(), _oe_event_payload(base_commit=wrong_base)),
    ]
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-oe-badbase")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="base_commit"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


@pytest.mark.parametrize("case,event_ids,carry_event,reason", [
    ("dangling", ["evt-missing"], False,
     "dangling or foreign-owned listing"),
    ("orphan", [], True, "no orphan event"),
])
def test_accept_rejects_unclosed_operation_event_listing(
        tmp_path, case, event_ids, carry_event, reason):
    # Paths and scope are canonical; the listing itself does not close:
    # the operation names an event this commit does not carry
    # (dangling), or an event of this operation is not listed (orphan).
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = [
        (_oe_operation_path(),
         _oe_operation_payload(base_commit=genesis, event_ids=event_ids)),
    ]
    if carry_event:
        records.append(
            (_oe_event_path(), _oe_event_payload(base_commit=genesis)))
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-oe-%s" % case)
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match=reason):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_rejects_event_path_identity_mismatch_before_cas(tmp_path):
    # Design 97 §4 item 1 (路径身份) for the event family: the event
    # file sits at the canonical path of a different id (evt-other)
    # while its body names evt-1. The payload-level closure still
    # closes — the operation lists exactly evt-1 and the carried event
    # body is evt-1 owned by that operation — so only the path
    # identity leg can refuse this, and it must happen before the CAS.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    planted_event_path = _oe_event_path("evt-other")
    records = [
        (_oe_operation_path(),
         _oe_operation_payload(
             base_commit=genesis, event_ids=[_OE_EVENT_ID])),
        (planted_event_path,
         _oe_event_payload(base_commit=genesis)),
    ]
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-oe-badpath")
    commit = repo.create_commit(tree, genesis)
    with pytest.raises(GitRepositoryError, match="canonical record_path"):
        repo.accept(genesis, commit, expected_paths=_accept_paths(*paths))
    assert repo.head_oid() == genesis
    assert _head(repo.path) == genesis
    assert FactsGitRepository.open(repo.path, **_IDS).head_oid() == genesis


def test_accept_genesis_zero_base_operation_passes_closure_but_loses_cas(
        tmp_path):
    # The all-zero base_commit is the legal genesis form for a new
    # operation: a zero-parent commit carrying a closed operation/event
    # pair bound to the all-zero OID must pass every closure leg. The
    # ref already sits at the real genesis, so only the CAS rejects this
    # late zero-parent sibling (a lost race), never a closure error.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    records = _closed_operation_event_records(repo.zero_oid)
    paths = [path for path, _payload in records]
    tree = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), records, tmp_path,
        "index-oe-zerobase")
    zero_parent = _commit_tree(
        repo.path, tree, [], "facts: zero-base operations")
    result = repo.accept(
        repo.zero_oid, zero_parent,
        expected_paths=_accept_paths(*paths))
    assert result == CasResult(
        accepted=False, current_oid=genesis,
        expected_old=repo.zero_oid, new_oid=zero_parent)
    assert repo.head_oid() == genesis


def test_accept_later_commit_retains_older_operation_with_original_base(
        tmp_path):
    # Commit C1 accepts op-1/event-1 bound to genesis. The next commit
    # C2 adds a second operation op-2: op-2 must bind to C1 (its real
    # parent), while op-1 rides along unchanged and must stay valid even
    # though its original base_commit (genesis) is no longer the current
    # parent. A new op-2 that wrongly binds to genesis is refused first.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    first = _closed_operation_event_records(genesis)
    first_paths = [path for path, _payload in first]
    tree1 = _tree_with_records(
        repo.path, _tree_oid(repo.path, genesis), first, tmp_path,
        "index-oe-c1")
    commit1 = repo.create_commit(tree1, genesis)
    assert repo.accept(
        genesis, commit1,
        expected_paths=_accept_paths(*first_paths)).accepted

    op2_id = "op-evt-2"
    evt2_id = "evt-2"
    op2_path = _oe_operation_path(op2_id)
    evt2_path = _oe_event_path(evt2_id)
    second_paths = [op2_path, evt2_path]
    all_paths = first_paths + second_paths

    bad_second = [
        (op2_path, _oe_operation_payload(
            operation_id=op2_id, base_commit=genesis,
            event_ids=[evt2_id])),
        (evt2_path, _oe_event_payload(
            event_id=evt2_id, operation_id=op2_id,
            base_commit=genesis)),
    ]
    bad_tree = _tree_with_records(
        repo.path, tree1, bad_second, tmp_path, "index-oe-c2-bad")
    bad_commit = repo.create_commit(bad_tree, commit1)
    with pytest.raises(GitRepositoryError, match="base_commit"):
        repo.accept(
            commit1, bad_commit,
            expected_paths=_accept_paths(*all_paths))
    assert repo.head_oid() == commit1

    good_second = [
        (op2_path, _oe_operation_payload(
            operation_id=op2_id, base_commit=commit1,
            event_ids=[evt2_id])),
        (evt2_path, _oe_event_payload(
            event_id=evt2_id, operation_id=op2_id,
            base_commit=commit1)),
    ]
    tree2 = _tree_with_records(
        repo.path, tree1, good_second, tmp_path, "index-oe-c2-ok")
    commit2 = repo.create_commit(tree2, commit1)
    assert repo.accept(
        commit1, commit2,
        expected_paths=_accept_paths(*all_paths)).accepted
    assert repo.head_oid() == commit2
    snapshot = repo.read_snapshot_at(commit2)
    bytes_by_path = {
        record.path: record.content for record in snapshot.records}
    op1_path = _oe_operation_path()
    assert bytes_by_path[op1_path] == git_format.dumps_record(
        _oe_operation_payload(base_commit=genesis))
    assert bytes_by_path[op2_path] == git_format.dumps_record(
        _oe_operation_payload(
            operation_id=op2_id, base_commit=commit1,
            event_ids=[evt2_id]))


def test_accept_with_no_operation_or_event_records_passes_event_closure(
        tmp_path):
    # Two empty operation/event collections must stay legal: an ordinary
    # snapshot carrying neither family accepts exactly as before (as does
    # the genesis acceptance in every fixture above).
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    path = "facts/fact-2/current.json"
    tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis), path,
        _write_blob(repo.path, git_format.dumps_record({"fact": 2})),
        tmp_path, "index-oe-empty")
    commit = repo.create_commit(tree, genesis)
    assert repo.accept(
        genesis, commit, expected_paths=_accept_paths(path)).accepted
    assert repo.head_oid() == commit
    assert _head(repo.path) == commit


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


# Persistent backup maintenance pins (CHG-066 section 3 step 4 and
# section 4): the pin is a plain ref under the dedicated refs/sam
# namespace, created through one atomic update-ref transaction and
# never moved afterwards.


def _raw_pin_value(repo_path, ref):
    result = _git(
        repo_path, "rev-parse", "--verify", "--quiet", ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii").strip()


def _raw_refs_under(repo_path, prefix):
    out = _git_text(repo_path, "for-each-ref", "--format=%(refname)",
                    prefix)
    return [line for line in out.splitlines() if line]


def test_create_maintenance_pin_freezes_current_head(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    tree_before = _git_text(repo.path, "ls-tree", "-r", h1)
    objects_before = _all_object_oids(repo.path)
    result = repo.create_maintenance_pin("bk-0001", "bar-0001", h1)
    expected_ref = maintenance_pin_ref("bk-0001", "bar-0001")
    assert result == PinCreationResult(
        pin=MaintenancePin(
            backup_id="bk-0001", barrier_id="bar-0001",
            commit_oid=h1, ref=expected_ref),
        created=True)
    # The dedicated namespace: inside refs/sam, disjoint from the
    # business authority ref.
    assert expected_ref == PIN_REF_NAMESPACE + "/bk-0001/bar-0001"
    assert expected_ref.startswith("refs/sam/")
    assert _raw_pin_value(repo.path, expected_ref) == h1
    # The business authority is untouched: head, tree and the whole
    # object set are unchanged; a pin is a ref, never tree content.
    assert repo.head_oid() == h1
    assert _head(repo.path) == h1
    assert _git_text(repo.path, "ls-tree", "-r", h1) == tree_before
    assert _all_object_oids(repo.path) == objects_before
    # The query legs report the same pin.
    assert repo.read_maintenance_pin("bk-0001", "bar-0001") == result.pin
    assert repo.list_maintenance_pins() == (result.pin,)


def test_pin_keeps_original_commit_after_head_advances(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    assert repo.create_maintenance_pin("bk-1", "bar-1", h1).created
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-advance-1")
    h3 = _append_commit(repo, tmp_path, "fact-2", "index-pin-advance-2")
    pin = repo.read_maintenance_pin("bk-1", "bar-1")
    assert pin.commit_oid == h1
    assert _raw_pin_value(repo.path, pin.ref) == h1
    assert repo.head_oid() == h3
    assert h3 != h1
    # An idempotent re-assertion of the same pin stays legal after the
    # head moved past H: the head equality constrains only a new pin.
    again = repo.create_maintenance_pin("bk-1", "bar-1", h1)
    assert again.created is False
    assert again.pin.commit_oid == h1
    # The new head can be pinned under a different identity.
    assert repo.create_maintenance_pin("bk-2", "bar-2", h3).created
    assert repo.read_maintenance_pin("bk-1", "bar-1").commit_oid == h1


def test_repeated_identical_pin_request_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    first = repo.create_maintenance_pin("bk-i", "bar-i", h1)
    second = repo.create_maintenance_pin("bk-i", "bar-i", h1)
    assert first.created is True
    assert second.created is False
    assert second.pin == first.pin
    assert _raw_refs_under(repo.path, PIN_REF_NAMESPACE) == [first.pin.ref]
    assert _head(repo.path) == h1


def test_same_identity_different_commit_conflicts_and_never_moves(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    repo.create_maintenance_pin("bk-c", "bar-c", h1)
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-conflict")
    ref = maintenance_pin_ref("bk-c", "bar-c")
    # h2 is a perfectly valid current head, but the identity is already
    # bound to h1: rebinding fails closed and moves nothing.
    with pytest.raises(PinConflictError):
        repo.create_maintenance_pin("bk-c", "bar-c", h2)
    assert _raw_pin_value(repo.path, ref) == h1
    assert repo.read_maintenance_pin("bk-c", "bar-c").commit_oid == h1
    assert _head(repo.path) == h2


def test_new_pin_requires_target_equal_current_head(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-stale")
    # h1 is a real existing commit, but it is no longer the captured
    # head: a new pin identity may only freeze the current head.
    with pytest.raises(PinHeadMismatchError):
        repo.create_maintenance_pin("bk-old", "bar-old", h1)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert repo.read_maintenance_pin("bk-old", "bar-old") is None
    assert _head(repo.path) == h2


@pytest.mark.parametrize("backup_id,barrier_id", [
    ("", "bar-1"),
    ("bk-1", ""),
    (None, "bar-1"),
    ("bk-1", None),
    (42, "bar-1"),
    ("bk-1", 42),
    ("bk/1", "bar-1"),
    ("bk..1", "bar-1"),
    (".bk1", "bar-1"),
    ("bk1.", "bar-1"),
    ("bk.lock", "bar-1"),
    ("bk 1", "bar-1"),
    ("bk~1", "bar-1"),
    ("bk^1", "bar-1"),
    ("bk:1", "bar-1"),
    ("bk?1", "bar-1"),
    ("bk*1", "bar-1"),
    ("bk[1", "bar-1"),
    ("bk\\1", "bar-1"),
    ("bk@{1}", "bar-1"),
    ("bké1", "bar-1"),
    ("a" * 65, "bar-1"),
    ("bk-1", "b" * 65),
])
def test_pin_rejects_malformed_identifiers(tmp_path, backup_id, barrier_id):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    with pytest.raises(ValueError):
        repo.create_maintenance_pin(backup_id, barrier_id, h1)
    with pytest.raises(ValueError):
        repo.read_maintenance_pin(backup_id, barrier_id)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert repo.head_oid() == h1


def test_pin_rejects_malformed_oid_input(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    for bad in ("123", "A" * repo.oid_length, repo.zero_oid, 42,
                "0" * (repo.oid_length - 1)):
        with pytest.raises(ValueError):
            repo.create_maintenance_pin("bk-o", "bar-o", bad)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert repo.head_oid() == h1


def test_pin_rejects_non_commit_and_unknown_objects(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _append_commit(repo, tmp_path, "fact-1", "index-pin-noncommit")
    tree = _tree_oid(repo.path, tip)
    blob = _git_text(
        repo.path, "rev-parse",
        "%s:facts/fact-1/current.json" % tip).strip()
    unknown = "f" * repo.oid_length
    before = _head(repo.path)
    for oid in (tree, blob, unknown):
        with pytest.raises(PinTargetError):
            repo.create_maintenance_pin("bk-t", "bar-t", oid)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert _head(repo.path) == before == tip


def test_pin_creation_requires_bound_identity(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    unbound = FactsGitRepository(repo.path)
    with pytest.raises(MaintenancePinError, match="not bound"):
        unbound.create_maintenance_pin("bk-b", "bar-b", h1)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    # The queries are pure ref reads and do not require the binding.
    assert unbound.read_maintenance_pin("bk-b", "bar-b") is None
    assert unbound.list_maintenance_pins() == ()


def test_pin_creation_rejects_fsync_policy_violation(tmp_path):
    # The section 10.9.1 durability policy is re-verified immediately
    # before the pin transaction: a drifted repository never gains a
    # pin whose persistence cannot be confirmed.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    _git(repo.path, "config", "--local", "core.fsync", "none")
    with pytest.raises(GitRepositoryError, match="fsync policy"):
        repo.create_maintenance_pin("bk-fsync", "bar-fsync", h1)
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert repo.head_oid() == h1
    # The refusal was the configuration check: with the exact policy
    # restored, the same pin request succeeds.
    _git(repo.path, "config", "--local", "core.fsync",
         _FSYNC_EXPECTED["core.fsync"])
    assert repo.create_maintenance_pin("bk-fsync", "bar-fsync", h1).created


def test_pin_survives_reopen_by_a_new_instance(tmp_path):
    # Persistence across a process restart: the pin is a plain ref on
    # disk, and a freshly opened instance sees the identical value.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    created = repo.create_maintenance_pin("bk-p", "bar-p", h1)
    reopened = FactsGitRepository.open(repo.path, **_IDS)
    pin = reopened.read_maintenance_pin("bk-p", "bar-p")
    assert pin == created.pin
    assert reopened.list_maintenance_pins() == (created.pin,)
    again = reopened.create_maintenance_pin("bk-p", "bar-p", h1)
    assert again.created is False
    assert again.pin == created.pin


def test_pin_queries_report_absent_pins(tmp_path):
    repo = _init_repo(tmp_path)
    assert repo.read_maintenance_pin("bk-none", "bar-none") is None
    assert repo.list_maintenance_pins() == ()
    assert _raw_refs_under(repo.path, "refs/sam/") == []


def test_list_maintenance_pins_sorts_and_rejects_foreign_refs(tmp_path):
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    # The namespace belongs exclusively to the pin primitive: any other
    # ref shape inside it fails closed instead of being skipped or
    # guessed at. These fixtures are planted before any pin exists so
    # the raw refs themselves have no directory/file conflict.
    _git(repo.path, "update-ref", PIN_REF_NAMESPACE, h1)
    with pytest.raises(MaintenancePinError):
        repo.list_maintenance_pins()
    _git(repo.path, "update-ref", "-d", PIN_REF_NAMESPACE)
    _git(repo.path, "update-ref", PIN_REF_NAMESPACE + "/junk", h1)
    with pytest.raises(MaintenancePinError):
        repo.list_maintenance_pins()
    _git(repo.path, "update-ref", "-d", PIN_REF_NAMESPACE + "/junk")
    identities = (("bk-b", "bar-2"), ("bk-a", "bar-9"), ("bk-a", "bar-1"))
    for backup_id, barrier_id in identities:
        assert repo.create_maintenance_pin(backup_id, barrier_id, h1).created
    pins = repo.list_maintenance_pins()
    assert [(pin.backup_id, pin.barrier_id) for pin in pins] == [
        ("bk-a", "bar-1"), ("bk-a", "bar-9"), ("bk-b", "bar-2")]
    assert all(pin.commit_oid == h1 for pin in pins)
    assert [pin.ref for pin in pins] == [
        maintenance_pin_ref(backup_id, barrier_id)
        for backup_id, barrier_id in
        (("bk-a", "bar-1"), ("bk-a", "bar-9"), ("bk-b", "bar-2"))]
    # A deeper nesting inside the namespace is equally anomalous.
    _git(repo.path, "update-ref", PIN_REF_NAMESPACE + "/bk/deep/extra", h1)
    with pytest.raises(MaintenancePinError):
        repo.list_maintenance_pins()
    _git(repo.path, "update-ref", "-d",
         PIN_REF_NAMESPACE + "/bk/deep/extra")
    assert len(repo.list_maintenance_pins()) == 3
    assert _head(repo.path) == h1


@pytest.mark.parametrize("target", ["blob", "tree", "tag"])
def test_pin_queries_reject_ref_repointed_at_non_commit(tmp_path, target):
    # Isolated rework reproduction: a validly created pin ref alone is
    # repointed at an existing non-commit object inside tmp_path. A
    # pin denotes one exact frozen commit H, so both query legs must
    # reject the corrupted ref instead of reporting a MaintenancePin,
    # and the corruption must stay exactly as planted: no repair, no
    # move, no delete.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    created = repo.create_maintenance_pin("bk-d", "bar-d", h1)
    ref = created.pin.ref
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-corrupt")
    healthy = repo.create_maintenance_pin("bk-ok", "bar-ok", h2)
    assert healthy.created
    assert len(repo.list_maintenance_pins()) == 2
    if target == "blob":
        corrupt = _write_blob(repo.path, b"not a commit\n")
    elif target == "tree":
        corrupt = _tree_oid(repo.path, h2)
    else:
        corrupt = _git_text(
            repo.path, "mktag",
            input_bytes=(
                "object %s\ntype commit\ntag fixture\n"
                "tagger Fixture <fixture@example.invalid> "
                "1700000000 +0000\n\nfixture tag\n" % h1
            ).encode("ascii")).strip()
    _point_ref_at(repo.path, corrupt, ref=ref)
    with pytest.raises(PinTargetError, match="not a commit"):
        repo.read_maintenance_pin("bk-d", "bar-d")
    # One corrupted ref fails the whole listing even with a healthy
    # pin present: the healthy pin is neither returned alone nor
    # silently dropped along with the corrupted one.
    with pytest.raises(PinTargetError, match="not a commit"):
        repo.list_maintenance_pins()
    # The healthy identity read on its own is unaffected.
    assert repo.read_maintenance_pin("bk-ok", "bar-ok") == healthy.pin
    # Nothing was repaired, moved or deleted.
    assert (repo.path / ref).read_text(encoding="ascii").strip() == corrupt
    assert _raw_refs_under(repo.path, PIN_REF_NAMESPACE) == sorted(
        [ref, healthy.pin.ref])
    assert _head(repo.path) == h2


def test_pin_queries_reject_unresolvable_target_not_confused_with_absent(
        tmp_path):
    # The same corruption one level deeper: the pin ref records a
    # syntactically full object id whose object does not exist in this
    # repository. Because the ref exists, this must never collapse
    # into the "no such pin" answer (None / silent omission): a
    # missing object is corruption, and both query legs fail closed
    # without repairing anything and without fetching from anywhere.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    created = repo.create_maintenance_pin("bk-u", "bar-u", h1)
    ref = created.pin.ref
    assert repo.list_maintenance_pins() == (created.pin,)
    missing = "f" * repo.oid_length
    _point_ref_at(repo.path, missing, ref=ref)
    with pytest.raises(PinTargetError, match="not a readable object"):
        repo.read_maintenance_pin("bk-u", "bar-u")
    with pytest.raises(PinTargetError, match="not a readable object"):
        repo.list_maintenance_pins()
    # True absence stays a distinct, non-raising answer.
    assert repo.read_maintenance_pin("bk-none", "bar-none") is None
    # The corrupted ref was left exactly as planted.
    assert (repo.path / ref).read_text(encoding="ascii").strip() == missing
    assert _head(repo.path) == h1


def test_pin_creation_race_with_head_move_fails_closed(tmp_path,
                                                       monkeypatch):
    # The authoritative head moves between the adapter's pre-check read
    # and the atomic ref write. The transaction's verify leg must abort
    # every leg together: no pin ref is created and nothing else moves.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-race-head")
    real_ref_value = repo._ref_value
    calls = []

    def stale_first_read():
        calls.append("read")
        if len(calls) == 1:
            # What a racing pre-check would believe it saw.
            return h1
        return real_ref_value()

    monkeypatch.setattr(repo, "_ref_value", stale_first_read)
    with pytest.raises(PinHeadMismatchError):
        repo.create_maintenance_pin("bk-race", "bar-race", h1)
    assert len(calls) >= 2
    # The aborted transaction created nothing anywhere.
    assert _raw_refs_under(repo.path, "refs/sam/") == []
    assert _head(repo.path) == h2


def test_pin_creation_race_with_identical_concurrent_pin_is_idempotent(
        tmp_path, monkeypatch):
    # A concurrent creator wins the ref between this call's pre-read
    # and the atomic create. The create leg aborts on the existing ref,
    # the classification finds exactly the same H, and the call reports
    # idempotent success without moving anything.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    first = repo.create_maintenance_pin("bk-conc", "bar-conc", h1)
    assert first.created
    real_read = repo._read_ref_oid
    calls = []

    def blind_first_read(ref):
        calls.append(ref)
        if len(calls) == 1:
            # The pre-read that misses the competitor's ref.
            return None
        return real_read(ref)

    monkeypatch.setattr(repo, "_read_ref_oid", blind_first_read)
    second = repo.create_maintenance_pin("bk-conc", "bar-conc", h1)
    assert second.created is False
    assert second.pin == first.pin
    assert _raw_pin_value(repo.path, first.pin.ref) == h1
    assert _head(repo.path) == h1


def test_pin_creation_race_with_different_existing_pin_conflicts(
        tmp_path, monkeypatch):
    # Same race window, but the existing pin holds a different H: the
    # create leg aborts, the classification sees the foreign value and
    # fails closed; the existing pin is never moved.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    repo.create_maintenance_pin("bk-x", "bar-x", h1)
    h2 = _append_commit(repo, tmp_path, "fact-1", "index-pin-race-conflict")
    ref = maintenance_pin_ref("bk-x", "bar-x")
    real_read = repo._read_ref_oid
    calls = []

    def blind_first_read(ref_name):
        calls.append(ref_name)
        if len(calls) == 1:
            return None
        return real_read(ref_name)

    monkeypatch.setattr(repo, "_read_ref_oid", blind_first_read)
    with pytest.raises(PinConflictError):
        repo.create_maintenance_pin("bk-x", "bar-x", h2)
    assert _raw_pin_value(repo.path, ref) == h1
    assert _head(repo.path) == h2


def test_pinned_commit_survives_gc_while_dangling_object_is_pruned(
        tmp_path):
    # CHG-066 section 4: the persistent pin is a Git reachability root.
    # After the business ref is forced onto an unrelated history, H is
    # reachable only through the maintenance pin; an aggressive
    # gc --prune=now must keep it, while an unpinned dangling control
    # commit is pruned. Everything stays inside this synthetic
    # tmp_path bare repository.
    repo = _init_repo(tmp_path)
    h1 = repo.head_oid()
    created = repo.create_maintenance_pin("bk-gc", "bar-gc", h1)
    assert created.created
    tree = _tree_oid(repo.path, h1)
    unrelated = _commit_tree(
        repo.path, tree, [], "facts: unrelated post-pin history")
    _git(repo.path, "update-ref", REF_FACTS, unrelated, h1)
    dangling = _commit_tree(repo.path, tree, [], "facts: dangling control")
    _git(repo.path, "reflog", "expire", "--expire=now",
         "--expire-unreachable=now", "--all", check=False)
    _git(repo.path, "gc", "--prune=now", "--quiet")
    # The pinned H survived as the pin's reachability root.
    assert _git_text(repo.path, "cat-file", "-t", h1).strip() == "commit"
    pin = repo.read_maintenance_pin("bk-gc", "bar-gc")
    assert pin == created.pin
    assert _raw_pin_value(repo.path, pin.ref) == h1
    # The control proves the gc really pruned unreachable objects.
    assert _git(repo.path, "cat-file", "-t", dangling,
                check=False).returncode != 0
    assert _head(repo.path) == unrelated


def test_pin_in_sha256_repository_and_cross_format_oid_rejection(tmp_path):
    repo = _init_repo(
        tmp_path, name="facts256-pin.git", object_format="sha256")
    h1 = repo.head_oid()
    assert len(h1) == 64
    result = repo.create_maintenance_pin("bk-256", "bar-256", h1)
    assert result.created
    assert result.pin.commit_oid == h1
    assert repo.read_maintenance_pin("bk-256", "bar-256") == result.pin
    assert repo.list_maintenance_pins() == (result.pin,)
    # A sha1-shaped OID is not a full OID of this repository's format.
    with pytest.raises(ValueError):
        repo.create_maintenance_pin("bk-40", "bar-40", "a" * 40)
    assert repo.read_maintenance_pin("bk-40", "bar-40") is None
    assert _raw_pin_value(repo.path, result.pin.ref) == h1
