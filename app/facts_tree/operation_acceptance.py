"""Read-only locator for the first acceptance of one operation record.

Approved design documentation baseline
247a6a68d9e8f3756318773aec046b55c525dbaf: F-001 and approved design 97
sections 5/6/8 (operation acceptance, history gate and recovery),
under CHG-064/CHG-065, fixed internal contract section 11. The
source-code baseline this slice implements against is
78e1142fb45e5437deb9b6e7744a55a915cad4bc. This module is a storage-evidence primitive only:
it is not connected to the Commit Broker, any employee, model, HTTP or
CLI entry point, SQLite projections, or the facts-tree write flow, and
it grants no read authorization, action permission or retry license.

Public entry point:

    locate_operation_acceptance(repository, *, operation_id, commit_oid)
        -> OperationAcceptanceOrigin

``repository`` must be a real FactsGitRepository instance whose host
identity was bound by initialize()/open(); ``operation_id`` is a
host-issued ID under the git_format.encode_id rules and ``commit_oid``
is the full non-zero OID of a commit in the repository object format
(symbolic names such as HEAD are never accepted). The operation path is
derived only from git_format.record_path("operation", ...); no caller
supplied path ever reaches Git here.

What one call proves:

- git_repository.read_snapshot_at(commit_oid) is called exactly once,
  so the unique authoritative head H captured by that call, the bound
  identity, the single-parent chain, the object-integrity (fsck) gate,
  the C-reachability proof and the validated canonical tree of the
  fixed commit C are all reused unchanged. No second ref read is ever
  spliced into the result, and a commit that merely exists in the
  object store (for example one that lost its CAS race) is never
  treated as an acceptance.
- Starting from that fixed C, the complete physical tree of C and of
  every ancestor down to the genesis is read once and held to the same
  git_tree_validation/git_format canonical rules read_snapshot_at
  enforces (regular non-executable blobs at canonical record_path()
  outputs with canonical dumps_record() bytes). The traversal walks the
  existing _commit_parents primitive with replacement refs ignored and
  promisor lazy fetch disabled (the shared _VERIFY_ENV), so a missing
  or damaged ancestor fails closed instead of being substituted or
  fetched over a network.
- The operation path is then compared from the genesis towards C. The
  first commit carrying it is accepted_commit_oid. Every later commit
  up to and including C must still carry the path with byte-identical
  canonical content: a deletion, a delete-then-re-add or any byte
  rewrite is refused, and C lacking the record while an ancestor
  carried it is an illegal deletion, never a None result. The scan
  never exits early at the first matching blob: every ancestor up to
  the genesis is read and validated, so an anomaly buried deeper in
  the fixed history cannot be skipped.
- The first occurrence's payload must be a dict whose record_type is
  exactly "operation", whose operation_id equals the queried ID and
  whose repo_id/company_id equal the identity bound to the repository
  instance at initialize()/open() time; a missing field, a wrong type
  or a foreign scope is refused. This is the minimal identity/scope
  check of the fixed contract, not a full business record schema:
  event_type, changed_refs, actor and every other business field stay
  the responsibility of the corresponding validation layers.

The returned OperationAcceptanceOrigin is frozen and carries exactly
the queried operation_id and commit_oid, the head_oid H captured at
the start of the read, and accepted_commit_oid (None when the fixed C
and its ancestors never carried the operation). None only states that
this fixed C does not contain the operation; it never proves from an
older C, a backup or this result alone that the operation was never
executed, and it is not by itself a license to retry side effects.
Commits accepted after C are never mapped back into an old C: a head
that legitimately advances while this call runs does not change the
result the captured H and fixed C already prove.

Error contract: TypeError/ValueError signal malformed caller input (a
repository that is not a FactsGitRepository, an illegal operation_id,
a symbolic, truncated, uppercase or all-zero commit_oid). Every
history, identity, reachability, tree or byte-continuity anomaly —
including an unbound instance, an unreachable or non-commit target, a
merge, a foreign identity, a missing or damaged object and any
operation-record violation above — raises OperationAcceptanceError
(a GitRepositoryError) with the underlying diagnosis preserved as the
cause, so object corruption is never misreported as "not found". The
locator creates no Git object, moves no ref, writes no SQLite table,
configuration or index, and never repairs or rolls back a repository;
both SHA-1 and SHA-256 repositories are handled through the actual
object format, never a hardcoded 40-hex assumption.

Boundary (explicitly NOT done here): no user-facing history query, no
authorization or action permission, no business schema validation, no
W reconciliation or consumption proof (a Git position can never replace
those), and no mutable persistent index — this read-only proof stays
the authoritative verification even if an index is ever added later.
The calling host must already hold a trustworthy bound repository
instance; objects and metadata must not be swapped by a non-cooperating
process while the call runs.
"""
from dataclasses import dataclass

from app.facts_tree import git_format
from app.facts_tree import git_repository
from app.facts_tree import git_tree_validation
from app.facts_tree.git_repository import GitRepositoryError


class OperationAcceptanceError(GitRepositoryError):
    """A refused operation-acceptance location.

    History, identity, reachability, tree or byte-continuity anomalies;
    never malformed caller input (that stays TypeError/ValueError) and
    never a clean "the fixed commit never carried this operation"
    answer (that is accepted_commit_oid=None on the result).
    """


@dataclass(frozen=True)
class OperationAcceptanceOrigin:
    """Result of one successful locate_operation_acceptance() call.

    operation_id and commit_oid are the queried operation and the fixed
    commit C the answer is proven from; head_oid is the authoritative
    head H read_snapshot_at() captured once at the start of the call
    (reported only as the reachability proof; no byte comes from H
    unless H is C); accepted_commit_oid is the earliest commit on the
    genesis-to-C chain carrying the operation, or None when that fixed
    history never carried it.
    """

    operation_id: str
    commit_oid: str
    head_oid: str
    accepted_commit_oid: "str | None"


def locate_operation_acceptance(repository, *, operation_id, commit_oid):
    """Locate the first acceptance of one operation inside a fixed commit.

    See the module docstring for the full contract. Returns a frozen
    OperationAcceptanceOrigin; raises TypeError/ValueError for malformed
    input and OperationAcceptanceError for every history, identity,
    reachability, tree or byte-continuity refusal. The repository is
    never modified.
    """
    if not isinstance(repository, git_repository.FactsGitRepository):
        raise TypeError(
            "repository must be a FactsGitRepository bound by "
            "initialize()/open(), got %s" % type(repository).__name__)
    # record_path() is the operation_id legality check itself (the
    # git_format.encode_id rules); it runs before any Git access, and
    # its TypeError/ValueError stay malformed-input errors.
    path = git_format.record_path("operation", operation_id=operation_id)
    # Exactly one snapshot read: it captures the unique head H, proves
    # C is reachable from it, runs the bound-identity, single-parent
    # chain and fsck gate over H's complete history, and validates C's
    # complete physical tree. A malformed commit_oid keeps its
    # ValueError; every storage refusal becomes OperationAcceptanceError.
    try:
        snapshot = repository.read_snapshot_at(commit_oid)
    except (TypeError, ValueError):
        raise
    except GitRepositoryError as exc:
        raise OperationAcceptanceError(
            "cannot locate operation %r acceptance at commit %s: %s"
            % (operation_id, commit_oid, exc)) from exc
    # read_snapshot_at() only returns for a bound instance, so the
    # identity verified at initialize()/open() time is available here.
    bound_meta = repository._expected_meta
    # Read and validate the operation path content of the fixed C (from
    # the already-verified snapshot, never a second snapshot read) and
    # of every ancestor down to the genesis. The chain was just proven
    # single-parent and intact by the snapshot gate; the walk below
    # still refuses a multi-parent or cyclic shape instead of trusting
    # that proof blindly, and it never stops early, so a deeper
    # ancestor anomaly cannot hide behind an already-found match.
    chain = [(snapshot.commit_oid, _snapshot_content(snapshot, path))]
    seen = {snapshot.commit_oid}
    current = snapshot.commit_oid
    while True:
        try:
            parents = repository._commit_parents(
                current, git_repository._VERIFY_ENV)
        except GitRepositoryError as exc:
            raise OperationAcceptanceError(
                "operation acceptance history at %s is unreadable: %s"
                % (current, exc)) from exc
        if not parents:
            break
        if len(parents) != 1:
            raise OperationAcceptanceError(
                "commit %s has %d parents; the authoritative history "
                "must be a single-parent chain" % (current, len(parents)))
        parent = parents[0]
        if parent in seen:
            raise OperationAcceptanceError(
                "commit chain revisits %s; refusing a cyclic history"
                % parent)
        seen.add(parent)
        chain.append(
            (parent, _ancestor_content(repository, parent, path)))
        current = parent
    # Compare genesis -> C: the first occurrence fixes the accepted
    # commit and the canonical bytes; every later commit must carry the
    # record unchanged.
    accepted = None
    canonical = None
    for oid, content in reversed(chain):
        if accepted is None:
            if content is None:
                continue
            _check_operation_payload(
                content, path=path, operation_id=operation_id,
                bound_meta=bound_meta, commit_oid=oid)
            accepted = oid
            canonical = content
            continue
        if content is None:
            raise OperationAcceptanceError(
                "operation %r accepted at %s is missing at the later "
                "commit %s; an accepted operation record is never "
                "deleted" % (operation_id, accepted, oid))
        if content != canonical:
            raise OperationAcceptanceError(
                "operation %r accepted at %s carries different canonical "
                "bytes at the later commit %s; an accepted operation "
                "record is never rewritten" % (operation_id, accepted, oid))
    return OperationAcceptanceOrigin(
        operation_id=operation_id,
        commit_oid=snapshot.commit_oid,
        head_oid=snapshot.head_oid,
        accepted_commit_oid=accepted)


def _snapshot_content(snapshot, path):
    # The verified tree bytes of the fixed C itself, reused from the one
    # read_snapshot_at() call; None when C does not carry the path.
    for record in snapshot.records:
        if record.path == path:
            return record.content
    return None


def _ancestor_content(repository, commit_oid, path):
    # One ancestor's complete physical tree, read and held to exactly
    # the structural and canonical-JSON rules read_snapshot_at()
    # enforces (the tree's own path set is the whitelist, as there is no
    # host whitelist for a historical commit). Replacement refs are
    # ignored and promisor lazy fetch is disabled through the shared
    # _VERIFY_ENV, so a missing or damaged object fails closed here
    # instead of being substituted or fetched over the network.
    try:
        entries = repository._read_commit_tree_entries(
            commit_oid, env_extra=git_repository._VERIFY_ENV,
            context="operation acceptance history tree")
        git_tree_validation.validate_tree_entries(
            entries,
            expected_paths=[
                entry_path for _mode, entry_path, _oid, _content in entries],
            object_format=repository.object_format)
    except GitRepositoryError as exc:
        raise OperationAcceptanceError(
            "operation acceptance history tree at %s is unreadable: %s"
            % (commit_oid, exc)) from exc
    except (TypeError, ValueError) as exc:
        raise OperationAcceptanceError(
            "operation acceptance history tree at %s failed validation: %s"
            % (commit_oid, exc)) from exc
    for _mode, entry_path, _oid, content in entries:
        if entry_path == path:
            return content
    return None


def _check_operation_payload(content, *, path, operation_id, bound_meta,
                             commit_oid):
    # The minimal identity/scope check of the fixed contract: the record
    # must be a dict (guaranteed for tree-validated canonical bytes, but
    # never assumed) whose record_type, operation_id and repo/company
    # scope exactly match the query and the instance-bound identity. It
    # deliberately says nothing about the full business schema.
    try:
        payload = git_format.loads_record(content)
    except (TypeError, ValueError) as exc:
        raise OperationAcceptanceError(
            "operation record at %s in commit %s is not a readable "
            "record: %s" % (path, commit_oid, exc)) from exc
    if not isinstance(payload, dict):
        raise OperationAcceptanceError(
            "operation record at %s in commit %s is not a JSON object"
            % (path, commit_oid))
    expected_fields = (
        ("record_type", "operation"),
        ("operation_id", operation_id),
        ("repo_id", bound_meta["repo_id"]),
        ("company_id", bound_meta["company_id"]),
    )
    for field, expected in expected_fields:
        actual = payload.get(field)
        if actual != expected:
            raise OperationAcceptanceError(
                "operation record at %s in commit %s carries %s %r, "
                "expected %r" % (path, commit_oid, field, actual, expected))
