"""Low-level bare Git repository adapter for the facts authority repo.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 sections 10.1, 10.2 and 10.4. This module is a storage
primitive only: it is not connected to the Commit Broker, any employee,
model, HTTP or CLI entry point, SQLite projections, or the existing
facts-tree write flow, and it does not claim to enforce the
"only the broker may write the ref" runtime boundary by itself.

Guarantees enforced here:

- initialize() only creates a brand-new target: an existing file,
  non-empty directory or repository is refused and left untouched, and
  any symbolic link at the target is refused before its contents are
  ever inspected. The new repository is bare, has no remote, and HEAD
  is fixed to the single authoritative ref REF_FACTS.
- Durability policy (design section 10.9.1): Git's default
  configuration can lose recently written objects on an abnormal
  shutdown, so the repository must carry the exact local configuration
  core.fsync=objects,reference and core.fsyncMethod=fsync.
  initialize() sets and reads back this configuration before any
  object or the genesis commit is written; open() re-verifies the
  effective local values and refuses a repository whose policy is
  missing, duplicated or drifted; accept() re-verifies them once more
  immediately before the authoritative ref CAS, so a policy violation
  keeps the ref at its previous value. A configuration value that
  cannot be written or verified fails closed and is never silently
  degraded. This slice only sets and verifies configuration values:
  it does not prove
  that the running Git or filesystem actually performs and persists
  fsync, and it is neither a power-loss verification nor a complete
  backup verification; those underlying capabilities and failure
  behaviors still require independent validation.
- The genesis snapshot is a zero-parent commit carrying only the fixed
  repository identity record built through
  git_format.record_path("repository_meta") and git_format.dumps_record;
  callers can never supply a file path. The record binds repo_id,
  installation_id and company_id plus serializer and schema versions.
- open() re-verifies the bare state, the HEAD symref, the absence of
  remotes, the identity record at the current ref tip and the complete
  authoritative history behind that tip with the same identity,
  single-parent-chain and fsck rules verify_integrity() uses (F-001 and
  approved design 97 section 6); any mismatch is a refusal and the
  repository is never modified.
- Later commits are built with exactly one parent equal to the current
  ref tip; acceptance happens only through an expected-old compare and
  swap on REF_FACTS. A commit object that exists but whose CAS lost the
  race is never reported as accepted, and no merge or rebase of an old
  tree is offered. Before candidate validation, accept() gates on the
  history the swap would actually extend: the ref's current value H is
  walked with verify_integrity()'s identity, single-parent-chain and
  fsck rule set (F-001 and approved design 97 section 6), so accepting
  onto a multi-parent, foreign-identity or object-damaged history fails
  closed before the CAS. A stale expected_old against a healthy,
  legitimately advanced H is not corruption: that gate passes and the
  ordinary lost-CAS result is returned exactly as before. Genesis
  acceptance (all-zero expected_old) has no prior history to gate and
  leaves the arbitration to the CAS itself.
- The identity record verified at initialize()/open() time is bound to
  the instance, and accept() refuses any commit whose tree lacks that
  record or carries a different one before the CAS is attempted, so
  this adapter can never advance the authoritative ref into a state
  open() would later reject. There is no metadata migration path yet,
  so the whole record (identity ids plus serializer and schema
  versions) must match exactly.
- accept() also re-validates the new commit's complete physical tree
  against the host-supplied exact expected_paths whitelist before the
  CAS (design sections 10.2.3, 10.4 step 6 and 14.2): every entry must
  be a regular non-executable blob at the canonical
  git_format.record_path() output of one fixed record template with
  canonical git_format.dumps_record() bytes, the blob set must cover
  exactly expected_paths, and symlinks, executables, Git submodules,
  unknown object types and directories holding no whitelisted record
  (for example an empty directory outside the snapshot) are refused
  with the authoritative ref left at its previous value. The accepted
  parent tree's append-only records (design section 10.2.2) must also
  survive unchanged in the new tree: a deleted or in-place rewritten
  history revision, evidence, operation, consumed-candidate or event
  record is refused through
  git_append_only.validate_append_only_retention, while appending new
  versions and advancing the *_current pointers stay legal. A commit
  assembled outside build_tree() is held to exactly the same physical
  contract.
- accept() additionally enforces the candidate closure of approved
  design 97 §4 item 1 (结构与作用域: 路径身份) and item 5 (操作闭合)
  after that physical validation and before the CAS: every
  input_candidate, operation and consumed_candidate record of the
  candidate commit must sit at the canonical git_format.record_path()
  path its own payload identity rebuilds, and typed candidate_refs,
  payload digests and terminal consumptions must close exactly inside
  that one commit, under the repo_id/company_id bound to this instance
  at initialize()/open() time (the records' self-reported scope is
  never the expected scope), through
  candidate_reference_closure.validate_candidate_path_payload_identity.
  A tree carrying none of the three families passes with three empty
  collections; any closure failure raises GitRepositoryError before the
  ref can move.
- accept() additionally enforces, on that same pre-CAS leg, the
  operation/event cross-record closure of approved design 97 §3, §4
  item 5 (操作闭合) and §5 (事件契约): every operation and event record
  of the whole accepted commit C is parsed with git_format.loads_record
  from the exact bytes the physical tree-validation leg already read and
  validated, and handed to
  operation_event_closure.validate_operation_event_closure under the
  repo_id/company_id bound to this instance and the repository object
  format, so typed event_ids listings, event ownership, scope, ordinals
  and the operation/event base_commit agreement close exactly inside C;
  two empty collections pass. That payload-level validator never sees
  Git paths, so on the same leg every event record is additionally bound
  to its path per design 97 §4 item 1 (路径身份): the path must equal
  git_format.record_path("event", event_id=...) rebuilt from that
  payload's own event_id, and a mismatch refuses the commit. Only
  operation records newly added by this commit (operation paths absent
  from the accepted parent tree already listed for append-only
  retention) are additionally bound to this commit's own parent H:
  their base_commit must equal expected_old, including the all-zero
  genesis form. Old operation records are append-only, so their original
  base_commit stays valid in every later commit and is never compared
  against the then-current parent.
- accept() additionally enforces, on that same pre-CAS leg, the
  bindings/current plan-A closure of approved design 97 §§10.2-10.6:
  the fixed bindings/current.json pointer, its referenced taxonomy
  manifest and every binding_initialized/binding_changed event are read
  from the exact validated bytes of this one commit C, never from HEAD,
  SQLite or another commit. Paths come from the fixed templates and
  payload-owned IDs only (record_path("bindings_current"),
  bindings_current.derive_manifest_path(current taxonomy_path_id) and
  bindings_current.derive_event_path(event_id)), and each loaded record
  is bound to that canonical path before
  bindings_current.validate_current_binding runs under the
  repo_id/company_id bound to this instance. The approved no-binding
  state passes: a tree with neither the current pointer nor binding
  lifecycle events (genesis and unbound repositories included) needs no
  further records; binding lifecycle events without the current pointer,
  a missing or wrong manifest at the derived path, or a broken or stale
  event history all raise GitRepositoryError before the ref can move.
- build_tree() materializes a caller-assembled complete final snapshot
  of (repository-relative path, canonical JSON bytes) records into Git
  blob and tree objects and returns the root tree OID. Before any
  object is written it requires the identity record bound at
  initialize()/open() time to be present and byte-exact, requires every
  path to be the exact git_format.record_path() output of one fixed
  record template with canonically encoded IDs, and validates the exact
  caller path whitelist, regular file modes, canonical JSON, OID shapes
  and a conflict-free tree structure. It never reads the
  current head, merges a delta, creates a commit or moves the ref;
  business completeness of the snapshot is the caller's
  responsibility.
- Inherited GIT_* environment variables (GIT_DIR, GIT_WORK_TREE,
  GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, GIT_CONFIG* and friends) can
  redirect repository discovery, the object store or configuration
  away from the fixed target path, so every git child process runs
  with all of them stripped; only the explicit settings below are
  re-added, and non-Git variables such as PATH pass through.
- OID handling (full length, all-zero value) is derived from the
  repository object format instead of assuming 40 hex characters.
- verify_integrity() is the strictly read-only check behind the
  section 10.8 recovery table and the Git side of the section 10.9.2
  backup verification: it confirms the authoritative ref value H (and
  rejects a caller-supplied expected head that does not match it), the
  single-parent chain from H back to a zero-parent genesis, the
  consistency of the repository identity record along that chain, and
  the integrity of every reachable Git object. The fsck pass uses the
  captured H as its only reachability root with reflogs disabled, so
  other refs or reflog entries can never pull objects outside the
  authoritative history into the check; replacement refs are ignored
  and promisor lazy fetch is disabled, so a missing object fails
  closed instead of being substituted or fetched over the network.
  Corruption, missing objects or structural anomalies fail closed with
  IntegrityError; the check never repairs, rebuilds, rolls back,
  consults the reflog or touches the network, and it does not claim to
  prove a full H/W/R backup set or the freshness of an external backup
  watermark.
- read_snapshot_at() is the strictly read-only storage leg behind the
  section 10.9.3 history view: given a full commit OID it captures the
  authoritative head H once, proves the target commit C is reachable
  from that captured H (an object that merely exists in the local
  object store, for example a commit that lost its CAS race, is never
  treated as part of the authoritative history), re-verifies the
  repository identity record at C against the identity bound to this
  instance, and additionally walks the captured H's complete history
  with verify_integrity()'s identity, single-parent-chain and fsck rule
  set before any tree byte is returned (the reachability proof puts C
  on that chain, so a merge, an identity drift or a missing/damaged
  ancestor anywhere behind H fails the read closed), and validates the
  complete physical tree of C with the same structural and
  canonical-JSON rules accept() enforces. Every
  returned byte comes from the single pinned C: a later move of H is
  never spliced into the result, and the captured H is reported only
  as the reachability proof. The read creates no Git object, moves no
  ref and touches no SQLite; replacement refs are ignored and promisor
  lazy fetch is disabled so a missing object fails closed instead of
  being substituted or fetched over the network. Authorization,
  business record schemas, reference closure, projections and the
  tree.history logical action itself remain out of scope for this
  primitive.
- create_maintenance_pin(), read_maintenance_pin() and
  list_maintenance_pins() are the persistent backup maintenance pin
  primitive of the approved full-backup design (CHG-066, section 3
  step 4 and section 4). A pin is a plain Git ref under the dedicated,
  validated refs/sam maintenance namespace
  (refs/sam/maintenance/backup-pins/<backup_id>/<barrier_id>) that
  freezes the exact commit H the host captured, associated through the
  same backup_id/barrier_id the future job, W checkpoint and R
  retention records will use. A pin is never a second business
  authority ref: it enters no business tree, and creating, reading or
  listing pins never moves REF_FACTS. Only a full, non-zero OID of the
  repository object format that names an existing commit object can be
  pinned, and both a new pin and an idempotent same-OID re-assertion
  additionally pass the pinned commit's complete history through
  verify_integrity()'s identity, single-parent-chain and fsck rule set
  (F-001/design 97 section 6): a pin behind an advanced but healthy
  head keeps re-asserting, while a merge, foreign identity or
  missing/damaged ancestor is refused with PinTargetError and adds no
  pin; a new pin additionally requires H to equal the authoritative
  head at creation time; the ref is written inside one
  atomic git update-ref --stdin transaction that simultaneously
  re-verifies REF_FACTS == H and creates the pin ref only if it is
  absent, so a head that moves or a concurrent creation that wins
  between the checks and the write aborts every leg and leaves all
  refs at their previous values. Re-creating the same identity with
  the same H is idempotent; the same identity bound to a different H
  raises PinConflictError and an existing pin is never moved,
  overwritten or deleted by this adapter. There is deliberately no
  move, delete, TTL or auto-release counterpart in this slice: once
  created the pin survives process exit, restart and timeout, and as a
  Git reachability root it protects H and its history from gc/prune.
  The query legs verify what they report: a pin denotes one exact
  frozen commit H, so read_maintenance_pin() returns None only for a
  truly absent ref, while an existing ref whose recorded value is not
  a full object id of this repository's format, whose target object is
  missing or whose target is a non-commit (blob, tree or tag) fails
  closed (MaintenancePinError / PinTargetError) instead of being
  reported as a valid pin or confused with absence, and
  list_maintenance_pins() fails closed as a whole when any ref inside
  the namespace is so corrupted, never listing, skipping or repairing
  a corrupted ref among the healthy ones. Every pin target object
  check runs against this repository's real objects with replacement
  refs ignored and promisor lazy fetch disabled, so verification never
  substitutes an object and never fetches over the network; the
  section 10.9.1 fsync policy is re-verified immediately before the
  pin transaction. Barrier participation, backup job state, W
  checkpoints, R retention, restore, authorization and pin
  release all remain out of scope for this primitive.
- export_maintenance_pin_bundle() is the section 4 Git-only export leg:
  from exactly one persistent maintenance pin it builds a
  self-contained bundle advertising exactly that one pin ref together
  with the complete history reachable from its frozen commit H. The
  instance must be bound to a repository identity, and H's history is
  validated in the source with the same identity-record, single-parent
  chain and fsck rules as verify_integrity() before a byte is staged, so
  a forked/merge or foreign-identity pinned history fails closed. The
  pin is read by its exact backup_id/barrier_id name only; there is
  deliberately no fallback to REF_FACTS, HEAD, reflogs or any other ref,
  and bundle creation never uses --all (nor --branches/--tags/--glob),
  so a head that advanced past the pin after the barrier is never
  spliced in and no other ref is advertised. The source repository is
  never modified. The generated file is first written to a private
  staging path beside the requested output and is published only after
  it passes, inside a brand-new empty bare repository created for that
  one call with --object-format matching this repository explicitly:
  git bundle verify (which, in an empty repository, proves the bundle
  self-contained with no prerequisite commits and no pre-existing
  objects), bundle list-heads showing exactly the one pin ref at H, a
  fetch of that one ref from the local bundle file, confirmation of the
  fetched commit and its single zero-parent root, an fsck of the fetched
  closure and a non-empty file size. Every verification git process runs
  with the same replacement-ref and promisor lazy-fetch guards as the
  other read legs. Publication is a no-clobber hard link, so an output
  that already exists (including a symbolic link) is refused and never
  replaced; a generation or verification failure removes only this
  call's own staging file and verification directory. A staging cleanup
  failure after successful publication is reported explicitly
  (BundleStagingCleanupError, published=True) instead of being
  swallowed, so the already-published output is never mistaken for a
  safe-to-retry failure. The result records the published file's size,
  SHA-256 digest and object format for a later manifest; the bundle
  carries Git history alone: no W/R components, repository
  configuration, manifest, coordinator, restore flow or remote is
  included, and this leg does not claim a complete H/W/R backup. The
  output parent directory is a trusted, host-provided precondition
  (existing and not concurrently replaced during the call); this leg
  does not defend against racing parent-path replacement and local
  rename/hard-link success is not deployment-verified persistence.

All Git invocations use argv lists without a shell, the fixed ref and a
timeout; the facts repository itself never configures or contacts a
remote, never pushes and never issues a network command, and no Git
command text comes from a model. The export verifier's ``git fetch``
takes the local bundle file as its only source inside a throwaway empty
repository; it is never given a remote URL.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

from app.facts_tree import bindings_current
from app.facts_tree import candidate_reference_closure
from app.facts_tree import git_append_only
from app.facts_tree import git_format
from app.facts_tree import git_tree_validation
from app.facts_tree import operation_event_closure

REF_FACTS = "refs/heads/facts"
# CHG-066 section 3 step 4 / section 4: the dedicated maintenance
# namespace for persistent backup pins. It is disjoint from the
# business authority ref (refs/heads/facts) and from every standard Git
# namespace, so pins can never be confused with business history, and
# no code path in this module writes a ref outside REF_FACTS and this
# namespace.
PIN_REF_NAMESPACE = "refs/sam/maintenance/backup-pins"
REPOSITORY_META_SCHEMA_VERSION = 1
# Design 97 §4 item 1 (结构与作用域: 路径身份) and item 5 (操作闭合):
# the three candidate-related record families accept() must close
# inside the one candidate commit C. Classification goes through
# git_format.canonical_record_type, so no path template is duplicated.
_CANDIDATE_CLOSURE_FAMILIES = (
    "input_candidate",
    "operation",
    "consumed_candidate",
)
# Design 97 §3, §4 item 5 and §5: the two record families accept()
# closes cross-record inside the one accepted commit C. Classification
# goes through git_format.canonical_record_type, so no path template is
# duplicated.
_OPERATION_EVENT_CLOSURE_FAMILIES = frozenset(("operation", "event"))
# Design 97 §10.2 (plan A of §10.5): the single fixed current-pointer
# record of the bindings family. Unlike every other identity-bearing
# record it has no payload ID: its path identity is this fixed template
# output itself.
_BINDINGS_CURRENT_PATH = git_format.record_path("bindings_current")
GIT_TIMEOUT_SECONDS = 30
_OBJECT_FORMAT_OID_LENGTH = {"sha1": 40, "sha256": 64}
_HEX_DIGITS = frozenset("0123456789abcdef")
# Host-issued pin identity segments (backup_id, barrier_id) are
# restricted to a conservative charset: no dot (so "..", a leading or
# trailing dot and ".lock" cannot occur), no slash (so one ref path
# maps back to exactly one identity pair), no ASCII control characters
# and none of Git's ref-special characters (~^:?*[\ or "@{"). This
# keeps every git-check-ref-format rule satisfied by construction and
# makes the ref name a pure function of the identity.
_PIN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Read-only verification must see the real objects, never a substitute:
# GIT_NO_REPLACE_OBJECTS stops refs/replace/* from swapping object
# contents, and GIT_NO_LAZY_FETCH stops a promisor remote from fetching
# a missing object over the network (a missing object is corruption,
# not a cache miss). Both knobs are honored by Git 2.53.
_VERIFY_ENV = {
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",
}
# Git requires author/committer metadata; per section 10.2.1 these are
# bookkeeping fields, never a user identity or approval proof.
_IDENTITY_NAME = "SAM Facts Repository"
_IDENTITY_EMAIL = "sam-facts-repository@localhost.invalid"
# Design section 10.9.1: the exact local durability configuration the
# facts authority repo must carry. initialize() installs it before any
# object is written; open() and accept() re-verify the effective local
# values read back through Git itself (never assumed from an earlier
# write) and fail closed on a missing, duplicated or drifted value.
# This is a configuration-value check only: whether the platform
# actually honors fsync is out of scope here and needs independent
# validation.
_FSYNC_POLICY = (
    ("core.fsync", "objects,reference"),
    ("core.fsyncMethod", "fsync"),
)


class GitRepositoryError(Exception):
    """Base error for the facts Git repository adapter."""


class GitCommandError(GitRepositoryError):
    """An unexpected Git plumbing failure (not a lost CAS race)."""

    def __init__(self, message, result=None):
        detail = ""
        if result is not None and result.stderr:
            detail = ": " + result.stderr.decode("utf-8", "replace").strip()
        super().__init__(message + detail)
        self.returncode = result.returncode if result is not None else None


class InitializeError(GitRepositoryError):
    """The target cannot be created or already exists."""


class OpenError(GitRepositoryError):
    """An existing repository failed verification; it is left untouched."""


class IdentityMismatchError(OpenError):
    """The repository identity record does not match the caller."""


class StaleBaseError(GitRepositoryError):
    """The requested parent is not the current authoritative ref tip."""


class BuildTreeError(GitRepositoryError):
    """A complete snapshot failed validation before any object write."""


class IntegrityError(GitRepositoryError):
    """Read-only integrity verification failed; nothing was modified."""


class SnapshotReadError(GitRepositoryError):
    """A pinned historical snapshot read failed; nothing was modified."""


class MaintenancePinError(GitRepositoryError):
    """A maintenance pin operation failed closed; no ref was moved."""


class PinTargetError(MaintenancePinError):
    """The pin target is not an existing commit object of this repo."""


class PinHeadMismatchError(MaintenancePinError):
    """The pin target is not the authoritative head at pin time."""


class PinConflictError(MaintenancePinError):
    """The identity is already pinned to a different commit.

    An existing pin is never moved: rebinding one backup_id/barrier_id
    pair to another H fails closed instead.
    """


class PinNotFoundError(MaintenancePinError):
    """The named maintenance pin does not exist at export time.

    Unlike the read query (which reports a missing pin as None so its
    caller can distinguish absence from corruption), an export has no
    legal object to produce for a missing pin and refuses.
    """


class BundleExportError(GitRepositoryError):
    """A maintenance pin bundle could not be generated or verified.

    No output is published on this failure: the staging bundle and the
    throwaway verification repository are the only things removed, and
    the source repository is never modified.
    """


class BundleOutputExistsError(BundleExportError):
    """The requested bundle output already exists and is never replaced."""


class BundleStagingCleanupError(GitRepositoryError):
    """This call's private staging file could not be removed.

    This deliberately is not a BundleExportError subclass: a caller
    catching the export failure family must not mistake it for
    "nothing was published". published is True exactly when the verified
    bundle had already reached output_path through the no-clobber hard
    link; the published file is then intact (the staging name is only a
    second directory entry for the same inode) and a retry against the
    same output path is guaranteed to meet BundleOutputExistsError, so
    the caller must not re-run the export "to be safe": host cleanup of
    staging_path is the only remaining action. published is False when
    the export itself failed before publication and the staging cleanup
    additionally failed; output_path was never created and the original
    export failure remains chained as this exception's context.
    """

    def __init__(self, message, *, published, output_path, staging_path):
        super().__init__(message)
        self.published = published
        self.output_path = output_path
        self.staging_path = staging_path


@dataclass(frozen=True)
class IntegrityReport:
    """Result of one successful verify_integrity() pass."""

    head_oid: str
    genesis_oid: str
    commit_count: int
    object_format: str
    ref: str = REF_FACTS


@dataclass(frozen=True)
class CasResult:
    """Outcome of one expected-old compare and swap on REF_FACTS."""

    accepted: bool
    current_oid: str
    expected_old: str
    new_oid: str
    ref: str = REF_FACTS


@dataclass(frozen=True)
class SnapshotRecord:
    """One record of a pinned snapshot: its path and raw canonical bytes."""

    path: str
    content: bytes


@dataclass(frozen=True)
class HistoricalSnapshot:
    """Result of one successful read_snapshot_at() call.

    head_oid is the authoritative head captured to prove the
    reachability of commit_oid for this read; every record byte comes
    from commit_oid alone and records are sorted by path.
    """

    commit_oid: str
    head_oid: str
    records: tuple
    ref: str = REF_FACTS


@dataclass(frozen=True)
class MaintenancePin:
    """One persistent maintenance pin in the refs/sam namespace.

    backup_id and barrier_id are the host-issued identity the future
    backup job, W checkpoint and R retention records share (CHG-066
    section 4); commit_oid is the exact frozen H and ref is the full
    maintenance ref name holding it.
    """

    backup_id: str
    barrier_id: str
    commit_oid: str
    ref: str


@dataclass(frozen=True)
class MaintenancePinBundle:
    """One successfully published maintenance pin bundle.

    pin is the exact frozen maintenance ref and commit H the bundle
    advertises and contains; output_path is the absolute no-clobber
    publication target holding a self-contained Git bundle. size_bytes
    and sha256_digest describe that file's bytes (the staging file is
    published as a hard link, so output holds exactly those bytes) and
    are the values a later CHG-066 section 4 manifest can record without
    re-reading the file; object_format is the source repository's
    object format ("sha1"/"sha256"), which the verification repository
    matched explicitly.
    """

    pin: MaintenancePin
    output_path: Path
    size_bytes: int
    sha256_digest: str
    object_format: str


@dataclass(frozen=True)
class PinCreationResult:
    """Outcome of one create_maintenance_pin() call.

    created is True only when this call's atomic transaction created
    the pin ref; created=False reports a pin that already existed with
    exactly the requested commit_oid (an idempotent re-assertion, or a
    concurrent identical creation that won the ref).
    """

    pin: MaintenancePin
    created: bool


def _git_env():
    # Drop every inherited GIT_* variable first: GIT_DIR alone would
    # make git ignore the -C target and act on an unrelated repository,
    # and the other variables can relocate the work tree, index, object
    # store or configuration sources just as silently.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    # User or system Git configuration (init defaults, signing, user
    # identity) must never change this adapter's behavior.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_AUTHOR_NAME"] = _IDENTITY_NAME
    env["GIT_AUTHOR_EMAIL"] = _IDENTITY_EMAIL
    env["GIT_COMMITTER_NAME"] = _IDENTITY_NAME
    env["GIT_COMMITTER_EMAIL"] = _IDENTITY_EMAIL
    return env


def _run_git(args, input_bytes=None, check=True, env_extra=None):
    env = _git_env()
    if env_extra:
        env.update(env_extra)
    try:
        result = subprocess.run(
            ["git"] + list(args),
            input=input_bytes,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommandError("git invocation failed: %s" % exc)
    if check and result.returncode != 0:
        raise GitCommandError("git %s failed" % args[0], result)
    return result


def _require_id(name, value):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty host-issued str" % name)


def maintenance_pin_ref(backup_id, barrier_id):
    """Validated maintenance ref name for one (backup_id, barrier_id).

    Both ids must be 1-64 character strs of [A-Za-z0-9_-] (ValueError
    otherwise, before any repository access); the conservative charset
    keeps every Git ref-name rule satisfied by construction and makes
    the mapping between the ref path and the identity pair injective,
    so list_maintenance_pins() can recover both ids from the ref name
    without ambiguity.
    """
    for name, value in (("backup_id", backup_id),
                        ("barrier_id", barrier_id)):
        if not isinstance(value, str) or not _PIN_ID_RE.fullmatch(value):
            raise ValueError(
                "%s must be a 1-64 character str of [A-Za-z0-9_-]: %r"
                % (name, value))
    return "%s/%s/%s" % (PIN_REF_NAMESPACE, backup_id, barrier_id)


def _repository_meta_payload(repo_id, installation_id, company_id):
    return {
        "schema_version": REPOSITORY_META_SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": repo_id,
        "installation_id": installation_id,
        "company_id": company_id,
    }


class FactsGitRepository:
    """Adapter over one bare facts repository at a fixed path."""

    def __init__(self, path):
        self.path = Path(path)
        self.genesis_oid = None
        # Bound only by initialize()/open() after the identity record is
        # verified; accept() refuses to run without it.
        self._expected_meta = None
        result = _run_git(
            ["-C", str(self.path), "rev-parse", "--show-object-format"])
        object_format = result.stdout.decode("ascii").strip()
        if object_format not in _OBJECT_FORMAT_OID_LENGTH:
            raise GitRepositoryError(
                "unsupported repository object format: %r" % object_format)
        self.object_format = object_format

    @property
    def oid_length(self):
        return _OBJECT_FORMAT_OID_LENGTH[self.object_format]

    @property
    def zero_oid(self):
        return "0" * self.oid_length

    @classmethod
    def initialize(cls, path, *, repo_id, installation_id, company_id,
                   object_format=None, message="facts: repository genesis"):
        """Create a new bare repository and accept its genesis snapshot.

        Only a brand-new target is allowed: an existing file, non-empty
        directory or repository is refused unchanged, and any symbolic
        link at the target is refused before the link's target is ever
        inspected; a pre-existing empty directory may be filled. The
        genesis commit has zero parents and is accepted through the
        expected-old CAS on REF_FACTS with the all-zero OID of the
        repository object format.

        Before any object or the genesis commit is written, the exact
        section 10.9.1 local fsync configuration (_FSYNC_POLICY) is
        installed and read back through Git; if it cannot be
        established, initialization fails instead of silently degrading
        to Git's default durability behavior. Establishing the
        configured values does not prove the platform honors fsync.
        """
        _require_id("repo_id", repo_id)
        _require_id("installation_id", installation_id)
        _require_id("company_id", company_id)
        if object_format is not None and \
                object_format not in _OBJECT_FORMAT_OID_LENGTH:
            raise ValueError(
                "unsupported object format: %r" % (object_format,))
        target = Path(path)
        # A symlink is refused before its target is inspected at all: a
        # link pointing at an empty directory must not be "filled".
        if target.is_symlink():
            raise InitializeError(
                "target must not be a symbolic link: %s" % target)
        if target.exists():
            if not target.is_dir():
                raise InitializeError(
                    "target exists and is not an empty directory: %s" % target)
            if any(target.iterdir()):
                raise InitializeError(
                    "target directory is not empty: %s" % target)
        init_args = ["init", "--bare", "--initial-branch=facts"]
        if object_format is not None:
            init_args.append("--object-format=" + object_format)
        init_args.append(str(target))
        try:
            _run_git(init_args)
        except GitCommandError as exc:
            raise InitializeError("git init failed: %s" % exc) from exc
        repo = cls(target)
        # The fsync policy must be in effect before the first object is
        # written, so it is installed and read back before symbolic-ref
        # setup and before any blob, tree or commit object exists.
        try:
            repo._install_fsync_policy()
        except GitRepositoryError as exc:
            raise InitializeError(
                "cannot establish the section 10.9.1 fsync "
                "configuration: %s" % exc) from exc
        repo._git(["symbolic-ref", "HEAD", REF_FACTS])
        payload = _repository_meta_payload(
            repo_id, installation_id, company_id)
        repo._expected_meta = payload
        blob = repo._write_blob(git_format.dumps_record(payload))
        meta_tree = repo._write_tree([("100644", "repository.json", blob)])
        root_tree = repo._write_tree([("40000", "meta", meta_tree)])
        genesis = repo._create_commit_object(root_tree, (), message)
        result = repo.accept(
            repo.zero_oid, genesis,
            expected_paths=[git_format.record_path("repository_meta")])
        if not result.accepted:
            raise InitializeError(
                "genesis commit was not accepted on %s" % REF_FACTS)
        repo.genesis_oid = genesis
        return repo

    @classmethod
    def open(cls, path, *, repo_id, installation_id, company_id):
        """Open and verify an existing repository without modifying it.

        The repository must be bare, remote-less, with HEAD fixed to
        REF_FACTS, carry the exact section 10.9.1 local fsync
        configuration (_FSYNC_POLICY), the identity record at the
        current ref tip must match the host-issued identity exactly,
        and the complete history behind that tip must pass
        verify_integrity()'s identity, single-parent-chain and fsck
        rule set (F-001/design 97 section 6); any mismatch refuses the
        open and leaves the repository untouched. A missing, duplicated
        or drifted fsync value fails closed: a missing, duplicated or
        drifted local configuration value is refused, and so is a
        merge commit, an ancestor carrying another identity record, a
        missing ancestor object or any fsck-damaged reachable object.
        This check does not detect whether the running Git supports
        these settings at runtime.
        """
        _require_id("repo_id", repo_id)
        _require_id("installation_id", installation_id)
        _require_id("company_id", company_id)
        target = Path(path)
        if not target.is_dir():
            raise OpenError("repository path is not a directory: %s" % target)
        try:
            repo = cls(target)
            bare = repo._git_text(
                ["rev-parse", "--is-bare-repository"]).strip()
            head_ref = repo._git_text(["symbolic-ref", "HEAD"]).strip()
            remotes = repo._git_text(["remote"]).strip()
        except GitCommandError as exc:
            raise OpenError("not a usable git repository: %s" % exc) from exc
        if bare != "true":
            raise OpenError("repository is not bare: %s" % target)
        if head_ref != REF_FACTS:
            raise OpenError(
                "repository HEAD is %r, not %r" % (head_ref, REF_FACTS))
        if remotes:
            raise OpenError("repository has remotes configured")
        try:
            repo._enforce_fsync_policy()
        except GitRepositoryError as exc:
            raise OpenError(
                "repository fsync configuration failed section 10.9.1 "
                "verification: %s" % exc) from exc
        head = repo._ref_value()
        if head is None:
            raise OpenError("authoritative ref %s is missing" % REF_FACTS)
        try:
            object_type = repo._object_type(head)
        except GitRepositoryError as exc:
            raise OpenError("repository tip is unreadable: %s" % exc) from exc
        if object_type != "commit":
            raise OpenError(
                "authoritative ref %s does not point to a commit"
                % REF_FACTS)
        try:
            meta = repo._read_meta(head)
        except GitRepositoryError as exc:
            raise OpenError("repository metadata is unreadable: %s" % exc) \
                from exc
        expected = _repository_meta_payload(
            repo_id, installation_id, company_id)
        if meta != expected:
            raise IdentityMismatchError(
                "repository identity record does not match the host-issued "
                "repo_id, installation_id and company_id")
        # Bind the host identity before the chain walk so every ancestor
        # is compared against it, not just the tip. F-001/design 97 §6:
        # a valid tip is not enough — the whole authoritative history
        # must be one bound-identity, single-parent chain with every
        # reachable object intact, the same gate verify_integrity()
        # runs. The walk and fsck are strictly read-only, so a refused
        # open leaves the repository exactly as it was found.
        repo._expected_meta = expected
        try:
            repo._verify_identity_chain(head)
        except GitRepositoryError as exc:
            raise OpenError(
                "repository authoritative history failed the bound-"
                "identity, single-parent chain and object integrity "
                "checks: %s" % exc) from exc
        return repo

    def head_oid(self):
        """Full OID the authoritative ref currently points to."""
        value = self._ref_value()
        if value is None:
            raise GitRepositoryError(
                "authoritative ref %s is missing" % REF_FACTS)
        return value

    def create_commit(self, tree_oid, parent_oid,
                      message="facts: commit"):
        """Persist a commit object with exactly one parent.

        The parent must equal the current authoritative ref tip, so a
        commit is always constructed on top of the H it will later be
        CAS-accepted against. Creating the object never moves the ref.
        """
        self._require_oid(tree_oid, "tree_oid")
        self._require_oid(parent_oid, "parent_oid")
        if self._object_type(tree_oid) != "tree":
            raise GitRepositoryError("tree_oid is not a tree object")
        if self._object_type(parent_oid) != "commit":
            raise GitRepositoryError("parent_oid is not a commit object")
        head = self.head_oid()
        if parent_oid != head:
            raise StaleBaseError(
                "parent %s is not the current authoritative tip %s; "
                "rebuild the new tree on the current tip instead of "
                "rebasing an old one" % (parent_oid, head))
        return self._create_commit_object(tree_oid, (parent_oid,), message)

    def accept(self, expected_old, new_oid, *, expected_paths):
        """Compare and swap REF_FACTS from expected_old to new_oid.

        This is the only way the authoritative ref moves.
        ``expected_paths`` is the host-generated exact whitelist of
        record paths the new commit's complete tree must carry (the
        identity record plus every record path of the full snapshot),
        matching the build_tree() contract. Before the swap:

        - after the candidate's own object-type, identity-record and
          direct-parentage checks, the current authoritative history H
          (the ref value captured at this point, not expected_old) is
          validated with verify_integrity()'s identity,
          single-parent-chain and fsck rule set (F-001/design 97
          section 6), before the physical tree validation: the ref must
          not be extended onto a multi-parent, identity-drifted or
          object-damaged baseline, which raises IntegrityError before
          the CAS. When a competitor has already advanced H cleanly
          past a stale expected_old, that history passes this gate and
          the call keeps the ordinary lost-CAS result below instead of
          misreporting the competition as corruption. The gate is
          skipped only for genesis acceptance (all-zero expected_old),
          which has no prior history;
        - the new object must be a commit whose parentage matches the
          claim: zero parents when expected_old is the all-zero OID
          (genesis), otherwise exactly one parent equal to expected_old;
        - the commit's tree must carry the repository identity record
          unchanged, exactly as verified when this instance was
          initialized or opened;
        - the commit's complete physical tree is re-validated against
          expected_paths (design sections 10.2.3, 10.4 step 6 and
          14.2): every entry must be a regular non-executable blob
          (mode 100644) at the canonical git_format.record_path()
          output of one fixed record template, carrying canonical
          git_format.dumps_record() bytes, and the blob set must cover
          exactly expected_paths. Unknown object types, symlinks,
          executables, Git submodules (gitlinks) and directories
          holding no whitelisted record (for example empty directories
          outside the snapshot) are refused, and the accepted parent
          tree's append-only records (design section 10.2.2) must all
          still be present with unchanged blob OIDs in the new tree: a
          deleted or in-place rewritten history revision, evidence,
          operation, consumed-candidate or event record is refused
          through git_append_only.validate_append_only_retention, while
          appending new revisions and advancing the *_current pointers
          stay legal. A commit assembled outside
          build_tree() is held to exactly the same physical contract.
        - after the physical tree validation and the append-only
          retention comparison, the candidate closure of design 97 §4
          items 1 (路径身份) and 5 (操作闭合) is enforced on the same
          tree through
          candidate_reference_closure.validate_candidate_path_payload_identity:
          every input_candidate, operation and consumed_candidate record
          is parsed with git_format.loads_record from the exact bytes the
          physical tree-validation leg already read and validated, its
          path must equal the canonical
          record_path() rebuilt from its payload identity, and typed
          candidate_refs, digests and terminal consumptions must resolve
          inside this one commit under the repo_id/company_id of the
          identity bound to this instance (the records' own scope fields
          are never trusted as the expected scope). A tree carrying none
          of the three families passes with three empty collections.
        - the operation/event cross-record closure of design 97 §3, §4
          item 5 and §5 is enforced next, on the same validated bytes:
          every operation and event record of the whole commit C is
          parsed with git_format.loads_record from content_by_path (the
          physical leg's exact bytes, never re-read from Git) and closed
          through
          operation_event_closure.validate_operation_event_closure under
          the bound repo_id/company_id and this repository's object
          format. event_ids listings must resolve to events of the same
          operation in this commit, no event may be an orphan, ordinals
          must be consecutive and an operation must agree with its
          events on base_commit; two empty collections pass. Because
          that payload-level closure never sees Git paths, each event is
          then bound to its path per design 97 §4 item 1 (路径身份): the
          path must equal record_path("event", event_id=...) rebuilt
          from that payload's own event_id, and a mismatch refuses the
          commit before the CAS. On top of that whole-commit closure,
          only the operation records this commit newly adds (paths
          absent from the accepted parent tree already listed for
          append-only retention) are bound to this commit's own parent
          H: their base_commit must equal expected_old, including the
          all-zero genesis form. Old
          operation records survive unchanged under append-only
          retention and their original base_commit is deliberately not
          compared to the current parent, so an operation first accepted
          at an earlier commit stays valid in every later commit C.
        - the bindings/current plan-A closure of design 97 §§10.2-10.6 is
          enforced next, on the same validated bytes: the tree either
          carries none of the binding family (no bindings/current.json
          and no binding_initialized/binding_changed event, the approved
          no-binding state genesis and unbound repositories keep) or it
          carries the fixed-path current record, the taxonomy manifest
          at the path derived from the current payload's own
          taxonomy_path_id, and every binding lifecycle event of this
          same tree at the path derived from its own event_id. All three
          are parsed from content_by_path (never re-read from Git, never
          filled from HEAD, SQLite or another commit), path-bound and
          handed as one current, one manifest and the full event history
          to bindings_current.validate_current_binding under the
          repo_id/company_id of the identity bound to this instance; a
          binding event without the current pointer, a manifest missing
          from the derived path or parked under another pack_version id,
          a foreign scope or a broken, gapped, non-continuous or stale
          event history all refuse the commit before the CAS.
        - the repository's effective local fsync configuration is
          re-verified against the exact section 10.9.1 policy
          (_FSYNC_POLICY) immediately before the swap: a missing,
          duplicated or drifted value refuses the acceptance so the
          ref is never advanced while the configured policy cannot
          even be confirmed. Confirming the configuration values does
          not prove the underlying Git or filesystem actually honors
          fsync; that durability behavior requires independent
          validation.

        Any validation failure raises GitRepositoryError before the CAS
        is attempted, so the authoritative ref keeps its previous
        value. A lost race returns CasResult(accepted=False) with the
        value the ref actually kept; the ref is never forced.
        """
        self._require_oid(expected_old, "expected_old", allow_zero=True)
        self._require_oid(new_oid, "new_oid")
        if self._expected_meta is None:
            raise GitRepositoryError(
                "repository identity is not bound to this instance; "
                "use initialize() or open()")
        if self._object_type(new_oid) != "commit":
            raise GitRepositoryError("accepted object must be a commit")
        if self._read_meta(new_oid) != self._expected_meta:
            raise GitRepositoryError(
                "accepted commit must carry the repository identity "
                "record unchanged")
        parents = self._commit_parents(new_oid)
        if expected_old == self.zero_oid:
            if parents:
                raise GitRepositoryError(
                    "genesis acceptance requires a zero-parent commit")
        elif parents != [expected_old]:
            raise GitRepositoryError(
                "accepted commit must have exactly one parent equal to "
                "expected_old %s, got %s" % (expected_old, parents))
        # F-001/design 97 §6: gate the history this swap would actually
        # extend, i.e. the ref's current value — never expected_old.
        # When a competitor already advanced H cleanly, this passes and
        # the lost-CAS result at the end stands as before; a
        # multi-parent, identity-drifted or object-damaged current
        # history fails closed here, before the physical tree work and
        # the fsync recheck, and the ref keeps its value. Every check in
        # the walk is read-only. A genesis acceptance (all-zero base)
        # has no prior history; the CAS itself arbitrates that case.
        if expected_old != self.zero_oid:
            current_base = self._ref_value()
            if current_base is not None:
                self._verify_identity_chain(current_base)
        new_entries, content_by_path = self._validate_commit_tree(
            new_oid, expected_paths)
        base_entries = self._validate_append_only_retention(
            expected_old, new_entries)
        self._validate_candidate_closure(new_entries, content_by_path)
        self._validate_operation_event_closure(
            expected_old, new_entries, content_by_path, base_entries)
        self._validate_bindings_current_closure(new_entries, content_by_path)
        # Last guard before the authoritative ref moves: the section
        # 10.9.1 durability configuration is re-read through Git, so a
        # repository whose policy was removed or drifted since open()
        # refuses here and the ref keeps its previous value.
        self._enforce_fsync_policy()
        result = self._git(
            ["update-ref", REF_FACTS, new_oid, expected_old], check=False)
        if result.returncode == 0:
            return CasResult(
                accepted=True, current_oid=new_oid,
                expected_old=expected_old, new_oid=new_oid)
        current = self._ref_value()
        if current is not None and current != expected_old:
            return CasResult(
                accepted=False, current_oid=current,
                expected_old=expected_old, new_oid=new_oid)
        raise GitCommandError(
            "git update-ref failed without a competing change", result)

    def _validate_commit_tree(self, commit_oid, expected_paths):
        # Design sections 10.2.3, 10.4 step 6 and 14.2: the complete
        # physical tree of the commit being accepted is re-validated
        # against the host-generated exact path whitelist before the
        # CAS. Persisted objects are not proof of validity: a commit
        # assembled outside build_tree() could carry non-canonical
        # bytes, unexpected paths or dangerous entry types, and the
        # authoritative ref must never advance onto such a tree.
        # Returns the tree's actual blob entries as (path, oid) tuples
        # so the append-only retention check compares exactly the
        # physical snapshot that passed validation here, together with a
        # {path: content_bytes} map of the exact bytes that passed
        # validation: later legs (candidate closure) consume those bytes
        # instead of re-reading the blobs from Git.
        entries = self._read_commit_tree_entries(
            commit_oid, context="accepted commit tree")
        try:
            git_tree_validation.validate_tree_entries(
                entries, expected_paths=expected_paths,
                object_format=self.object_format)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit tree failed validation: %s" % exc) from exc
        new_entries = [(path, oid) for _mode, path, oid, _content in entries]
        content_by_path = {
            path: content for _mode, path, _oid, content in entries}
        return new_entries, content_by_path

    def _read_commit_tree_entries(self, commit_oid, env_extra=None,
                                  context="accepted commit tree"):
        # Structural leg shared by accept() and read_snapshot_at():
        # list the complete physical tree of a commit and read every
        # blob, so unknown object types, symlinks, executables, Git
        # submodules, directories holding no record, paths outside the
        # fixed record templates and unreadable blobs all fail closed
        # before any caller-specific comparison. Persisted objects are
        # not proof of validity: a commit assembled outside build_tree()
        # could carry non-canonical bytes, unexpected paths or dangerous
        # entry types. ``context`` is the message prefix identifying the
        # caller. Returns (mode, path, oid, content) tuples in tree
        # listing order; modes, OID shapes, whitelist membership and
        # canonical JSON bytes are validated by the caller through
        # git_tree_validation.validate_tree_entries.
        try:
            listed = self._list_tree_entries(commit_oid, env_extra)
        except (GitRepositoryError, UnicodeDecodeError, ValueError) as exc:
            raise GitRepositoryError(
                "%s listing is unreadable: %s" % (context, exc)) from exc
        blobs = []
        directories = []
        for mode, object_type, oid, path in listed:
            if object_type == "tree":
                if mode != "040000":
                    raise GitRepositoryError(
                        "%s directory %r has mode %r, not 040000"
                        % (context, path, mode))
                directories.append(path)
                continue
            if object_type != "blob":
                raise GitRepositoryError(
                    "%s entry %r is a %s object; only regular file blobs "
                    "and directories are accepted"
                    % (context, path, object_type))
            blobs.append((mode, path, oid))
        blob_paths = {path for _mode, path, _oid in blobs}
        for directory in directories:
            prefix = directory + "/"
            if not any(path.startswith(prefix) for path in blob_paths):
                raise GitRepositoryError(
                    "%s directory %r holds no record; empty or "
                    "unexpected directories are refused"
                    % (context, directory))
        entries = []
        for mode, path, oid in blobs:
            try:
                content = self._git_bytes(
                    ["cat-file", "blob", oid], env_extra=env_extra)
            except GitCommandError as exc:
                raise GitRepositoryError(
                    "%s blob at %r is unreadable: %s"
                    % (context, path, exc)) from exc
            entries.append((mode, path, oid, content))
        # The complete tree may only carry paths a host generated from
        # the fixed record templates with canonically encoded IDs; a
        # path that is merely safe, or a decodable but non-canonical
        # escape alias of a legitimate ID, fails here even when it sits
        # inside the caller whitelist.
        for _mode, path, _oid, _content in entries:
            if git_format.canonical_record_type(path) is None:
                raise GitRepositoryError(
                    "%s path %r is not the canonical record_path() "
                    "output of any fixed record template"
                    % (context, path))
        return entries

    def _validate_append_only_retention(self, expected_old, new_entries):
        # Design sections 10.2.2 and 14.2: history revisions, evidence,
        # operations, consumed candidates and event files already on the
        # authoritative history are append-only; old versions are never
        # deleted or rewritten in place, while new revisions and
        # *_current pointer advances stay legal. Compare the accepted
        # parent commit's actual (path, blob OID) entries against the
        # candidate's before the CAS. The parentage check above has
        # already bound expected_old to the new commit's own parent, so
        # the compared base is exactly the history being extended; a
        # genesis acceptance (all-zero expected_old) has an empty base
        # and trivially passes. The repository identity record is not
        # part of the compared append-only set; accept() guarantees its
        # immutability through the identity-record comparison above.
        # Returns the parent's blob entries as (path, oid) tuples: the
        # operation/event closure leg reuses exactly this listing to
        # tell newly added operation paths apart from retained ones, so
        # the parent tree is listed only once for both checks.
        if expected_old == self.zero_oid:
            base_entries = []
        else:
            try:
                listed = self._list_tree_entries(expected_old)
            except (GitRepositoryError, UnicodeDecodeError, ValueError) \
                    as exc:
                raise GitRepositoryError(
                    "accepted parent commit tree listing is unreadable: "
                    "%s" % exc) from exc
            base_entries = [
                (path, oid) for _mode, object_type, oid, path in listed
                if object_type == "blob"]
        try:
            git_append_only.validate_append_only_retention(
                base_entries, new_entries)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit violates append-only retention: %s"
                % exc) from exc
        return base_entries

    def _validate_operation_event_closure(self, expected_old, new_entries,
                                          content_by_path, base_entries):
        # Design 97 §3 (the operation/event rows), §4 item 5 (操作闭合)
        # and §5 (事件契约): after the physical tree validation, the
        # append-only retention comparison and the candidate closure,
        # every operation and event record of the whole accepted commit
        # C is collected from the exact bytes the physical leg validated
        # and handed to the pure operation/event closure validator:
        # event_ids listings must resolve to events of the same
        # operation in this one commit, no event may be an orphan, the
        # scope/ordinal rules must hold, an operation must agree with
        # its events on base_commit and an event never embeds its own
        # accepted commit. That validator compares payload identities
        # only and never sees the Git path an event was read from, so
        # right after it every event is bound to its path per §4 item 1
        # (路径身份): record_path("event", event_id=...) rebuilt from
        # the payload's own event_id must equal the tree path exactly.
        # The closure pass has just proved event_id is present and a
        # legal encode_id id, so record_path() cannot raise here; an
        # event body naming a different identity than its path is
        # refused before the CAS. The parent binding on top of that
        # whole-commit check applies only to operations this commit
        # newly adds: append-only retention has already proved an
        # operation path present in the parent keeps identical bytes,
        # so "absent from the parent path set" is exactly the set of
        # newly introduced operations, and only those must carry
        # base_commit equal to expected_old (this commit's own parent H,
        # including the all-zero genesis form). A retained operation's
        # original base_commit names an earlier commit by construction
        # and must remain valid in a later commit. Everything here runs
        # before the fsync recheck and the REF_FACTS CAS, so a closure
        # failure leaves the ref at its previous value. Two empty
        # collections (genesis and ordinary snapshots) pass.
        operations = []
        events = []
        for path, _oid in sorted(new_entries):
            family = git_format.canonical_record_type(path)
            if family not in _OPERATION_EVENT_CLOSURE_FAMILIES:
                continue
            try:
                payload = git_format.loads_record(content_by_path[path])
            except (TypeError, ValueError) as exc:
                raise GitRepositoryError(
                    "accepted commit %s record at %r is not a valid "
                    "canonical record: %s" % (family, path, exc)) from exc
            if family == "operation":
                operations.append((path, payload))
            else:
                events.append((path, payload))
        try:
            operation_event_closure.validate_operation_event_closure(
                [payload for _path, payload in operations],
                [payload for _path, payload in events],
                repo_id=self._expected_meta["repo_id"],
                company_id=self._expected_meta["company_id"],
                object_format=self.object_format)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit failed operation/event closure: %s"
                % exc) from exc
        # Design 97 §4 item 1 (路径身份), event family: the pure closure
        # only compares payload-level event_id values, so an event body
        # whose event_id disagrees with its canonical tree path must be
        # refused here. The check rides on the same validated bytes and
        # runs, like everything in this method, before the fsync recheck
        # and the REF_FACTS CAS; operation path identity is already
        # covered by the candidate path/payload identity leg above.
        for path, event in events:
            canonical_path = git_format.record_path(
                "event", event_id=event["event_id"])
            if canonical_path != path:
                raise GitRepositoryError(
                    "accepted commit event record at %r carries event_id "
                    "%r, whose canonical record_path() is %r; an event's "
                    "path must be rebuilt from its payload's own event_id"
                    % (path, event["event_id"], canonical_path))
        parent_operation_paths = {
            path for path, _oid in base_entries
            if git_format.canonical_record_type(path) == "operation"}
        for path, operation in operations:
            if path in parent_operation_paths:
                continue
            if operation["base_commit"] != expected_old:
                raise GitRepositoryError(
                    "new operation record at %r is bound to base_commit %r, "
                    "not the accepted parent expected_old %s; a newly "
                    "accepted operation must build on this commit's own "
                    "parent" % (path, operation["base_commit"], expected_old))

    def _validate_bindings_current_closure(self, new_entries,
                                            content_by_path):
        # Design 97 §§10.2-10.6 (plan A of §10.5), the full-tree
        # integration of the existing pure
        # bindings_current.validate_current_binding contract: on the
        # exact bytes the physical leg already validated for the one
        # accepted commit C, close bindings/current.json, its referenced
        # taxonomy manifest and every binding lifecycle event inside C
        # alone. Paths are never read from payloads as strings: the
        # current path is the fixed record_path("bindings_current")
        # output, the manifest path is derived from the current
        # payload's own taxonomy_path_id through
        # bindings_current.derive_manifest_path, and each binding
        # event's path is derived from that payload's own event_id
        # through bindings_current.derive_event_path, so a record parked
        # at another record's canonical path fails closed. Nothing here
        # reads HEAD, SQLite or any other commit: every record comes from
        # content_by_path, i.e. the validated bytes of this one tree, and
        # a missing referenced record is a refusal, never filled from
        # another commit. The expected repo_id/company_id are the
        # repository identity bound to this instance, never the scope a
        # binding record claims for itself. The approved no-binding state
        # (genesis and unbound repositories included) passes: no current
        # pointer and no binding_initialized/binding_changed event in the
        # same tree is the empty case. Binding lifecycle events without
        # the current pointer are refused; a current pointer without the
        # manifest at its derived path, without a continuous event
        # history or with any manifest/history disagreement is refused
        # through the pure validator. Everything runs before the fsync
        # recheck and the REF_FACTS CAS, so any failure leaves the ref at
        # its previous value.
        binding_events = []
        for path, _oid in sorted(new_entries):
            if git_format.canonical_record_type(path) != "event":
                continue
            try:
                payload = git_format.loads_record(content_by_path[path])
            except (TypeError, ValueError) as exc:
                raise GitRepositoryError(
                    "accepted commit event record at %r is not a valid "
                    "canonical record: %s" % (path, exc)) from exc
            event_type = payload.get("event_type")
            if not isinstance(event_type, str) \
                    or event_type not in bindings_current.BINDING_EVENT_TYPES:
                # Other event families (for example fact revision
                # events) do not participate in the binding history and
                # must not trigger the binding-present rule; they stay
                # the generic operation/event closure leg's business.
                continue
            # Path identity per §4 item 1 (路径身份), binding event
            # family: the pure history validator only compares event_id
            # values, so an event body whose event_id disagrees with its
            # canonical path is refused here on top of the same binding
            # the generic event leg already enforces for every event.
            try:
                canonical_path = bindings_current.derive_event_path(
                    payload.get("event_id"))
            except (TypeError, ValueError) as exc:
                raise GitRepositoryError(
                    "accepted commit binding event at %r does not carry "
                    "a path-derivable event_id: %s" % (path, exc)) from exc
            if canonical_path != path:
                raise GitRepositoryError(
                    "accepted commit binding event record at %r carries "
                    "event_id %r, whose canonical record_path() is %r; a "
                    "binding event's path must be rebuilt from its "
                    "payload's own event_id"
                    % (path, payload.get("event_id"), canonical_path))
            binding_events.append((path, payload))
        current_bytes = content_by_path.get(_BINDINGS_CURRENT_PATH)
        if current_bytes is None:
            if binding_events:
                raise GitRepositoryError(
                    "accepted commit carries binding lifecycle event(s) "
                    "at %s without the current bindings record at %s; a "
                    "binding history without its current pointer cannot "
                    "close"
                    % ([path for path, _payload in binding_events],
                       _BINDINGS_CURRENT_PATH))
            # The approved no-binding state: no current pointer and no
            # binding lifecycle events (genesis and unbound repositories
            # included).
            return
        try:
            current = git_format.loads_record(current_bytes)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit bindings current record at %r is not a "
                "valid canonical record: %s"
                % (_BINDINGS_CURRENT_PATH, exc)) from exc
        # Validate the record structure first so taxonomy_path_id is
        # guaranteed present and legal before its owned manifest path is
        # derived; scope and the full closure are re-checked, not
        # assumed, by validate_current_binding below.
        try:
            bindings_current.validate_bindings_current(current)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit failed bindings/current validation: %s"
                % exc) from exc
        try:
            manifest_path = bindings_current.derive_manifest_path(
                current["taxonomy_path_id"])
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit bindings current record at %r does not "
                "carry a path-derivable taxonomy_path_id: %s"
                % (_BINDINGS_CURRENT_PATH, exc)) from exc
        # The referenced manifest must be a record of this same tree at
        # exactly the derived path; HEAD, a different commit or a manifest
        # parked under another pack_version id are never substitutes.
        manifest_bytes = content_by_path.get(manifest_path)
        if manifest_bytes is None:
            raise GitRepositoryError(
                "accepted commit bindings current record at %r references "
                "the taxonomy manifest at %s, which this commit does not "
                "carry; the manifest must close inside this same tree at "
                "the derived path"
                % (_BINDINGS_CURRENT_PATH, manifest_path))
        try:
            manifest = git_format.loads_record(manifest_bytes)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit taxonomy manifest at %r is not a valid "
                "canonical record: %s" % (manifest_path, exc)) from exc
        try:
            bindings_current.validate_current_binding(
                current,
                manifest=manifest,
                events=[payload for _path, payload in binding_events],
                repo_id=self._expected_meta["repo_id"],
                company_id=self._expected_meta["company_id"])
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit failed bindings/current closure: %s"
                % exc) from exc

    def _validate_candidate_closure(self, new_entries, content_by_path):
        # Design 97 §4 item 1 (结构与作用域: 路径身份) and item 5 (操作
        # 闭合): after the complete physical tree has passed
        # git_tree_validation's structure/path/canonical-JSON checks and
        # the append-only retention comparison, every input_candidate,
        # operation and consumed_candidate record of this one candidate
        # commit C is collected as a (path, payload) item and handed to
        # the pure closure validator: each path must be the canonical
        # record_path() output rebuilt from that payload's own identity,
        # and typed candidate references, digests and terminal
        # consumptions must close exactly inside C. This leg always
        # runs before the REF_FACTS CAS (the fsync re-verification and
        # update-ref are the only later steps), so a closure failure
        # leaves the authoritative ref at its previous value. The
        # expected repo_id/company_id come from the repository identity
        # bound at initialize()/open() time, never from scope fields the
        # candidate records claim for themselves; a tree carrying none
        # of the three families (genesis and ordinary snapshots) passes
        # with three empty collections. Payload bytes are the exact
        # validated contents handed over from _validate_commit_tree via
        # content_by_path, never re-read from Git, so loads_record and
        # the closure check consume exactly the bytes that passed
        # canonical JSON validation on the physical leg.
        items = {family: [] for family in _CANDIDATE_CLOSURE_FAMILIES}
        for path, _oid in sorted(new_entries):
            family = git_format.canonical_record_type(path)
            if family not in _CANDIDATE_CLOSURE_FAMILIES:
                continue
            try:
                payload = git_format.loads_record(content_by_path[path])
            except (TypeError, ValueError) as exc:
                raise GitRepositoryError(
                    "accepted commit candidate record at %r is not a valid "
                    "canonical record: %s" % (path, exc)) from exc
            items[family].append((path, payload))
        try:
            candidate_reference_closure.\
                validate_candidate_path_payload_identity(
                    items["input_candidate"],
                    items["operation"],
                    items["consumed_candidate"],
                    repo_id=self._expected_meta["repo_id"],
                    company_id=self._expected_meta["company_id"])
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit failed candidate path/reference closure: "
                "%s" % exc) from exc

    def _list_tree_entries(self, commit_oid, env_extra=None):
        # -z keeps paths raw (no quoting) with NUL terminators; -r
        # recurses and -t also lists the tree entries themselves, so an
        # empty or unexpected directory cannot hide between the blobs.
        data = self._git_bytes(
            ["ls-tree", "-r", "-t", "-z", commit_oid],
            env_extra=env_extra)
        listed = []
        for record in data.split(b"\0"):
            if not record:
                continue
            info, path_bytes = record.split(b"\t", 1)
            mode, object_type, oid = info.split(b" ")
            listed.append((
                mode.decode("ascii"), object_type.decode("ascii"),
                oid.decode("ascii"), path_bytes.decode("utf-8")))
        return listed

    def build_tree(self, records, *, expected_paths):
        """Materialize a complete final snapshot into Git tree objects.

        ``records`` is the caller-assembled complete final snapshot: an
        iterable of ``(repository_relative_path, canonical_json_bytes)``
        tuples covering exactly the host-generated ``expected_paths``
        whitelist. This is not a delta or overlay: nothing is read from
        the current head, and the caller alone is responsible for the
        business completeness of the snapshot. Fact identity, unique
        leaf placement, business reference closure, candidate
        consumption and authorization are not checked here; this
        function only verifies the repository identity record, the
        exact path set, regular file modes, canonical JSON, Git object
        OIDs and a materializable tree structure.

        Every check runs before any object is written:

        - the instance must be bound to a repository identity by
          initialize()/open();
        - each record must be a 2-tuple of a str path that encodes as
          UTF-8 and bytes content, with no duplicated path;
        - every path must be the exact git_format.record_path() output
          of one fixed record template built from canonically encoded
          host IDs (git_format.canonical_record_type): a path that is
          merely safe, or a decodable but non-canonical escape alias of
          a legitimate ID, is refused before any object is written;
        - no path may be a proper directory prefix of another (a file
          and its own subtree cannot coexist), a conflict
          git_tree_validation cannot detect on its own;
        - the snapshot must carry git_format.record_path(
          "repository_meta") whose canonical bytes exactly equal the
          identity record bound to this instance;
        - each blob OID is precomputed with git hash-object without
          -w, and git_tree_validation.validate_tree_entries then
          confirms regular file modes, the exact whitelist set, OID
          shapes and canonical JSON for every entry.

        Only then are the blobs written, with each returned OID
        required to equal the precomputed one, and the directory trees
        written recursively with Git tree mode 40000. Returns the root
        tree OID. The fixed authoritative ref is never read for input
        and never moved: head_oid() is unchanged on success and on
        failure. A mid-write I/O failure may leave unreferenced
        objects behind; they are not cleaned up and are never reported
        as accepted.

        TypeError/ValueError signal malformed input structure (the
        validator's own errors propagate unchanged); BuildTreeError
        signals an unbound instance, a path outside the fixed record
        templates or with a non-canonical ID encoding, a file/directory
        prefix conflict or a missing or rewritten identity record.
        """
        if self._expected_meta is None:
            raise BuildTreeError(
                "repository identity is not bound to this instance; "
                "use initialize() or open()")
        if isinstance(records, (str, bytes)):
            raise TypeError(
                "records must be an iterable of (path, canonical bytes) "
                "tuples, not a single %s" % type(records).__name__)
        try:
            items = list(records)
        except TypeError:
            raise TypeError(
                "records must be an iterable of (path, canonical bytes) "
                "tuples") from None
        prepared = []
        seen = set()
        for item in items:
            if not isinstance(item, tuple):
                raise TypeError(
                    "record must be a tuple (path, canonical bytes), got "
                    "%s" % type(item).__name__)
            if len(item) != 2:
                raise ValueError(
                    "record must have exactly 2 items (path, canonical "
                    "bytes), got %d" % len(item))
            path, content = item
            if not isinstance(path, str):
                raise TypeError(
                    "record path must be a str, got %s"
                    % type(path).__name__)
            try:
                path.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    "record path %r is not encodable as UTF-8"
                    % (path,)) from exc
            if not isinstance(content, bytes):
                raise TypeError(
                    "record %r content must be bytes, got %s"
                    % (path, type(content).__name__))
            if path in seen:
                raise ValueError("record path %r is duplicated" % path)
            seen.add(path)
            # The complete tree may only carry paths a host generated
            # from the fixed record templates with canonically encoded
            # IDs; a path that is merely safe, or an escape alias that
            # decodes but is not the encode_id round-trip of its ID,
            # fails here before any Git object is written.
            if git_format.canonical_record_type(path) is None:
                raise BuildTreeError(
                    "record path %r is not the canonical record_path() "
                    "output of any fixed record template" % (path,))
            prepared.append((path, content))
        # A file and its own subtree cannot coexist in one tree; the
        # validator checks each path alone and cannot see this conflict.
        # Adjacent entries in sorted order are not enough: an unrelated
        # sibling such as "a-b" sorts between "a" and "a/child.json"
        # because "-" < "/", hiding the conflict from pairwise checks.
        # Compare every proper directory ancestor of each path against
        # the full path set instead; this is independent of input order.
        for path in sorted(seen):
            segments = path.split("/")
            for depth in range(1, len(segments)):
                ancestor = "/".join(segments[:depth])
                if ancestor in seen:
                    raise BuildTreeError(
                        "record paths %r and %r conflict: a file cannot "
                        "also be a directory" % (ancestor, path))
        meta_path = git_format.record_path("repository_meta")
        meta_content = None
        for path, content in prepared:
            if path == meta_path:
                meta_content = content
                break
        if meta_content is None:
            raise BuildTreeError(
                "complete snapshot is missing the repository identity "
                "record at %s" % meta_path)
        if meta_content != git_format.dumps_record(self._expected_meta):
            raise BuildTreeError(
                "repository identity record at %s does not exactly match "
                "the identity bound to this instance" % meta_path)
        # Precompute every blob OID without writing (-w is absent), then
        # run the shared entry validator; only after both pass is any
        # object written.
        entries = []
        for path, content in prepared:
            entries.append(
                ("100644", path, self._compute_blob_oid(content), content))
        git_tree_validation.validate_tree_entries(
            entries, expected_paths=expected_paths,
            object_format=self.object_format)
        written = {}
        for _mode, path, oid, content in entries:
            actual = self._write_blob(content)
            if actual != oid:
                raise GitCommandError(
                    "git hash-object -w returned %s for the blob at %r, "
                    "expected the precomputed %s" % (actual, path, oid))
            written[path] = actual
        return self._materialize_tree(written)

    def verify_integrity(self, expected_head=None):
        """Verify the authoritative history without modifying the repo.

        Confirms that REF_FACTS resolves to a commit H (and to
        expected_head when the caller supplies one), that the parent
        chain from H reaches a zero-parent genesis with exactly one
        parent per commit, that every commit on the chain carries the
        identical repository identity record (which must also match the
        host-issued identity when this instance was bound by
        initialize()/open()), and that git fsck finds every object
        reachable from the captured H intact, with H as the only
        reachability root, reflogs disabled, replacement refs ignored
        and promisor lazy fetch disabled. Corruption, missing objects
        or structural anomalies raise IntegrityError; malformed parent
        headers are converted to IntegrityError instead of leaking a
        decoding error. The check never repairs, rebuilds or rolls back
        anything: no ref, object or configuration is written and no
        reflog, network or push is involved. It verifies only the Git
        authority; it does not prove a full H/W/R backup set or that an
        external backup watermark is current.
        """
        if expected_head is not None:
            self._require_oid(expected_head, "expected_head")
        head = self._ref_value()
        if head is None:
            raise IntegrityError(
                "authoritative ref %s is missing" % REF_FACTS)
        if expected_head is not None and expected_head != head:
            raise IntegrityError(
                "expected head %s does not match authoritative ref %s "
                "pointing at %s" % (expected_head, REF_FACTS, head))
        genesis, count = self._verify_identity_chain(head)
        return IntegrityReport(
            head_oid=head, genesis_oid=genesis, commit_count=count,
            object_format=self.object_format)

    def _verify_identity_chain(self, head):
        """Walk head->genesis enforcing identity and single-parent shape.

        Every object on the walk must be a commit carrying the identical
        repository identity record (also matching the host-issued
        identity when this instance was bound by initialize()/open()),
        every non-genesis commit must have exactly one parent, the
        genesis exactly zero, and a revisited oid refuses a cyclic
        history. Afterwards git fsck runs with head as its only
        reachability root. Returns (genesis_oid, commit_count). This is
        the one identity/history rule set (F-001/design 97 section 6),
        shared by verify_integrity(), the open() history gate, the
        accept() baseline gate, the read_snapshot_at() history gate,
        maintenance pin creation and same-OID re-assertion, and the
        maintenance pin export: every consumer validates the exact
        commit it freezes or reads with this one walk instead of
        inventing a second identity format.
        """
        bound_meta = self._expected_meta
        chain_meta = None
        current = head
        seen = set()
        count = 0
        genesis = None
        while True:
            if current in seen:
                raise IntegrityError(
                    "commit chain revisits %s; refusing a cyclic history"
                    % current)
            seen.add(current)
            try:
                object_type = self._object_type(current, _VERIFY_ENV)
                if object_type != "commit":
                    raise IntegrityError(
                        "history entry %s is a %s, not a commit"
                        % (current, object_type))
                meta = self._read_meta(current, _VERIFY_ENV)
                parents = self._commit_parents(current, _VERIFY_ENV)
            except IntegrityError:
                raise
            except GitRepositoryError as exc:
                raise IntegrityError(
                    "history entry %s is unreadable: %s" % (current, exc)) \
                    from exc
            if bound_meta is not None and meta != bound_meta:
                raise IntegrityError(
                    "repository identity record at %s does not match the "
                    "host-issued identity" % current)
            if chain_meta is None:
                chain_meta = meta
            elif meta != chain_meta:
                raise IntegrityError(
                    "repository identity record at %s differs from the "
                    "record at the starting commit" % current)
            count += 1
            if not parents:
                genesis = current
                break
            if len(parents) != 1:
                raise IntegrityError(
                    "commit %s has %d parents; the authoritative history "
                    "must be a single-parent chain" % (current,
                                                       len(parents)))
            current = parents[0]
        self._verify_reachable_objects(head)
        return genesis, count

    def _require_pinned_history(self, pin_ref, commit_oid):
        # CHG-066 section 3 step 4/section 4 and design 97 section 6:
        # a pin freezes one exact commit together with the whole history
        # behind it, so pin creation and same-OID idempotent
        # re-assertion (and the bundle export leg) all pass that history
        # through the single shared _verify_identity_chain rule set. A
        # forked/merge, foreign-identity or object-damaged pinned history
        # fails as PinTargetError — the same family a non-commit/missing
        # pin target already raises. The walk and fsck are strictly
        # read-only; no refusal path here moves any ref or writes an
        # object.
        try:
            self._verify_identity_chain(commit_oid)
        except GitRepositoryError as exc:
            raise PinTargetError(
                "maintenance pin %s freezes %s, whose history fails the "
                "bound-identity, single-parent chain and object "
                "integrity checks: %s" % (pin_ref, commit_oid, exc)) \
                from exc

    def read_snapshot_at(self, commit_oid):
        """Read the record paths and raw canonical bytes of one commit.

        This is the strictly read-only storage leg behind the design
        section 10.9.3 history view; it is not the tree.history logical
        action and does no authorization, business schema, reference
        closure or projection work. ``commit_oid`` must be the full OID
        of a commit reachable from the authoritative head H captured at
        the start of this call (ValueError for a malformed or all-zero
        OID). Every returned record comes from that single pinned
        commit C alone: a later move of H is never spliced into the
        result, and the captured H is reported as head_oid only to
        document the reachability proof.

        Fail-closed checks, in order:

        - the instance must be bound to a repository identity by
          initialize()/open();
        - the captured authoritative head must resolve to a commit;
        - commit_oid must name an existing commit object (an object id
          that merely exists in the local store, for example a commit
          that lost its CAS race, is not proof of history membership);
        - the captured H's complete history passes verify_integrity()'s
          identity, single-parent-chain and fsck rule set
          (F-001/design 97 section 6) before any reachability probe:
          the probe itself walks H's ancestry and would exit with a raw
          non-0/non-1 Git failure on a missing or damaged object, so a
          merge commit, an ancestor carrying another identity record, a
          missing ancestor or a damaged reachable object anywhere
          behind H must fail here as SnapshotReadError before the probe
          (and before any record byte is returned);
        - the target must be reachable from the captured H;
        - the repository identity record at C must exactly match the
          identity bound to this instance;
        - the complete physical tree of C is re-read and held to the
          same structural and canonical-JSON rules accept() enforces:
          only regular non-executable blobs at canonical
          git_format.record_path() outputs with canonical
          git_format.dumps_record() bytes, no unknown object types,
          symlinks, executables, gitlinks or record-less directories.

        SnapshotReadError signals any of these refusals; the repository
        is never modified: no Git object is created, no ref moves and
        no SQLite table is touched. Replacement refs are ignored and
        promisor lazy fetch is disabled for every object read, so a
        missing object fails closed instead of being substituted or
        fetched over the network.
        """
        self._require_oid(commit_oid, "commit_oid")
        if self._expected_meta is None:
            raise SnapshotReadError(
                "repository identity is not bound to this instance; "
                "use initialize() or open()")
        head = self._ref_value()
        if head is None:
            raise SnapshotReadError(
                "authoritative ref %s is missing" % REF_FACTS)
        try:
            head_type = self._object_type(head, _VERIFY_ENV)
        except GitRepositoryError as exc:
            raise SnapshotReadError(
                "authoritative ref %s is unreadable: %s"
                % (REF_FACTS, exc)) from exc
        if head_type != "commit":
            raise SnapshotReadError(
                "authoritative ref %s does not point to a commit"
                % REF_FACTS)
        try:
            target_type = self._object_type(commit_oid, _VERIFY_ENV)
        except GitRepositoryError as exc:
            raise SnapshotReadError(
                "snapshot target %s is not a readable object: %s"
                % (commit_oid, exc)) from exc
        if target_type != "commit":
            raise SnapshotReadError(
                "snapshot target %s is a %s, not a commit"
                % (commit_oid, target_type))
        # F-001/design 97 §6: prove the captured H is one bound-
        # identity, single-parent chain with every reachable object
        # intact BEFORE the reachability probe below. git merge-base
        # --is-ancestor itself walks H's ancestry, and on a missing or
        # damaged ancestor it exits with a raw non-0/non-1 failure that
        # would otherwise escape as GitCommandError; running the shared
        # gate first maps every such history defect to
        # SnapshotReadError. Once C is proven reachable afterwards it is
        # on this verified chain, so a merge, identity drift or a
        # missing/damaged ancestor anywhere behind H fails the read
        # closed before any tree byte is returned. All checks are
        # read-only.
        try:
            self._verify_identity_chain(head)
        except GitRepositoryError as exc:
            raise SnapshotReadError(
                "authoritative history behind the captured head %s fails "
                "the bound-identity, single-parent chain and object "
                "integrity checks: %s" % (head, exc)) from exc
        # Reachability against the single H captured above: only commits
        # on the authoritative history may be read. merge-base with
        # --is-ancestor is a pure read and treats a commit as an
        # ancestor of itself, so reading the current head is allowed.
        # H's whole ancestry was just verified intact above, so this
        # probe can only answer 0/1 instead of tripping over a missing
        # object.
        result = self._git(
            ["merge-base", "--is-ancestor", commit_oid, head],
            check=False, env_extra=_VERIFY_ENV)
        if result.returncode == 1:
            raise SnapshotReadError(
                "snapshot target %s is not reachable from the "
                "authoritative head %s captured for this read"
                % (commit_oid, head))
        if result.returncode != 0:
            raise GitCommandError("git merge-base failed", result)
        try:
            meta = self._read_meta(commit_oid, _VERIFY_ENV)
        except GitRepositoryError as exc:
            raise SnapshotReadError(
                "repository metadata at %s is unreadable: %s"
                % (commit_oid, exc)) from exc
        if meta != self._expected_meta:
            raise SnapshotReadError(
                "repository identity record at %s does not match the "
                "host-issued identity bound to this instance"
                % commit_oid)
        try:
            entries = self._read_commit_tree_entries(
                commit_oid, env_extra=_VERIFY_ENV, context="snapshot tree")
        except GitRepositoryError as exc:
            raise SnapshotReadError(
                "snapshot tree at %s failed validation: %s"
                % (commit_oid, exc)) from exc
        # The tree's own path set is the whitelist here: there is no
        # host-supplied whitelist for a historical commit, so this pass
        # exists to enforce regular modes, OID shapes, path uniqueness
        # and canonical JSON bytes, not set membership.
        try:
            git_tree_validation.validate_tree_entries(
                entries,
                expected_paths=[path for _mode, path, _oid, _content
                                in entries],
                object_format=self.object_format)
        except (TypeError, ValueError) as exc:
            raise SnapshotReadError(
                "snapshot tree at %s failed validation: %s"
                % (commit_oid, exc)) from exc
        records = tuple(
            SnapshotRecord(path=path, content=content)
            for _mode, path, _oid, content in sorted(
                entries, key=lambda entry: entry[1]))
        return HistoricalSnapshot(
            commit_oid=commit_oid, head_oid=head, records=records)

    def create_maintenance_pin(self, backup_id, barrier_id, commit_oid):
        """Freeze the captured commit H under one backup/barrier identity.

        The CHG-066 section 3 step 4 / section 4 persistent maintenance
        pin primitive: a plain Git ref at
        maintenance_pin_ref(backup_id, barrier_id) inside the dedicated
        refs/sam maintenance namespace. The pin is a host-controlled
        reachability root that protects H (and through H's single-parent
        chain the whole accepted history behind it) from gc/prune; it
        is never a second business authority ref. No business tree is
        touched and REF_FACTS is never moved by this call, on the
        success path or on any refusal path.

        Fail-closed contract, in order:

        - backup_id and barrier_id must be 1-64 character strs of
          [A-Za-z0-9_-], and commit_oid a full non-zero OID of this
          repository's object format (ValueError otherwise);
        - the instance must be bound to a repository identity by
          initialize()/open();
        - commit_oid must name an existing commit object, checked with
          replacement refs ignored and promisor lazy fetch disabled
          (PinTargetError otherwise);
        - an existing pin under the same identity either already
          freezes exactly commit_oid, in which case the call is
          idempotent and reports created=False without requiring H to
          still be the current head (the head legitimately advances
          past a pin after the barrier), but the frozen commit's
          complete history is still re-validated on every such
          re-assertion with verify_integrity()'s identity,
          single-parent-chain and fsck rule set (F-001/design 97
          section 6): a merge, foreign identity or missing/damaged
          ancestor raises PinTargetError instead of reporting the pin
          healthy; or it freezes a different commit and
          PinConflictError is raised: an existing pin is never moved,
          overwritten or deleted;
        - a new pin requires commit_oid to equal the current
          authoritative head and that commit's complete history to
          pass that same identity, single-parent-chain and fsck gate
          (PinTargetError otherwise), and the ref itself is written
          inside one atomic git update-ref --stdin transaction that
          simultaneously
          re-verifies REF_FACTS == commit_oid and creates the pin ref
          only if it is absent. A head that moves, or a concurrent
          creation that wins between the checks above and this write,
          aborts every leg of the transaction together
          (PinHeadMismatchError / PinConflictError) with no pin created
          and every existing ref left at its previous value;
        - the section 10.9.1 fsync policy is re-verified immediately
          before the transaction, so a repository whose durability
          configuration is missing, duplicated or drifted never gains a
          pin whose persistence could not even be confirmed.

        There is deliberately no move, delete, TTL or auto-release
        counterpart in this slice: once created, the pin survives
        process exit, restart and timeout until an explicitly
        authorized future flow removes it. Returns a
        PinCreationResult; created=True only when this call's
        transaction created the ref.
        """
        pin_ref = maintenance_pin_ref(backup_id, barrier_id)
        self._require_oid(commit_oid, "commit_oid")
        if self._expected_meta is None:
            raise MaintenancePinError(
                "repository identity is not bound to this instance; "
                "use initialize() or open()")
        try:
            object_type = self._object_type(commit_oid, _VERIFY_ENV)
        except GitRepositoryError as exc:
            raise PinTargetError(
                "pin target %s is not a readable object: %s"
                % (commit_oid, exc)) from exc
        if object_type != "commit":
            raise PinTargetError(
                "pin target %s is a %s, not a commit"
                % (commit_oid, object_type))
        head = self._ref_value()
        if head is None:
            raise MaintenancePinError(
                "authoritative ref %s is missing" % REF_FACTS)
        pin = MaintenancePin(
            backup_id=backup_id, barrier_id=barrier_id,
            commit_oid=commit_oid, ref=pin_ref)
        existing = self._read_ref_oid(pin_ref)
        if existing is not None:
            if existing == commit_oid:
                # Idempotent re-assertion writes nothing and does not
                # require H to still be the current head, but the frozen
                # history is re-validated every time: a pin whose commit
                # later turns out to sit on a merge, a foreign identity
                # record or a missing/damaged ancestor must not report
                # success just because the ref already exists
                # (F-001/design 97 section 6).
                self._require_pinned_history(pin_ref, commit_oid)
                return PinCreationResult(pin=pin, created=False)
            raise PinConflictError(
                "maintenance pin %s already freezes %s; refusing to bind "
                "the same backup_id/barrier_id to %s (an existing pin is "
                "never moved)" % (pin_ref, existing, commit_oid))
        if head != commit_oid:
            raise PinHeadMismatchError(
                "pin target %s is not the current authoritative head %s; "
                "a new pin may only freeze the head the host captured"
                % (commit_oid, head))
        # A new pin freezes exactly H together with the complete history
        # behind it: same bound-identity, single-parent chain and fsck
        # gate verify_integrity() runs, before anything is written.
        self._require_pinned_history(pin_ref, commit_oid)
        # Last guard before the ref write: the section 10.9.1 durability
        # configuration is re-read through Git, so a drifted repository
        # never gains a pin whose persistence cannot be confirmed.
        self._enforce_fsync_policy()
        # One atomic transaction: re-verify REF_FACTS == H and create
        # the pin ref only if it does not exist yet. Git executes the
        # batched update-ref commands all-or-nothing, so neither a head
        # that moved nor a concurrent creation of the same ref can
        # leave a half-written result behind.
        transaction = "verify %s %s\ncreate %s %s\n" % (
            REF_FACTS, commit_oid, pin_ref, commit_oid)
        result = self._git(
            ["update-ref", "--stdin"],
            input_bytes=transaction.encode("ascii"), check=False)
        if result.returncode == 0:
            return PinCreationResult(pin=pin, created=True)
        # Classify the aborted transaction from the state the repository
        # actually kept; never force, move or delete anything.
        existing = self._read_ref_oid(pin_ref)
        if existing is not None:
            if existing == commit_oid:
                return PinCreationResult(pin=pin, created=False)
            raise PinConflictError(
                "maintenance pin %s was concurrently bound to %s; "
                "refusing to bind the same backup_id/barrier_id to %s "
                "(an existing pin is never moved)"
                % (pin_ref, existing, commit_oid))
        head_now = self._ref_value()
        if head_now is None or head_now != commit_oid:
            raise PinHeadMismatchError(
                "authoritative head moved away from %s while the pin "
                "transaction was in flight; the transaction aborted and "
                "no pin was created" % commit_oid)
        raise GitCommandError(
            "git update-ref --stdin failed without a competing change",
            result)

    def read_maintenance_pin(self, backup_id, barrier_id):
        """Return the MaintenancePin for one identity, or None if absent.

        Strictly read-only: validates the identity, reads the exact
        maintenance ref and verifies its recorded target before
        reporting anything. None means exactly one thing: the
        maintenance ref does not exist. An existing ref whose recorded
        value is not a full object id of this repository's format,
        whose target object is missing, or whose target resolves to a
        non-commit (blob, tree or tag) fails closed
        (MaintenancePinError / PinTargetError): a pin denotes one
        exact frozen commit H, so a corrupted ref is never reported as
        a valid pin and never collapses into the absent answer. The
        target check sees this repository's real objects with
        replacement refs ignored and promisor lazy fetch disabled, so
        it never substitutes an object and never touches the network.
        No object is created, no ref moves and REF_FACTS is never
        touched; a corrupted ref is never repaired, moved or deleted
        either: the interference is for the backup and integrity flows
        to judge, never for this query.
        """
        pin_ref = maintenance_pin_ref(backup_id, barrier_id)
        value = self._pin_ref_oid(pin_ref)
        if value is None:
            return None
        self._require_pin_target_commit(pin_ref, value)
        return MaintenancePin(
            backup_id=backup_id, barrier_id=barrier_id,
            commit_oid=value, ref=pin_ref)

    def list_maintenance_pins(self):
        """All maintenance pins under PIN_REF_NAMESPACE, sorted.

        Strictly read-only enumeration of the dedicated namespace,
        ordered by (backup_id, barrier_id). The namespace belongs
        exclusively to this primitive, so a ref inside it that is not
        exactly <backup_id>/<barrier_id> with validated ids and a full
        object id of this repository's format is an anomaly and raises
        MaintenancePinError instead of being skipped, guessed at or
        repaired. Every surviving ref's recorded target is then
        verified to be an existing commit object of this repository,
        with replacement refs ignored and promisor lazy fetch
        disabled, and a missing or non-commit target fails the whole
        query closed (PinTargetError): one corrupted ref is never
        listed as a valid pin, never silently dropped among the
        healthy ones and never repaired. REF_FACTS and every other
        namespace are outside the scan.
        """
        data = self._git_bytes(
            ["for-each-ref", "--format=%(objectname) %(refname)",
             PIN_REF_NAMESPACE])
        prefix = PIN_REF_NAMESPACE + "/"
        pins = []
        for line in data.decode("utf-8", "replace").splitlines():
            if not line:
                continue
            oid, refname = line.split(" ", 1)
            parts = refname[len(prefix):].split("/") \
                if refname.startswith(prefix) else []
            if len(parts) != 2 or not all(
                    _PIN_ID_RE.fullmatch(part) for part in parts):
                raise MaintenancePinError(
                    "unexpected ref %r inside the dedicated maintenance "
                    "pin namespace; refusing to interpret it" % refname)
            self._require_pin_target_commit(refname, oid)
            pins.append(MaintenancePin(
                backup_id=parts[0], barrier_id=parts[1],
                commit_oid=oid, ref=refname))
        pins.sort(key=lambda pin: (pin.backup_id, pin.barrier_id))
        return tuple(pins)

    def export_maintenance_pin_bundle(self, backup_id, barrier_id,
                                      output_path):
        """Export one frozen maintenance pin as a self-contained bundle.

        The CHG-066 section 4 Git-only export leg: after the maintenance
        barrier has been released, the exact commit H captured under the
        named pin is exported from the pin itself, never from a ref that
        could have advanced since. The resulting bundle advertises
        exactly the one pin ref
        (maintenance_pin_ref(backup_id, barrier_id)) and carries every
        object reachable from H, so it imports into a repository that
        has neither a remote configured nor any pre-existing object.

        Fail-closed contract, in order:

        - backup_id and barrier_id must pass maintenance_pin_ref()
          (ValueError otherwise), and output_path must be a non-empty
          str/os.PathLike naming a file (TypeError/ValueError otherwise);
        - the instance must be bound to a repository identity by
          initialize()/open(): a pin bundle has no legitimate producer
          that cannot prove the repo_id/installation_id/company_id of H,
          so an unbound instance is refused (MaintenancePinError) and no
          output is created;
        - the output is refused while it already exists in any form,
          including a symbolic link or a directory
          (BundleOutputExistsError), so an existing file or a previously
          completed backup is never overwritten, and the parent
          directory must already exist (BundleExportError);
        - the exact named pin ref is read and its recorded target is
          verified to be an existing commit object with replacement
          refs ignored and promisor lazy fetch disabled: a missing pin
          raises PinNotFoundError and a corrupted or non-commit pin
          raises MaintenancePinError/PinTargetError. There is
          deliberately no fallback to REF_FACTS, HEAD, reflogs or any
          other ref;
        - before anything is bundled, H's complete history is validated
          in the source repository with the same rule set as
          verify_integrity(): every object from H back to the genesis is
          a commit carrying this instance's bound identity record, every
          non-genesis commit has exactly one parent and the genesis
          none, and git fsck passes with H as its only root. A forked or
          merge history, a foreign identity record or damaged object
          therefore fails closed as PinTargetError before a byte is
          staged;
        - the bundle is created from the full pin ref name only (never
          --all, --branches, --tags or a glob) into a private staging
          file next to the requested output;
        - publication happens only after the staging bundle passes, in a
          brand-new empty bare repository created beside the output for
          this one call and removed afterwards. That verification
          repository is initialized with
          ``--object-format=<this repository's format>`` explicitly, so
          a SHA-256 bundle is never checked against a host whose
          init.defaultObjectFormat happens to be SHA-1: the checks are
          ``git bundle verify`` (which fails on any prerequisite commit
          the empty repository does not have, proving the bundle
          self-contained), ``git bundle list-heads`` reporting exactly
          the one pin ref at exactly H, a fetch of that one ref from the
          local bundle file, the fetched commit resolving to H with
          exactly one zero-parent root, and an fsck of the fetched
          closure;
        - the verified bundle is published with a no-clobber hard link
          (os.link), so an output that appears concurrently is not
          replaced. Any generation or verification failure removes only
          this call's own staging file and verification directory, and
          never publishes an output.

        The returned MaintenancePinBundle additionally carries the
        published file's non-empty size_bytes and SHA-256 digest
        (computed over the verified staging bytes, which the hard link
        publishes byte-identically) and this repository's object
        format, ready for a later section 4 manifest to record; this
        leg itself writes no manifest.

        Post-publication cleanup is a separate, explicit failure: if
        removing this call's private staging name fails after the hard
        link succeeded, BundleStagingCleanupError is raised with
        published=True. The bundle file is already complete at
        output_path and must not be re-exported over the same path (a
        retry is refused with BundleOutputExistsError); the leftover
        staging path is reported for host cleanup. A cleanup failure on
        a path where publication never happened is reported with
        published=False, chained after the original export failure.

        Trusted-directory precondition: the output's parent directory is
        taken as host-provided, already existing, trusted and not
        concurrently replaced (including a swap to a symlinked or
        attacker-controlled directory) for the duration of the call. The
        pre-existing output and output-symlink cases are refused, but
        this leg does not attempt to prevent a racing replacement of a
        parent path component and must not be read as claiming that
        defense. Likewise, local rename/hard-link success and the
        repository's fsync policy do not constitute deployment-verified
        persistence of the output file or its directory; that remains a
        host/deployment concern.

        The source repository is strictly read-only on every leg: no
        object is written into it, no ref (REF_FACTS, the pin or any
        other), HEAD or configuration is moved, and the fetch used in
        verification reads the local bundle file inside the throwaway
        repository rather than contacting a remote. This is the Git
        component of the backup only: W/R components, manifests, the
        backup coordinator, restore and authorization are out of scope,
        and a successful export is not a complete H/W/R backup.
        Returns a MaintenancePinBundle.
        """
        pin_ref = maintenance_pin_ref(backup_id, barrier_id)
        try:
            output = Path(os.fspath(output_path))
        except TypeError:
            raise TypeError(
                "output_path must be a str or os.PathLike, got %s"
                % type(output_path).__name__) from None
        if not output.name or output.name in (".", ".."):
            raise ValueError(
                "output_path must name a file, got %r" % (output_path,))
        if self._expected_meta is None:
            raise MaintenancePinError(
                "repository identity is not bound to this instance; "
                "use initialize() or open()")
        # Refuse a symbolic link before its target is inspected and
        # refuse any existing path, exactly as initialize() does: an
        # existing output is never overwritten or truncated.
        if output.is_symlink():
            raise BundleOutputExistsError(
                "bundle output path must not be a symbolic link: %s"
                % output)
        if output.exists():
            raise BundleOutputExistsError(
                "bundle output path already exists; an existing output "
                "is never overwritten: %s" % output)
        if not output.is_absolute():
            output = Path.cwd() / output
        parent = output.parent
        if not parent.is_dir():
            raise BundleExportError(
                "bundle output parent directory does not exist: %s"
                % parent)
        # The only source ref this leg ever reads: the exact named pin.
        # None means precisely that the pin ref is absent; anything
        # recorded but not a full object id naming an existing commit is
        # corruption and fails closed inside the target check.
        commit_oid = self._pin_ref_oid(pin_ref)
        if commit_oid is None:
            raise PinNotFoundError(
                "maintenance pin %s does not exist; refusing to export "
                "any other ref in its name" % pin_ref)
        self._require_pin_target_commit(pin_ref, commit_oid)
        # Validate H's identity and single-parent history in the source
        # repository before bundling, with the same rule set as
        # verify_integrity() and the pin creation leg; a pin behind the
        # current head is fine, a forked/foreign/damaged pinned history
        # is not.
        self._require_pinned_history(pin_ref, commit_oid)
        pin = MaintenancePin(
            backup_id=backup_id, barrier_id=barrier_id,
            commit_oid=commit_oid, ref=pin_ref)
        # Stage beside the final target so publication is one
        # filesystem's atomic no-clobber link; the random name is
        # private to this call and never user-controlled.
        staging_fd, staging_name = tempfile.mkstemp(
            dir=str(parent), prefix=".sam-bundle-", suffix=".staging")
        os.close(staging_fd)
        staging = Path(staging_name)
        published = False
        original_failure = None
        try:
            try:
                self._git(
                    ["bundle", "create", str(staging), pin_ref],
                    env_extra=_VERIFY_ENV)
            except GitCommandError as exc:
                raise BundleExportError(
                    "failed to create the maintenance pin bundle: %s"
                    % exc) from exc
            self._verify_bundle_in_fresh_repository(
                staging, parent, pin)
            try:
                size = staging.stat().st_size
            except OSError as exc:
                raise BundleExportError(
                    "the generated staging bundle is unreadable: %s"
                    % exc) from exc
            if size <= 0:
                raise BundleExportError(
                    "the generated bundle at %s is empty" % staging)
            digest = hashlib.sha256()
            try:
                with staging.open("rb") as staged:
                    while True:
                        chunk = staged.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            except OSError as exc:
                raise BundleExportError(
                    "the generated staging bundle is unreadable: %s"
                    % exc) from exc
            try:
                os.link(staging, output)
            except FileExistsError as exc:
                raise BundleOutputExistsError(
                    "bundle output path appeared before publication; an "
                    "existing output is never overwritten: %s" % output
                ) from exc
            except OSError as exc:
                raise BundleExportError(
                    "failed to publish the verified bundle to %s: %s"
                    % (output, exc)) from exc
            published = True
            return MaintenancePinBundle(
                pin=pin, output_path=output, size_bytes=size,
                sha256_digest=digest.hexdigest(),
                object_format=self.object_format)
        except BaseException as exc:
            # Remember the export's own failure so a staging cleanup
            # failure in the finally chains after it rather than masking
            # why the export aborted.
            original_failure = exc
            raise
        finally:
            # Always remove only this call's private staging name. The
            # publication is a separate hard-linked directory entry, so
            # unlinking the staging name after success leaves the
            # published output intact; on failure there is nothing else
            # to clean up here (the verification directory is removed by
            # its own TemporaryDirectory context). A cleanup failure is
            # reported explicitly instead of swallowed: after a
            # successful publication the caller must not read it as
            # "export failed, safe to retry" (the no-clobber link makes
            # a retry meet BundleOutputExistsError), and before
            # publication it means the staging path leaked.
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if published:
                    raise BundleStagingCleanupError(
                        "the verified bundle was already published at %s, "
                        "but removing this call's private staging file %s "
                        "failed: %s. The output is complete; do not retry "
                        "the export against this path (it is refused as an "
                        "existing output), remove the leftover staging file "
                        "through host cleanup"
                        % (output, staging, exc),
                        published=True, output_path=output,
                        staging_path=staging) from exc
                raise BundleStagingCleanupError(
                    "the bundle export failed before publication and "
                    "removing the private staging file %s also failed: %s; "
                    "no bundle was published at %s and the leftover staging "
                    "file requires host cleanup"
                    % (staging, exc, output),
                    published=False, output_path=output,
                    staging_path=staging) from (original_failure or exc)

    def _verify_bundle_in_fresh_repository(self, staging, scratch_parent,
                                           pin):
        # Section 4 empty-environment verification. Every command below
        # targets either the throwaway bare repository or the local
        # staging bundle file, never this facts repository and never a
        # remote; _VERIFY_ENV keeps replacement objects and promisor lazy
        # fetch out of every read. A failure at any step raises
        # BundleExportError before the caller publishes anything.
        with tempfile.TemporaryDirectory(
                dir=str(scratch_parent),
                prefix=".sam-bundle-verify-") as scratch_name:
            fresh = Path(scratch_name) / "import.git"
            try:
                # Match the source object format explicitly: depending on
                # the host's init.defaultObjectFormat would make a
                # SHA-256 bundle fail import (or, with a SHA-256
                # default, a SHA-1 bundle) for a reason unrelated to the
                # bundle's own self-containment.
                _run_git([
                    "init", "--bare",
                    "--object-format=" + self.object_format,
                    str(fresh)])
                verify = _run_git(
                    ["-C", str(fresh), "bundle", "verify", str(staging)],
                    check=False, env_extra=_VERIFY_ENV)
                if verify.returncode != 0:
                    detail = (verify.stdout + verify.stderr).decode(
                        "utf-8", "replace").strip()
                    raise BundleExportError(
                        "bundle verification failed inside a brand-new "
                        "empty bare repository, so the bundle is not "
                        "self-contained: %s" % detail)
                heads_data = _run_git(
                    ["-C", str(fresh), "bundle", "list-heads",
                     str(staging)],
                    env_extra=_VERIFY_ENV).stdout
                heads = []
                for line in heads_data.decode("utf-8", "replace") \
                        .splitlines():
                    if not line:
                        continue
                    advertised_oid, advertised_ref = line.split(" ", 1)
                    heads.append((advertised_oid, advertised_ref))
                if heads != [(pin.commit_oid, pin.ref)]:
                    raise BundleExportError(
                        "bundle advertises %r, expected exactly the one "
                        "pin ref %s at %s; the export never uses --all"
                        % (heads, pin.ref, pin.commit_oid))
                fetch = _run_git(
                    ["-C", str(fresh), "fetch", "--quiet", "--no-tags",
                     str(staging), "%s:%s" % (pin.ref, pin.ref)],
                    check=False, env_extra=_VERIFY_ENV)
                if fetch.returncode != 0:
                    detail = (fetch.stdout + fetch.stderr).decode(
                        "utf-8", "replace").strip()
                    raise BundleExportError(
                        "importing the advertised pin ref from the bundle "
                        "failed in the empty repository: %s" % detail)
                fetched = _run_git(
                    ["-C", str(fresh), "rev-parse", "--verify", pin.ref],
                    env_extra=_VERIFY_ENV).stdout.decode("ascii").strip()
                if fetched != pin.commit_oid:
                    raise BundleExportError(
                        "the fetched pin ref resolves to %s, not the "
                        "frozen commit %s" % (fetched, pin.commit_oid))
                object_type = _run_git(
                    ["-C", str(fresh), "cat-file", "-t", pin.ref],
                    env_extra=_VERIFY_ENV).stdout.decode("ascii").strip()
                if object_type != "commit":
                    raise BundleExportError(
                        "the fetched pin target is a %s, not a commit"
                        % object_type)
                refs_out = _run_git(
                    ["-C", str(fresh), "for-each-ref",
                     "--format=%(refname)"],
                    env_extra=_VERIFY_ENV).stdout.decode("utf-8")
                refs = [line for line in refs_out.splitlines() if line]
                if refs != [pin.ref]:
                    raise BundleExportError(
                        "the empty repository holds %r after the fetch, "
                        "expected only the one pin ref %s"
                        % (refs, pin.ref))
                roots = _run_git(
                    ["-C", str(fresh), "rev-list", "--max-parents=0",
                     pin.ref], env_extra=_VERIFY_ENV
                ).stdout.decode("ascii").split()
                if len(roots) != 1:
                    raise BundleExportError(
                        "the exported history has %d zero-parent roots, "
                        "expected exactly one" % len(roots))
                count_out = _run_git(
                    ["-C", str(fresh), "rev-list", "--count", pin.ref],
                    env_extra=_VERIFY_ENV).stdout.decode("ascii").strip()
                try:
                    commit_count = int(count_out)
                except ValueError:
                    commit_count = 0
                if commit_count < 1:
                    raise BundleExportError(
                        "the exported pin history is empty")
                fsck = _run_git(
                    ["-C", str(fresh), "fsck", "--no-dangling",
                     "--no-reflogs", "--strict", pin.ref],
                    check=False, env_extra=_VERIFY_ENV)
                output = (fsck.stdout + fsck.stderr).decode(
                    "utf-8", "replace")
                problems = [line for line in output.splitlines()
                            if line.startswith(("error", "fatal"))]
                if fsck.returncode != 0 or problems:
                    detail = "; ".join(problems[:5]) or (
                        "git fsck exited with %d" % fsck.returncode)
                    raise BundleExportError(
                        "objects imported from the bundle failed "
                        "integrity checks in the empty repository: %s"
                        % detail)
            except BundleExportError:
                raise
            except GitCommandError as exc:
                raise BundleExportError(
                    "bundle verification in the empty repository "
                    "failed: %s" % exc) from exc

    def _verify_reachable_objects(self, head):
        # git fsck re-hashes and re-validates the objects reachable from
        # its roots. The captured authoritative head is passed as the
        # only root and reflog roots are disabled, so other refs or
        # reflog entries can never make objects outside the verified
        # history part of this check. --strict keeps full object
        # validation; --connectivity-only is never used. Dangling
        # objects (for example a commit that lost a CAS race) are not
        # part of the accepted history and are not errors. fsck without
        # --lost-found does not write anything.
        result = self._git(
            ["fsck", "--no-dangling", "--no-reflogs", "--strict", head],
            check=False, env_extra=_VERIFY_ENV)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        problems = [line for line in output.splitlines()
                    if line.startswith(("error", "fatal"))]
        if result.returncode != 0 or problems:
            detail = "; ".join(problems[:5])
            if not detail:
                detail = "git fsck exited with %d" % result.returncode
            raise IntegrityError(
                "reachable Git objects failed integrity checks: %s"
                % detail)

    def _install_fsync_policy(self):
        # Write the exact policy values into the repository's own
        # config file, then confirm the values by reading the effective
        # local configuration back through Git instead of trusting the
        # write. This confirms configuration values only, not the
        # platform's actual fsync behavior.
        for key, value in _FSYNC_POLICY:
            self._git(["config", "--local", key, value])
        self._enforce_fsync_policy()

    def _enforce_fsync_policy(self):
        # Section 10.9.1 fail-closed check. --local reads only the
        # repository's own config file (GIT_CONFIG_NOSYSTEM and the
        # devnull global file in _git_env already keep user and system
        # configuration out), and --get-all exposes duplicated entries
        # that a plain --get would silently collapse to the last value.
        # Missing, duplicated or drifted values raise
        # GitRepositoryError; open() converts that into OpenError while
        # accept() lets it fail closed before the CAS.
        for key, expected in _FSYNC_POLICY:
            result = self._git(
                ["config", "--local", "--get-all", key], check=False)
            if result.returncode == 1:
                values = []
            elif result.returncode != 0:
                raise GitCommandError(
                    "git config --get-all %s failed" % key, result)
            else:
                values = result.stdout.decode(
                    "utf-8", "replace").splitlines()
            if not values:
                raise GitRepositoryError(
                    "repository configuration %s is missing; the section "
                    "10.9.1 fsync policy requires exactly one local "
                    "value %r" % (key, expected))
            if len(values) != 1:
                raise GitRepositoryError(
                    "repository configuration %s is set %d times; the "
                    "section 10.9.1 fsync policy requires exactly one "
                    "local value %r" % (key, len(values), expected))
            if values[0] != expected:
                raise GitRepositoryError(
                    "repository configuration %s is %r, not the section "
                    "10.9.1 fsync policy value %r"
                    % (key, values[0], expected))

    def _git(self, args, input_bytes=None, check=True, env_extra=None):
        return _run_git(
            ["-C", str(self.path)] + list(args), input_bytes, check,
            env_extra)

    def _git_text(self, args, input_bytes=None, env_extra=None):
        return self._git(
            args, input_bytes=input_bytes, env_extra=env_extra
        ).stdout.decode("utf-8")

    def _git_bytes(self, args, env_extra=None):
        return self._git(args, env_extra=env_extra).stdout

    def _require_oid(self, value, name, allow_zero=False):
        if not isinstance(value, str) \
                or len(value) != self.oid_length \
                or any(char not in _HEX_DIGITS for char in value):
            raise ValueError(
                "%s must be a full %d-hex-character object id for the "
                "repository object format %s"
                % (name, self.oid_length, self.object_format))
        if not allow_zero and value == self.zero_oid:
            raise ValueError("%s must not be the all-zero object id" % name)

    def _ref_value(self):
        result = self._git(["rev-parse", "--verify", REF_FACTS], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def _read_ref_oid(self, ref):
        # Strict read of one exact ref name: the full OID it records, or
        # None when the ref does not exist. Any recorded value that is
        # not a full object id of this repository's format fails closed
        # instead of being interpreted; nothing dereferences a symbolic
        # ref or peels a tag, because this adapter only ever creates
        # direct commit-valued refs.
        result = self._git(
            ["rev-parse", "--verify", "--quiet", ref], check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.decode("ascii", "replace").strip()
        if len(value) != self.oid_length or any(
                char not in _HEX_DIGITS for char in value):
            raise MaintenancePinError(
                "ref %s does not record a full %s object id: %r"
                % (ref, self.object_format, value))
        return value

    def _pin_ref_oid(self, pin_ref):
        # The raw object id one exact pin ref records, or None only
        # when the ref itself is absent. Read through for-each-ref
        # instead of rev-parse: for-each-ref reports the recorded
        # value without requiring the target object to exist and
        # without ever peeling a tag, so a ref whose target went
        # missing stays distinguishable from a truly absent ref (never
        # None) and a tag target can never masquerade as the commit it
        # annotates. The pattern also matches deeper refs under a
        # directory/file-conflicting name; those are namespace
        # anomalies list_maintenance_pins() rejects, and for this
        # exact-name query they simply mean the pin ref is absent.
        data = self._git_bytes(
            ["for-each-ref", "--format=%(objectname) %(refname)", pin_ref])
        for line in data.decode("utf-8", "replace").splitlines():
            if not line:
                continue
            oid, refname = line.split(" ", 1)
            if refname == pin_ref:
                return oid
        return None

    def _require_pin_target_commit(self, pin_ref, oid):
        # Fail-closed target verification shared by both pin queries:
        # the recorded value must be a full object id of this
        # repository's format naming an existing commit object of this
        # very repository. Replacement refs are ignored and promisor
        # lazy fetch is disabled (_VERIFY_ENV), so the check sees the
        # real objects and a missing object fails closed here instead
        # of being substituted or fetched over the network. No refusal
        # path repairs, moves or deletes anything.
        if len(oid) != self.oid_length or any(
                char not in _HEX_DIGITS for char in oid):
            raise MaintenancePinError(
                "maintenance pin ref %s does not record a full %s "
                "object id: %r" % (pin_ref, self.object_format, oid))
        try:
            object_type = self._object_type(oid, _VERIFY_ENV)
        except GitRepositoryError as exc:
            raise PinTargetError(
                "maintenance pin ref %s targets %s, which is not a "
                "readable object of this repository: %s"
                % (pin_ref, oid, exc)) from exc
        if object_type != "commit":
            raise PinTargetError(
                "maintenance pin ref %s targets %s, a %s, not a commit; "
                "refusing to report it as a valid pin"
                % (pin_ref, oid, object_type))

    def _object_type(self, oid, env_extra=None):
        result = self._git(
            ["cat-file", "-t", oid], check=False, env_extra=env_extra)
        if result.returncode != 0:
            raise GitRepositoryError("unknown object: %s" % oid)
        return result.stdout.decode("ascii").strip()

    def _commit_parents(self, oid, env_extra=None):
        data = self._git_bytes(["cat-file", "commit", oid], env_extra)
        parents = []
        for line in data.split(b"\n"):
            if line == b"":
                break
            if line.startswith(b"parent "):
                try:
                    parents.append(line[len(b"parent "):].decode("ascii"))
                except UnicodeDecodeError as exc:
                    raise GitRepositoryError(
                        "commit %s has a malformed parent header"
                        % oid) from exc
        return parents

    def _create_commit_object(self, tree_oid, parents, message):
        if not isinstance(message, str) or not message:
            raise ValueError("commit message must be a non-empty str")
        args = ["commit-tree", tree_oid]
        for parent in parents:
            args += ["-p", parent]
        args += ["-m", message]
        oid = self._git_text(args).strip()
        self._require_oid(oid, "commit oid produced by git")
        return oid

    def _compute_blob_oid(self, content):
        # The same hashing as _write_blob but without -w: the OID follows
        # the repository object format and no object is written, so a
        # validation failure never leaves an object behind.
        return self._git_text(
            ["hash-object", "-t", "blob", "--stdin"], content).strip()

    def _write_blob(self, data):
        return self._git_text(
            ["hash-object", "-w", "--stdin"], data).strip()

    def _write_tree(self, entries):
        # entries: (mode, name, oid); Git orders tree entries by name
        # with directories compared as if they had a trailing slash.
        ordered = sorted(
            entries, key=lambda e: e[1] + ("/" if e[0] == "40000" else ""))
        body = bytearray()
        for mode, name, oid in ordered:
            self._require_oid(oid, "tree entry oid")
            body += mode.encode("ascii")
            body += b" "
            body += name.encode("utf-8")
            body += b"\0"
            body += bytes.fromhex(oid)
        return self._git_text(
            ["hash-object", "-t", "tree", "-w", "--stdin"],
            bytes(body)).strip()

    def _materialize_tree(self, path_to_oid):
        # path_to_oid maps every validated record path to its written
        # blob OID. Paths were already checked to be prefix-free, so a
        # directory segment can never collide with a file of the same
        # name while the nested structure is assembled.
        nested = {}
        for path, oid in path_to_oid.items():
            node = nested
            segments = path.split("/")
            for segment in segments[:-1]:
                node = node.setdefault(segment, {})
            node[segments[-1]] = oid
        return self._write_nested_tree(nested)

    def _write_nested_tree(self, node):
        # Trees are written bottom-up so every subtree OID is known
        # before its parent tree body is assembled; _write_tree applies
        # Git's name ordering to the entries.
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(
                    ("40000", name, self._write_nested_tree(value)))
            else:
                entries.append(("100644", name, value))
        return self._write_tree(entries)

    def _read_meta(self, head, env_extra=None):
        # Raises GitRepositoryError; open() converts that into OpenError,
        # while accept() lets it fail closed before the CAS.
        meta_path = git_format.record_path("repository_meta")
        try:
            data = self._git_bytes(
                ["cat-file", "blob", "%s:%s" % (head, meta_path)],
                env_extra)
        except GitCommandError as exc:
            raise GitRepositoryError(
                "repository metadata record is missing at %s" % meta_path) \
                from exc
        try:
            return git_format.loads_record(data)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "repository metadata record is not a valid record") from exc
