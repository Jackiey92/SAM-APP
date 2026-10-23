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
- The genesis snapshot is a zero-parent commit carrying only the fixed
  repository identity record built through
  git_format.record_path("repository_meta") and git_format.dumps_record;
  callers can never supply a file path. The record binds repo_id,
  installation_id and company_id plus serializer and schema versions.
- open() re-verifies the bare state, the HEAD symref, the absence of
  remotes and the identity record at the current ref tip; any mismatch
  is a refusal and the repository is never modified.
- Later commits are built with exactly one parent equal to the current
  ref tip; acceptance happens only through an expected-old compare and
  swap on REF_FACTS. A commit object that exists but whose CAS lost the
  race is never reported as accepted, and no merge or rebase of an old
  tree is offered.
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
  with the authoritative ref left at its previous value. A commit
  assembled outside build_tree() is held to exactly the same physical
  contract.
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

All Git invocations use argv lists without a shell, the fixed ref and a
timeout; no remote, push or network command is ever issued, and no
Git command text comes from a model.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from app.facts_tree import git_format
from app.facts_tree import git_tree_validation

REF_FACTS = "refs/heads/facts"
REPOSITORY_META_SCHEMA_VERSION = 1
GIT_TIMEOUT_SECONDS = 30
_OBJECT_FORMAT_OID_LENGTH = {"sha1": 40, "sha256": 64}
_HEX_DIGITS = frozenset("0123456789abcdef")
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
        REF_FACTS, and the identity record at the current ref tip must
        match the host-issued identity exactly; any mismatch refuses
        the open and leaves the repository untouched.
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
        repo._expected_meta = expected
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
          outside the snapshot) are refused. A commit assembled outside
          build_tree() is held to exactly the same physical contract.

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
        self._validate_commit_tree(new_oid, expected_paths)
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
        try:
            listed = self._list_tree_entries(commit_oid)
        except (GitRepositoryError, UnicodeDecodeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit tree listing is unreadable: %s" % exc) \
                from exc
        blobs = []
        directories = []
        for mode, object_type, oid, path in listed:
            if object_type == "tree":
                if mode != "040000":
                    raise GitRepositoryError(
                        "accepted commit tree directory %r has mode %r, "
                        "not 040000" % (path, mode))
                directories.append(path)
                continue
            if object_type != "blob":
                raise GitRepositoryError(
                    "accepted commit tree entry %r is a %s object; only "
                    "regular file blobs and directories are accepted"
                    % (path, object_type))
            blobs.append((mode, path, oid))
        blob_paths = {path for _mode, path, _oid in blobs}
        for directory in directories:
            prefix = directory + "/"
            if not any(path.startswith(prefix) for path in blob_paths):
                raise GitRepositoryError(
                    "accepted commit tree directory %r holds no record; "
                    "empty or unexpected directories are refused"
                    % directory)
        entries = []
        for mode, path, oid in blobs:
            try:
                content = self._git_bytes(["cat-file", "blob", oid])
            except GitCommandError as exc:
                raise GitRepositoryError(
                    "accepted commit tree blob at %r is unreadable: %s"
                    % (path, exc)) from exc
            entries.append((mode, path, oid, content))
        # The complete tree may only carry paths a host generated from
        # the fixed record templates with canonically encoded IDs; a
        # path that is merely safe, or a decodable but non-canonical
        # escape alias of a legitimate ID, fails here even when it sits
        # inside the caller whitelist.
        for _mode, path, _oid, _content in entries:
            if git_format.canonical_record_type(path) is None:
                raise GitRepositoryError(
                    "accepted commit tree path %r is not the canonical "
                    "record_path() output of any fixed record template"
                    % (path,))
        try:
            git_tree_validation.validate_tree_entries(
                entries, expected_paths=expected_paths,
                object_format=self.object_format)
        except (TypeError, ValueError) as exc:
            raise GitRepositoryError(
                "accepted commit tree failed validation: %s" % exc) from exc

    def _list_tree_entries(self, commit_oid):
        # -z keeps paths raw (no quoting) with NUL terminators; -r
        # recurses and -t also lists the tree entries themselves, so an
        # empty or unexpected directory cannot hide between the blobs.
        data = self._git_bytes(["ls-tree", "-r", "-t", "-z", commit_oid])
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
                    "record at the authoritative tip" % current)
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
        return IntegrityReport(
            head_oid=head, genesis_oid=genesis, commit_count=count,
            object_format=self.object_format)

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
