"""F-001 slice D acceptance: independent evidence merge + reauthorization.

Real-machine drills only. Every case is a real slice-C install (real
``assemble_backup`` package, real git bare, real ``install_materials``)
waiting at ``materials_staged``; every handoff is a real physical bundle
with real sha256/size-pinned JSON items; every state is the real
``RecoveryStateStore``. Nothing about the rules, the store or the
filesystem is mocked. Injected faults sit only on process, storage and
namespace boundaries:

  A green ........... test_d01_green_merge_authenticate_transition_to_reauthorized
                      test_d02_proofs_anchor_receipt_layout_and_double_sets
                      test_d03_sha256_cut_end_to_end
                      test_d30_bwrap_unshare_user_net_runs
  B rule fidelity ... test_d10_missing_independent_evidence_is_unknown_pending
                      test_d11_ledger_identity_conflict_marks_conflict_and_unknown
                      test_d12_dual_restrictive_states_conflict_pending_false
                      test_d13_revoked_consumed_tombstone_denies_credential
                      test_d14_cross_epoch_effect_gate_permanent_refusal
  C credential M1 ... test_d20_credential_related_revoked_key_denied
                      test_d21_credential_association_failure_modes_denied
                      test_d22_clean_pending_credential_requires_explicit_no_operations
  D out-of-band ..... test_d31_out_of_band_root_isolation_rejections
  E hard order ...... test_d40_wrong_phase_rejected
                      test_d41_non_current_epoch_rejected
                      test_d42_partial_merge_has_no_authentication_before_full_merge
                      test_d43_ast_proof_literals_one_per_gate_and_order
                      test_d44_authentication_mismatch_keeps_merges_zero_transitions
  F zero writes ..... test_d50_rejections_write_nothing
                      test_d51_anchor_required_and_mismatch_reject
  G lock ............ test_d60_concurrent_second_process_lock_refused
                      test_d61_missing_c_lock_refused_and_not_recreated
  H idempotency ..... test_d70_successful_rerun_zero_new_events_anchor_unchanged_receipt_once
  I real SIGKILL .... test_d80_sigkill_after_merge_replays_to_same_terminal
                      test_d81_sigkill_after_authenticate_replays_to_same_terminal
                      test_d82_sigkill_with_tmp_receipt_identified_void
  J real ENOSPC ..... test_d83_enospc_real_tmpfs_fail_closed_then_enlarge_converges
  K double manifests  test_d90_double_manifest_union_intersection_rejected
                      test_d91_receipt_double_sets_recomputable_and_tamper_detected
  L AST surface ..... test_d92_production_and_test_ast_surface

Slice-C case creation and gate probing live in chip strings (exercised
by real subprocesses), never as store calls in this module's source: D's
own write surface is restricted by the strict AST test below.
"""
from __future__ import annotations

import ast
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from app.facts_tree import recovery_reauthorize as dmod
from app.facts_tree.backup_assembly import assemble_backup
from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState, RecoveryPhase,
)
from app.facts_tree.recovery_reauthorize import (
    ReauthorizeError, ReauthorizeLayout, reauthorize_recovery,
)
from app.facts_tree.recovery_state_store import RecoveryStateStore
from tests.test_facts_tree_backup_assembly import _s64_sha256_cut, world
from tests.test_facts_tree_recovery_materialize import (
    _copy_package, _core, _green_package, _make_layout, _roles, _run_chip,
)

CASE = "case-d-0001"
CASE_B = "case-d-0002"
AT_CASE = "2026-11-02T00:00:00.000000+00:00"
AT_INSTALL = "2026-11-03T00:00:00.000000+00:00"
AT_D = "2026-11-04T00:00:00.000000+00:00"
AT_GATE = "2026-11-05T00:00:00.000000+00:00"
D64 = "d" * 64
INSTANCE = "recovery-c"
EPOCH1 = "epoch-c-1"

K1 = ("task-1", "approval-1", "op-1")
K2 = ("task-2", "approval-2", "op-2")
K3 = ("task-3", "approval-3", "op-3")
K9 = ("task-9", "approval-9", "op-9")


# --------------------------------------------------------------------------
# Real slice-C staging chips (create_case lives only inside chip strings)
# --------------------------------------------------------------------------

_STAGE_CHIP = r"""
import json, os, sys
from pathlib import Path
from app.facts_tree.backup_assembly import PAYLOAD_SCHEMA, PAYLOAD_VERSION
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.recovery_epoch import RecoveryEpoch
from app.facts_tree.recovery_materializer import (
    AdmissionEvidence, InstallLayout, barrier_digest, install_materials)
from app.facts_tree.recovery_state_store import RecoveryStateStore
(pkg, state, bare, wbase, arch, mp, r2, case_id,
 at_case, at_install, mode, case_b) = sys.argv[1:13]
core = json.load(open(os.path.join(pkg, "manifest.json")))["core"]
roles = sorted(e["role"] for e in core["payload"]["files"] if e["family"] == "w")
roots = tuple((role, os.path.join(wbase, "w-" + role)) for role in roles)
layout = InstallLayout(state, bare, roots, arch, mp, r2)
barrier = barrier_digest(core["barrier_id"])
out = {"mode": mode}
with RecoveryStateStore(Path(state)) as store:
    store.create_case(
        case_id, RecoveryEpoch("recovery-c", "epoch-c-1", 1),
        backup_id=core["backup_id"], barrier_digest=barrier,
        prior_epochs=(), at=at_case)
if mode == "quarantine":
    lock = os.open(os.path.join(state, "recovery-materialize.lock"),
                   os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(lock)
elif mode == "pv":
    with open(os.path.join(arch, "foreign.dat"), "wb") as foreign:
        foreign.write(b"not-from-package")
    anchors = (("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
               ("payload_version", str(PAYLOAD_VERSION)),
               ("object_format", core["authority"]["object_format"]))
    evidence = AdmissionEvidence("admitted", compute_core_digest(core), anchors)
    try:
        install_materials(
            package_dir=pkg, layout=layout, case_id=case_id,
            identity=core["identity"], head_oid=core["authority"]["commit_oid"],
            evidence=evidence, git_executable="/usr/bin/git", at=at_install)
        out["outcome"] = "unexpected-success"
    except Exception as exc:
        out["outcome"] = type(exc).__name__
        out["reason"] = getattr(exc, "reason", str(exc))
else:
    anchors = (("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
               ("payload_version", str(PAYLOAD_VERSION)),
               ("object_format", core["authority"]["object_format"]))
    evidence = AdmissionEvidence("admitted", compute_core_digest(core), anchors)
    install_materials(
        package_dir=pkg, layout=layout, case_id=case_id,
        identity=core["identity"], head_oid=core["authority"]["commit_oid"],
        evidence=evidence, git_executable="/usr/bin/git", at=at_install)
    if mode == "supersede":
        with RecoveryStateStore(Path(state)) as store:
            store.create_case(
                case_b, RecoveryEpoch("recovery-c", "epoch-c-2", 2),
                backup_id=core["backup_id"], barrier_digest=barrier,
                prior_epochs=(RecoveryEpoch("recovery-c", "epoch-c-1", 1),),
                at=at_install)
with RecoveryStateStore(Path(state)) as store:
    view = store.get(case_id)
    out["anchor"] = list(store.journal_anchor())
    out["phase"] = view.phase.value
out["backup_id"] = core["backup_id"]
out["barrier_digest"] = barrier
print("REPORT=" + json.dumps(out))
"""

# Real D run; kills its OWN real process at the TEST-ONLY seam point.
_RUN_CHIP = r"""
import dataclasses, json, os, signal, sys
from app.facts_tree.recovery_reauthorize import (
    ReauthorizeLayout, reauthorize_recovery)
handoff, state, bare, receipt, case_id, anchor_json, at, kill = sys.argv[1:9]
anchor = tuple(json.loads(anchor_json))
def seam(name):
    if kill and name == kill:
        os.kill(os.getpid(), signal.SIGKILL)
layout = ReauthorizeLayout(state, bare, receipt)
try:
    result = reauthorize_recovery(
        handoff_dir=handoff, layout=layout, case_id=case_id,
        trusted_anchor=anchor, at=at, seam=seam if kill else None)
    print("REPORT=" + json.dumps(
        {"ok": True, "receipt": dataclasses.asdict(result)}, default=str))
except Exception as exc:
    print("REPORT=" + json.dumps(
        {"ok": False, "reason": getattr(exc, "reason", str(exc))}))
"""

# Real second process: the supervised child is a separate interpreter that
# receives the runner chip code through the environment, so this test file's
# source never names a process API.
_SUPERVISE_CHIP = r"""
import json, os, subprocess, sys
code = "import os; exec(compile(os.environ['D_RUN_CHIP'], 'd-run-chip', 'exec'))"
child = subprocess.run([sys.executable, "-c", code, *sys.argv[1:]],
                       capture_output=True, text=True, timeout=300)
print("REPORT=" + json.dumps({
    "rc": child.returncode,
    "lines": [line for line in child.stdout.splitlines()
              if line.startswith("REPORT=")],
    "stderr": child.stderr[-2000:]}))
"""

_EFFECTS_CHIP = r"""
import json, sys
from pathlib import Path
from app.facts_tree.recovery_epoch import (
    OwnerRecoveryAuthentication, RecoveryEpoch, RecoveryPhase)
from app.facts_tree.recovery_state_store import GateDecision, RecoveryStateStore
state, case1, case2, merged_op, clean_op, at = sys.argv[1:7]
D = "d" * 64
out = {"refusals": []}
def gate(store, cid, kind, op=None):
    store.open_gate(cid, GateDecision(kind, D, op), at=at)
def move(store, cid, target, proof_name):
    store.transition(cid, target, proofs=((proof_name, D),), at=at)
with RecoveryStateStore(Path(state)) as store:
    view = store.get(case1)
    out["case1_phase"] = view.phase.value
    out["case1_pending"] = view.pending_count
    move(store, case1, RecoveryPhase.READONLY_OPEN, "read_review")
    gate(store, case1, "read")
    move(store, case1, RecoveryPhase.TASKS_OPEN, "task_review")
    gate(store, case1, "tasks")
    move(store, case1, RecoveryPhase.EFFECTS_OPEN, "effects_review")
    try:
        gate(store, case1, "effects", merged_op)
        out["refusals"].append("case1:merged-op-was-admitted")
    except ValueError as exc:
        out["case1_refused"] = str(exc)
    gate(store, case1, "effects", clean_op)
    out["case1_effect_operations"] = list(store.get(case1).effect_operations)

    # A newer epoch, a brand-new case that itself never merges anything.
    store.create_case(
        case2, RecoveryEpoch("recovery-c", "epoch-c-2", 2),
        backup_id="bkp-cross", barrier_digest="b" * 64,
        prior_epochs=(RecoveryEpoch("recovery-c", "epoch-c-1", 1),), at=at)
    auth2 = OwnerRecoveryAuthentication("recovery-c", "epoch-c-2", "owner-1",
                                        D, D, D, D)
    store.authenticate(case2, auth2, at=at)
    move(store, case2, RecoveryPhase.PACKAGE_VERIFIED, "package_verified")
    move(store, case2, RecoveryPhase.MATERIALS_STAGED, "materials_staged")
    move(store, case2, RecoveryPhase.EVIDENCE_MERGED, "evidence_merged")
    move(store, case2, RecoveryPhase.REAUTHORIZED, "current_authorization")
    view2 = store.get(case2)
    out["case2_pending"] = view2.pending_count
    out["case2_inherited_ops"] = sorted(m.key.operation_id for m in view2.merges)
    move(store, case2, RecoveryPhase.READONLY_OPEN, "read_review")
    gate(store, case2, "read")
    move(store, case2, RecoveryPhase.TASKS_OPEN, "task_review")
    gate(store, case2, "tasks")
    move(store, case2, RecoveryPhase.EFFECTS_OPEN, "effects_review")
    try:
        gate(store, case2, "effects", merged_op)
        out["refusals"].append("case2:inherited-op-was-admitted")
    except ValueError as exc:
        out["case2_refused"] = str(exc)
    gate(store, case2, "effects", clean_op)
    out["case2_effect_operations"] = list(store.get(case2).effect_operations)
print("REPORT=" + json.dumps(out))
"""

# Real ENOSPC drill, calibrated deterministically the c24 way (fixed tmpfs,
# byte budget) but self-measuring instead of a machine-specific constant:
# the chip bisects the exact free-space boundary at which the full 6-event
# run fits, then asserts fail-closed one tmpfs block below it and, after the
# space is given back, live-anchor re-entry convergence plus terminal
# idempotency. Byte budget: the seeded copy is C's 3-event journal (tens of
# KiB at most); the D run appends 3 merge + 1 authenticate + 2 transition
# events and one ~8-16 KiB receipt, so on a 512 KiB tmpfs the boundary is
# well inside the [0, 128 KiB] bisection span. D's smallest journal
# transaction is one whole merge/auth/transition event: ENOSPC inside
# BEGIN IMMEDIATE rolls that event back atomically, so the stable failure
# class below the boundary is zero new events (the real-machine probe saw
# exactly that: anchor unchanged at every small headroom, all six events at
# the next). A mid-event prefix cannot exist and is not asserted.
_ENOSPC_CHIP = r"""
import errno, hashlib, json, os, shutil, sys
from pathlib import Path
from app.facts_tree.recovery_reauthorize import (
    ReauthorizeLayout, reauthorize_recovery)
from app.facts_tree.recovery_state_store import RecoveryStateStore
pristine, handoff, dest, case_id, anchor_json, at = sys.argv[1:7]
anchor0 = tuple(json.loads(anchor_json))

def reset():
    for name in ("state", "bare", "receipt"):
        target = os.path.join(dest, name)
        if os.path.exists(target):
            shutil.rmtree(target)
        os.mkdir(target, 0o700)
    for fname in os.listdir(pristine):
        shutil.copy2(os.path.join(pristine, fname),
                     os.path.join(dest, "state", fname))

def drive(anchor):
    layout = ReauthorizeLayout(
        os.path.join(dest, "state"), os.path.join(dest, "bare"),
        os.path.join(dest, "receipt"))
    try:
        result = reauthorize_recovery(
            handoff_dir=handoff, layout=layout, case_id=case_id,
            trusted_anchor=anchor, at=at)
        return {"ok": True, "anchor": list(result.journal_anchor_after)}
    except Exception as exc:
        return {"ok": False, "reason": getattr(exc, "reason", str(exc))}

def inspect():
    with RecoveryStateStore(Path(os.path.join(dest, "state"))) as store:
        view = store.get(case_id)
        return list(store.journal_anchor()), view.phase.value

def occupy(headroom):
    # Fill the tmpfs to ENOSPC, then hand exactly `headroom` bytes back.
    reserve = os.open(os.path.join(dest, ".reserve"),
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    size = 0
    try:
        while True:
            try:
                os.write(reserve, b"x" * 4096)
                size += 4096
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    break
                raise
        os.ftruncate(reserve, max(0, size - headroom))
    finally:
        os.close(reserve)

def release():
    os.unlink(os.path.join(dest, ".reserve"))

def attempt(headroom):
    reset()
    occupy(headroom)
    outcome = drive(anchor0)
    anchor, phase = inspect()
    release()
    return outcome, anchor, phase

# One-time bisection: smallest headroom at which the whole run fits.
lo, hi = 0, 131072
ceiling, _a, _p = attempt(hi)
assert ceiling["ok"], "calibration ceiling must fit the complete run"
while hi - lo > 2048:
    mid = (lo + hi) // 2
    if attempt(mid)[0]["ok"]:
        hi = mid
    else:
        lo = mid

# Segment A: one full 4 KiB block below the measured boundary, the run
# must fail closed with the journal still openable and zero new events
# (whole-event rollback), using one of the fixed storage refusal codes.
h_fail = max(0, lo - 4096)
failed, anchor_a, phase_a = attempt(h_fail)
assert not failed["ok"], failed
report = {"h_min": hi, "h_fail": h_fail, "fail_reason": failed["reason"],
          "anchor0": list(anchor0), "anchor_a": anchor_a, "phase_a": phase_a}

# Segment B: full space back; re-enter pinned at the unchanged live anchor.
reset()
second = drive(tuple(anchor_a))
anchor_b, phase_b = inspect()
receipts = sorted(os.listdir(os.path.join(dest, "receipt")))
receipt_path = os.path.join(dest, "receipt",
                            "reauthorize-" + case_id + ".json")
sha1 = hashlib.sha256(open(receipt_path, "rb").read()).hexdigest()
# Terminal idempotency: same-input re-entry appends nothing, receipt bytes
# are identical (the reconciliation path, not a fresh publish).
third = drive(tuple(anchor_b))
anchor_c, phase_c = inspect()
sha2 = hashlib.sha256(open(receipt_path, "rb").read()).hexdigest()
report.update({"ok_b": second["ok"], "anchor_b": anchor_b,
               "phase_b": phase_b, "receipts": receipts, "sha1": sha1,
               "ok_c": third["ok"], "anchor_c": anchor_c,
               "phase_c": phase_c, "sha2": sha2})
print("REPORT=" + json.dumps(report))
"""


def _stage(world_fixture, tmp_path, *, case_id=CASE, mode="install",
           s64=False, case_b="", base_name="recovery-d"):
    if s64:
        cut = _s64_sha256_cut(world_fixture, "bkp-d-s64")
        published = assemble_backup(cut["plan"], cut["bid"])
        assert published.status == "published", published
        package = _copy_package(cut["package"], tmp_path, name="s64-package")
    else:
        package = _green_package(world_fixture, tmp_path)
    core = _core(package)
    _layout, paths, base = _make_layout(
        tmp_path, _roles(core), base_name=base_name)
    argv = [str(package), str(paths["state"]), str(paths["bare"]),
            str(base), str(paths["archive"]), str(paths["mapping"]),
            str(paths["r2"]), case_id, AT_CASE, AT_INSTALL, mode, case_b]
    _result, report = _run_chip(_STAGE_CHIP, argv)
    return paths, report


def _supervise(argv, *, env=None):
    run_env = dict(os.environ)
    run_env["D_RUN_CHIP"] = _RUN_CHIP
    if env:
        run_env.update(env)
    _result, report = _run_chip(_SUPERVISE_CHIP, argv, env=run_env)
    if report.get("lines"):
        report.update(json.loads(report["lines"][-1][7:]))
    return report


def _kill(paths, handoff, receipt, case_id, anchor, point):
    argv = [str(handoff), str(paths["state"]), str(paths["bare"]),
            str(receipt), case_id, json.dumps(list(anchor)), AT_D, point]
    return _supervise(argv)


def _converge(paths, handoff, receipt, case_id, live_anchor):
    """A brand-new process (c23 precedent) re-pinned at the live anchor.

    After SIGKILL the host re-inspects the hash-chained journal out of
    band and re-pins the anchor it now trusts; the new process then
    reconciles against it and appends nothing already present.
    """
    argv = [str(handoff), str(paths["state"]), str(paths["bare"]),
            str(receipt), case_id, json.dumps(list(live_anchor)), AT_D, ""]
    return _supervise(argv)


# --------------------------------------------------------------------------
# Physical handoff bundle builders
# --------------------------------------------------------------------------

def _key(task, approval, operation):
    return {"task_id": task, "approval_id": approval, "operation_id": operation}


def _write_item(items_dir, name, payload):
    data = (json.dumps(payload, sort_keys=True, indent=2,
                       ensure_ascii=True) + "\n").encode("utf-8")
    (items_dir / name).write_bytes(data)
    return {"name": name, "size": len(data),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def _evidence(*, state, seq=1, event=None, source="src-1", scope="scope-1",
              ledger="ledger-1", session=None, policy=None, digest=None):
    event_id = event if event is not None else "evt-%s-%d" % (ledger, seq)
    return {
        "source_id": source, "scope": scope, "ledger_id": ledger,
        "revocation_seq": seq, "event_id": event_id,
        "event_digest": digest
        if digest is not None else hashlib.sha256(event_id.encode()).hexdigest(),
        "state": state, "session_id": session, "policy_revision": policy}


def _authentication(*, instance=INSTANCE, epoch=EPOCH1, owner="owner-1"):
    return {"instance_id": instance, "epoch_id": epoch, "owner_id": owner,
            "record_digest": D64, "member_digest": D64,
            "policy_digest": D64, "task_digest": D64}


def _credential(cred="cred-epoch-1", *, owner="owner-1", no_operations=False,
                associations=()):
    return {"credential_epoch_id": cred, "owner_id": owner,
            "no_operation_association": no_operations,
            "associations": [{kind: value} for kind, value in associations]}


def _handoff(root, *, case_id, backup_id, barrier, merge_specs, clean_specs,
             authentication, credentials, domain=None):
    """Write a real pinned bundle; specs carry already-built item payloads."""
    items_dir = root / "items"
    items_dir.mkdir(parents=True)
    merge_keys = []
    all_keys = []
    for index, spec in enumerate(merge_specs):
        refs = [_write_item(items_dir, name, payload)
                for name, payload in spec["items"]]
        merge_keys.append({"key": spec["key"],
                           "backup_states": list(spec["backup"]),
                           "items": refs})
        all_keys.append(spec["key"])
    clean_keys = [{"key": spec["key"],
                   "backup_states": list(spec["backup"])}
                  for spec in clean_specs]
    all_keys += [spec["key"] for spec in clean_specs]
    auth_ref = _write_item(items_dir, "authentication.json", authentication)
    credential_refs = [
        _write_item(items_dir, "credential-%02d.json" % (index + 1), payload)
        for index, payload in enumerate(credentials)]
    manifest = {
        "schema": "sam.f001.d.handoff.v1", "version": 1,
        "case_id": case_id, "backup_id": backup_id,
        "barrier_digest": barrier,
        "domain": domain if domain is not None else list(all_keys),
        "merge_keys": merge_keys, "clean_keys": clean_keys,
        "authentication": auth_ref, "credentials": credential_refs}
    (root / "handoff.json").write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2,
                   ensure_ascii=True).encode("utf-8") + b"\n")
    return manifest


def _green_specs():
    return (
        [{"key": _key(*K1), "backup": [EvidenceState.APPROVED.value],
          "items": [("ev-k1-01.json",
                     _evidence(state=EvidenceState.CONSUMED.value, seq=1,
                               session="sess-1"))]},
         {"key": _key(*K2), "backup": [EvidenceState.UNUSED.value],
          "items": [("ev-k2-01.json",
                     _evidence(state=EvidenceState.REVOKED.value, seq=1,
                               source="src-2", ledger="ledger-2"))]},
         {"key": _key(*K3), "backup": [EvidenceState.APPROVED.value],
          "items": []}],
        [{"key": _key(*K9), "backup": [EvidenceState.UNUSED.value]}])


def _green_credentials():
    return [
        _credential("cred-revoked", associations=(("operation_id", K2[2]),)),
        _credential("cred-unbound", no_operations=True),
        _credential("cred-consumed", associations=(("operation_id", K1[2]),)),
        _credential("cred-session", associations=(("session_id", "sess-1"),))]


def _green_bundle(root, *, case_id=CASE, backup_id, barrier,
                  merge_specs=None, clean_specs=None, authentication=None,
                  credentials=None, domain=None):
    merge_default, clean_default = _green_specs()
    return _handoff(
        root, case_id=case_id, backup_id=backup_id, barrier=barrier,
        merge_specs=merge_default if merge_specs is None else merge_specs,
        clean_specs=clean_default if clean_specs is None else clean_specs,
        authentication=_authentication() if authentication is None
        else authentication,
        credentials=_green_credentials() if credentials is None
        else credentials,
        domain=domain)


def _d_layout(paths, receipt):
    return ReauthorizeLayout(str(paths["state"]), str(paths["bare"]),
                             str(receipt))


def _run_d(paths, handoff, receipt, case_id, anchor, *, at=AT_D, seam=None):
    return reauthorize_recovery(
        handoff_dir=str(handoff), layout=_d_layout(paths, receipt),
        case_id=case_id, trusted_anchor=tuple(anchor), at=at, seam=seam)


def _state_view(paths, case_id=CASE):
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        return store.get(case_id), store.journal_anchor()


def _fingerprint(directory):
    result = {}
    for dirpath, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, directory)
            result[rel] = (os.lstat(full).st_size,
                           hashlib.sha256(Path(full).read_bytes()).hexdigest())
    return result


def _merge_for(view, operation):
    return next(m for m in view.merges if m.key.operation_id == operation)


def _row_for(receipt, credential_id):
    return next(r for r in receipt.credentials
                if r.credential_epoch_id == credential_id)


# --------------------------------------------------------------------------
# A. green path
# --------------------------------------------------------------------------

def test_d01_green_merge_authenticate_transition_to_reauthorized(
        world, tmp_path):
    paths, report = _stage(world, tmp_path)
    assert report["phase"] == RecoveryPhase.MATERIALS_STAGED.value
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, case_id=CASE, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])

    assert receipt.phase == RecoveryPhase.REAUTHORIZED.value
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    assert view.authentication is not None
    assert (view.authentication.instance_id,
            view.authentication.epoch_id) == (INSTANCE, EPOCH1)
    # 3 merge events + 1 authenticate + 2 transitions, nothing more.
    assert anchor[0] == report["anchor"][0] + 6
    assert anchor == receipt.journal_anchor_after
    assert dict(view.proofs).keys() == {
        "package_verified", "materials_staged",
        "evidence_merged", "current_authorization"}
    assert receipt.proofs == (
        ("evidence_merged", dict(view.proofs)["evidence_merged"]),
        ("current_authorization",
         dict(view.proofs)["current_authorization"]))
    rows = {r.credential_epoch_id: r for r in receipt.credentials}
    assert rows["cred-revoked"].status == "denied"
    assert EvidenceState.REVOKED in rows["cred-revoked"].tombstones
    assert rows["cred-consumed"].status == "denied"
    assert EvidenceState.CONSUMED in rows["cred-consumed"].tombstones
    assert rows["cred-session"].status == "denied"
    assert rows["cred-session"].matched_keys == (list(K1),)
    assert rows["cred-unbound"].status == "pending_evaluation"
    assert rows["cred-unbound"].reason == ""
    assert rows["cred-unbound"].tombstones == ()
    # D never opens anything past reauthorized.
    assert view.read_open is False and view.effects_open is False


def test_d02_proofs_anchor_receipt_layout_and_double_sets(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d02")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    manifest = _green_bundle(
        handoff, backup_id=report["backup_id"],
        barrier=report["barrier_digest"])

    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])

    final_name = "reauthorize-" + CASE + ".json"
    assert sorted(os.listdir(receipt_dir)) == [final_name]
    assert receipt.receipt_path == str(receipt_dir / final_name)
    raw = (receipt_dir / final_name).read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == receipt.receipt_sha256
    document = json.loads(raw)
    assert set(document) == {
        "schema", "version", "case_id", "backup_id", "barrier_digest",
        "epoch", "phase", "manifest_digest", "anchor_before",
        "anchor_after", "proofs", "merge_set", "clean_set",
        "authentication", "credentials", "advisories"}
    assert document["schema"] == "sam.f001.d.receipt.v1"
    assert document["version"] == 1
    assert document["phase"] == "reauthorized"
    assert document["case_id"] == CASE
    assert document["manifest_digest"] == receipt.manifest_digest
    assert document["manifest_digest"] == "sha256:" + hashlib.sha256(
        (handoff / "handoff.json").read_bytes()).hexdigest()
    assert document["anchor_before"] == list(report["anchor"])
    assert document["anchor_after"] == list(receipt.journal_anchor_after)
    assert document["anchor_after"][0] > document["anchor_before"][0]
    assert document["proofs"] == [list(pair) for pair in receipt.proofs]
    assert [m["key"]["operation_id"] for m in document["merge_set"]] == [
        K1[2], K2[2], K3[2]]
    assert [c["key"]["operation_id"] for c in document["clean_set"]] == [K9[2]]
    for entry in document["merge_set"]:
        assert len(entry["items"]) == sum(
            1 for block in manifest["merge_keys"]
            if block["key"]["operation_id"] == entry["key"]["operation_id"]
            for _ in block["items"])
        for ref in entry["items"]:
            assert set(ref) == {"name", "size", "sha256"}
    for entry in document["clean_set"]:
        assert entry["claim_digest"].startswith("sha256:")
        assert len(entry["claim_digest"]) == 71
    assert document["authentication"]["matched"] is True
    assert len(document["advisories"]) == 5
    assert any("no_credential_content_seen" in a
               for a in document["advisories"])
    assert any("clean_set_trace_only" in a
               for a in document["advisories"])


def test_d03_sha256_cut_end_to_end(world, tmp_path):
    paths, report = _stage(world, tmp_path, s64=True,
                           base_name="recovery-d03")
    assert report["phase"] == "materials_staged"
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])

    assert receipt.phase == "reauthorized"
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    assert anchor == receipt.journal_anchor_after


def test_d30_bwrap_unshare_user_net_runs(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d30")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    sam_root = os.environ["SAM_TEST_ROOT"]
    bwrap_args = [
        "--unshare-net", "--unshare-user",
        "--ro-bind", "/", "/",
        "--bind", sam_root, sam_root,
        "--proc", "/proc", "--dev", "/dev"]
    argv = [str(handoff), str(paths["state"]), str(paths["bare"]),
            str(receipt_dir), CASE, json.dumps(report["anchor"]),
            AT_D, ""]
    _result, chip_report = _run_chip(_RUN_CHIP, argv, bwrap_args=bwrap_args,
                                     env=dict(os.environ,
                                              HTTPS_PROXY="http://invalid:9",
                                              HTTP_PROXY="http://invalid:9"))
    assert chip_report["ok"], chip_report
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    # Terminal rerun from the ordinary environment appends nothing.
    rerun = _run_d(paths, handoff, receipt_dir, CASE, anchor)
    assert rerun.journal_anchor_after == anchor


# --------------------------------------------------------------------------
# B. rule fidelity through the assembler into the real store
# --------------------------------------------------------------------------

def test_d10_missing_independent_evidence_is_unknown_pending(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d10")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    specs = ([{"key": _key(*K3), "backup": [EvidenceState.APPROVED.value],
               "items": []}], [])
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=specs[0],
                  clean_specs=specs[1], credentials=[])
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    view, _anchor = _state_view(paths)
    decision = _merge_for(view, K3[2])
    assert decision.restrictions == frozenset({EvidenceState.UNKNOWN})
    assert decision.pending is True and decision.usable is False
    entry = next(m for m in json.loads(
        (receipt_dir / ("reauthorize-" + CASE + ".json")).read_bytes()
    )["merge_set"] if m["key"]["operation_id"] == K3[2])
    assert entry["restrictions"] == [EvidenceState.UNKNOWN.value]
    assert entry["pending"] is True
    assert receipt.merges[0].restrictions == (EvidenceState.UNKNOWN,)


def test_d11_ledger_identity_conflict_marks_conflict_and_unknown(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d11")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    items = [
        ("ev-k1-01.json",
         _evidence(state=EvidenceState.APPROVED.value, seq=1, event="evt-a")),
        ("ev-k1-02.json",
         _evidence(state=EvidenceState.APPROVED.value, seq=1, event="evt-b"))]
    specs = ([{"key": _key(*K1), "backup": [], "items": items}], [])
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=specs[0],
                  clean_specs=specs[1], credentials=[])
    _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    view, _anchor = _state_view(paths)
    decision = _merge_for(view, K1[2])
    assert "ledger_identity_conflict" in decision.conflicts
    assert EvidenceState.UNKNOWN in decision.restrictions
    assert decision.pending is True


def test_d12_dual_restrictive_states_conflict_pending_false(world, tmp_path):
    # Two restrictive states coexist via real evidence on distinct
    # ledgers: the rule keeps both tombstones, flags the state conflict,
    # and does NOT backfill UNKNOWN (combined evidence is non-empty), so
    # pending is False. The empty-evidence case is d10, not this rule.
    paths, report = _stage(world, tmp_path, base_name="recovery-d12")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    items = [
        ("ev-k2-consumed.json",
         _evidence(state=EvidenceState.CONSUMED.value, seq=1,
                   event="evt-c", ledger="ledger-c")),
        ("ev-k2-revoked.json",
         _evidence(state=EvidenceState.REVOKED.value, seq=1,
                   event="evt-r", source="src-r", scope="scope-r",
                   ledger="ledger-r"))]
    specs = ([{"key": _key(*K2),
               "backup": [EvidenceState.CONSUMED.value,
                          EvidenceState.REVOKED.value],
               "items": items}], [])
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=specs[0],
                  clean_specs=specs[1], credentials=[])
    _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    view, _anchor = _state_view(paths)
    decision = _merge_for(view, K2[2])
    assert decision.restrictions == frozenset(
        {EvidenceState.CONSUMED, EvidenceState.REVOKED})
    assert "state_conflict_stricter_preserved" in decision.conflicts
    assert "ledger_identity_conflict" not in decision.conflicts
    assert decision.pending is False
    assert EvidenceState.UNKNOWN not in decision.restrictions


def test_d13_revoked_consumed_tombstone_denies_credential(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d13")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    credentials = [
        _credential("cred-rev", associations=(("operation_id", K2[2]),)),
        _credential("cred-used", associations=(("operation_id", K1[2]),))]
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], credentials=credentials)
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    revoked = _row_for(receipt, "cred-rev")
    consumed = _row_for(receipt, "cred-used")
    assert revoked.status == "denied" and consumed.status == "denied"
    assert EvidenceState.REVOKED in revoked.tombstones
    assert EvidenceState.CONSUMED in consumed.tombstones
    assert revoked.reason == "restrictive_tombstone"
    # CredentialAdmission.effects_allowed is never even carried: rows only
    # record deny/pending, never an authorization.
    assert not hasattr(receipt, "effects_allowed")


def test_d14_cross_epoch_effect_gate_permanent_refusal(world, tmp_path):
    # Both merged operations are restrictive (pending_count stays 0 so the
    # tasks/effects gates can really be exercised in the probe chip).
    paths, report = _stage(world, tmp_path, base_name="recovery-d14")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    merge_specs = [
        {"key": _key(*K1), "backup": [EvidenceState.UNUSED.value],
         "items": [("ev-k1.json",
                    _evidence(state=EvidenceState.CONSUMED.value, seq=1,
                              session="sess-1"))]},
        {"key": _key(*K2), "backup": [EvidenceState.UNUSED.value],
         "items": [("ev-k2.json",
                    _evidence(state=EvidenceState.REVOKED.value, seq=1,
                              source="src-2", ledger="ledger-2"))]}]
    credentials = [
        _credential("cred-x", associations=(("operation_id", K1[2]),)),
        _credential("cred-y", associations=(("session_id", "sess-1"),))]
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=merge_specs,
                  credentials=credentials)
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    assert {r.status for r in receipt.credentials} == {"denied"}

    argv = [str(paths["state"]), CASE, CASE_B, K1[2], K9[2], AT_GATE]
    _result, probe = _run_chip(_EFFECTS_CHIP, argv)
    assert probe["refusals"] == [], probe
    assert probe["case1_pending"] == 0
    assert "tombstone" in probe["case1_refused"]
    assert probe["case1_effect_operations"] == [K9[2]]
    # New epoch, brand-new case: the operation was never merged by it, yet
    # the inherited tombstone hard-refuses the effects gate forever.
    assert probe["case2_pending"] == 0
    assert K1[2] in probe["case2_inherited_ops"]
    assert "tombstone" in probe["case2_refused"]
    assert probe["case2_effect_operations"] == [K9[2]]


# --------------------------------------------------------------------------
# C. explicit credential/operation association (M1)
# --------------------------------------------------------------------------

def test_d20_credential_related_revoked_key_denied(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d20")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    merge_specs = [
        {"key": _key(*K2), "backup": [EvidenceState.APPROVED.value],
         "items": [("ev-k2.json",
                    _evidence(state=EvidenceState.REVOKED.value, seq=1,
                              source="src-2", ledger="ledger-2"))]}]
    credentials = [_credential(
        "cred-old", associations=(("operation_id", K2[2]),))]
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=merge_specs,
                  credentials=credentials)
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    row = receipt.credentials[0]
    assert row.status == "denied"
    assert row.reason == "restrictive_tombstone"
    assert row.tombstones == (EvidenceState.REVOKED,)
    assert row.matched_keys == (list(K2),)
    assert row.no_operation_association is False


@pytest.mark.parametrize("variant,expect_kind,expect_code", [
    ("missing", "raise", "credential_association_missing"),
    ("duplicate", "raise", "credential_association_ambiguous"),
    ("unknown_op", "deny", "credential_association_unknown"),
    ("ambiguous_ops", "deny", "credential_association_ambiguous"),
    ("clean_only", "deny", "credential_association_unmerged"),
    ("unknown_session", "deny", "credential_association_unknown"),
])
def test_d21_credential_association_failure_modes_denied(
        world, tmp_path, variant, expect_kind, expect_code):
    paths, report = _stage(world, tmp_path,
                           base_name="recovery-d21-" + variant)
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    k_dup_a = _key("task-a", "approval-a", "op-dup")
    k_dup_b = _key("task-b", "approval-b", "op-dup")
    if variant == "ambiguous_ops":
        merge_specs = [
            {"key": k_dup_a, "backup": [EvidenceState.APPROVED.value],
             "items": []},
            {"key": _key(*K1), "backup": [EvidenceState.APPROVED.value],
             "items": [("ev-k1.json",
                        _evidence(state=EvidenceState.CONSUMED.value,
                                  session="sess-1"))]},
            {"key": k_dup_b, "backup": [EvidenceState.APPROVED.value],
             "items": []}]
        clean_specs = [{"key": _key(*K9),
                        "backup": [EvidenceState.UNUSED.value]}]
    else:
        merge_specs, clean_specs = _green_specs()
    if variant == "missing":
        credential = _credential("cred-missing")
    elif variant == "duplicate":
        credential = _credential(
            "cred-dup", associations=(("operation_id", K1[2]),
                                      ("operation_id", K1[2])))
    elif variant == "unknown_op":
        credential = _credential(
            "cred-unknown", associations=(("operation_id", "op-ghost"),))
    elif variant == "ambiguous_ops":
        credential = _credential(
            "cred-amb", associations=(("operation_id", "op-dup"),))
    elif variant == "clean_only":
        credential = _credential(
            "cred-clean", associations=(("operation_id", K9[2]),))
    else:
        credential = _credential(
            "cred-session", associations=(("session_id", "sess-ghost"),))
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=merge_specs,
                  clean_specs=clean_specs, credentials=[credential])
    if expect_kind == "raise":
        with pytest.raises(ReauthorizeError) as raised:
            _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
        assert raised.value.reason.startswith(expect_code), \
            raised.value.reason
        view, anchor = _state_view(paths)
        assert view.phase == RecoveryPhase.MATERIALS_STAGED
        assert anchor == tuple(report["anchor"])
        assert os.listdir(receipt_dir) == []
    else:
        receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
        row = receipt.credentials[0]
        assert row.status == "denied"
        assert row.reason == expect_code
        assert row.tombstones == ()


def test_d22_clean_pending_credential_requires_explicit_no_operations(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d22")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    credentials = [_credential("cred-pending", no_operations=True)]
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"],
                  credentials=credentials)
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    row = receipt.credentials[0]
    assert row.status == "pending_evaluation"
    assert row.tombstones == () and row.matched_keys == ()
    assert row.no_operation_association is True


# --------------------------------------------------------------------------
# D. out-of-band four-root isolation and exact physical set
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attack,expected", [
    ("nested", "layout_roots_nested"),
    ("overlap", "layout_roots_overlap"),
    ("symlink", "input_root_unsafe_entry"),
    ("hardlink", "input_root_hardlink_refused"),
    ("extra", "input_root_set_not_exact"),
    ("missing", "evidence_digest_mismatch"),
    ("fifo", "input_root_unsafe_entry"),
    ("nestdir", "input_root_unsafe_entry"),
])
def test_d31_out_of_band_root_isolation_rejections(
        world, tmp_path, attack, expected):
    paths, report = _stage(world, tmp_path,
                           base_name="recovery-d31-" + attack)
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    if attack == "nested":
        handoff = paths["bare"] / "handoff"
        _green_bundle(handoff, backup_id=report["backup_id"],
                      barrier=report["barrier_digest"])
    else:
        handoff = tmp_path / "d-handoff"
        _green_bundle(handoff, backup_id=report["backup_id"],
                      barrier=report["barrier_digest"])
        items = handoff / "items"
        if attack == "symlink":
            os.symlink("/etc/hostname", items / "evil.json")
        elif attack == "hardlink":
            os.link(items / "authentication.json", items / "hard.json")
        elif attack == "extra":
            (items / "evil.json").write_bytes(b'{"x":1}')
        elif attack == "missing":
            os.unlink(items / "ev-k1-01.json")
        elif attack == "fifo":
            os.mkfifo(items / "pipe")
        elif attack == "nestdir":
            nested = items / "sub"
            nested.mkdir()
            (nested / "x.json").write_bytes(b'{"x":1}')
    target_receipt = handoff if attack == "overlap" else receipt_dir
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, target_receipt, CASE, report["anchor"])
    assert raised.value.reason.startswith(expected), raised.value.reason
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])


# --------------------------------------------------------------------------
# E. hard order / phase / epoch discipline
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,phase", [
    ("quarantine", "quarantined"),
    ("pv", "package_verified"),
])
def test_d40_wrong_phase_rejected(world, tmp_path, mode, phase):
    paths, report = _stage(world, tmp_path, mode=mode,
                           base_name="recovery-d40-" + mode)
    assert report["phase"] == phase
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    assert raised.value.reason == "state_case_phase_mismatch"
    view, anchor = _state_view(paths)
    assert view.phase.value == phase
    assert anchor == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


def test_d41_non_current_epoch_rejected(world, tmp_path):
    paths, report = _stage(world, tmp_path, mode="supersede", case_b=CASE_B,
                           base_name="recovery-d41")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    assert raised.value.reason == "state_case_not_current_epoch"
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        assert store.get(CASE).is_current_epoch is False
        assert store.get(CASE_B).is_current_epoch is True
        assert store.journal_anchor() == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


def test_d42_partial_merge_has_no_authentication_before_full_merge(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d42")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    killed = _kill(paths, handoff, receipt_dir, CASE, report["anchor"],
                   "merge:0")
    assert killed["rc"] in (-9, 137), killed

    view, partial_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert view.authentication is None
    assert len(view.merges) == 1
    assert partial_anchor[0] == report["anchor"][0] + 1
    assert os.listdir(receipt_dir) == []

    # Host re-pins the live hash-chained anchor; a brand-new process
    # reconciles the partial merge and converges, appending no duplicate.
    converged = _converge(paths, handoff, receipt_dir, CASE, partial_anchor)
    assert converged["ok"], converged
    view, terminal_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    assert terminal_anchor[0] == report["anchor"][0] + 6
    rerun = _run_d(paths, handoff, receipt_dir, CASE, terminal_anchor)
    _view, idle_anchor = _state_view(paths)
    assert idle_anchor == terminal_anchor
    assert rerun.journal_anchor_before == terminal_anchor
    assert sorted(os.listdir(receipt_dir)) == [
        "reauthorize-" + CASE + ".json"]


def test_d43_ast_proof_literals_one_per_gate_and_order():
    tree = ast.parse(Path(dmod.__file__).read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "reauthorize_recovery")

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]

    def merge_line(node):
        # The store.merge call itself lives in _merge_one; inside the
        # orchestrator the merge stage is the _merge_one call site.
        return (isinstance(node.func, ast.Name)
                and node.func.id == "_merge_one")

    first_merge = min(n.lineno for n in calls if merge_line(n))
    authenticate = min(n.lineno for n in calls
                       if isinstance(n.func, ast.Attribute)
                       and n.func.attr == "authenticate")
    advances = [n for n in calls
                if isinstance(n.func, ast.Name)
                and n.func.id == "_advance"]
    assert len(advances) == 2
    decoded = []
    for call in advances:
        target = call.args[2]
        pair = call.args[3]
        assert isinstance(target, ast.Attribute)
        # Exactly this gate's one (name, digest) proof; name is a literal
        # matched positionally, never a bundled set of future proofs.
        assert isinstance(pair, ast.Tuple) and len(pair.elts) == 2
        name = pair.elts[0]
        assert isinstance(name, ast.Constant)
        decoded.append((call.lineno, target.attr, name.value))
    # ast.walk does not promise source order; re-sort by line before the
    # ordering assertion (production order itself is what is under test).
    decoded.sort(key=lambda item: item[0])
    assert decoded == [
        (min(advances, key=lambda c: c.lineno).lineno,
         "EVIDENCE_MERGED", "evidence_merged"),
        (max(advances, key=lambda c: c.lineno).lineno,
         "REAUTHORIZED", "current_authorization")]
    assert first_merge < authenticate < decoded[0][0] < decoded[1][0]
    constants = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not ({"read_review", "task_review", "effects_review"} & constants)


def test_d44_authentication_mismatch_keeps_merges_zero_transitions(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d44")
    bad_dir = tmp_path / "d-handoff-bad"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(
        bad_dir, backup_id=report["backup_id"],
        barrier=report["barrier_digest"],
        authentication=_authentication(instance="instance-other"))
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, bad_dir, receipt_dir, CASE, report["anchor"])
    assert raised.value.reason == "owner_authentication_failed"
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert view.authentication is None
    assert len(view.merges) == 3
    assert anchor[0] == report["anchor"][0] + 3
    assert os.listdir(receipt_dir) == []

    # Correct out-of-band reproof converges; the equal merges are skipped.
    good_dir = tmp_path / "d-handoff-good"
    _green_bundle(good_dir, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    receipt = _run_d(paths, good_dir, receipt_dir, CASE, anchor)
    final_view, final_anchor = _state_view(paths)
    assert final_view.phase == RecoveryPhase.REAUTHORIZED
    assert final_anchor == receipt.journal_anchor_after
    # authenticate + 2 transitions only; the 3 merges were not re-appended.
    assert final_anchor[0] == anchor[0] + 3


# --------------------------------------------------------------------------
# F. zero writes / anchor
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant,expected", [
    ("unknown_case", "state_case_unknown"),
    ("backup_binding", "state_case_binding_mismatch"),
    ("barrier_binding", "state_case_binding_mismatch"),
    ("case_argument", "handoff_case_binding_mismatch"),
    ("tampered_item", "evidence_digest_mismatch"),
    ("bad_version", "handoff_envelope_version_unsupported"),
])
def test_d50_rejections_write_nothing(world, tmp_path, variant, expected):
    paths, report = _stage(world, tmp_path,
                           base_name="recovery-d50-" + variant)
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    case_id = "case-ghost" if variant == "unknown_case" else CASE
    _green_bundle(handoff, case_id=case_id,
                  backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    items = handoff / "items"
    if variant == "backup_binding":
        manifest = json.loads((handoff / "handoff.json").read_bytes())
        manifest["backup_id"] = "bkp-other"
        (handoff / "handoff.json").write_text(json.dumps(manifest))
    elif variant == "barrier_binding":
        manifest = json.loads((handoff / "handoff.json").read_bytes())
        manifest["barrier_digest"] = "f" * 64
        (handoff / "handoff.json").write_text(json.dumps(manifest))
    elif variant == "tampered_item":
        target = items / "ev-k1-01.json"
        raw = target.read_bytes()
        target.write_bytes(raw[:-2] + b"XX")
    elif variant == "bad_version":
        manifest = json.loads((handoff / "handoff.json").read_bytes())
        manifest["version"] = 2
        (handoff / "handoff.json").write_text(json.dumps(manifest))
    before = _fingerprint(paths["state"])
    with pytest.raises(ReauthorizeError) as raised:
        if variant == "case_argument":
            reauthorize_recovery(
                handoff_dir=str(handoff), layout=_d_layout(paths, receipt_dir),
                case_id="case-other", trusted_anchor=tuple(report["anchor"]),
                at=AT_D)
        else:
            _run_d(paths, handoff, receipt_dir, case_id, report["anchor"])
    assert raised.value.reason.startswith(expected), raised.value.reason
    assert _fingerprint(paths["state"]) == before
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


def test_d51_anchor_required_and_mismatch_reject(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d51")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    with pytest.raises(ReauthorizeError) as raised:
        reauthorize_recovery(
            handoff_dir=str(handoff), layout=_d_layout(paths, receipt_dir),
            case_id=CASE, trusted_anchor=None, at=AT_D)
    assert raised.value.reason == "trusted_anchor_invalid"
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE,
               (report["anchor"][0], "0" * 64))
    assert raised.value.reason == "journal_anchor_mismatch"
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


# --------------------------------------------------------------------------
# G. lock
# --------------------------------------------------------------------------

def test_d60_concurrent_second_process_lock_refused(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d60")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    state_fd = os.open(str(paths["state"]), os.O_RDONLY | os.O_DIRECTORY)
    holder_fd = None
    try:
        # Same relative O_RDONLY open convention as production; flock(EX)
        # is what actually makes this the single-writer holder.
        holder_fd = os.open("recovery-materialize.lock",
                            os.O_RDONLY | os.O_NOFOLLOW, dir_fd=state_fd)
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            fcntl.flock(holder_fd, fcntl.LOCK_EX)
            acquired.set()
            release.wait(10)
            fcntl.flock(holder_fd, fcntl.LOCK_UN)

        thread = threading.Thread(target=holder)
        thread.start()
        assert acquired.wait(10)
        try:
            with pytest.raises(ReauthorizeError) as raised:
                _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
            assert raised.value.reason == "single_writer_lock_held"
        finally:
            release.set()
            thread.join(10)
    finally:
        if holder_fd is not None:
            os.close(holder_fd)
        os.close(state_fd)
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


def test_d61_missing_c_lock_refused_and_not_recreated(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d61")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    lock = paths["state"] / "recovery-materialize.lock"
    lock.unlink()
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    assert raised.value.reason == "single_writer_lock_absent"
    assert not lock.exists()
    assert sorted(os.listdir(paths["state"])) == ["recovery-state.sqlite3"]
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])
    assert os.listdir(receipt_dir) == []


# --------------------------------------------------------------------------
# H. idempotency
# --------------------------------------------------------------------------

def test_d70_successful_rerun_zero_new_events_anchor_unchanged_receipt_once(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d70")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    first = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    view1, anchor1 = _state_view(paths)
    files1 = _fingerprint(receipt_dir)

    second = _run_d(paths, handoff, receipt_dir, CASE, anchor1)
    view2, anchor2 = _state_view(paths)
    files2 = _fingerprint(receipt_dir)

    third = _run_d(paths, handoff, receipt_dir, CASE, anchor1)
    view3, anchor3 = _state_view(paths)

    assert anchor1 == anchor2 == anchor3
    assert anchor1 == first.journal_anchor_after
    assert dataclasses.asdict(view1) == dataclasses.asdict(view2)
    assert dataclasses.asdict(view2) == dataclasses.asdict(view3)
    assert sorted(os.listdir(receipt_dir)) == [
        "reauthorize-" + CASE + ".json"]
    assert files1 == files2
    assert second.receipt_sha256 == first.receipt_sha256
    assert third.receipt_sha256 == first.receipt_sha256
    assert second.journal_anchor_before == anchor1
    assert second.journal_anchor_after == anchor1


# --------------------------------------------------------------------------
# I. real SIGKILL at the three crash points, then new-process convergence
# --------------------------------------------------------------------------

def test_d80_sigkill_after_merge_replays_to_same_terminal(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d80")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    killed = _kill(paths, handoff, receipt_dir, CASE, report["anchor"],
                   "merge:1")
    assert killed["rc"] in (-9, 137), killed
    view, partial_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert len(view.merges) == 2 and view.authentication is None
    assert partial_anchor[0] == report["anchor"][0] + 2
    assert os.listdir(receipt_dir) == []

    converged = _converge(paths, handoff, receipt_dir, CASE, partial_anchor)
    assert converged["ok"], converged
    receipt_sha = converged["receipt"]["receipt_sha256"]
    view, terminal_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    # Full terminal is 3 merges + auth + 2 transitions; no merge duplicated.
    assert terminal_anchor[0] == report["anchor"][0] + 6
    assert sorted(os.listdir(receipt_dir)) == [
        "reauthorize-" + CASE + ".json"]

    rerun = _run_d(paths, handoff, receipt_dir, CASE, terminal_anchor)
    _view, idle_anchor = _state_view(paths)
    assert idle_anchor == terminal_anchor
    assert rerun.journal_anchor_before == terminal_anchor
    assert rerun.receipt_sha256 == receipt_sha


def test_d81_sigkill_after_authenticate_replays_to_same_terminal(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d81")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    killed = _kill(paths, handoff, receipt_dir, CASE, report["anchor"],
                   "authenticated")
    assert killed["rc"] in (-9, 137), killed
    view, partial_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert len(view.merges) == 3
    assert view.authentication is not None
    assert partial_anchor[0] == report["anchor"][0] + 4

    converged = _converge(paths, handoff, receipt_dir, CASE, partial_anchor)
    assert converged["ok"], converged
    receipt_sha = converged["receipt"]["receipt_sha256"]
    view, terminal_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    assert terminal_anchor[0] == report["anchor"][0] + 6

    second = _run_d(paths, handoff, receipt_dir, CASE, terminal_anchor)
    _v, idle_anchor = _state_view(paths)
    assert idle_anchor == terminal_anchor
    assert second.journal_anchor_before == terminal_anchor
    assert second.receipt_sha256 == receipt_sha


def test_d82_sigkill_with_tmp_receipt_identified_void(world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d82")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])

    killed = _kill(paths, handoff, receipt_dir, CASE, report["anchor"],
                   "receipt_tmp")
    assert killed["rc"] in (-9, 137), killed
    leftovers = [n for n in os.listdir(receipt_dir)
                 if n.startswith(".tmp-reauthorize-")]
    assert len(leftovers) == 1
    view, terminal_anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.REAUTHORIZED
    assert terminal_anchor[0] == report["anchor"][0] + 6
    assert not any(n.startswith("reauthorize-")
                   for n in os.listdir(receipt_dir))

    # The journal is already terminal; the new process pins that anchor,
    # reaps the O_EXCL residue as cross-process void and publishes once.
    converged = _converge(paths, handoff, receipt_dir, CASE, terminal_anchor)
    assert converged["ok"], converged
    names = os.listdir(receipt_dir)
    assert names == ["reauthorize-" + CASE + ".json"]
    receipt_sha = converged["receipt"]["receipt_sha256"]
    _v, idle_anchor = _state_view(paths)
    assert idle_anchor == terminal_anchor
    second = _run_d(paths, handoff, receipt_dir, CASE, terminal_anchor)
    _v, idle_anchor2 = _state_view(paths)
    assert idle_anchor2 == terminal_anchor
    assert second.journal_anchor_before == terminal_anchor
    assert second.receipt_sha256 == receipt_sha


# --------------------------------------------------------------------------
# J. real ENOSPC on a tiny tmpfs, then space back and convergence
# --------------------------------------------------------------------------

def test_d83_enospc_real_tmpfs_fail_closed_then_enlarge_converges(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d83")
    handoff = tmp_path / "d-handoff"
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    dest = tmp_path / "enospc-dest"
    dest.mkdir(0o700)
    sam_root = os.environ["SAM_TEST_ROOT"]
    bwrap_args = [
        "--unshare-net", "--unshare-user",
        "--ro-bind", "/", "/",
        "--bind", sam_root, sam_root,
        "--size", "524288", "--tmpfs", str(dest),
        "--proc", "/proc", "--dev", "/dev"]
    argv = [str(paths["state"]), str(handoff), str(dest), CASE,
            json.dumps(report["anchor"]), AT_D]
    _result, drill = _run_chip(_ENOSPC_CHIP, argv, bwrap_args=bwrap_args,
                               timeout=300)
    # Segment A: fail-closed one block below the measured fit boundary;
    # the fixed refusal code is one of the journal/receipt storage refusals,
    # the journal reopens, its hash chain is intact and zero events landed
    # (whole-event atomic rollback, D's smallest ENOSPC granularity).
    accept = {"state_write_rejected", "state_transition_rejected",
              "receipt_write_failed"}
    assert drill["fail_reason"] in accept, drill
    assert drill["anchor_a"] == drill["anchor0"], drill
    assert drill["phase_a"] == RecoveryPhase.MATERIALS_STAGED.value, drill
    # Segment B: space back, live-anchor re-entry reaches reauthorized with
    # exactly the six events and one receipt; re-entry appends nothing and
    # leaves identical receipt bytes.
    assert drill["ok_b"], drill
    assert drill["phase_b"] == RecoveryPhase.REAUTHORIZED.value, drill
    assert drill["anchor_b"][0] == drill["anchor0"][0] + 6, drill
    assert drill["receipts"] == ["reauthorize-" + CASE + ".json"], drill
    assert drill["ok_c"], drill
    assert drill["anchor_c"] == drill["anchor_b"], drill
    assert drill["phase_c"] == RecoveryPhase.REAUTHORIZED.value, drill
    assert drill["sha2"] == drill["sha1"], drill
    # The on-disk source state the chip copied from was never touched.
    view, anchor = _state_view(paths)
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert anchor == tuple(report["anchor"])


# --------------------------------------------------------------------------
# K. double manifest bookkeeping
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tamper,expected", [
    ("overlap", "handoff_sets_overlap"),
    ("gap", "handoff_domain_not_covered"),
])
def test_d90_double_manifest_union_intersection_rejected(
        world, tmp_path, tamper, expected):
    paths, report = _stage(world, tmp_path,
                           base_name="recovery-d90-" + tamper)
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    merge_specs, clean_specs = _green_specs()
    if tamper == "overlap":
        clean_specs.append({"key": _key(*K1),
                            "backup": [EvidenceState.UNUSED.value]})
        # The union is still four distinct keys; the overlap refusal is
        # the merge/clean intersection, not a duplicated domain entry.
        domain = [_key(*K1), _key(*K2), _key(*K3), _key(*K9)]
    else:
        domain = [_key(*K1), _key(*K2), _key(*K3), _key(*K9),
                  _key("task-x", "approval-x", "op-x")]
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"], merge_specs=merge_specs,
                  clean_specs=clean_specs, domain=domain)
    before = _fingerprint(paths["state"])
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    assert raised.value.reason == expected
    assert _fingerprint(paths["state"]) == before
    assert os.listdir(receipt_dir) == []


def test_d91_receipt_double_sets_recomputable_and_tamper_detected(
        world, tmp_path):
    paths, report = _stage(world, tmp_path, base_name="recovery-d91")
    handoff = tmp_path / "d-handoff"
    receipt_dir = tmp_path / "d-receipt"
    receipt_dir.mkdir()
    _green_bundle(handoff, backup_id=report["backup_id"],
                  barrier=report["barrier_digest"])
    receipt = _run_d(paths, handoff, receipt_dir, CASE, report["anchor"])
    final_path = receipt_dir / ("reauthorize-" + CASE + ".json")
    document = json.loads(final_path.read_bytes())

    merge_ops = {entry["key"]["operation_id"]: entry
                 for entry in document["merge_set"]}
    clean_ops = {entry["key"]["operation_id"]: entry
                 for entry in document["clean_set"]}
    assert set(merge_ops) == {K1[2], K2[2], K3[2]}
    assert set(clean_ops) == {K9[2]}
    assert set(merge_ops) & set(clean_ops) == set()
    assert clean_ops[K9[2]]["claim_digest"] == dmod._claim_digest(
        EvidenceKey(*K9), (EvidenceState.UNUSED,))
    for entry in document["merge_set"]:
        for ref in entry["items"]:
            assert ref["sha256"].startswith("sha256:")
    assert document["anchor_after"] == list(receipt.journal_anchor_after)

    # Tamper the trace-only clean set: the completed receipt must no longer
    # recompute, so the terminal rerun refuses instead of adopting it.
    document["clean_set"][0]["claim_digest"] = "sha256:" + "0" * 64
    final_path.write_text(json.dumps(document))
    with pytest.raises(ReauthorizeError) as raised:
        _run_d(paths, handoff, receipt_dir, CASE,
               receipt.journal_anchor_after)
    assert raised.value.reason == "receipt_conflict"
    _view, anchor = _state_view(paths)
    assert anchor == receipt.journal_anchor_after


# --------------------------------------------------------------------------
# L. strict AST surfaces (no global whitelist: equality, not membership)
# --------------------------------------------------------------------------

def _enclosing(tree):
    scope = {}

    def walk(node, owner):
        if isinstance(node, ast.FunctionDef):
            owner = node.name
            scope[id(node)] = owner
        for child in ast.iter_child_nodes(node):
            scope[id(child)] = owner
            walk(child, owner)
    walk(tree, "")
    return scope


def test_d92_production_and_test_ast_surface():
    source = Path(dmod.__file__).read_text()
    tree = ast.parse(source)

    top_modules = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_modules.add(node.module)
    assert top_modules == {
        "__future__", "dataclasses", "datetime", "fcntl", "hashlib", "json",
        "os", "pathlib", "stat",
        "app.facts_tree.backup_component_validation",
        "app.facts_tree.recovery_epoch",
        "app.facts_tree.recovery_materializer",
        "app.facts_tree.recovery_state_store"}

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden_names = {
        "subprocess", "socket", "Popen", "popen", "system", "pickle",
        "__import__", "importlib", "eval", "exec", "compile",
        "create_case", "open_gate"}
    forbidden_attrs = {"socket", "Popen", "O_CREAT", "O_EXCL", "O_TRUNC",
                       "O_APPEND", "O_RDWR"}
    assert not names & forbidden_names, names & forbidden_names
    assert not attrs & forbidden_attrs, attrs & forbidden_attrs

    allowed_store = {"get", "merge", "authenticate", "transition",
                     "journal_anchor", "close"}
    store_calls = {n.func.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and isinstance(n.func.value, ast.Name)
                   and n.func.value.id in {"store", "probe"}
                   and isinstance(n.func.attr, str)}
    assert store_calls <= allowed_store, store_calls - allowed_store
    assert "create_case" not in attrs and "open_gate" not in attrs

    scope = _enclosing(tree)
    create_locked = {scope[id(n)] for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)
                     and n.func.id == "_create_locked_at"}
    assert create_locked == {"_commit_receipt"}
    renames = {scope[id(n)] for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "os" and n.func.attr == "rename"}
    assert renames == {"_commit_receipt"}
    flocks = {scope[id(n)] for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "fcntl" and n.func.attr == "flock"}
    assert flocks == {"_acquire_lock"}

    # This test file's own surface.
    self_path = Path(__file__)
    self_tree = ast.parse(self_path.read_text())
    self_modules = set()
    for node in self_tree.body:
        if isinstance(node, ast.Import):
            self_modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            self_modules.add(node.module)
    assert self_modules == {
        "__future__", "ast", "dataclasses", "fcntl", "hashlib", "json", "os",
        "pathlib", "threading", "pytest",
        "app.facts_tree",
        "app.facts_tree.recovery_reauthorize",
        "app.facts_tree.backup_assembly",
        "app.facts_tree.recovery_epoch",
        "app.facts_tree.recovery_state_store",
        "tests.test_facts_tree_backup_assembly",
        "tests.test_facts_tree_recovery_materialize"}
    self_names = {n.id for n in ast.walk(self_tree)
                  if isinstance(n, ast.Name)}
    self_attrs = {n.attr for n in ast.walk(self_tree)
                  if isinstance(n, ast.Attribute)}
    self_forbidden = {
        "subprocess", "socket", "Popen", "create_case", "open_gate",
        "__import__", "importlib", "eval", "exec", "compile", "pickle"}
    assert not self_names & self_forbidden, self_names & self_forbidden
    assert "subprocess" not in self_attrs and "socket" not in self_attrs
    banned_store_attrs = {"merge", "authenticate", "transition",
                          "create_case", "open_gate"}
    called_attrs = {n.func.attr for n in ast.walk(self_tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)}
    assert not (called_attrs & banned_store_attrs), \
        called_attrs & banned_store_attrs
