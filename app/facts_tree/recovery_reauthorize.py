"""D: out-of-band evidence merge and owner recovery reauthorization.

Offline-only assembler for F-001 recovery. Slice C built the trusted
bare, created the case and staged materials; the case waits at
``materials_staged``. This module reads a physically separate,
out-of-band handoff bundle from its own pinned root:

* per-operation independent ledger observations (the merge set A),
* the host's weak backup-state claims for both sets, including the
  keys the host claims clean and did not put up for merge (set B),
* the owner's post-disaster out-of-band recovery authentication,
* per-credential descriptors that explicitly name the merge keys or
  sessions each old credential may be judged against.

It advances one current-epoch case exactly
``materials_staged -> evidence_merged -> reauthorized`` through R5's
public :class:`RecoveryStateStore` write surface and writes one receipt
of its own into a separate receipt root. Hard order (freeze revB section 4):

    full deterministic merge (all A keys)
      -> authenticate            (failure: merges kept, ZERO transitions)
      -> read-only credential evaluation
      -> transition evidence_merged   (this gate's single proof only)
      -> transition reauthorized      (this gate's single proof only)
      -> this module's receipt

D performs no positive authorization: ``MergeDecision.usable`` is always
False, credentials are only reported denied/pending_evaluation, no token
body is ever read or changed, no read/tasks/effects gate is opened, no
network/git/subprocess is touched, and D is never imported by a startup,
host, Broker or webapp path.

Other enforced rules (freeze revB invariants 1-16):

* fail closed: every shape/digest/set/phase/epoch/anchor/lock check that
  can refuse the run completes before the first journal write;
* roots are pinned segment by segment with O_DIRECTORY|O_NOFOLLOW and
  compared by fstat (st_dev, st_ino) identity plus an openat("..")
  ancestor walk on those same fds - no realpath, no reopen;
* the physical items set must equal the manifest-declared set exactly
  (extra file, missing file, symlink, special file, nested directory or
  a second hard link all refuse);
* credential tombstones are the union of restrictions of ONLY the
  explicitly associated merged keys/sessions; missing, ambiguous or
  unresolvable associations deny; the empty frozenset is used solely
  when the descriptor explicitly declares no operation association;
* C's existing ``recovery-materialize.lock`` is opened O_RDONLY|O_NOFOLLOW
  relative to the pinned state fd (never O_CREAT: a missing lock means C
  never ran) and flock'ed LOCK_EX|NB; the open is read-only but flock is an
  inode-level exclusive writer lock, so a second D is still refused. O_EXCL
  is used only for the receipt temp file;
* idempotency: re-entering at materials_staged (crash mid-merge/after
  authenticate), evidence_merged (crash between the two transitions) or
  reauthorized (same-input rerun) reconciles against the live view and
  skips every equal merge/authentication/proof, so a terminal rerun
  appends zero journal events and never duplicates the receipt;
* ``trusted_anchor`` is mandatory and pins the journal at construction;
* cross-process void rule for receipts: any ``.tmp-reauthorize-*`` file
  is killed-process residue, never adopted; it is unlinked at publish
  time and only the completed final receipt counts as success.

``seam`` is a TEST-ONLY keyword hook. Production callers never pass it.
It fires ``merge:N`` after the Nth newly appended merge commits,
``authenticated`` after a newly appended authentication commits,
``evidence_merged`` after that transition commits, and ``receipt_tmp``
after the receipt temp file was created and fsynced, before its atomic
rename. The crash tests send a real SIGKILL to a real subprocess there
instead of faking an in-process exception.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from app.facts_tree.backup_component_validation import (
    verify_backup_component,
)
from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState, IndependentEvidence, MergeDecision,
    OwnerRecoveryAuthentication, RecoveryPhase, evaluate_restored_credential,
    merge_evidence, require_digest, require_label,
)
# C's pinned file primitives, constants and digests are reused as-is.
# Opening paths any other way would reintroduce the symlink/TOCTOU
# surface C closed; duplicating its canonical/proof helpers would only
# drift from it.
from app.facts_tree.recovery_materializer import (
    MaterialInstallError, _DIR_FLAGS, _LOCK_NAME, _PATH_FLAGS,
    _STATE_DB_NAME, _canonical, _create_locked_at, _open_abs, _open_file,
    _proof,
)
from app.facts_tree.recovery_state_store import (
    RecoveryStateError, RecoveryStateStore,
)

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_ENVELOPE_SCHEMA = "sam.f001.d.handoff.v1"
_ENVELOPE_VERSION = 1
_RECEIPT_SCHEMA = "sam.f001.d.receipt.v1"
_RECEIPT_VERSION = 1
_HANDOFF_NAME = "handoff.json"
_ITEMS_DIR = "items"
_RECEIPT_PREFIX = "reauthorize-"
_RECEIPT_SUFFIX = ".json"
_TMP_PREFIX = ".tmp-" + _RECEIPT_PREFIX
_PROOF_KIND_EVIDENCE = "sam.f001.recovery.reauthorize.proof.v1:evidence_merged"
_PROOF_KIND_REAUTH = (
    "sam.f001.recovery.reauthorize.proof.v1:current_authorization")
_CLAIM_TAG = "sam.f001.d.weak-claim.v1"

_ADVISORIES = (
    "merge_is_never_authorization:every MergeDecision.usable is False; "
    "D opens no read/tasks/effects gate",
    "clean_set_trace_only:host-claimed-clean keys bind no decision and "
    "receive no authorization from D; E/F/I audit host under-reporting",
    "credential_epoch_ownership_unverified:credential_epoch_id passes "
    "shape validation only and is never compared to the recovery epoch",
    "no_credential_content_seen:descriptors name associations only; "
    "D never reads, validates or changes a token body",
    "external_isolation_and_rotation_unverified",
)


class ReauthorizeError(Exception):
    """Fixed reason codes; hostile file or subprocess text never escapes."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReauthorizeLayout:
    """The three layout roots C established; the handoff arrives separately.

    state_root is C's state directory (journal plus its persistent lock),
    bare_root is the trusted bare parent (pinned for isolation only; D
    never opens the bare), receipt_root is D's own append directory.
    """

    state_root: str
    bare_root: str
    receipt_root: str


@dataclass(frozen=True)
class FileRef:
    name: str
    size: int
    sha256: str  # "sha256:" + 64 lowercase hex


@dataclass(frozen=True)
class MergeReport:
    key: EvidenceKey
    backup_states: tuple[EvidenceState, ...]
    items: tuple[FileRef, ...]
    restrictions: tuple[EvidenceState, ...]
    pending: bool
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class CleanReport:
    key: EvidenceKey
    backup_states: tuple[EvidenceState, ...]
    claim_digest: str


@dataclass(frozen=True)
class CredentialReport:
    file: FileRef
    credential_epoch_id: str
    owner_id: str
    no_operation_association: bool
    associations: tuple[tuple[str, str], ...]
    matched_keys: tuple[tuple[str, str, str], ...]
    status: str
    reason: str
    tombstones: tuple[EvidenceState, ...]


@dataclass(frozen=True)
class ReauthorizationReceipt:
    case_id: str
    phase: str
    manifest_digest: str
    journal_anchor_before: tuple[int, str]
    journal_anchor_after: tuple[int, str]
    proofs: tuple[tuple[str, str], ...]
    merges: tuple[MergeReport, ...]
    clean: tuple[CleanReport, ...]
    credentials: tuple[CredentialReport, ...]
    receipt_path: str
    receipt_sha256: str


# ------------------------------------------------------------- small helpers

def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sorted_states(states):
    return tuple(sorted(states, key=lambda state: state.value))


def _key_triple(key):
    return [key.task_id, key.approval_id, key.operation_id]


def _json_bytes(root_fd, relative, reason):
    """Read one pinned regular file and parse JSON; duplicate keys refuse."""
    try:
        fd = _open_file(root_fd, relative)
    except MaterialInstallError as exc:
        raise ReauthorizeError(reason) from exc
    try:
        data = b""
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise ReauthorizeError(reason) from exc
    finally:
        os.close(fd)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReauthorizeError(reason)
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs), data
    except ValueError as exc:
        raise ReauthorizeError(reason) from exc


def _exact_keys(value, keys, context):
    if type(value) is not dict or set(value) != set(keys):
        raise ReauthorizeError("handoff_envelope_malformed:" + context)


def _str_field(value, context):
    if type(value) is not str:
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    return value


def _item_name(value, context):
    """A flat items/ basename: require_label shape, no slash semantics."""
    if type(value) is not str:
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    try:
        require_label(value)
    except ValueError as exc:
        raise ReauthorizeError("handoff_envelope_malformed:" + context) from exc
    if "/" in value or value in (".", "..") or value.startswith("."):
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    return value


def _key_object(value, context):
    _exact_keys(value, ("task_id", "approval_id", "operation_id"), context)
    try:
        return EvidenceKey(
            _str_field(value["task_id"], context),
            _str_field(value["approval_id"], context),
            _str_field(value["operation_id"], context))
    except ValueError as exc:
        raise ReauthorizeError("handoff_envelope_malformed:" + context) from exc


def _state_list(value, context):
    if type(value) is not list:
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    if any(type(item) is not str for item in value):
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    try:
        states = tuple(EvidenceState(item) for item in value)
    except ValueError as exc:
        raise ReauthorizeError("handoff_envelope_malformed:" + context) from exc
    if len(set(states)) != len(states):
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    return _sorted_states(states)


def _file_ref(value, context):
    _exact_keys(value, ("name", "size", "sha256"), context)
    name = _item_name(value["name"], context)
    if type(value["size"]) is not int or value["size"] < 0:
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    digest = value["sha256"]
    if type(digest) is not str or not digest.startswith("sha256:") \
            or len(digest) != 71:
        raise ReauthorizeError("handoff_envelope_malformed:" + context)
    try:
        require_digest(digest[7:])
    except ValueError as exc:
        raise ReauthorizeError("handoff_envelope_malformed:" + context) from exc
    return FileRef(name, value["size"], digest)


# ------------------------------------------------------------- root pinning

def _pin_root(path, context):
    try:
        fd, pinned = _open_abs(path)
    except (MaterialInstallError, OSError) as exc:
        raise ReauthorizeError("layout_root_missing:" + context) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ReauthorizeError("layout_root_not_directory:" + context)
    except ReauthorizeError:
        os.close(fd)
        raise
    return fd, pinned


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _ancestor_identities(fd):
    """fstat identities of every parent via openat(".."); no realpath.

    The walk starts from the pinned fd's own descriptor and only ever
    reopens "..", so a renamed or re-symlinked path string cannot
    influence the result.
    """
    current = os.dup(fd)
    ancestors = []
    try:
        while True:
            here = os.fstat(current)
            parent = os.open("..", _DIR_FLAGS, dir_fd=current)
            up = os.fstat(parent)
            if (up.st_dev, up.st_ino) == (here.st_dev, here.st_ino):
                os.close(parent)
                break
            ancestors.append((up.st_dev, up.st_ino))
            os.close(current)
            current = parent
    finally:
        os.close(current)
    return tuple(ancestors)


def _assert_roots_isolated(roots):
    identities = {name: _identity(fd) for name, fd in roots}
    ancestors = {name: _ancestor_identities(fd) for name, fd in roots}
    if len(set(identities.values())) != len(identities):
        raise ReauthorizeError("layout_roots_overlap")
    for name, _fd in roots:
        for other, _ofd in roots:
            if other != name and identities[name] in ancestors[other]:
                raise ReauthorizeError(
                    "layout_roots_nested:" + name + "-in-" + other)


def _entry_map(root_fd, context):
    """Pin every direct child O_PATH; symlinks and special files refuse."""
    entries = {}
    for name in os.listdir(root_fd):
        # O_PATH|O_NOFOLLOW on a symlink leaf fails with ELOOP instead of
        # returning a pin, so map that refusal onto the fixed reason too:
        # hostile entry names/text must never escape as a raw OSError.
        try:
            pinned = os.open(name, _PATH_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise ReauthorizeError(
                "input_root_unsafe_entry:" + context + ":" + name) from exc
        try:
            info = os.fstat(pinned)
        finally:
            os.close(pinned)
        mode = info.st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ReauthorizeError(
                "input_root_unsafe_entry:" + context + ":" + name)
        entries[name] = info
    return entries


# ------------------------------------------------------------- handoff plan

@dataclass(frozen=True)
class _MergeBlock:
    key: EvidenceKey
    backup: tuple[EvidenceState, ...]
    items: tuple[FileRef, ...]
    evidence: tuple[IndependentEvidence, ...]
    decision: MergeDecision


@dataclass(frozen=True)
class _CleanBlock:
    key: EvidenceKey
    backup: tuple[EvidenceState, ...]
    claim_digest: str


@dataclass(frozen=True)
class _CredentialSpec:
    file: FileRef
    credential_epoch_id: str
    owner_id: str
    no_operation_association: bool
    associations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Plan:
    case_id: str
    backup_id: str
    barrier_digest: str
    manifest_digest: str
    domain: tuple[EvidenceKey, ...]
    merge_blocks: tuple[_MergeBlock, ...]
    clean_blocks: tuple[_CleanBlock, ...]
    authentication_file: FileRef
    authentication: OwnerRecoveryAuthentication
    credentials: tuple[_CredentialSpec, ...]


def _verify_component(items_fd, relative, ref, context):
    try:
        verify_backup_component(
            items_fd, relative_path=relative, size_bytes=ref.size,
            sha256_digest=ref.sha256)
    except ValueError as exc:
        raise ReauthorizeError("evidence_digest_mismatch:" + context) from exc


def _load_evidence_item(items_fd, ref, key, context):
    # items_fd is already pinned inside items/, so the relative name is
    # the bare leaf; another "items/" prefix would resolve to items/items/.
    full = ref.name
    _verify_component(items_fd, full, ref, context + ":" + ref.name)
    parsed, raw = _json_bytes(items_fd, full,
                              "evidence_item_malformed:" + ref.name)
    if _sha256_hex(raw) != ref.sha256:
        # The component pin already proved size+sha256 of the inode it
        # opened; re-hashing the exact bytes we parsed closes the gap
        # between that read and this one.
        raise ReauthorizeError("evidence_digest_mismatch:" + ref.name)
    _exact_keys(parsed, ("source_id", "scope", "ledger_id", "revocation_seq",
                         "event_id", "event_digest", "state", "session_id",
                         "policy_revision"), context + ":" + ref.name)
    try:
        return IndependentEvidence(
            source_id=_str_field(parsed["source_id"], context),
            scope=_str_field(parsed["scope"], context),
            ledger_id=_str_field(parsed["ledger_id"], context),
            revocation_seq=parsed["revocation_seq"],
            event_id=_str_field(parsed["event_id"], context),
            event_digest=_str_field(parsed["event_digest"], context),
            key=key,
            state=EvidenceState(_str_field(parsed["state"], context)),
            session_id=(None if parsed["session_id"] is None
                        else _str_field(parsed["session_id"], context)),
            policy_revision=parsed["policy_revision"])
    except (ValueError, TypeError) as exc:
        raise ReauthorizeError("evidence_item_malformed:" + ref.name) from exc


def _load_credential(items_fd, ref, context):
    full = ref.name
    _verify_component(items_fd, full, ref, context + ":" + ref.name)
    parsed, raw = _json_bytes(items_fd, full,
                              "credential_descriptor_malformed:" + ref.name)
    if _sha256_hex(raw) != ref.sha256:
        raise ReauthorizeError("evidence_digest_mismatch:" + ref.name)
    _exact_keys(parsed, ("credential_epoch_id", "owner_id",
                         "no_operation_association", "associations"),
                context + ":" + ref.name)
    cred_id = _str_field(parsed["credential_epoch_id"], context)
    owner_id = _str_field(parsed["owner_id"], context)
    explicit = parsed["no_operation_association"]
    if type(explicit) is not bool:
        raise ReauthorizeError(
            "credential_descriptor_malformed:" + ref.name)
    raw_associations = parsed["associations"]
    if type(raw_associations) is not list:
        raise ReauthorizeError(
            "credential_descriptor_malformed:" + ref.name)
    associations = []
    for entry in raw_associations:
        if type(entry) is not dict or len(entry) != 1:
            raise ReauthorizeError(
                "credential_association_malformed:" + ref.name)
        (kind, value), = entry.items()
        if kind not in ("operation_id", "session_id"):
            raise ReauthorizeError(
                "credential_association_malformed:" + ref.name)
        value = _str_field(value, "credential_association:" + ref.name)
        try:
            require_label(value)
        except ValueError as exc:
            raise ReauthorizeError(
                "credential_association_malformed:" + ref.name) from exc
        pair = (kind, value)
        if pair in associations:
            raise ReauthorizeError(
                "credential_association_ambiguous:" + ref.name)
        associations.append(pair)
    associations = tuple(associations)
    if explicit and associations:
        raise ReauthorizeError(
            "credential_association_conflicts_with_none:" + ref.name)
    if not explicit and not associations:
        # Fail closed: neither an explicit no-association declaration nor
        # an association list may degrade into an empty frozenset.
        raise ReauthorizeError("credential_association_missing:" + ref.name)
    try:
        require_label(cred_id)
        require_label(owner_id)
    except ValueError as exc:
        raise ReauthorizeError(
            "credential_descriptor_malformed:" + ref.name) from exc
    return _CredentialSpec(ref, cred_id, owner_id, explicit, associations)


def _claim_digest(key, backup):
    return _sha256_hex(_canonical(
        [_CLAIM_TAG, key.task_id, key.approval_id, key.operation_id,
         tuple(state.value for state in backup)]).encode("ascii"))


def _load_plan(input_fd):
    root_entries = _entry_map(input_fd, "handoff-root")
    if set(root_entries) != {_HANDOFF_NAME, _ITEMS_DIR}:
        raise ReauthorizeError("input_root_set_not_exact")
    handoff_info = root_entries[_HANDOFF_NAME]
    if not stat.S_ISREG(handoff_info.st_mode) or handoff_info.st_nlink != 1:
        raise ReauthorizeError("input_root_unsafe_entry:handoff.json")
    items_info = root_entries[_ITEMS_DIR]
    if not stat.S_ISDIR(items_info.st_mode):
        raise ReauthorizeError("input_root_unsafe_entry:items")
    items_fd = os.open(_ITEMS_DIR, _DIR_FLAGS, dir_fd=input_fd)
    try:
        physical = _entry_map(items_fd, "items")
        for name, info in physical.items():
            if stat.S_ISDIR(info.st_mode):
                raise ReauthorizeError(
                    "input_root_unsafe_entry:items-nesting:" + name)
            if info.st_nlink != 1:
                raise ReauthorizeError(
                    "input_root_hardlink_refused:items:" + name)
        manifest, manifest_raw = _json_bytes(
            input_fd, _HANDOFF_NAME, "handoff_envelope_malformed")
        manifest_digest = _sha256_hex(manifest_raw)
        _exact_keys(manifest, ("schema", "version", "case_id", "backup_id",
                               "barrier_digest", "domain", "merge_keys",
                               "clean_keys", "authentication", "credentials"),
                    "manifest")
        if manifest["schema"] != _ENVELOPE_SCHEMA \
                or type(manifest["version"]) is not int \
                or manifest["version"] != _ENVELOPE_VERSION:
            raise ReauthorizeError("handoff_envelope_version_unsupported")
        case_id = _str_field(manifest["case_id"], "case_id")
        backup_id = _str_field(manifest["backup_id"], "backup_id")
        barrier = manifest["barrier_digest"]
        if type(barrier) is not str:
            raise ReauthorizeError(
                "handoff_envelope_malformed:barrier_digest")
        try:
            require_label(case_id)
            require_label(backup_id)
            require_digest(barrier)
        except ValueError as exc:
            raise ReauthorizeError(
                "handoff_envelope_malformed:binding") from exc

        raw_domain = manifest["domain"]
        if type(raw_domain) is not list or not raw_domain:
            raise ReauthorizeError("handoff_envelope_malformed:domain")
        domain = tuple(_key_object(item, "domain") for item in raw_domain)
        if len(set(domain)) != len(domain):
            raise ReauthorizeError("handoff_envelope_malformed:domain")

        declared_names = set()

        def claim_name(ref, context):
            if ref.name in declared_names:
                raise ReauthorizeError(
                    "handoff_envelope_malformed:" + context)
            declared_names.add(ref.name)

        if type(manifest["merge_keys"]) is not list \
                or type(manifest["clean_keys"]) is not list:
            raise ReauthorizeError("handoff_envelope_malformed:sets")
        merge_blocks = []
        merge_key_set = set()
        for index, block in enumerate(manifest["merge_keys"]):
            context = "merge_keys:%d" % index
            _exact_keys(block, ("key", "backup_states", "items"), context)
            key = _key_object(block["key"], context)
            if key in merge_key_set:
                raise ReauthorizeError(
                    "handoff_envelope_malformed:" + context)
            merge_key_set.add(key)
            backup = _state_list(block["backup_states"], context)
            if type(block["items"]) is not list:
                raise ReauthorizeError(
                    "handoff_envelope_malformed:" + context)
            refs = tuple(sorted(
                (_file_ref(item, context) for item in block["items"]),
                key=lambda ref: ref.name))
            for ref in refs:
                claim_name(ref, context)
            evidence = tuple(
                _load_evidence_item(items_fd, ref, key, context)
                for ref in refs)
            # Dry-run through R5 now so malformed combinations refuse
            # before any journal write exists. This case's first merge
            # event for a key is replayed by the store with no previous
            # (an inherited decision is a different key history and is
            # reconciled as a conflict below, never silently composed).
            try:
                decision = merge_evidence(key, backup, evidence)
            except ValueError as exc:
                raise ReauthorizeError(
                    "evidence_item_malformed:" + context) from exc
            merge_blocks.append(
                _MergeBlock(key, backup, refs, evidence, decision))
        clean_blocks = []
        clean_key_set = set()
        for index, block in enumerate(manifest["clean_keys"]):
            context = "clean_keys:%d" % index
            _exact_keys(block, ("key", "backup_states"), context)
            key = _key_object(block["key"], context)
            if key in clean_key_set:
                raise ReauthorizeError(
                    "handoff_envelope_malformed:" + context)
            clean_key_set.add(key)
            backup = _state_list(block["backup_states"], context)
            clean_blocks.append(
                _CleanBlock(key, backup, _claim_digest(key, backup)))

        if merge_key_set & clean_key_set:
            raise ReauthorizeError("handoff_sets_overlap")
        if set(domain) != merge_key_set | clean_key_set:
            raise ReauthorizeError("handoff_domain_not_covered")

        auth_ref = _file_ref(manifest["authentication"], "authentication")
        claim_name(auth_ref, "authentication")
        auth_full = auth_ref.name
        _verify_component(items_fd, auth_full, auth_ref, "authentication")
        auth_parsed, auth_raw = _json_bytes(
            items_fd, auth_full, "authentication_malformed")
        if _sha256_hex(auth_raw) != auth_ref.sha256:
            raise ReauthorizeError("evidence_digest_mismatch:authentication")
        _exact_keys(auth_parsed, ("instance_id", "epoch_id", "owner_id",
                                  "record_digest", "member_digest",
                                  "policy_digest", "task_digest"),
                    "authentication")
        try:
            authentication = OwnerRecoveryAuthentication(
                **{key: _str_field(auth_parsed[key], "authentication")
                   for key in ("instance_id", "epoch_id", "owner_id",
                               "record_digest", "member_digest",
                               "policy_digest", "task_digest")})
        except (ValueError, TypeError) as exc:
            raise ReauthorizeError("authentication_malformed") from exc

        if type(manifest["credentials"]) is not list:
            raise ReauthorizeError(
                "handoff_envelope_malformed:credentials")
        credentials = []
        for index, entry in enumerate(manifest["credentials"]):
            context = "credentials:%d" % index
            ref = _file_ref(entry, context)
            claim_name(ref, context)
            credentials.append(_load_credential(items_fd, ref, context))

        if set(physical) != declared_names:
            raise ReauthorizeError("input_root_set_not_exact")

        return _Plan(
            case_id=case_id, backup_id=backup_id, barrier_digest=barrier,
            manifest_digest=manifest_digest,
            domain=tuple(sorted(domain)),
            merge_blocks=tuple(sorted(merge_blocks, key=lambda b: b.key)),
            clean_blocks=tuple(sorted(clean_blocks, key=lambda b: b.key)),
            authentication_file=auth_ref, authentication=authentication,
            credentials=tuple(credentials))
    finally:
        os.close(items_fd)


# ---------------------------------------------------------------- lock/store

def _probe_regular(dir_fd, name):
    try:
        pinned = os.open(name, _PATH_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(pinned).st_mode)
    finally:
        os.close(pinned)


def _acquire_lock(state_fd):
    """Open C's existing lock (never create it), then flock exclusive NB."""
    try:
        # O_RDONLY open, same relative-open convention as C's reverify
        # (recovery_materializer.py): flock(LOCK_EX) serializes writers at
        # the inode regardless of the open mode. The isolation audit hook
        # does not resolve dir_fd, so a write-mode relative open reads as a
        # write to the bare lock name outside the temp root and is refused.
        lock_fd = os.open(_LOCK_NAME,
                          os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC,
                          dir_fd=state_fd)
    except FileNotFoundError as exc:
        raise ReauthorizeError("single_writer_lock_absent") from exc
    except OSError as exc:
        raise ReauthorizeError("single_writer_lock_unsafe") from exc
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReauthorizeError("single_writer_lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReauthorizeError("single_writer_lock_held") from exc
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def _valid_anchor(anchor):
    return (type(anchor) is tuple and len(anchor) == 2
            and type(anchor[0]) is int and type(anchor[1]) is str
            and ((anchor[0] == 0 and anchor[1] == "")
                 or (anchor[0] > 0 and len(anchor[1]) == 64)))


def _open_state_store(state_path, anchor):
    if not _valid_anchor(anchor):
        raise ReauthorizeError("trusted_anchor_invalid")
    try:
        return RecoveryStateStore(Path(state_path), trusted_anchor=anchor)
    except RecoveryStateError:
        # Classify without writing: a damaged journal and a wrong host
        # pin are different refusals. The db existence probe upstream
        # guarantees this reopen cannot create one.
        try:
            probe = RecoveryStateStore(Path(state_path))
        except RecoveryStateError as exc:
            raise ReauthorizeError("state_journal_unavailable") from exc
        try:
            actual = probe.journal_anchor()
        except RecoveryStateError as exc:
            raise ReauthorizeError("state_journal_unavailable") from exc
        finally:
            probe.close()
        if actual != anchor:
            raise ReauthorizeError("journal_anchor_mismatch")
        raise ReauthorizeError("state_journal_unavailable")


# --------------------------------------------------------- proof discipline

_TARGET_PROOF = {
    RecoveryPhase.EVIDENCE_MERGED: "evidence_merged",
    RecoveryPhase.REAUTHORIZED: "current_authorization",
}


def _guard_proof(target, pair):
    """D self-discipline: exactly this gate's one proof name, no futures.

    The store treats supplied proof names as a required subset and does
    not reject extra names, so this one-gate-one-name rule is policed
    here (freeze invariant 15).
    """
    if target not in _TARGET_PROOF:
        raise ReauthorizeError("recovery_transition_unsupported")
    if type(pair) is not tuple or len(pair) != 2:
        raise ReauthorizeError("proof_discipline_violated")
    name, digest = pair
    if name != _TARGET_PROOF[target] or type(digest) is not str:
        raise ReauthorizeError("proof_discipline_violated:" + str(name))
    try:
        require_digest(digest)
    except ValueError as exc:
        raise ReauthorizeError("proof_discipline_violated") from exc


def _advance(store, case_id, target, pair, at):
    _guard_proof(target, pair)
    try:
        return store.transition(case_id, target, proofs=(pair,), at=at)
    except (RecoveryStateError, ValueError) as exc:
        raise ReauthorizeError("state_transition_rejected") from exc


def _merge_one(store, case_id, block, at):
    try:
        return store.merge(case_id, block.key, backup=block.backup,
                           evidence=block.evidence, at=at)
    except (RecoveryStateError, ValueError) as exc:
        raise ReauthorizeError("state_write_rejected") from exc


# -------------------------------------------------------- credential judges

def _session_index(plan):
    index = {}
    for block in plan.merge_blocks:
        sessions = {item.session_id for item in block.evidence
                    if item.session_id is not None}
        for session in sessions:
            index.setdefault(session, set()).add(block.key)
    return index


def _resolve_associations(spec, plan, merge_by_key, session_index):
    """Union restrictions of explicitly associated merged keys."""
    if spec.no_operation_association:
        return frozenset(), (), ""
    merge_key_set = {block.key for block in plan.merge_blocks}
    matched = set()
    states = set()
    for kind, value in spec.associations:
        if kind == "operation_id":
            hits = tuple(key for key in plan.domain
                         if key.operation_id == value)
            if len(hits) == 0:
                return frozenset(), (), "credential_association_unknown"
            if len(hits) > 1:
                return frozenset(), (), "credential_association_ambiguous"
            key = hits[0]
            if key not in merge_key_set:
                # Resolving to a clean-set-only key names a key with no
                # merge result; letting it through with no tombstone
                # would be the empty-frozenset waterline.
                return frozenset(), (), "credential_association_unmerged"
            matched.add(key)
            states.update(merge_by_key[key].restrictions)
        else:
            keys = session_index.get(value)
            if not keys:
                return frozenset(), (), "credential_association_unknown"
            if any(key not in merge_key_set for key in keys):
                return frozenset(), (), "credential_association_unmerged"
            matched.update(keys)
            for key in keys:
                states.update(merge_by_key[key].restrictions)
    return frozenset(states), tuple(sorted(matched)), ""


def _evaluate_credentials(plan, view):
    merge_by_key = {decision.key: decision for decision in view.merges}
    session_index = _session_index(plan)
    rows = []
    for spec in plan.credentials:
        tombstones, matched, reason = _resolve_associations(
            spec, plan, merge_by_key, session_index)
        if not reason:
            try:
                verdict = evaluate_restored_credential(
                    view.epoch, spec.credential_epoch_id, plan.authentication,
                    tombstones)
            except ValueError as exc:
                raise ReauthorizeError(
                    "credential_evaluation_failed:" + spec.file.name) from exc
            status = verdict.status
            if status == "denied":
                reason = ("restrictive_tombstone" if tombstones
                          else "denied_without_independent_owner_reproof")
        else:
            status = "denied"
        rows.append(CredentialReport(
            file=spec.file,
            credential_epoch_id=spec.credential_epoch_id,
            owner_id=spec.owner_id,
            no_operation_association=spec.no_operation_association,
            associations=spec.associations,
            matched_keys=tuple(_key_triple(key) for key in matched),
            status=status, reason=reason,
            tombstones=tuple(sorted(tombstones,
                                    key=lambda state: state.value))))
    return tuple(rows)


# -------------------------------------------------------------- proofs/receipt

def _file_ref_json(ref):
    # Same object shape as the handoff manifest's file references.
    return {"name": ref.name, "size": ref.size, "sha256": ref.sha256}


def _evidence_payload(plan, reports):
    return {
        "case_id": plan.case_id,
        "backup_id": plan.backup_id,
        "barrier_digest": plan.barrier_digest,
        "manifest_digest": plan.manifest_digest,
        "merge_set": [
            {"key": _key_triple(report.key),
             "backup_states": [state.value for state in report.backup_states],
             "items": [_file_ref_json(item) for item in report.items],
             "restrictions": [state.value for state in report.restrictions],
             "pending": report.pending,
             "conflicts": list(report.conflicts)}
            for report in reports],
        "clean_set": [
            {"key": _key_triple(block.key),
             "backup_states": [state.value for state in block.backup],
             "claim_digest": block.claim_digest}
            for block in plan.clean_blocks],
    }


def _reauth_payload(plan, evidence_hex, rows):
    return {
        "case_id": plan.case_id,
        "manifest_digest": plan.manifest_digest,
        "evidence_merged": evidence_hex,
        "authentication": {
            "file": _file_ref_json(plan.authentication_file),
            "instance_id": plan.authentication.instance_id,
            "epoch_id": plan.authentication.epoch_id,
            "owner_id": plan.authentication.owner_id,
            "record_digest": plan.authentication.record_digest,
            "member_digest": plan.authentication.member_digest,
            "policy_digest": plan.authentication.policy_digest,
            "task_digest": plan.authentication.task_digest},
        "credentials": [
            {"file": _file_ref_json(row.file),
             "credential_epoch_id": row.credential_epoch_id,
             "associations": [list(pair) for pair in row.associations],
             "matched_keys": [list(triple) for triple in row.matched_keys],
             "status": row.status, "reason": row.reason,
             "tombstones": [state.value for state in row.tombstones]}
            for row in rows],
    }


def _receipt_document(plan, view, reports, clean_reports, rows, proofs,
                      anchor_before, anchor_after):
    return {
        "schema": _RECEIPT_SCHEMA,
        "version": _RECEIPT_VERSION,
        "case_id": plan.case_id,
        "backup_id": plan.backup_id,
        "barrier_digest": plan.barrier_digest,
        "epoch": {"instance_id": view.epoch.instance_id,
                  "epoch_id": view.epoch.epoch_id,
                  "ordinal": view.epoch.ordinal},
        "phase": RecoveryPhase.REAUTHORIZED.value,
        "manifest_digest": plan.manifest_digest,
        "anchor_before": list(anchor_before),
        "anchor_after": list(anchor_after),
        "proofs": [list(pair) for pair in proofs],
        "merge_set": [
            {"key": {"task_id": r.key.task_id,
                     "approval_id": r.key.approval_id,
                     "operation_id": r.key.operation_id},
             "backup_states": [s.value for s in r.backup_states],
             "items": [_file_ref_json(item) for item in r.items],
             "restrictions": [s.value for s in r.restrictions],
             "pending": r.pending,
             "conflicts": list(r.conflicts)}
            for r in reports],
        "clean_set": [
            {"key": {"task_id": r.key.task_id,
                     "approval_id": r.key.approval_id,
                     "operation_id": r.key.operation_id},
             "backup_states": [s.value for s in r.backup_states],
             "claim_digest": r.claim_digest}
            for r in clean_reports],
        "authentication": {
            "file": _file_ref_json(plan.authentication_file),
            "matched": True,
            "instance_id": plan.authentication.instance_id,
            "epoch_id": plan.authentication.epoch_id,
            "owner_id": plan.authentication.owner_id,
            "record_digest": plan.authentication.record_digest,
            "member_digest": plan.authentication.member_digest,
            "policy_digest": plan.authentication.policy_digest,
            "task_digest": plan.authentication.task_digest},
        "credentials": [
            {"file": _file_ref_json(r.file),
             "credential_epoch_id": r.credential_epoch_id,
             "owner_id": r.owner_id,
             "no_operation_association": r.no_operation_association,
             "associations": [list(pair) for pair in r.associations],
             "matched_keys": [list(triple) for triple in r.matched_keys],
             "status": r.status, "reason": r.reason,
             "tombstones": [s.value for s in r.tombstones]}
            for r in rows],
        "advisories": list(_ADVISORIES),
    }


def _receipt_final_name(case_id):
    return _RECEIPT_PREFIX + case_id + _RECEIPT_SUFFIX


def _receipt_name_safe(name):
    if not (name.startswith(_RECEIPT_PREFIX)
            and name.endswith(_RECEIPT_SUFFIX)):
        return False
    middle = name[len(_RECEIPT_PREFIX):-len(_RECEIPT_SUFFIX)]
    try:
        require_label(middle)
    except ValueError:
        return False
    return True


def _audit_receipt_root(receipt_fd):
    for name in os.listdir(receipt_fd):
        # Same ELOOP-on-symlink-leaf refusal as the input pin: a hostile
        # receipt root must fail with the fixed reason, never a raw OSError.
        try:
            pinned = os.open(name, _PATH_FLAGS, dir_fd=receipt_fd)
        except OSError as exc:
            raise ReauthorizeError("receipt_root_unsafe_entry:" + name) from exc
        try:
            info = os.fstat(pinned)
        finally:
            os.close(pinned)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) \
                or info.st_nlink != 1:
            raise ReauthorizeError("receipt_root_unsafe_entry:" + name)
        if not (_receipt_name_safe(name) or name.startswith(_TMP_PREFIX)):
            raise ReauthorizeError("receipt_root_unsafe_entry:" + name)


def _read_pinned(root_fd, name):
    fd = _open_file(root_fd, name)
    try:
        data = b""
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def _discard_tmp(receipt_fd, tmp_name):
    try:
        os.unlink(tmp_name, dir_fd=receipt_fd)
        os.fsync(receipt_fd)
    except OSError:
        pass


def _reap_void_tmp(receipt_fd):
    """Cross-process void rule (freeze invariant 10): any tmp name is
    killed-process residue regardless of bytes or pid; never adopt it.
    """
    for name in os.listdir(receipt_fd):
        if name.startswith(_TMP_PREFIX):
            os.unlink(name, dir_fd=receipt_fd)


def _anchor_pair(value):
    if type(value) is not list or len(value) != 2:
        raise ReauthorizeError("receipt_conflict")
    seq, digest = value
    pair = (seq, digest)
    if not _valid_anchor(pair):
        raise ReauthorizeError("receipt_conflict")
    return pair


def _read_completed_receipt(receipt_fd, final_name, rebuild, current_anchor):
    """Verify an existing final receipt by recomputation, not bytes.

    A terminal rerun starts at the final anchor, so recomputing with the
    current run's anchor_before would never byte-match the first run's
    document. The completed receipt is authoritative for its own anchors:
    parse it, rebuild the exact document it claims from this same input
    and compare canonically; its anchor_after must pin the live journal.
    """
    try:
        raw = _read_pinned(receipt_fd, final_name)
    except MaterialInstallError as exc:
        raise ReauthorizeError("receipt_write_failed") from exc

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReauthorizeError("receipt_conflict")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=pairs)
    except ValueError as exc:
        raise ReauthorizeError("receipt_conflict") from exc
    if type(document) is not dict:
        raise ReauthorizeError("receipt_conflict")
    before = _anchor_pair(document.get("anchor_before"))
    after = _anchor_pair(document.get("anchor_after"))
    if after != current_anchor or before[0] > after[0]:
        raise ReauthorizeError("receipt_conflict")
    expected = rebuild(before, after)
    if _canonical(document) != _canonical(expected):
        raise ReauthorizeError("receipt_conflict")
    return raw


def _commit_receipt(receipt_fd, final_name, document, seam):
    """O_EXCL temp + fsync + atomic rename; never overwrite."""
    payload = (json.dumps(document, sort_keys=True, indent=2,
                          ensure_ascii=True, allow_nan=False) + "\n").encode()
    tmp_name = _TMP_PREFIX + final_name + "." + str(os.getpid())
    try:
        tmp_fd = _create_locked_at(receipt_fd, tmp_name)
    except OSError as exc:
        raise ReauthorizeError("receipt_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(tmp_fd, view[:1024 * 1024])
            view = view[written:]
        os.fsync(tmp_fd)
    except OSError as exc:
        os.close(tmp_fd)
        _discard_tmp(receipt_fd, tmp_name)
        raise ReauthorizeError("receipt_write_failed") from exc
    os.close(tmp_fd)
    # TEST-ONLY crash point: the temp file exists and is durable and
    # the rename has not happened, so a SIGKILL here leaves exactly the
    # void residue the next process must reap, never adopt.
    if seam is not None:
        seam("receipt_tmp")
    try:
        os.rename(tmp_name, final_name, src_dir_fd=receipt_fd,
                  dst_dir_fd=receipt_fd)
        os.fsync(receipt_fd)
    except OSError as exc:
        _discard_tmp(receipt_fd, tmp_name)
        raise ReauthorizeError("receipt_write_failed") from exc
    return payload


def _reports_from_view(plan, view):
    by_key = {decision.key: decision for decision in view.merges}
    return tuple(
        MergeReport(
            key=block.key, backup_states=block.backup, items=block.items,
            restrictions=tuple(sorted(by_key[block.key].restrictions,
                                      key=lambda state: state.value)),
            pending=by_key[block.key].pending,
            conflicts=by_key[block.key].conflicts)
        for block in plan.merge_blocks)


# ---------------------------------------------------------------- public API

def reauthorize_recovery(*, handoff_dir, layout, case_id, trusted_anchor,
                         at=None, seam=None):
    """Merge out-of-band evidence and reauthorize one case to reauthorized.

    On refusal the state journal stays byte-identical to the pinned
    anchor and no receipt is added. Same-input reruns reconcile, and a
    terminal rerun appends nothing. ``seam`` is TEST-ONLY.
    """
    if not isinstance(layout, ReauthorizeLayout):
        raise ReauthorizeError("layout_invalid")
    if type(case_id) is not str:
        raise ReauthorizeError("case_id_invalid")
    try:
        require_label(case_id)
    except ValueError as exc:
        raise ReauthorizeError("case_id_invalid") from exc
    if at is not None and type(at) is not str:
        raise ReauthorizeError("timestamp_invalid")
    if seam is not None and not callable(seam):
        raise ReauthorizeError("seam_invalid")
    at = at or _utcnow()

    input_fd, input_path = _pin_root(handoff_dir, "input")
    state_fd, state_path = _pin_root(layout.state_root, "state")
    bare_fd, _bare_path = _pin_root(layout.bare_root, "bare")
    receipt_fd, receipt_path = _pin_root(layout.receipt_root, "receipt")
    owned = [input_fd, state_fd, bare_fd, receipt_fd]
    try:
        # Pin all four before the inode identity/ancestor judgement; all
        # later opens reuse this exact descriptor set.
        _assert_roots_isolated((("input", input_fd), ("state", state_fd),
                                ("bare", bare_fd), ("receipt", receipt_fd)))

        # Read-only validation of the whole bundle first.
        plan = _load_plan(input_fd)
        if plan.case_id != case_id:
            raise ReauthorizeError("handoff_case_binding_mismatch")
        _audit_receipt_root(receipt_fd)

        # C must have run: journal and its lock pre-exist; D makes neither.
        if not _probe_regular(state_fd, _STATE_DB_NAME):
            raise ReauthorizeError("state_journal_absent")

        lock_fd = _acquire_lock(state_fd)
        store = None
        try:
            store = _open_state_store(state_path, trusted_anchor)
            anchor_before = store.journal_anchor()
            try:
                view = store.get(case_id)
            except ValueError as exc:
                raise ReauthorizeError("state_case_unknown") from exc
            if not view.is_current_epoch:
                raise ReauthorizeError("state_case_not_current_epoch")
            if view.backup_id != plan.backup_id \
                    or view.barrier_digest != plan.barrier_digest:
                raise ReauthorizeError("state_case_binding_mismatch")
            phase = view.phase
            # Normal entry is exactly materials_staged. evidence_merged
            # and reauthorized are accepted solely as crash/idempotency
            # re-entry, under strict same-input reconciliation below.
            if phase not in (RecoveryPhase.MATERIALS_STAGED,
                             RecoveryPhase.EVIDENCE_MERGED,
                             RecoveryPhase.REAUTHORIZED):
                raise ReauthorizeError("state_case_phase_mismatch")

            # 1) Full, deterministic merge. block.decision is the no-
            # previous MergeDecision the store produces when this case
            # first merges the key, so crash/idempotent reruns compare
            # byte-equal and append nothing equal. A key already present
            # from an older epoch (inherited) cannot be silently re-
            # composed: it must equal this input decision or be refused.
            existing = {decision.key: decision for decision in view.merges}
            for block in plan.merge_blocks:
                have = existing.get(block.key)
                if have is not None and have != block.decision:
                    raise ReauthorizeError("merge_decision_conflict")
                if have is None and phase != RecoveryPhase.MATERIALS_STAGED:
                    raise ReauthorizeError("merge_decision_conflict")
            if phase == RecoveryPhase.MATERIALS_STAGED:
                for index, block in enumerate(plan.merge_blocks):
                    if block.key not in existing:
                        _merge_one(store, case_id, block, at)
                        if seam is not None:
                            seam("merge:%d" % index)

            # 2) Authenticate only after the full merge landed. A failed
            # post-disaster re-proof keeps the merged restrictions and
            # no transition happens.
            merged_view = store.get(case_id)
            if merged_view.authentication is None:
                if not plan.authentication.matches(merged_view.epoch):
                    raise ReauthorizeError("owner_authentication_failed")
                try:
                    store.authenticate(case_id, plan.authentication, at=at)
                except (RecoveryStateError, ValueError) as exc:
                    raise ReauthorizeError("state_write_rejected") from exc
                if seam is not None:
                    seam("authenticated")
            elif merged_view.authentication != plan.authentication:
                raise ReauthorizeError("owner_authentication_conflict")
            auth_view = store.get(case_id)

            # 3) Read-only credential evaluation against stored merges.
            rows = _evaluate_credentials(plan, auth_view)

            # 4) Gate proofs: deterministic, anchor-free content so the
            # exact digest is recomputable on every crash/idempotent run.
            reports = _reports_from_view(plan, auth_view)
            clean_reports = tuple(
                CleanReport(key=block.key, backup_states=block.backup,
                            claim_digest=block.claim_digest)
                for block in plan.clean_blocks)
            evidence_hex = _proof(
                _PROOF_KIND_EVIDENCE, _evidence_payload(plan, reports))
            reauth_hex = _proof(
                _PROOF_KIND_REAUTH, _reauth_payload(plan, evidence_hex, rows))
            stored_proofs = dict(auth_view.proofs)

            # 5) The two transitions, each carrying exactly its own one
            # proof name; historical proofs stay accumulated in store.
            if phase == RecoveryPhase.REAUTHORIZED:
                if stored_proofs.get("evidence_merged") != evidence_hex \
                        or stored_proofs.get("current_authorization") != reauth_hex:
                    raise ReauthorizeError("proof_conflict")
            else:
                if phase == RecoveryPhase.EVIDENCE_MERGED:
                    if stored_proofs.get("evidence_merged") != evidence_hex:
                        raise ReauthorizeError("proof_conflict:evidence_merged")
                else:
                    _advance(store, case_id, RecoveryPhase.EVIDENCE_MERGED,
                             ("evidence_merged", evidence_hex), at)
                    if seam is not None:
                        seam("evidence_merged")
                _advance(store, case_id, RecoveryPhase.REAUTHORIZED,
                         ("current_authorization", reauth_hex), at)

            # 6) D's own receipt into the independent receipt root.
            final_view = store.get(case_id)
            anchor_after = store.journal_anchor()
            proofs = (("evidence_merged", evidence_hex),
                      ("current_authorization", reauth_hex))

            def rebuild_document(before, after):
                return _receipt_document(
                    plan, final_view, reports, clean_reports, rows, proofs,
                    before, after)

            receipt_name = _receipt_final_name(case_id)
            _reap_void_tmp(receipt_fd)
            if _probe_regular(receipt_fd, receipt_name):
                raw = _read_completed_receipt(
                    receipt_fd, receipt_name, rebuild_document, anchor_after)
            else:
                raw = _commit_receipt(
                    receipt_fd, receipt_name,
                    rebuild_document(anchor_before, anchor_after), seam)
            return ReauthorizationReceipt(
                case_id=case_id,
                phase=RecoveryPhase.REAUTHORIZED.value,
                manifest_digest=plan.manifest_digest,
                journal_anchor_before=anchor_before,
                journal_anchor_after=anchor_after, proofs=proofs,
                merges=reports, clean=clean_reports, credentials=rows,
                receipt_path=os.path.join(receipt_path, receipt_name),
                receipt_sha256=_sha256_hex(raw))
        finally:
            if store is not None:
                store.close()
            os.close(lock_fd)
    finally:
        for fd in owned:
            os.close(fd)
