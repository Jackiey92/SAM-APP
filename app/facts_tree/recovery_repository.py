"""Trusted bare repository bootstrap for F-001 isolated recovery.

A brand-new, empty, private bare repository is created inside the
host-pinned isolated parent; the package bundle is the *only* object
source. This module never runs a network command, never inherits
configuration, hooks, remotes, alternates or templates, creates the
authoritative ``refs/heads/facts`` ref exactly once through create-only
``update-ref --stdin`` semantics, and only then reopens the result with
the existing :class:`FactsGitRepository` read-only rule set. It opens no
runtime admission: producing this bare does not make the recovered tree
runnable.

Every Git invocation uses one absolute executable path handed in by the
host (signed configuration seam); ``PATH`` is never searched. argv is a
list built from literal tables in this module, the process environment
is scrubbed, and anything outside the fixed whitelist raises before a
process is spawned.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import stat
import subprocess

from app.facts_tree.git_repository import FactsGitRepository

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
_PATH_FLAGS = getattr(os, "O_PATH", 0) | os.O_NOFOLLOW | _O_CLOEXEC
_HEX = frozenset("0123456789abcdef")
_FORMATS = {"sha1": 40, "sha256": 64}
_TIMEOUT_SECONDS = 30
_FACTS_REF = "refs/heads/facts"
_REPO_META_FILE = "meta/repository.json"
_BARE_PREFIX = "sam-recovery-facts-"
_HOME_PREFIX = ".sam-recovery-git-home-"
_DIR_MODE = 0o700


class RecoveryRepositoryError(Exception):
    """Fixed reason codes only; subprocess text never reaches the message."""


@dataclass(frozen=True)
class TrustedBareReport:
    """Byte-level outcome of one bootstrap. No runnable/open-gate semantics."""

    path: str
    object_format: str
    head_oid: str
    genesis_oid: str
    commit_count: int
    object_count: int
    refs: tuple[tuple[str, str], ...]


def _hex(value, length):
    return (type(value) is str and len(value) == length
            and set(value) <= _HEX)


def _open_root(path):
    """Open an absolute directory pinned from `/`, refusing symlinks."""
    path = os.path.abspath(os.fspath(path))
    fd = os.open("/", _DIR_FLAGS)
    try:
        for segment in path.split("/")[1:]:
            if not segment:
                continue
            child = os.open(segment, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path
    except BaseException:
        os.close(fd)
        raise


def _validate_executable(executable):
    """Absolute, regular, non-symlink, executable; PATH is never consulted."""
    if type(executable) is not str or not os.path.isabs(executable) or "\x00" in executable:
        raise RecoveryRepositoryError("git_executable_invalid")
    try:
        pinned = os.open(executable, _PATH_FLAGS)
        try:
            info = os.fstat(pinned)
            if not stat.S_ISREG(info.st_mode):
                raise RecoveryRepositoryError("git_executable_invalid")
        finally:
            os.close(pinned)
    except OSError as exc:
        raise RecoveryRepositoryError("git_executable_invalid") from exc
    if not os.access(executable, os.X_OK):
        raise RecoveryRepositoryError("git_executable_invalid")


# Exact argv whitelist. Every tuple is the complete argument vector after
# the absolute executable; values are shape-checked at the call site, so a
# non-whitelisted command or option raises instead of spawning a process.
# This is an extension of the R4 read-only set with the minimum mutating
# commands a trusted bootstrap needs (init/config/update-ref/for-each-ref).
def _check_shape(args, stdin_kind):
    if type(args) is not list or not args or any(type(a) is not str for a in args):
        raise RecoveryRepositoryError("git_argv_invalid")
    head = args[0]
    if any(any(c in ("\x00", "\n", "\r") for c in arg) for arg in args):
        raise RecoveryRepositoryError("git_argv_invalid")
    if head == "init":
        if (len(args) != 6 or args[1] != "--bare"
                or args[2] not in {"--object-format=sha1", "--object-format=sha256"}
                or args[3] != "--template="
                or args[4] != "--initial-branch=facts"
                or not os.path.isabs(args[5])):
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "symbolic-ref":
        if args != ["symbolic-ref", "HEAD"]:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "config":
        if args[:3] == ["config", "--local", "--get-all"]:
            if len(args) != 4 or args[3] not in {"core.fsync", "core.fsyncMethod"}:
                raise RecoveryRepositoryError("git_argv_not_whitelisted")
        elif args == ["config", "--local", "--list"]:
            pass
        elif (len(args) == 4 and args[:2] == ["config", "--local"]
                and args[2] in {"core.fsync", "core.fsyncMethod"}):
            if args[2] == "core.fsync" and args[3] != "objects,reference":
                raise RecoveryRepositoryError("git_argv_not_whitelisted")
            if args[2] == "core.fsyncMethod" and args[3] != "fsync":
                raise RecoveryRepositoryError("git_argv_not_whitelisted")
        else:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "bundle":
        if (len(args) != 3 or args[1] not in {"verify", "list-heads", "unbundle"}
                or args[2] != "-" or stdin_kind != "bundle"):
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "update-ref":
        if args != ["update-ref", "--stdin"] or stdin_kind != "create":
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "for-each-ref":
        if args != ["for-each-ref", "--format=%(objectname) %(refname)"]:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "cat-file":
        if args == ["cat-file", "--batch-all-objects", "--batch-check"]:
            pass
        elif len(args) == 3 and args[1] == "-e" and args[2].endswith("^{commit}"):
            if not _hex(args[2][:-9], 40) and not _hex(args[2][:-9], 64):
                raise RecoveryRepositoryError("git_argv_not_whitelisted")
        elif len(args) == 3 and args[1] == "blob" and args[2].endswith(":" + _REPO_META_FILE):
            if not _hex(args[2].split(":", 1)[0], 40) and not _hex(
                    args[2].split(":", 1)[0], 64):
                raise RecoveryRepositoryError("git_argv_not_whitelisted")
        else:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "rev-list":
        if len(args) == 2:
            oid = args[1]
        elif len(args) == 3 and args[1] in {"--parents", "--max-parents=0", "--objects"}:
            oid = args[2]
        else:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
        if not (_hex(oid, 40) or _hex(oid, 64)):
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    elif head == "fsck":
        if len(args) != 4 or args[1:3] != ["--full", "--no-reflogs"]:
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
        if not (_hex(args[3], 40) or _hex(args[3], 64)):
            raise RecoveryRepositoryError("git_argv_not_whitelisted")
    else:
        raise RecoveryRepositoryError("git_argv_not_whitelisted")


# Configuration that must never appear in the local config of a recovered
# bare: network, include indirection, hooks, alternates, partial clone.
_FORBIDDEN_CONFIG_PREFIXES = (
    "remote.", "url.", "http.", "https.", "include.", "includeif.",
    "core.hookspath", "core.gitproxy", "core.alternaterefs",
    "core.alternateobjectformat", "extensions.partialclone",
    "extensions.promisor", "promisor.", "protocol.", "branch.",
    "submodule.", "fetch.", "pull.", "push.",
)


def _assert_local_config_clean(config_text):
    for line in config_text.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if any(key == prefix[:-1] or key.startswith(prefix)
               for prefix in _FORBIDDEN_CONFIG_PREFIXES):
            raise RecoveryRepositoryError("git_forbidden_local_config")
    # git canonicalises config keys to lower case on write, so --list
    # reports core.fsyncmethod even though the value was set camelCase.
    pairs = [line.partition("=")[::2] for line in config_text.splitlines()]
    fsync = [value for key, value in pairs if key.lower() == "core.fsync"]
    method = [value for key, value in pairs
              if key.lower() == "core.fsyncmethod"]
    if (len(fsync) != 1 or fsync[0] != "objects,reference"
            or len(method) != 1 or method[0] != "fsync"):
        raise RecoveryRepositoryError("git_fsync_policy_missing")


def _assert_no_offline_bypasses(bare_fd):
    """Physical sentinels: alternates, grafts, shallow, promisor packs.

    A dup of the bare fd anchors every coordinate walk, so a missing
    intermediate directory never consumes the caller's pinned descriptor.
    """
    for coordinate in ("objects/info/alternates", "objects/info/http-alternates",
                       "info/grafts", "shallow", "commondir"):
        parts = coordinate.split("/")
        parent = os.dup(bare_fd)
        try:
            for segment in parts[:-1]:
                child = os.open(segment, _DIR_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
            pinned = os.open(parts[-1], _PATH_FLAGS, dir_fd=parent)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise RecoveryRepositoryError("git_physical_layout_unsafe") from exc
        else:
            os.close(pinned)
            raise RecoveryRepositoryError("git_offline_bypass_present")
        finally:
            os.close(parent)
    pack_fd = os.dup(bare_fd)
    try:
        child = os.open("objects/pack", _DIR_FLAGS, dir_fd=pack_fd)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as exc:
        raise RecoveryRepositoryError("git_physical_layout_unsafe") from exc
    else:
        try:
            for name in os.listdir(child):
                if name.endswith(".promisor"):
                    raise RecoveryRepositoryError("git_promisor_pack_present")
        finally:
            os.close(child)
    finally:
        os.close(pack_fd)


class _Git:
    def __init__(self, executable, bare_path, home_path):
        self._executable = executable
        self._bare = bare_path
        self._home = home_path

    def _environment(self):
        # Scrub Git, proxy and prompt inputs; pin configuration sources to
        # one empty private HOME with a nonexistent global config file.
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")
               and "proxy" not in key.lower()
               and key not in {"HOME", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
                               "PATH", "TMPDIR", "TMP", "TEMP"}}
        env.update(
            HOME=self._home,
            XDG_CONFIG_HOME=self._home,
            XDG_CONFIG_DIRS=self._home,
            TMPDIR=self._home,
            TMP=self._home,
            TEMP=self._home,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.path.join(self._home, "nonexistent-config"),
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_NO_LAZY_FETCH="1",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="core.hooksPath",
            GIT_CONFIG_VALUE_0="/dev/null",
            GIT_CONFIG_KEY_1="protocol.allow",
            GIT_CONFIG_VALUE_1="never",
        )
        return env

    def run(self, args, *, bundle_fd=None, create_ref=None):
        stdin_kind = None
        if bundle_fd is not None and create_ref is not None:
            raise RecoveryRepositoryError("git_stdin_invalid")
        if bundle_fd is not None:
            stdin_kind = "bundle"
        elif create_ref is not None:
            stdin_kind = "create"
        _check_shape(args, stdin_kind)
        if create_ref is not None:
            if not _hex(create_ref, 40) and not _hex(create_ref, 64):
                raise RecoveryRepositoryError("git_argv_invalid")
            payload = ("create %s %s\n" % (_FACTS_REF, create_ref)).encode("ascii")
            # input= alone implies stdin=PIPE; subprocess forbids passing both.
            stdin = None
            input_bytes = payload
        else:
            stdin = bundle_fd if bundle_fd is not None else subprocess.DEVNULL
            input_bytes = None
        if bundle_fd is not None:
            os.lseek(bundle_fd, 0, os.SEEK_SET)
        try:
            result = subprocess.run(
                [self._executable, *args],
                cwd=self._bare,
                env=self._environment(),
                stdin=stdin,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecoveryRepositoryError("git_invocation_failed") from exc
        if result.returncode != 0:
            raise RecoveryRepositoryError("git_command_failed")
        return result.stdout.decode("utf-8", errors="strict")


def _commit_identity(meta, identity):
    expected = {"schema_version": 1, "format_version": 1,
                "repo_id": identity["repo_id"],
                "installation_id": identity["installation_id"],
                "company_id": identity["company_id"]}
    return meta == expected


def bootstrap_trusted_bare(*, isolated_parent, bundle_fd, head_oid, pin_ref,
                           genesis_oid, commit_count, object_format, identity,
                           git_executable, post_unbundle_seam=None):
    """Create the trusted bare from the fixed bundle and reopen it read-only.

    ``post_unbundle_seam(bare_path)`` is a TEST-ONLY fault-injection seam
    (smuggled loose object / planted alternates); production callers never
    pass it. On any failure the freshly created bare is moved aside under a
    non-reusable abandoned name; an existing directory is never reused.
    """
    if object_format not in _FORMATS or not _hex(head_oid, _FORMATS[object_format]):
        raise RecoveryRepositoryError("head_anchor_invalid")
    if not _hex(genesis_oid, _FORMATS[object_format]):
        raise RecoveryRepositoryError("head_anchor_invalid")
    if type(commit_count) is not int or commit_count < 1:
        raise RecoveryRepositoryError("head_anchor_invalid")
    if (type(pin_ref) is not str or not pin_ref.startswith("refs/")
            or any(c in pin_ref for c in " \x00\n")):
        raise RecoveryRepositoryError("head_anchor_invalid")
    if (not isinstance(identity, dict)
            or not all(type(identity.get(k)) is str and identity[k]
                       for k in ("repo_id", "installation_id", "company_id"))):
        raise RecoveryRepositoryError("identity_invalid")
    if post_unbundle_seam is not None and not callable(post_unbundle_seam):
        raise RecoveryRepositoryError("seam_invalid")
    _validate_executable(git_executable)

    parent_fd, parent_path = _open_root(isolated_parent)
    nonce = os.urandom(6).hex()
    bare_name = _BARE_PREFIX + nonce + ".git"
    home_name = _HOME_PREFIX + nonce
    bare_created = False
    home_created = False
    try:
        try:
            os.mkdir(bare_name, _DIR_MODE, dir_fd=parent_fd)
            bare_created = True
            os.mkdir(home_name, _DIR_MODE, dir_fd=parent_fd)
            home_created = True
            os.fsync(parent_fd)
        except OSError as exc:
            raise RecoveryRepositoryError("bare_create_failed") from exc
        bare_path = os.path.join(parent_path, bare_name)
        home_path = os.path.join(parent_path, home_name)
        bare_fd = os.open(bare_name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            info = os.fstat(bare_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise RecoveryRepositoryError("bare_layout_invalid")
        finally:
            os.close(bare_fd)
        git = _Git(git_executable, bare_path, home_path)

        git.run(["init", "--bare", "--object-format=" + object_format,
                 "--template=", "--initial-branch=facts", bare_path])
        # Re-read the unborn HEAD the new repository actually carries; the
        # explicit init flag must not have been flipped by inherited config.
        symbolic = git.run(["symbolic-ref", "HEAD"]).strip()
        if symbolic != _FACTS_REF:
            raise RecoveryRepositoryError("bare_initial_branch_invalid")
        git.run(["config", "--local", "core.fsync", "objects,reference"])
        git.run(["config", "--local", "core.fsyncMethod", "fsync"])
        for key in ("core.fsync", "core.fsyncMethod"):
            values = git.run(["config", "--local", "--get-all", key]).splitlines()
            if len(values) != 1:
                raise RecoveryRepositoryError("git_fsync_policy_missing")
        _assert_local_config_clean(git.run(["config", "--local", "--list"]))
        sentinel_fd = os.open(bare_name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            _assert_no_offline_bypasses(sentinel_fd)
        finally:
            os.close(sentinel_fd)

        heads_text = git.run(["bundle", "list-heads", "-"], bundle_fd=bundle_fd)
        git.run(["bundle", "verify", "-"], bundle_fd=bundle_fd)
        heads = [line for line in heads_text.splitlines() if line.strip()]
        if len(heads) != 1 or heads[0] != "%s %s" % (head_oid, pin_ref):
            raise RecoveryRepositoryError("bundle_head_not_authoritative")
        git.run(["bundle", "unbundle", "-"], bundle_fd=bundle_fd)

        git.run(["cat-file", "-e", head_oid + "^{commit}"])
        git.run(["fsck", "--full", "--no-reflogs", head_oid])
        commits = git.run(["rev-list", head_oid]).splitlines()
        parents = git.run(["rev-list", "--parents", head_oid]).splitlines()
        roots = git.run(["rev-list", "--max-parents=0", head_oid]).splitlines()
        if any(len(line.split()) > 2 for line in parents):
            raise RecoveryRepositoryError("commit_chain_not_linear")
        if len(commits) != commit_count or roots != [genesis_oid]:
            raise RecoveryRepositoryError("commit_chain_anchor_mismatch")
        for commit in commits:
            raw = git.run(["cat-file", "blob", commit + ":" + _REPO_META_FILE])
            try:
                meta = json.loads(raw)
            except ValueError as exc:
                raise RecoveryRepositoryError("commit_identity_invalid") from exc
            if not _commit_identity(meta, identity):
                raise RecoveryRepositoryError("commit_identity_mismatch")

        # TEST-ONLY seam runs after unbundle, before every post-unbundle
        # guard; production callers leave it unset.
        if post_unbundle_seam is not None:
            post_unbundle_seam(bare_path)

        config_fd = os.open(bare_name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            _assert_no_offline_bypasses(config_fd)
        finally:
            os.close(config_fd)
        _assert_local_config_clean(git.run(["config", "--local", "--list"]))

        all_objects = {
            line.split(" ", 1)[0]
            for line in git.run(
                ["cat-file", "--batch-all-objects", "--batch-check"]).splitlines()
            if line.strip()
        }
        reachable = {
            line.split(" ", 1)[0]
            for line in git.run(["rev-list", "--objects", head_oid]).splitlines()
            if line.strip()
        }
        if all_objects != reachable or not all_objects:
            raise RecoveryRepositoryError("object_set_not_exact")

        git.run(["update-ref", "--stdin"], create_ref=head_oid)
        refs_text = git.run(["for-each-ref", "--format=%(objectname) %(refname)"])
        refs = tuple(tuple(line.split(" ", 1)) for line in refs_text.splitlines()
                     if line.strip())
        # for-each-ref format is objectname then refname; report stores
        # (refname, oid) to match the public TrustedBareReport shape.
        if refs != ((head_oid, _FACTS_REF),):
            raise RecoveryRepositoryError("refs_not_exact")
        config_fd = os.open(bare_name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            _assert_no_offline_bypasses(config_fd)
            os.fsync(config_fd)
        finally:
            os.close(config_fd)

        # Only after the create-only ref exists may the shared read-only
        # adapter open the bare; initialize() (genesis creation) is never used.
        try:
            repo = FactsGitRepository.open(
                bare_path,
                repo_id=identity["repo_id"],
                installation_id=identity["installation_id"],
                company_id=identity["company_id"])
            report = repo.verify_integrity(expected_head=head_oid)
        except RecoveryRepositoryError:
            raise
        except Exception as exc:
            raise RecoveryRepositoryError("reopen_verification_failed") from exc
        if (report.object_format != object_format
                or report.head_oid != head_oid
                or report.genesis_oid != genesis_oid
                or report.commit_count != commit_count):
            raise RecoveryRepositoryError("reopen_report_mismatch")
        return TrustedBareReport(
            path=bare_path,
            object_format=object_format,
            head_oid=head_oid,
            genesis_oid=genesis_oid,
            commit_count=commit_count,
            object_count=len(all_objects),
            refs=((_FACTS_REF, head_oid),))
    except BaseException as exc:
        abandoned = None
        if bare_created:
            abandoned = _BARE_PREFIX + nonce + ".abandoned.git"
            try:
                os.rename(bare_name, abandoned, src_dir_fd=parent_fd,
                          dst_dir_fd=parent_fd)
                bare_created = False
            except OSError:
                abandoned = None
        if home_created and abandoned is not None:
            try:
                os.rename(home_name, os.path.join(abandoned, ".scratch-home"),
                          src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                home_created = False
            except OSError:
                pass
        if abandoned is not None:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit,
                            RecoveryRepositoryError)):
            raise
        raise RecoveryRepositoryError("bootstrap_failed") from exc
    finally:
        if home_created:
            try:
                os.rmdir(home_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
