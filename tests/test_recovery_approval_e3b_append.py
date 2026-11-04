"""Real-environment pytest for the E3b restriction-append boundary.

Zero mocks: every case is a real slice-C install staged by the D-piece
chips (``_stage``), evidence items are real files verified by size/sha256,
and every refusal is checked against the live hash-chained journal.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.facts_tree.recovery_epoch import EvidenceState, RecoveryPhase
from app.facts_tree.recovery_restriction_append import (
    AppendLayout, RestrictionAppendError, append_restrictions,
)
from app.facts_tree.recovery_state_store import RecoveryStateStore
from tests.test_facts_tree_backup_assembly import world  # noqa: F401
from tests.test_facts_tree_recovery_reauthorize import (
    CASE, CASE_B, K1, K2, K3, K9,
    _evidence, _green_bundle, _key, _run_d, _stage, _state_view, _write_item,
)

_APPEND_SCHEMA = "sam.f001.e3b.restriction-append.v1"
AT_E3B = "2026-11-06T00:00:00.000000+00:00"

_S = EvidenceState


# --------------------------------------------------------------------------
# Real bundle/call chips (same shapes as D's physical handoff builders)
# --------------------------------------------------------------------------

def _append_bundle(root, *, case_id, backup_id, barrier, blocks):
    """Write append.json + items/; blocks are (key, [backup states], items)."""
    items_dir = root / "items"
    items_dir.mkdir(parents=True)
    manifest_blocks = []
    for key, states, specs in blocks:
        refs = [_write_item(items_dir, name, payload)
                for name, payload in specs]
        manifest_blocks.append({
            "key": _key(*key), "backup_states": list(states), "items": refs})
    manifest = {
        "schema": _APPEND_SCHEMA, "version": 1,
        "case_id": case_id, "backup_id": backup_id,
        "barrier_digest": barrier, "restriction_keys": manifest_blocks}
    (root / "append.json").write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2,
                   ensure_ascii=True).encode("utf-8") + b"\n")


def _one_block(key, states, specs):
    return [(key, states, specs)]


def _layout(paths, receipt_dir):
    return AppendLayout(str(paths["state"]), str(receipt_dir))


def _run_append(paths, receipt_dir, append_dir, case_id, anchor, *, at=AT_E3B):
    return append_restrictions(
        append_dir=str(append_dir), layout=_layout(paths, receipt_dir),
        case_id=case_id, trusted_anchor=tuple(anchor), at=at)


def _decision(paths, operation, case_id=CASE):
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        view = store.get(case_id)
        decision = next((m for m in view.merges
                         if m.key.operation_id == operation), None)
        return None if decision is None else (
            frozenset(decision.restrictions), decision.usable,
            tuple(decision.conflicts))


def _report(receipt, operation):
    return next(r for r in receipt.append_set
                if r.key.operation_id == operation)


def _consumed_item(name, *, ledger="ledger-1", source="src-1", scope="scope-1",
                   seq=1, session="sess-1"):
    return (name, _evidence(state=_S.CONSUMED.value, seq=seq, event=None,
                            source=source, scope=scope, ledger=ledger,
                            session=session))


def _revoked_item(name, *, ledger="ledger-2", source="src-2", scope="scope-2",
                  seq=1):
    return (name, _evidence(state=_S.REVOKED.value, seq=seq, event=None,
                            source=source, scope=scope, ledger=ledger))


def _run_green_d(paths, report, tmp_path):
    handoff = tmp_path / "d-handoff"
    d_receipt = tmp_path / "d-receipt"
    d_receipt.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    _run_d(paths, handoff, d_receipt, CASE, report["anchor"])
    return d_receipt


# --------------------------------------------------------------------------
# a) hard evidence tightens; receipt anchors and bytes are real
# --------------------------------------------------------------------------

def test_e3b01_hard_evidence_merges_with_real_receipt(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b01")
    assert report["phase"] == RecoveryPhase.MATERIALS_STAGED.value
    view0, anchor0 = _state_view(paths)
    assert view0.backup_id == report["backup_id"]
    assert view0.barrier_digest == report["barrier_digest"]

    append_dir = tmp_path / "append"
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()

    receipt = _run_append(paths, receipt_dir, append_dir, CASE, anchor0)

    assert receipt.case_id == CASE
    assert receipt.phase == RecoveryPhase.MATERIALS_STAGED.value
    assert receipt.journal_anchor_before == anchor0
    # One merge = one journal event; the digest half must move too.
    assert receipt.journal_anchor_after[0] == anchor0[0] + 1
    assert receipt.journal_anchor_after[1] != anchor0[1]

    view1, anchor1 = _state_view(paths)
    assert anchor1 == receipt.journal_anchor_after
    restrictions, usable, conflicts = _decision(paths, K1[2])
    assert restrictions == frozenset({_S.CONSUMED})
    assert usable is False
    # The approved backup claim disagrees with the hard consumed evidence;
    # the fixed conflict trace is retained while the stricter state wins.
    assert conflicts == ("state_conflict_stricter_preserved",)

    row = _report(receipt, K1[2])
    assert set(row.restrictions) == {_S.CONSUMED}
    assert row.backup_states == (_S.APPROVED,)
    assert row.conflicts == ("state_conflict_stricter_preserved",)
    assert len(row.items) == 1

    names = sorted(os.listdir(receipt_dir))
    assert names == [os.path.basename(receipt.receipt_path)]
    assert names[0].startswith("restriction-append-")
    raw = Path(receipt.receipt_path).read_bytes()
    assert Path(receipt.receipt_path).is_file()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == receipt.receipt_sha256
    assert receipt.manifest_digest == "sha256:" + hashlib.sha256(
        (append_dir / "append.json").read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# b) exact replay is a zero-write no-op; receipt bytes stay stable
# --------------------------------------------------------------------------

def test_e3b02_same_input_replay_is_zero_write(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b02")
    _view, anchor0 = _state_view(paths)
    append_dir = tmp_path / "append"
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()

    first = _run_append(paths, receipt_dir, append_dir, CASE, anchor0)
    raw_after_first = Path(first.receipt_path).read_bytes()
    names_after_first = sorted(os.listdir(receipt_dir))

    # Re-entry model from D: the host re-inspects the journal out of band
    # and re-pins the brand-new process at the now-live anchor.
    _view, anchor_live = _state_view(paths)
    second = _run_append(paths, receipt_dir, append_dir, CASE, anchor_live)

    assert first.journal_anchor_after == second.journal_anchor_before
    assert second.journal_anchor_before == second.journal_anchor_after
    _view, anchor_live = _state_view(paths)
    assert anchor_live == first.journal_anchor_after
    assert second.receipt_path == first.receipt_path
    assert second.receipt_sha256 == first.receipt_sha256
    assert Path(second.receipt_path).read_bytes() == raw_after_first
    assert sorted(os.listdir(receipt_dir)) == names_after_first
    assert len(names_after_first) == 1


# --------------------------------------------------------------------------
# c) same-width / clean-claim attempts are refused with the fixed code
# --------------------------------------------------------------------------

def test_e3b03_same_width_and_clean_clear_refused(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b03")
    _run_green_d(paths, report, tmp_path)
    view_d, anchor_d = _state_view(paths)
    assert view_d.phase == RecoveryPhase.REAUTHORIZED
    snap_k1 = _decision(paths, K1[2])
    snap_k2 = _decision(paths, K2[2])
    assert snap_k1[0] == frozenset({_S.CONSUMED})
    assert snap_k2[0] == frozenset({_S.REVOKED})

    # Another ledger's consumed at the same width must not swap the tombstone.
    wide_dir = tmp_path / "append-wide"
    _append_bundle(
        wide_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(
            K1, [_S.APPROVED.value],
            [_consumed_item("ev-k1-wide.json", ledger="ledger-9",
                            source="src-9", scope="scope-9", seq=1,
                            session=None)]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, wide_dir, CASE, anchor_d)
    assert raised.value.reason == "append_restriction_not_stricter"
    assert _state_view(paths)[1] == anchor_d
    assert _decision(paths, K1[2]) == snap_k1
    assert os.listdir(receipt_dir) == []

    # An approved weak claim paired with same-width revoked cannot clear K2.
    clean_dir = tmp_path / "append-clean"
    _append_bundle(
        clean_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(
            K2, [_S.APPROVED.value],
            [_revoked_item("ev-k2-wide.json", ledger="ledger-8",
                           source="src-8", scope="scope-8", seq=1)]))
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, clean_dir, CASE, anchor_d)
    assert raised.value.reason == "append_restriction_not_stricter"
    assert _state_view(paths)[1] == anchor_d
    assert _decision(paths, K2[2]) == snap_k2
    assert os.listdir(receipt_dir) == []


# --------------------------------------------------------------------------
# d) hard restrictions land at reauthorized; non-current epoch is refused
# --------------------------------------------------------------------------

def test_e3b04a_lands_at_reauthorized_phase(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b04")
    _run_green_d(paths, report, tmp_path)
    view_d, anchor_d = _state_view(paths)

    # K9 was a clean (unused) claim in D; hard consumed now lands with no
    # re-authorization step from this boundary.
    append_dir = tmp_path / "append"
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(
            K9, [_S.UNUSED.value],
            [_consumed_item("ev-k9-01.json", ledger="ledger-9",
                            source="src-9", scope="scope-9", seq=1,
                            session=None)]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()
    receipt = _run_append(paths, receipt_dir, append_dir, CASE, anchor_d)

    assert receipt.phase == RecoveryPhase.REAUTHORIZED.value
    assert receipt.journal_anchor_after[0] == anchor_d[0] + 1
    restrictions, usable, _conflicts = _decision(paths, K9[2])
    assert restrictions == frozenset({_S.CONSUMED})
    assert usable is False
    raw = Path(receipt.receipt_path).read_bytes()
    assert b"append_requires_no_reauthorization" in raw


def test_e3b04b_non_current_epoch_refused(world, tmp_path):
    # Real superseded case: CASE is the prior epoch, CASE_B the current one.
    paths, report = _stage(world, tmp_path, mode="supersede", case_b=CASE_B,
                           base_name="recovery-e3b04b")
    stale_dir = tmp_path / "append-stale"
    _append_bundle(
        stale_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    stale_receipt = tmp_path / "append-receipt-stale"
    stale_receipt.mkdir()
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        assert store.get(CASE).is_current_epoch is False
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, stale_receipt, stale_dir, CASE, report["anchor"])
    assert raised.value.reason == "append_case_not_current_epoch"
    assert _state_view(paths)[1] == tuple(report["anchor"])
    assert os.listdir(stale_receipt) == []


# --------------------------------------------------------------------------
# e) backup weak claims are trace-only; union merge only ever adds
# --------------------------------------------------------------------------

def test_e3b05_clean_claim_does_not_neutralize_hard_evidence(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b05")
    _view, anchor0 = _state_view(paths)
    assert _decision(paths, K3[2]) is None

    append_dir = tmp_path / "append"
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(K3, [_S.APPROVED.value], [
            _consumed_item("ev-k3-consumed.json"),
            _revoked_item("ev-k3-revoked.json")]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()
    receipt = _run_append(paths, receipt_dir, append_dir, CASE, anchor0)

    row = _report(receipt, K3[2])
    # The approved backup claim sits in the receipt as trace only; both
    # independent hard restrictions survive and nothing is cleared.
    assert row.backup_states == (_S.APPROVED,)
    assert set(row.restrictions) == {_S.CONSUMED, _S.REVOKED}
    assert row.conflicts == ("state_conflict_stricter_preserved",)
    restrictions, usable, conflicts = _decision(paths, K3[2])
    assert restrictions == frozenset({_S.CONSUMED, _S.REVOKED})
    assert usable is False
    assert conflicts == ("state_conflict_stricter_preserved",)
    raw = Path(receipt.receipt_path).read_bytes()
    assert b"append_hard_evidence_beats_clean" in raw
    assert b"append_is_never_authorization" in raw


# --------------------------------------------------------------------------
# f) physical input hostility and binding refusals
# --------------------------------------------------------------------------

def test_e3b06_unsafe_entries_and_binding_refusals(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b06")
    _view, anchor0 = _state_view(paths)
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()

    def _fresh_bundle(name):
        root = tmp_path / name
        _append_bundle(
            root, case_id=CASE, backup_id=report["backup_id"],
            barrier=report["barrier_digest"],
            blocks=_one_block(K1, [_S.APPROVED.value],
                              [_consumed_item("ev-k1-01.json")]))
        return root

    # Symlink leaf inside items/ is refused at the O_PATH pin.
    link_root = _fresh_bundle("append-symlink")
    os.symlink("ev-k1-01.json", link_root / "items" / "evil-link.json")
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, link_root, CASE, anchor0)
    assert raised.value.reason == \
        "input_root_unsafe_entry:items:evil-link.json"

    # A second physical name sharing the inode is a hardlink.
    hard_root = _fresh_bundle("append-hardlink")
    os.link(hard_root / "items" / "ev-k1-01.json",
            hard_root / "items" / "evil-hard.json")
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, hard_root, CASE, anchor0)
    assert raised.value.reason.startswith(
        "input_root_hardlink_refused:items:")
    assert "ev-k1-01.json" in raised.value.reason or \
        "evil-hard.json" in raised.value.reason

    # Call-argument case binding differs from the envelope.
    mismatch_root = _fresh_bundle("append-case-arg")
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, mismatch_root, CASE_B, anchor0)
    assert raised.value.reason == "append_case_binding_mismatch"

    # Envelope claims another backup id / barrier digest.
    backup_root = tmp_path / "append-backup"
    _append_bundle(
        backup_root, case_id=CASE,
        backup_id=report["backup_id"] + "-x",
        barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, backup_root, CASE, anchor0)
    assert raised.value.reason == "append_case_binding_mismatch"

    flipped = ("a" if report["barrier_digest"][0] != "a" else "b") \
        + report["barrier_digest"][1:]
    barrier_root = tmp_path / "append-barrier"
    _append_bundle(
        barrier_root, case_id=CASE, backup_id=report["backup_id"],
        barrier=flipped,
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, barrier_root, CASE, anchor0)
    assert raised.value.reason == "append_case_binding_mismatch"

    # A label-valid case the journal never created.
    ghost_root = tmp_path / "append-ghost"
    _append_bundle(
        ghost_root, case_id="case-ghost-07",
        backup_id=report["backup_id"], barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, receipt_dir, ghost_root, "case-ghost-07", anchor0)
    assert raised.value.reason == "append_case_unknown"

    assert _state_view(paths)[1] == anchor0
    assert os.listdir(receipt_dir) == []


# --------------------------------------------------------------------------
# g) receipt root purity: foreign names refuse; void tmp residue is reaped
# --------------------------------------------------------------------------

def test_e3b07_receipt_root_purity_and_void_tmp_reap(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b07")
    _view, anchor0 = _state_view(paths)

    def _bundle(name):
        root = tmp_path / name
        _append_bundle(
            root, case_id=CASE, backup_id=report["backup_id"],
            barrier=report["barrier_digest"],
            blocks=_one_block(K1, [_S.APPROVED.value],
                              [_consumed_item("ev-k1-01.json")]))
        return root

    dirty_receipt = tmp_path / "receipt-dirty"
    dirty_receipt.mkdir()
    (dirty_receipt / "foreign-note.txt").write_bytes(b"not a receipt\n")
    with pytest.raises(RestrictionAppendError) as raised:
        _run_append(paths, dirty_receipt, _bundle("append-dirty"),
                    CASE, anchor0)
    assert raised.value.reason == \
        "receipt_root_unsafe_entry:foreign-note.txt"
    assert (dirty_receipt / "foreign-note.txt").is_file()

    reaped_receipt = tmp_path / "receipt-reap"
    reaped_receipt.mkdir()
    void_name = ".tmp-restriction-append-void.99999"
    (reaped_receipt / void_name).write_bytes(b"killed mid rename\n")
    receipt = _run_append(paths, reaped_receipt, _bundle("append-reap"),
                          CASE, anchor0)
    names = sorted(os.listdir(reaped_receipt))
    assert void_name not in names
    assert names == [os.path.basename(receipt.receipt_path)]
    assert _state_view(paths)[1] == receipt.journal_anchor_after


# --------------------------------------------------------------------------
# h) the D lock label is reused; no new lock is ever created
# --------------------------------------------------------------------------

def test_e3b08_lock_label_reused_from_d(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-e3b08")
    state_dir = Path(str(paths["state"]))
    before = sorted(name for name in os.listdir(state_dir)
                    if name.endswith(".lock"))
    assert before == ["recovery-materialize.lock"]

    append_dir = tmp_path / "append"
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=_one_block(K1, [_S.APPROVED.value],
                          [_consumed_item("ev-k1-01.json")]))
    receipt_dir = tmp_path / "append-receipt"
    receipt_dir.mkdir()
    _run_append(paths, receipt_dir, append_dir, CASE,
                _state_view(paths)[1])

    after = sorted(name for name in os.listdir(state_dir)
                   if name.endswith(".lock"))
    assert after == ["recovery-materialize.lock"]
    # No lock label is born in the append receipt root either.
    assert all(not name.endswith(".lock")
               for name in os.listdir(receipt_dir))
