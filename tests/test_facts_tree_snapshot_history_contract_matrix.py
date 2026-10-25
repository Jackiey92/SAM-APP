"""F-001 §6 history-chain contract matrix for the snapshot entries.

The four entries that may read or extend the authoritative history —
FactsGitRepository.open(), accept(), read_snapshot_at() and
create_maintenance_pin() — must all fail closed when the current
authoritative history is not one bound-identity, single-parent chain
with every ancestor object readable and fsck-clean. This file builds
the anomaly types (merge, buried identity drift, missing ancestor,
fsck-damaged reachable object) as synthetic bare repos inside pytest
tmp_path and checks every entry against each anomaly, plus the legal
side of the contract: healthy linear history stays accepted, the
lost-CAS competition semantics of accept() are unchanged, and an
idempotent same-OID pin re-assertion after the head advanced is still
allowed but re-validates the frozen chain.

Nothing here touches SAM-APP data, SAM-DOCS, a real SAM_GIT_DIR or a
remote; every Git repository is a throwaway bare repo in tmp_path. The
synthetic plumbing helpers are reused from
tests/test_facts_tree_git_repository.py rather than copied.
"""
import pytest

from app.facts_tree import git_format
from app.facts_tree.git_repository import (
    PIN_REF_NAMESPACE,
    REF_FACTS,
    CasResult,
    FactsGitRepository,
    IntegrityError,
    MaintenancePin,
    OpenError,
    PinCreationResult,
    PinTargetError,
    SnapshotReadError,
    maintenance_pin_ref,
)
from tests.test_facts_tree_git_repository import (
    _IDS,
    _META_PATH,
    _accept_paths,
    _all_object_oids,
    _append_commit,
    _commit_object_raw,
    _commit_tree,
    _git,
    _git_text,
    _head,
    _init_repo,
    _loose_object_path,
    _meta_payload,
    _overwrite_loose_object,
    _raw_refs_under,
    _tree_oid,
    _tree_with_added_file,
    _write_blob,
)

# anomaly id -> the message fragment the shared _verify_identity_chain
# walk produces for it, proving the refusal came from the chain gate
# itself rather than a tip-level check.
_CORRUPTIONS = {
    "merge": "single-parent",
    "identity_drift": "host-issued identity",
    "missing_ancestor": "is unreadable",
    "damaged_object": "failed integrity checks",
}
_RAW_AUTHOR_LINE = (
    b"author Fixture <fixture@example.invalid> 1700000000 +0000\n"
    b"committer Fixture <fixture@example.invalid> 1700000000 +0000\n")


class _CorruptHistory:
    """One planted anomaly and the coordinates the entries need."""

    def __init__(self, kind, tip, read_target, valid_tree):
        self.kind = kind
        # The value refs/heads/facts records after the anomaly is
        # planted; every entry refusal must leave it exactly here.
        self.tip = tip
        # A healthy commit reachable from the captured H: the read leg
        # must still fail because the gate walks the whole H chain.
        self.read_target = read_target
        # A well-formed, bound-identity tree the accept candidate can
        # extend, so its refusal is attributable to the history gate.
        self.valid_tree = valid_tree


def _healthy_history(tmp_path):
    # Genesis plus two ordinary accepted commits: the linear,
    # same-identity history every entry must keep accepting.
    repo = _init_repo(tmp_path)
    genesis = repo.genesis_oid
    h1 = _append_commit(repo, tmp_path, "fact-1", "index-h1")
    h2 = _append_commit(repo, tmp_path, "fact-2", "index-h2")
    return repo, genesis, h1, h2


def _raw_commit(repo_path, tree_oid, parents, message):
    # hash-object/commit-tree refuse nothing here structurally, but the
    # fixtures reuse the raw loose-object writer the integrity tests
    # use so every anomaly is expressed as plain Git objects.
    body = b"tree " + tree_oid.encode("ascii") + b"\n"
    for parent in parents:
        body += b"parent " + parent.encode("ascii") + b"\n"
    body += _RAW_AUTHOR_LINE + b"\n" + message + b"\n"
    return _commit_object_raw(repo_path, body)


def _plant_corruption(kind, repo, tmp_path, genesis, h1, h2):
    repo_path = repo.path
    if kind == "merge":
        # A second, divergent valid-identity child of the genesis plus
        # a raw two-parent merge whose tree is the healthy h2 tree:
        # tip-level identity checks pass; only a chain walk can see the
        # second parent.
        other_blob = _write_blob(
            repo_path, git_format.dumps_record({"fact": "other"}))
        other_tree = _tree_with_added_file(
            repo_path, _tree_oid(repo_path, genesis),
            "facts/other/current.json", other_blob, tmp_path,
            "index-merge-other")
        other = _commit_tree(repo_path, other_tree, [genesis], "other line")
        merge = _raw_commit(
            repo_path, _tree_oid(repo_path, h2), [h2, other],
            b"facts: synthetic merge")
        _git(repo_path, "update-ref", REF_FACTS, merge)
        return _CorruptHistory(
            kind, merge, h1, _tree_oid(repo_path, h2))
    if kind == "identity_drift":
        # A foreign-identity ancestor buried under a tip that
        # re-instates the correct identity tree: the tip itself is
        # indistinguishable from a healthy tip without walking back.
        evil_blob = _write_blob(
            repo_path,
            git_format.dumps_record(_meta_payload(company_id="co-evil")))
        env = {"GIT_INDEX_FILE": str(tmp_path / "index-evil-meta")}
        _git(repo_path, "read-tree", _tree_oid(repo_path, h2), env=env)
        _git(repo_path, "update-index", "--add", "--cacheinfo",
             "100644,%s,%s" % (evil_blob, _META_PATH), env=env)
        evil_tree = _git_text(repo_path, "write-tree", env=env).strip()
        evil = _commit_tree(
            repo_path, evil_tree, [h2], "facts: drift identity")
        valid_tree = _tree_oid(repo_path, h2)
        tip = _commit_tree(
            repo_path, valid_tree, [evil], "facts: reinstated identity")
        _git(repo_path, "update-ref", REF_FACTS, tip)
        return _CorruptHistory(kind, tip, h1, valid_tree)
    if kind == "missing_ancestor":
        # The middle commit object is physically gone while h2 and the
        # ref still name it: the tip is readable, its ancestry is not.
        _loose_object_path(repo_path, h1).unlink()
        return _CorruptHistory(
            kind, h2, h2, _tree_oid(repo_path, h2))
    if kind == "damaged_object":
        # A reachable business blob is overwritten with garbage: the
        # commit/identity chain walk alone passes, so only the fsck leg
        # of the shared gate can catch this one.
        blob = _git_text(
            repo_path, "rev-parse",
            "%s:facts/fact-1/current.json" % h2).strip()
        _overwrite_loose_object(repo_path, blob, b"not a zlib stream")
        return _CorruptHistory(
            kind, h2, h2, _tree_oid(repo_path, h2))
    raise AssertionError("unknown corruption kind: %r" % (kind,))


def _valid_candidate_on(repo, tmp_path, corrupt):
    # A structurally valid, bound-identity child commit of the corrupt
    # tip: if it is refused, the history gate (not candidate shape) is
    # why. The whitelist is the candidate tree's exact path set.
    tag = "gate-%s" % corrupt.kind.replace("_", "-")
    path = "facts/%s/current.json" % tag
    blob = _write_blob(
        repo.path, git_format.dumps_record({"fact": tag}))
    tree = _tree_with_added_file(
        repo.path, corrupt.valid_tree, path, blob, tmp_path,
        "index-%s" % tag)
    commit = _commit_tree(
        repo.path, tree, [corrupt.tip], "facts: %s" % tag)
    whitelist = sorted(
        _git_text(repo.path, "ls-tree", "-r", "--name-only",
                  commit).split())
    return commit, whitelist


def _plant_pin_ref(repo_path, ref, oid):
    # A loose pin ref written directly, like a pin a healthy repository
    # created before the history was damaged; no pin-write transaction
    # is available to plant one on an already-corrupt tip.
    path = repo_path / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(oid + "\n", encoding="ascii")


@pytest.mark.parametrize("kind", list(_CORRUPTIONS))
def test_corrupt_history_is_refused_by_all_four_entries(tmp_path, kind):
    repo, genesis, h1, h2 = _healthy_history(tmp_path)
    corrupt = _plant_corruption(kind, repo, tmp_path, genesis, h1, h2)
    assert _head(repo.path) == corrupt.tip
    fragment = _CORRUPTIONS[kind]

    # open(): tip-level configuration and identity checks pass for the
    # planted anomalies, so the refusal is the new history gate.
    with pytest.raises(OpenError, match=fragment) as excinfo:
        FactsGitRepository.open(repo.path, **_IDS)
    assert _head(repo.path) == corrupt.tip
    assert "authoritative history" in str(excinfo.value)

    # accept(): prepare a valid child after the anomaly, then prove the
    # baseline gate refuses before the CAS without moving the ref.
    candidate, whitelist = _valid_candidate_on(repo, tmp_path, corrupt)
    objects_after_candidate = _all_object_oids(repo.path)
    with pytest.raises(IntegrityError, match=fragment):
        repo.accept(corrupt.tip, candidate, expected_paths=whitelist)
    assert _head(repo.path) == corrupt.tip

    # read_snapshot_at(): a reachable healthy C is still refused
    # because the gate walks the captured H's whole chain.
    with pytest.raises(SnapshotReadError, match=fragment):
        repo.read_snapshot_at(corrupt.read_target)
    assert _head(repo.path) == corrupt.tip

    # create_maintenance_pin(): no pin may be created on the corrupt
    # head, and none of the four refusals wrote a Git object.
    assert _raw_refs_under(repo.path, PIN_REF_NAMESPACE) == []
    with pytest.raises(PinTargetError, match=fragment):
        repo.create_maintenance_pin("bk-gate", "bar-gate", corrupt.tip)
    assert _raw_refs_under(repo.path, PIN_REF_NAMESPACE) == []
    assert _head(repo.path) == corrupt.tip
    assert _all_object_oids(repo.path) == objects_after_candidate


@pytest.mark.parametrize("kind", list(_CORRUPTIONS))
def test_pin_same_oid_reassertion_revalidates_pinned_history(
        tmp_path, kind):
    # The idempotent re-assertion writes nothing and stays legal after
    # the head legitimately advances, but it must still re-walk the
    # frozen chain: a pin whose history later turns out to merge, drift
    # identity, lose an ancestor or hold a damaged object is refused
    # instead of reporting created=False success.
    repo, genesis, h1, h2 = _healthy_history(tmp_path)
    corrupt = _plant_corruption(kind, repo, tmp_path, genesis, h1, h2)
    pin_ref = maintenance_pin_ref("bk-reassert", "bar-reassert")
    _plant_pin_ref(repo.path, pin_ref, corrupt.tip)
    with pytest.raises(PinTargetError, match=_CORRUPTIONS[kind]):
        repo.create_maintenance_pin(
            "bk-reassert", "bar-reassert", corrupt.tip)
    # The existing pin keeps its value, no new pin appears, and the
    # business authority ref is untouched.
    assert _raw_refs_under(repo.path, PIN_REF_NAMESPACE) == [pin_ref]
    assert (repo.path / pin_ref).read_text(
        encoding="ascii").strip() == corrupt.tip
    assert _head(repo.path) == corrupt.tip


def test_healthy_history_passes_all_four_entries(tmp_path):
    repo, genesis, h1, h2 = _healthy_history(tmp_path)
    # open() walks the complete history and accepts it.
    reopened = FactsGitRepository.open(repo.path, **_IDS)
    assert reopened.head_oid() == h2

    # accept() gates the current baseline and advances normally.
    h3 = _append_commit(repo, tmp_path, "fact-3", "index-h3")
    assert _head(repo.path) == h3

    # read_snapshot_at() works for every commit on the chain, each read
    # pinned to its own C while reporting the captured current H.
    for target in (genesis, h1, h2):
        snapshot = repo.read_snapshot_at(target)
        assert snapshot.commit_oid == target
        assert snapshot.head_oid == h3
        assert any(record.path == _META_PATH for record in snapshot.records)

    # A new pin freezes the current head after the same gate.
    pin_ref = maintenance_pin_ref("bk-ok", "bar-ok")
    assert repo.create_maintenance_pin("bk-ok", "bar-ok", h3) == \
        PinCreationResult(
            pin=MaintenancePin(
                backup_id="bk-ok", barrier_id="bar-ok",
                commit_oid=h3, ref=pin_ref),
            created=True)
    # After H advances past the pin, the same-OID re-assertion is
    # idempotent (created=False) and its frozen chain re-validates,
    # while the new head gets its own pin.
    h4 = _append_commit(repo, tmp_path, "fact-4", "index-h4")
    again = repo.create_maintenance_pin("bk-ok", "bar-ok", h3)
    assert again.created is False
    assert again.pin == MaintenancePin(
        backup_id="bk-ok", barrier_id="bar-ok",
        commit_oid=h3, ref=pin_ref)
    assert repo.create_maintenance_pin("bk-ok2", "bar-ok2", h4).created
    assert repo.verify_integrity().commit_count == 5
    assert _head(repo.path) == h4


def test_accept_stale_expected_old_against_advanced_head_keeps_lost_cas(
        tmp_path):
    # A competitor wins the ref cleanly before our accept. The new
    # baseline gate must validate that healthy current H and then leave
    # the ordinary lost-CAS result intact: accepted=False with the
    # competitor value, never a corruption error.
    repo = _init_repo(tmp_path)
    genesis = repo.head_oid()
    base_tree = _tree_oid(repo.path, genesis)
    competitor_tree = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-1/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 1})),
        tmp_path, "index-race-competitor")
    competitor = repo.create_commit(competitor_tree, genesis)
    assert repo.accept(
        genesis, competitor,
        expected_paths=_accept_paths(
            "facts/fact-1/current.json")).accepted
    loser_tree = _tree_with_added_file(
        repo.path, base_tree, "facts/fact-2/current.json",
        _write_blob(repo.path, git_format.dumps_record({"fact": 2})),
        tmp_path, "index-race-loser")
    loser = _commit_tree(repo.path, loser_tree, [genesis], "facts: stale")
    result = repo.accept(
        genesis, loser,
        expected_paths=_accept_paths("facts/fact-2/current.json"))
    assert result == CasResult(
        accepted=False, current_oid=competitor,
        expected_old=genesis, new_oid=loser)
    assert _head(repo.path) == competitor


def test_accept_stale_expected_old_against_corrupt_current_head_fails_closed(
        tmp_path):
    # Even with a stale expected_old (a genuine lost race situation),
    # the gate runs against the ref's current value: when that current
    # history is corrupt, accept fails closed with IntegrityError
    # instead of hiding the damage behind a lost-CAS result, and no
    # swap is attempted.
    repo, genesis, h1, h2 = _healthy_history(tmp_path)
    # Loser prepared against the stale h1.
    loser_blob = _write_blob(
        repo.path, git_format.dumps_record({"fact": "stale"}))
    loser_tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, h1),
        "facts/stale/current.json", loser_blob, tmp_path,
        "index-corrupt-race-loser")
    loser = _commit_tree(
        repo.path, loser_tree, [h1], "facts: stale loser")
    whitelist = sorted(
        _git_text(repo.path, "ls-tree", "-r", "--name-only",
                  loser).split())
    # Meanwhile the current tip becomes a two-parent merge.
    other_blob = _write_blob(
        repo.path, git_format.dumps_record({"fact": "other"}))
    other_tree = _tree_with_added_file(
        repo.path, _tree_oid(repo.path, genesis),
        "facts/other/current.json", other_blob, tmp_path,
        "index-corrupt-race-other")
    other = _commit_tree(repo.path, other_tree, [genesis], "other line")
    merge = _raw_commit(
        repo.path, _tree_oid(repo.path, h2), [h2, other],
        b"facts: synthetic merge")
    _git(repo.path, "update-ref", REF_FACTS, merge)
    with pytest.raises(IntegrityError, match="single-parent"):
        repo.accept(h1, loser, expected_paths=whitelist)
    assert _head(repo.path) == merge
    assert _git_text(repo.path, "cat-file", "-t", loser).strip() == \
        "commit"
