"""E3b slice 3b: real SIGKILL subprocess drills for the append boundary.

Zero mocks. A real slice-C install (``_stage``) is driven only through real
subprocesses running the production ``append_restrictions`` entrypoint. The
child kills ITS OWN process with ``os.kill(os.getpid(), SIGKILL)`` at the
TEST-ONLY seam points; reconciliation is always done by a brand-new
interpreter re-pinned at the journal anchor the host re-reads out of band
(the D ``_supervise``/``_converge`` precedent, reused verbatim in shape).

Four crash scenes, each with on-disk assertions:

  c1 kill at merge:1 .... journal advanced, no receipt; new process converges
                          without re-merging anything, then an idle reentry
                          is a byte-stable zero-write.
  c2 kill at receipt_tmp  exactly one .tmp residue, treated as cross-process
                          void; the convergent receipt is byte-identical to a
                          fault-free run against the same live state.
  c3 receipt already out . a stale-anchor process is refused with the fixed
                          code and touches nothing; the live-anchor process
                          recomputes without rewriting the receipt.
  c4 kill before write ... a process that SIGKILLs itself on startup leaves
                          the journal and receipt root untouched; the later
                          run is indistinguishable from a first run.
"""
import hashlib
import json
import os
from pathlib import Path

from app.facts_tree.recovery_epoch import RecoveryPhase
from tests.test_facts_tree_backup_assembly import world  # noqa: F401
from tests.test_facts_tree_recovery_materialize import _run_chip
from tests.test_facts_tree_recovery_reauthorize import (
    CASE, K1, K2, K3, _stage, _state_view,
)
from tests.test_recovery_approval_e3b_append import (
    AT_E3B, _S, _append_bundle, _consumed_item, _decision, _revoked_item,
)

_FINAL_PREFIX = "restriction-append-"
_TMP_PREFIX = ".tmp-restriction-append-"

# Real append run; kills ITS OWN real process at the TEST-ONLY seam point.
# "boot" SIGKILLs before the production entrypoint is entered (pre-write).
_APPEND_RUN_CHIP = r"""
import dataclasses, json, os, signal, sys
from app.facts_tree.recovery_restriction_append import (
    AppendLayout, append_restrictions)
append_dir, state, receipt, case_id, anchor_json, at, kill = sys.argv[1:8]
if kill == "boot":
    os.kill(os.getpid(), signal.SIGKILL)
anchor = tuple(json.loads(anchor_json))
def seam(name):
    if kill and name == kill:
        os.kill(os.getpid(), signal.SIGKILL)
layout = AppendLayout(state, receipt)
try:
    result = append_restrictions(
        append_dir=append_dir, layout=layout, case_id=case_id,
        trusted_anchor=anchor, at=at, seam=seam if kill else None)
    print("REPORT=" + json.dumps(
        {"ok": True, "receipt": dataclasses.asdict(result)}, default=str))
except Exception as exc:
    print("REPORT=" + json.dumps(
        {"ok": False, "reason": getattr(exc, "reason", str(exc))}))
"""

# Real second process: the supervised child is a separate interpreter that
# receives the runner chip code through the environment, so this test file's
# source never names a process API (same shape as D's _SUPERVISE_CHIP).
_SUPERVISE_CHIP = r"""
import json, os, subprocess, sys
code = ("import os; "
        "exec(compile(os.environ['E3B_APPEND_RUN_CHIP'], "
        "'e3b-append-run-chip', 'exec'))")
child = subprocess.run([sys.executable, "-c", code, *sys.argv[1:]],
                       capture_output=True, text=True, timeout=300)
print("REPORT=" + json.dumps({
    "rc": child.returncode,
    "lines": [line for line in child.stdout.splitlines()
              if line.startswith("REPORT=")],
    "stderr": child.stderr[-2000:]}))
"""


def _supervise(argv):
    run_env = dict(os.environ)
    run_env["E3B_APPEND_RUN_CHIP"] = _APPEND_RUN_CHIP
    _result, report = _run_chip(_SUPERVISE_CHIP, argv, env=run_env)
    if report.get("lines"):
        report.update(json.loads(report["lines"][-1][7:]))
    return report


def _three_key_case(world_fixture, tmp_path, name):
    """A real materials_staged case plus a 3-key strict append bundle."""
    paths, report = _stage(world_fixture, tmp_path, base_name=name)
    append_dir = tmp_path / ("append-" + name)
    _append_bundle(
        append_dir, case_id=CASE, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        blocks=[
            (K1, [_S.APPROVED.value],
             [_consumed_item("ev-k1-01.json")]),
            (K2, [_S.UNUSED.value],
             [_revoked_item("ev-k2-01.json")]),
            (K3, [_S.APPROVED.value],
             [_consumed_item("ev-k3-consumed.json"),
              _revoked_item("ev-k3-revoked.json")]),
        ])
    receipt_dir = tmp_path / ("receipt-" + name)
    receipt_dir.mkdir()
    _view, anchor = _state_view(paths)
    return paths, report, append_dir, receipt_dir, anchor


def _drive(paths, append_dir, receipt_dir, anchor, point=""):
    """A brand-new interpreter, pinned at ``anchor``; point "" is fault-free."""
    argv = [str(append_dir), str(paths["state"]), str(receipt_dir), CASE,
            json.dumps(list(anchor)), AT_E3B, point]
    return _supervise(argv)


def _finals(receipt_dir):
    return sorted(n for n in os.listdir(receipt_dir)
                  if n.startswith(_FINAL_PREFIX))


def _tmps(receipt_dir):
    return sorted(n for n in os.listdir(receipt_dir)
                  if n.startswith(_TMP_PREFIX))


def _receipt_bytes(receipt_dir):
    names = _finals(receipt_dir)
    assert len(names) == 1, names
    return names[0], (Path(str(receipt_dir)) / names[0]).read_bytes()


# --------------------------------------------------------------------------
# c1) landed but not receipted: kill after the second merge commits
# --------------------------------------------------------------------------

def test_e3b_c1_sigkill_merge1_then_new_process_converges(world, tmp_path):
    paths, report, append_dir, receipt_dir, anchor0 = _three_key_case(
        world, tmp_path, "e3b-c1")

    killed = _drive(paths, append_dir, receipt_dir, anchor0, "merge:1")
    print("e3b-crash c1: real SIGKILL rc=%r" % killed.get("rc"))
    assert killed.get("rc") in (-9, 137), killed
    assert killed.get("lines") == []

    # Crash scene: exactly two merge events durable, receipt family empty.
    view, partial = _state_view(paths)
    assert partial[0] == anchor0[0] + 2
    assert partial[1] != anchor0[1]
    assert view.phase == RecoveryPhase.MATERIALS_STAGED.value
    assert os.listdir(receipt_dir) == []
    assert _decision(paths, K1[2]) == (
        frozenset({_S.CONSUMED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K2[2]) == (
        frozenset({_S.REVOKED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K3[2]) is None

    # A brand-new process, re-pinned at the host-observed live anchor, must
    # reach the unique terminal: K1/K2 exact-replay to skips, only K3 merges.
    converged = _drive(paths, append_dir, receipt_dir, partial)
    assert converged.get("ok"), converged
    receipt = converged["receipt"]
    assert receipt["journal_anchor_before"] == list(partial)
    view, terminal = _state_view(paths)
    assert terminal[0] == anchor0[0] + 3
    assert terminal[1] != partial[1]
    assert _decision(paths, K1[2]) == (
        frozenset({_S.CONSUMED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K2[2]) == (
        frozenset({_S.REVOKED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K3[2]) == (
        frozenset({_S.CONSUMED, _S.REVOKED}), False,
        ("state_conflict_stricter_preserved",))
    names = _finals(receipt_dir)
    assert len(names) == 1 and _tmps(receipt_dir) == []
    raw = (Path(str(receipt_dir)) / names[0]).read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() \
        == receipt["receipt_sha256"]
    mtime = (Path(str(receipt_dir)) / names[0]).stat().st_mtime_ns

    # Another brand-new process at the terminal anchor is a zero-write idle:
    # journal pinned, receipt bytes and mtime frozen.
    idle = _drive(paths, append_dir, receipt_dir, terminal)
    assert idle.get("ok"), idle
    assert idle["receipt"]["journal_anchor_before"] == list(terminal)
    assert idle["receipt"]["journal_anchor_after"] == list(terminal)
    assert idle["receipt"]["receipt_sha256"] == receipt["receipt_sha256"]
    assert _state_view(paths)[1] == terminal
    assert _finals(receipt_dir) == names and _tmps(receipt_dir) == []
    assert (Path(str(receipt_dir)) / names[0]).read_bytes() == raw
    assert (Path(str(receipt_dir)) / names[0]).stat().st_mtime_ns == mtime


# --------------------------------------------------------------------------
# c2) durable temp receipt, pre-rename kill: tmp is void, never adopted
# --------------------------------------------------------------------------

def test_e3b_c2_sigkill_receipt_tmp_void_and_sha_matches_clean_run(
        world, tmp_path):
    paths, report, append_dir, receipt_dir, anchor0 = _three_key_case(
        world, tmp_path, "e3b-c2")

    killed = _drive(paths, append_dir, receipt_dir, anchor0, "receipt_tmp")
    print("e3b-crash c2: real SIGKILL rc=%r" % killed.get("rc"))
    assert killed.get("rc") in (-9, 137), killed
    leftovers = _tmps(receipt_dir)
    assert len(leftovers) == 1, leftovers
    assert _finals(receipt_dir) == []
    # The journal is already terminal when the temp receipt is being written.
    view, terminal = _state_view(paths)
    assert terminal[0] == anchor0[0] + 3
    assert view.phase == RecoveryPhase.MATERIALS_STAGED.value
    for key in (K1, K2, K3):
        assert _decision(paths, key[2]) is not None

    # Independent fault-free success against the SAME live state, input and
    # at, but a pristine receipt root: every block is an exact replay and one
    # receipt is published with a terminal/terminal anchor pair.
    clean_dir = tmp_path / "receipt-e3b-c2-clean"
    clean_dir.mkdir()
    clean = _drive(paths, append_dir, clean_dir, terminal)
    assert clean.get("ok"), clean
    assert clean["receipt"]["journal_anchor_before"] == list(terminal)
    assert clean["receipt"]["journal_anchor_after"] == list(terminal)
    clean_names = _finals(clean_dir)
    assert len(clean_names) == 1 and _tmps(clean_dir) == []
    clean_raw = (Path(str(clean_dir)) / clean_names[0]).read_bytes()

    # The crashed root reentered by a new process: the tmp residue is reaped
    # as void and the published receipt is byte-identical to the clean run.
    converged = _drive(paths, append_dir, receipt_dir, terminal)
    assert converged.get("ok"), converged
    assert _tmps(receipt_dir) == []
    final_names = _finals(receipt_dir)
    assert final_names == clean_names
    final_raw = (Path(str(receipt_dir)) / final_names[0]).read_bytes()
    assert final_raw == clean_raw
    assert converged["receipt"]["receipt_sha256"] == \
        clean["receipt"]["receipt_sha256"]
    assert _state_view(paths)[1] == terminal


# --------------------------------------------------------------------------
# c3) receipt already out: stale pin refuses; live pin recomputes, no rewrite
# --------------------------------------------------------------------------

def test_e3b_c3_receipted_reentry_live_anchor_no_rewrite(world, tmp_path):
    paths, report, append_dir, receipt_dir, anchor0 = _three_key_case(
        world, tmp_path, "e3b-c3")

    green = _drive(paths, append_dir, receipt_dir, anchor0)
    assert green.get("ok"), green
    view, terminal = _state_view(paths)
    assert terminal[0] == anchor0[0] + 3
    name, raw0 = _receipt_bytes(receipt_dir)
    final_path = Path(str(receipt_dir)) / name
    mtime0 = final_path.stat().st_mtime_ns
    sha0 = green["receipt"]["receipt_sha256"]

    # A later process the host mistakenly pins at the OLD anchor is refused
    # by the fixed mismatch code; the receipt and journal are never touched.
    stale = _drive(paths, append_dir, receipt_dir, anchor0)
    assert stale.get("ok") is False, stale
    assert stale.get("reason") == "journal_anchor_mismatch"
    assert _state_view(paths)[1] == terminal
    assert final_path.read_bytes() == raw0
    assert final_path.stat().st_mtime_ns == mtime0
    assert sorted(os.listdir(receipt_dir)) == [name]

    # Re-pinned at the live anchor, a brand-new process recomputes against
    # the journal and returns the same receipt without rewriting its bytes.
    live = _drive(paths, append_dir, receipt_dir, terminal)
    assert live.get("ok"), live
    assert live["receipt"]["journal_anchor_before"] == list(terminal)
    assert live["receipt"]["journal_anchor_after"] == list(terminal)
    assert live["receipt"]["receipt_sha256"] == sha0
    assert final_path.read_bytes() == raw0
    assert final_path.stat().st_mtime_ns == mtime0
    assert sorted(os.listdir(receipt_dir)) == [name]
    assert _state_view(paths)[1] == terminal


# --------------------------------------------------------------------------
# c4) killed before the first write: startup suicide leaves zero half-state
# --------------------------------------------------------------------------

def test_e3b_c4_sigkill_at_boot_then_first_run_is_clean(world, tmp_path):
    paths, report, append_dir, receipt_dir, anchor0 = _three_key_case(
        world, tmp_path, "e3b-c4")

    killed = _drive(paths, append_dir, receipt_dir, anchor0, "boot")
    print("e3b-crash c4: real SIGKILL rc=%r" % killed.get("rc"))
    assert killed.get("rc") in (-9, 137), killed
    assert killed.get("lines") == []
    # The production entrypoint was never entered: journal and receipt root
    # are exactly as staged.
    assert _state_view(paths)[1] == anchor0
    assert os.listdir(receipt_dir) == []
    for key in (K1, K2, K3):
        assert _decision(paths, key[2]) is None

    # Reentry is indistinguishable from a first run: exactly three merges,
    # exactly one receipt, no duplication.
    first = _drive(paths, append_dir, receipt_dir, anchor0)
    assert first.get("ok"), first
    assert first["receipt"]["journal_anchor_before"] == list(anchor0)
    view, terminal = _state_view(paths)
    assert terminal[0] == anchor0[0] + 3
    assert view.phase == RecoveryPhase.MATERIALS_STAGED.value
    assert _decision(paths, K1[2]) == (
        frozenset({_S.CONSUMED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K2[2]) == (
        frozenset({_S.REVOKED}), False,
        ("state_conflict_stricter_preserved",))
    assert _decision(paths, K3[2]) == (
        frozenset({_S.CONSUMED, _S.REVOKED}), False,
        ("state_conflict_stricter_preserved",))
    names = _finals(receipt_dir)
    assert len(names) == 1 and _tmps(receipt_dir) == []

    # One more brand-new process proves the first-run terminal is stable.
    idle = _drive(paths, append_dir, receipt_dir, terminal)
    assert idle.get("ok"), idle
    assert idle["receipt"]["journal_anchor_before"] == list(terminal)
    assert idle["receipt"]["journal_anchor_after"] == list(terminal)
    assert idle["receipt"]["receipt_sha256"] == \
        first["receipt"]["receipt_sha256"]
    assert _state_view(paths)[1] == terminal
    assert _finals(receipt_dir) == names
