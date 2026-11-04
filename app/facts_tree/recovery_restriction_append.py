"""E3b §8 path 乙: trusted append boundary for late, stricter restrictions.

D's full-assembly semantics are exact-equality for an already merged key and
"new keys only at materials_staged"; after the case advances, consumed or
revoked evidence arriving later could never land. This parallel boundary
reuses D's physical discipline (pinned isolated roots, physical set exactly
equal to the declared set, size+SHA component pins with a second hash of the
parsed bytes) and R5's cumulative ``merge_evidence`` union, but performs only
restriction appends:

* no case creation, no authentication, no phase transition, no gate;
* hard restrictions import at ANY phase, without waiting for recovery
  reauthorization;
* every evidence item must itself be a restrictive state; the backup weak
  claims may still say ``approved`` — that is exactly the "hard evidence beats
  the original clean claim" case (D's clean_set_trace_only is preserved, the
  old claim is recorded in the receipt and never blocks the union);
* only-stricter composition: a byte-equal replay is a zero-write no-op; a
  same-width/widening/clear attempt refuses with a fixed code and leaves the
  stored restriction set untouched; ledger identity/order conflicts add
  UNKNOWN through the unchanged ``merge_evidence`` rules (§8.2, "不得加严");
* C's existing ``recovery-materialize.lock`` is opened read-only and flock'ed
  exactly like D — no new lock label, the lock is never created here;
* crash reentry distinguishes landed (journal advanced, receipt pending),
  incomplete (``.tmp-restriction-append-*`` residue is void, never adopted)
  and receipted (final receipt recomputed against the live anchor);
* D's handoff receipts and proofs are never opened for write: this boundary
  writes only its own ``restriction-append-<case>-<manifest>.json`` family in
  its own pinned receipt root.

``seam`` is a TEST-ONLY hook: ``merge:N`` after the Nth newly appended merge
commits, ``receipt_tmp`` after the durable temp receipt is written, before
its atomic rename. Crash tests SIGKILL a real subprocess there.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import stat

from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState, _RESTRICTIVE, merge_evidence,
    require_digest, require_label,
)
from app.facts_tree.recovery_materializer import (
    _create_locked_at, _STATE_DB_NAME,
)
from app.facts_tree.recovery_state_store import RecoveryStateError
# Same-grade physical/validation primitives as D. Importing them (instead of
# copying) is what keeps the two boundaries from drifting: this file never
# edits recovery_reauthorize.py, and D's frozen import surface is untouched.
from app.facts_tree.recovery_reauthorize import (
    ReauthorizeError,
    _acquire_lock, _assert_roots_isolated, _entry_map, _file_ref, _file_ref_json,
    _json_bytes, _key_object, _key_triple, _load_evidence_item,
    _open_state_store, _pin_root, _probe_regular, _read_completed_receipt,
    _sha256_hex, _state_list, _str_field, _ITEMS_DIR, _utcnow,
)

_APPEND_NAME = "append.json"
_ENVELOPE_SCHEMA = "sam.f001.e3b.restriction-append.v1"
_ENVELOPE_VERSION = 1
_RECEIPT_SCHEMA = "sam.f001.e3b.restriction-append.receipt.v1"
_RECEIPT_VERSION = 1
_RECEIPT_PREFIX = "restriction-append-"
_RECEIPT_SUFFIX = ".json"
_TMP_PREFIX = ".tmp-restriction-append-"

_ADVISORIES = (
    "append_restrictions_only:no case creation, authentication, phase "
    "transition or gate decision is performed by this boundary",
    "append_hard_evidence_beats_clean:union merge only ever adds "
    "restrictions; backup weak claims stay trace-only and cannot clear A",
    "append_is_never_authorization:every MergeDecision.usable is False; "
    "this boundary opens no read/tasks/effects gate",
    "append_requires_no_reauthorization:hard restrictions land at any phase",
    "append_original_d_receipts_untouched:only the restriction-append "
    "receipt family is written into this receipt root",
    "external_isolation_and_rotation_unverified",
)


class RestrictionAppendError(Exception):
    """Fixed reason code; hostile file/SQL/subprocess text never escapes."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AppendLayout:
    """C's state root plus this boundary's own append receipt directory."""

    state_root: str
    receipt_root: str


@dataclass(frozen=True)
class _AppendBlock:
    key: EvidenceKey
    backup: tuple[EvidenceState, ...]
    items: tuple
    evidence: tuple


@dataclass(frozen=True)
class _Plan:
    case_id: str
    backup_id: str
    barrier_digest: str
    manifest_digest: str
    blocks: tuple[_AppendBlock, ...]


@dataclass(frozen=True)
class AppendReport:
    key: EvidenceKey
    backup_states: tuple[EvidenceState, ...]
    items: tuple
    restrictions: tuple[EvidenceState, ...]
    pending: bool
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class RestrictionAppendReceipt:
    case_id: str
    phase: str
    manifest_digest: str
    journal_anchor_before: tuple[int, str]
    journal_anchor_after: tuple[int, str]
    append_set: tuple[AppendReport, ...]
    receipt_path: str
    receipt_sha256: str


def _exact_manifest_keys(value, keys, context):
    if type(value) is not dict or set(value) != set(keys):
        raise RestrictionAppendError("append_envelope_malformed:" + context)


def _load_append_plan(input_fd):
    """D-grade bundle validation; restrictive evidence is mandatory."""
    root_entries = _entry_map(input_fd, "append-root")
    if set(root_entries) != {_APPEND_NAME, _ITEMS_DIR}:
        raise RestrictionAppendError("input_root_set_not_exact")
    manifest_info = root_entries[_APPEND_NAME]
    if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_nlink != 1:
        raise RestrictionAppendError("input_root_unsafe_entry:append.json")
    items_info = root_entries[_ITEMS_DIR]
    if not stat.S_ISDIR(items_info.st_mode):
        raise RestrictionAppendError("input_root_unsafe_entry:items")
    items_fd = os.open(_ITEMS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                       | getattr(os, "O_CLOEXEC", 0), dir_fd=input_fd)
    try:
        physical = _entry_map(items_fd, "items")
        for name, info in physical.items():
            if stat.S_ISDIR(info.st_mode):
                raise RestrictionAppendError(
                    "input_root_unsafe_entry:items-nesting:" + name)
            if info.st_nlink != 1:
                raise RestrictionAppendError(
                    "input_root_hardlink_refused:items:" + name)
        try:
            manifest, manifest_raw = _json_bytes(
                input_fd, _APPEND_NAME, "append_envelope_malformed")
        except ReauthorizeError as exc:
            raise RestrictionAppendError(exc.reason) from exc
        manifest_digest = _sha256_hex(manifest_raw)
        _exact_manifest_keys(
            manifest, ("schema", "version", "case_id", "backup_id",
                       "barrier_digest", "restriction_keys"), "manifest")
        if (manifest["schema"] != _ENVELOPE_SCHEMA
                or type(manifest["version"]) is not int
                or manifest["version"] != _ENVELOPE_VERSION):
            raise RestrictionAppendError("append_envelope_version_unsupported")
        case_id = _str_field(manifest["case_id"], "case_id")
        backup_id = _str_field(manifest["backup_id"], "backup_id")
        barrier = manifest["barrier_digest"]
        try:
            require_label(case_id)
            require_label(backup_id)
            require_digest(barrier)
        except ValueError as exc:
            raise RestrictionAppendError(
                "append_envelope_malformed:binding") from exc

        raw_blocks = manifest["restriction_keys"]
        if type(raw_blocks) is not list or not raw_blocks:
            raise RestrictionAppendError("append_set_empty")
        declared_names, seen_keys = set(), set()
        blocks = []
        for index, block in enumerate(raw_blocks):
            context = "restriction_keys:%d" % index
            _exact_manifest_keys(block, ("key", "backup_states", "items"), context)
            try:
                key = _key_object(block["key"], context)
            except ReauthorizeError as exc:
                raise RestrictionAppendError(exc.reason) from exc
            if key in seen_keys:
                raise RestrictionAppendError(
                    "append_envelope_malformed:" + context)
            seen_keys.add(key)
            try:
                backup = _state_list(block["backup_states"], context)
            except ReauthorizeError as exc:
                raise RestrictionAppendError(exc.reason) from exc
            if type(block["items"]) is not list or not block["items"]:
                raise RestrictionAppendError(
                    "append_block_without_evidence:" + context)
            refs = []
            for item in block["items"]:
                try:
                    ref = _file_ref(item, context)
                except ReauthorizeError as exc:
                    raise RestrictionAppendError(exc.reason) from exc
                if ref.name in declared_names:
                    raise RestrictionAppendError(
                        "append_envelope_malformed:" + context)
                declared_names.add(ref.name)
                refs.append(ref)
            refs = tuple(sorted(refs, key=lambda ref: ref.name))
            evidence = []
            for ref in refs:
                try:
                    observed = _load_evidence_item(
                        items_fd, ref, key, "restriction_keys:" + str(index))
                except ReauthorizeError as exc:
                    raise RestrictionAppendError(exc.reason) from exc
                if observed.state not in _RESTRICTIVE:
                    raise RestrictionAppendError(
                        "append_evidence_not_restrictive:" + ref.name)
                evidence.append(observed)
            evidence = tuple(evidence)
            try:
                # Dry-run now, same fail-closed ordering as D: no journal
                # write exists until every combination composes.
                merge_evidence(key, backup, evidence)
            except ValueError as exc:
                raise RestrictionAppendError(
                    "append_evidence_malformed:" + context) from exc
            blocks.append(_AppendBlock(key, backup, refs, evidence))
        if set(physical) != declared_names:
            raise RestrictionAppendError("input_root_set_not_exact")
        return _Plan(case_id=case_id, backup_id=backup_id,
                     barrier_digest=barrier, manifest_digest=manifest_digest,
                     blocks=tuple(sorted(blocks, key=lambda b: b.key)))
    finally:
        os.close(items_fd)


# ------------------------------------------------------------- receipt family

def _receipt_final_name(case_id, manifest_digest):
    return _RECEIPT_PREFIX + case_id + "-" + manifest_digest[7:19] + _RECEIPT_SUFFIX


def _receipt_name_safe(name):
    if not (name.startswith(_RECEIPT_PREFIX) and name.endswith(_RECEIPT_SUFFIX)):
        return False
    middle = name[len(_RECEIPT_PREFIX):-len(_RECEIPT_SUFFIX)]
    try:
        require_label(middle)
    except ValueError:
        return False
    return True


def _audit_receipt_root(receipt_fd):
    for name in os.listdir(receipt_fd):
        try:
            pinned = os.open(name, os.O_PATH | os.O_NOFOLLOW
                             | getattr(os, "O_CLOEXEC", 0), dir_fd=receipt_fd)
        except OSError as exc:
            raise RestrictionAppendError(
                "receipt_root_unsafe_entry:" + name) from exc
        try:
            info = os.fstat(pinned)
        finally:
            os.close(pinned)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) \
                or info.st_nlink != 1:
            raise RestrictionAppendError("receipt_root_unsafe_entry:" + name)
        if not (_receipt_name_safe(name) or name.startswith(_TMP_PREFIX)):
            raise RestrictionAppendError("receipt_root_unsafe_entry:" + name)


def _reap_void_tmp(receipt_fd):
    """Any tmp name is killed-process residue regardless of bytes/pid."""
    for name in os.listdir(receipt_fd):
        if name.startswith(_TMP_PREFIX):
            os.unlink(name, dir_fd=receipt_fd)


def _discard_tmp(receipt_fd, tmp_name):
    try:
        os.unlink(tmp_name, dir_fd=receipt_fd)
        os.fsync(receipt_fd)
    except OSError:
        pass


def _commit_receipt(receipt_fd, final_name, document, seam):
    payload = (json.dumps(document, sort_keys=True, indent=2,
                          ensure_ascii=True, allow_nan=False) + "\n").encode()
    tmp_name = _TMP_PREFIX + final_name + "." + str(os.getpid())
    try:
        tmp_fd = _create_locked_at(receipt_fd, tmp_name)
    except OSError as exc:
        raise RestrictionAppendError("receipt_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(tmp_fd, view[:1024 * 1024])
            view = view[written:]
        os.fsync(tmp_fd)
    except OSError as exc:
        os.close(tmp_fd)
        _discard_tmp(receipt_fd, tmp_name)
        raise RestrictionAppendError("receipt_write_failed") from exc
    os.close(tmp_fd)
    if seam is not None:
        seam("receipt_tmp")
    try:
        os.rename(tmp_name, final_name, src_dir_fd=receipt_fd,
                  dst_dir_fd=receipt_fd)
        os.fsync(receipt_fd)
    except OSError as exc:
        _discard_tmp(receipt_fd, tmp_name)
        raise RestrictionAppendError("receipt_write_failed") from exc
    return payload


def _receipt_document(plan, view, reports, anchor_before, anchor_after):
    return {
        "schema": _RECEIPT_SCHEMA,
        "version": _RECEIPT_VERSION,
        "case_id": plan.case_id,
        "backup_id": plan.backup_id,
        "barrier_digest": plan.barrier_digest,
        "manifest_digest": plan.manifest_digest,
        "epoch": {"instance_id": view.epoch.instance_id,
                  "epoch_id": view.epoch.epoch_id,
                  "ordinal": view.epoch.ordinal},
        "phase": view.phase.value,
        "anchor_before": list(anchor_before),
        "anchor_after": list(anchor_after),
        "append_set": [
            {"key": _key_triple(report.key),
             "backup_states": [state.value for state in report.backup_states],
             "items": [_file_ref_json(item) for item in report.items],
             "restrictions": [state.value for state in report.restrictions],
             "pending": report.pending,
             "conflicts": list(report.conflicts)}
            for report in reports],
        "advisories": list(_ADVISORIES),
    }


def _reports_from_view(plan, view):
    by_key = {decision.key: decision for decision in view.merges}
    reports = []
    for block in plan.blocks:
        decision = by_key[block.key]
        reports.append(AppendReport(
            key=block.key, backup_states=block.backup, items=block.items,
            restrictions=tuple(sorted(decision.restrictions,
                                      key=lambda state: state.value)),
            pending=decision.pending, conflicts=decision.conflicts))
    return tuple(reports)


# ---------------------------------------------------------------- public API

def append_restrictions(*, append_dir, layout, case_id, trusted_anchor,
                        at=None, seam=None):
    """Append only-stricter late restrictions to one current-epoch case.

    On refusal the journal stays at the pinned anchor and no append receipt
    is added. Same-input reruns reconcile to a zero-write no-op. ``seam`` is
    TEST-ONLY.
    """
    try:
        return _append_restrictions(
            append_dir=append_dir, layout=layout, case_id=case_id,
            trusted_anchor=trusted_anchor, at=at, seam=seam)
    except ReauthorizeError as exc:
        # D-primitive failures already carry fixed, secret-free codes.
        raise RestrictionAppendError(exc.reason) from exc


def _append_restrictions(*, append_dir, layout, case_id, trusted_anchor,
                         at, seam):
    if not isinstance(layout, AppendLayout):
        raise RestrictionAppendError("layout_invalid")
    if type(case_id) is not str:
        raise RestrictionAppendError("case_id_invalid")
    try:
        require_label(case_id)
    except ValueError as exc:
        raise RestrictionAppendError("case_id_invalid") from exc
    if at is not None and type(at) is not str:
        raise RestrictionAppendError("timestamp_invalid")
    if seam is not None and not callable(seam):
        raise RestrictionAppendError("seam_invalid")
    at = at or _utcnow()

    input_fd, input_path = _pin_root(append_dir, "input")
    state_fd, state_path = _pin_root(layout.state_root, "state")
    receipt_fd, receipt_path = _pin_root(layout.receipt_root, "receipt")
    owned = [input_fd, state_fd, receipt_fd]
    try:
        _assert_roots_isolated((("input", input_fd), ("state", state_fd),
                                ("receipt", receipt_fd)))
        plan = _load_append_plan(input_fd)
        if plan.case_id != case_id:
            raise RestrictionAppendError("append_case_binding_mismatch")
        _audit_receipt_root(receipt_fd)
        if not _probe_regular(state_fd, _STATE_DB_NAME):
            raise RestrictionAppendError("state_journal_absent")

        lock_fd = _acquire_lock(state_fd)
        store = None
        try:
            store = _open_state_store(state_path, trusted_anchor)
            anchor_before = store.journal_anchor()
            try:
                view = store.get(case_id)
            except ValueError as exc:
                # A case the journal never created cannot be appended to;
                # this boundary never creates recovery cases.
                raise RestrictionAppendError("append_case_unknown") from exc
            if not view.is_current_epoch:
                raise RestrictionAppendError("append_case_not_current_epoch")
            if view.backup_id != plan.backup_id \
                    or view.barrier_digest != plan.barrier_digest:
                raise RestrictionAppendError("append_case_binding_mismatch")

            existing = {decision.key: decision for decision in view.merges}
            actions = []
            for block in plan.blocks:
                have = existing.get(block.key)
                if have is None:
                    actions.append((block, "merge"))
                    continue
                candidate = merge_evidence(
                    block.key, block.backup, block.evidence, have)
                if candidate == have:
                    # Exact replay (same restrictions, evidence coordinates
                    # and conflicts) is a zero-write no-op.
                    actions.append((block, "skip"))
                elif have.restrictions < candidate.restrictions:
                    # Only a STRICT restriction superset may land. New
                    # evidence at the same width (even a different ledger's
                    # consumed) is refused rather than swapping the tombstone;
                    # a ledger identity/order conflict adds UNKNOWN through
                    # merge_evidence itself, which is the strict-superset case.
                    actions.append((block, "merge"))
                else:
                    raise RestrictionAppendError(
                        "append_restriction_not_stricter")

            # All validation above precedes the first journal write.
            writes = 0
            for block, action in actions:
                if action != "merge":
                    continue
                try:
                    store.merge(case_id, block.key, backup=block.backup,
                                evidence=block.evidence, at=at)
                except (RecoveryStateError, ValueError) as exc:
                    raise RestrictionAppendError("state_write_rejected") from exc
                writes += 1
                if seam is not None:
                    seam("merge:%d" % (writes - 1))

            final_view = store.get(case_id)
            anchor_after = store.journal_anchor()
            reports = _reports_from_view(plan, final_view)

            def rebuild_document(before, after):
                return _receipt_document(
                    plan, final_view, reports, before, after)

            receipt_name = _receipt_final_name(case_id, plan.manifest_digest)
            _reap_void_tmp(receipt_fd)
            if _probe_regular(receipt_fd, receipt_name):
                raw = _read_completed_receipt(
                    receipt_fd, receipt_name, rebuild_document, anchor_after)
            else:
                raw = _commit_receipt(
                    receipt_fd, receipt_name,
                    rebuild_document(anchor_before, anchor_after), seam)
            return RestrictionAppendReceipt(
                case_id=case_id, phase=final_view.phase.value,
                manifest_digest=plan.manifest_digest,
                journal_anchor_before=anchor_before,
                journal_anchor_after=anchor_after, append_set=reports,
                receipt_path=os.path.join(receipt_path, receipt_name),
                receipt_sha256=_sha256_hex(raw))
        finally:
            if store is not None:
                store.close()
            os.close(lock_fd)
    finally:
        for fd in owned:
            os.close(fd)
