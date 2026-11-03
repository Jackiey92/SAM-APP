"""F-001 slice C acceptance: trusted bare bootstrap + material install.

Real-machine drills only. Every package is a real ``assemble_backup`` v2
package (one sha1 green world, one post-S64 sha256 cut), every Git
operation is a real subprocess, every hash comes from real bytes. No
git/hash/store is mocked; injected faults sit on storage, process and
namespace boundaries only:

  green sha1 ........ test_c01_installs_full_green_to_materials_staged
  sha256 cut ........ test_c03_s64_sha256_cut_installs_r2_skipped
  R2 ok (backing) ... test_c04_r2_ok_with_only_ledger_backing_objects
  R2 skipped ........ test_c05_r2_skipped_installs_nothing_in_r2_root
  evidence gate ..... test_c06_zero_writes_when_admission_not_admitted
                      test_c07_denied_evidence_with_everything_absent
  audit ............. test_c08_sqlite_audit_all_ro_except_journal
  AST surfaces ...... test_c09_repository_ast_surface
                      test_c10_materializer_ast_surface
  argv whitelist .... test_c11_runner_rejects_unapproved_shapes
  anchors ........... test_c12_bootstrap_precondition_validation
  smuggled object ... test_c13_smuggled_unreachable_loose_object_rejected
  hook canaries ..... test_c14_default_hook_canary_never_runs
                      test_c15_include_hookspath_injection_rejected
  sentinels ......... test_c16_offline_bypass_sentinels_fail_closed
  layout/state ...... test_c17_layout_rejection_writes_nothing
                      test_c18_case_binding_failures_write_nothing
  single writer ..... test_c19_concurrent_second_case_lock_refused
  no resume ......... test_c20_success_is_terminal_and_reverifies
  reverify edge ..... test_c21_reverify_without_attempt_and_unknown_case
  half install ...... test_c22_foreign_file_fails_exact_set_and_voids
  SIGKILL drill ..... test_c23_sigkill_then_new_process_readonly_reverify
  ENOSPC drill ...... test_c24_enospc_real_tmpfs_fails_closed
  format collision .. test_c25_head_oid_format_collisions_rejected
  user/net namespace  test_c26_install_inside_unshare_net_user_namespace
  hostile env ....... test_c27_sha1_explicit_wins_under_sha256_git_env
"""
from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading

import pytest

from app.facts_tree import recovery_materializer as materializer
from app.facts_tree import recovery_repository as repository
from app.facts_tree.backup_assembly import PAYLOAD_SCHEMA, PAYLOAD_VERSION, assemble_backup
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.recovery_epoch import RecoveryEpoch, RecoveryPhase
from app.facts_tree.recovery_materializer import (
    AdmissionEvidence,
    InstallLayout,
    MaterialInstallError,
    _expected_sets,
    barrier_digest,
    install_materials,
    reverify_materials,
)
from app.facts_tree.recovery_repository import (
    RecoveryRepositoryError,
    bootstrap_trusted_bare,
)
from app.facts_tree.recovery_state_store import RecoveryStateStore
from tests.test_facts_tree_backup_assembly import (
    _package_dir,
    _s64_sha256_cut,
    world,
)

GIT = "/usr/bin/git"
CASE = "case-c-0001"
CASE_B = "case-c-0002"
AT_CASE = "2026-11-02T00:00:00.000000+00:00"
AT_INSTALL = "2026-11-03T00:00:00.000000+00:00"


# --------------------------------------------------------------------------
# Package and layout helpers
# --------------------------------------------------------------------------

def _copy_package(src, tmp_path, name="package"):
    target = tmp_path / "clean-room" / name
    shutil.copytree(src, target)
    return target


def _green_package(world_obj, tmp_path):
    result = assemble_backup(world_obj["make_plan"](), world_obj["green_bid"])
    assert result.status == "published", result
    src = _package_dir(world_obj, world_obj["green_bid"], world_obj["barrier"])
    return _copy_package(src, tmp_path)


def _variant_package(world_obj, tmp_path, bid, *, refs, version_root):
    frozen = world_obj["freeze"](bid)
    plan = world_obj["make_plan"](bid=bid, refs=refs, version_root=version_root)
    result = assemble_backup(plan, bid)
    assert result.status == "published", result
    src = _package_dir(world_obj, bid, frozen.barrier_id)
    return _copy_package(src, tmp_path, name=bid)


def _core(package):
    return json.loads((package / "manifest.json").read_bytes())["core"]


def _evidence(core, *, verdict="admitted", digest=None, anchors=None):
    if digest is None:
        digest = compute_core_digest(core)
    if anchors is None:
        anchors = (
            ("envelope_version", "2"),
            ("payload_schema", PAYLOAD_SCHEMA),
            ("payload_version", str(PAYLOAD_VERSION)),
            ("object_format", core["authority"]["object_format"]),
        )
    return AdmissionEvidence(verdict, digest, tuple(anchors))


def _layout_paths(tmp_path, roles, *, base_name="recovery"):
    base = tmp_path / base_name
    paths = {
        "state": base / "state",
        "bare": base / "bare",
        "archive": base / "archive",
        "mapping": base / "mapping",
        "r2": base / "r2",
    }
    for role in roles:
        paths["w:" + role] = base / ("w-" + role)
    return base, paths


def _materialize_dirs(paths, roles):
    for key in ("state", "bare", "archive", "mapping", "r2"):
        paths[key].mkdir(0o700, parents=True)
    for role in roles:
        paths["w:" + role].mkdir(0o700, parents=True)


def _make_layout(tmp_path, roles, *, base_name="recovery"):
    base, paths = _layout_paths(tmp_path, roles, base_name=base_name)
    _materialize_dirs(paths, roles)
    layout = InstallLayout(
        str(paths["state"]), str(paths["bare"]),
        tuple((role, str(paths["w:" + role])) for role in roles),
        str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
    return layout, paths, base


def _create_case(state_dir, core, case_id=CASE, ordinal=1, prior=()):
    with RecoveryStateStore(Path(str(state_dir))) as store:
        store.create_case(
            case_id,
            RecoveryEpoch("recovery-c", "epoch-c-%d" % ordinal, ordinal),
            backup_id=core["backup_id"],
            barrier_digest=barrier_digest(core["barrier_id"]),
            prior_epochs=prior, at=AT_CASE)
        return store.journal_anchor()


def _roles(core):
    return sorted(e["role"] for e in core["payload"]["files"] if e["family"] == "w")


_UNSET = object()


def _kwargs(package, core, layout, *, evidence=_UNSET, at=AT_INSTALL,
            head=None, case_id=CASE, seam=None):
    kwargs = dict(
        package_dir=str(package), layout=layout, case_id=case_id,
        identity=core["identity"],
        head_oid=head or core["authority"]["commit_oid"],
        evidence=_evidence(core) if evidence is _UNSET else evidence,
        git_executable=GIT, at=at)
    if seam is not None:
        kwargs["seam_after_file"] = seam
    return kwargs


def _regular_files(root):
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _assert_private_tree(root, skip=(), skip_files=()):
    skip = set(skip)
    skip_files = {str(Path(root) / rel) for rel in skip_files}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            info = os.lstat(os.path.join(dirpath, name))
            assert not stat.S_ISLNK(info.st_mode)
            assert stat.S_ISDIR(info.st_mode)
            if dirpath == str(root) and name in skip:
                assert stat.S_IMODE(info.st_mode) == 0o700, (name, oct(info.st_mode))
                dirnames.remove(name)
                continue
            assert stat.S_IMODE(info.st_mode) == 0o700, (name, oct(info.st_mode))
        for name in filenames:
            full = os.path.join(dirpath, name)
            info = os.lstat(full)
            assert not stat.S_ISLNK(info.st_mode)
            assert stat.S_ISREG(info.st_mode)
            # R5 owns the sqlite journal mode; this slice only consumes it.
            if full not in skip_files:
                assert stat.S_IMODE(info.st_mode) == 0o600, (name, oct(info.st_mode))


def _bare_entries(bare_parent):
    return list(os.listdir(bare_parent))


def _live_bare_names(bare_parent):
    return [name for name in _bare_entries(bare_parent)
            if name.startswith("sam-recovery-facts-") and name.endswith(".git")
            and not name.endswith(".abandoned.git")]


def _abandoned_names(bare_parent):
    return [name for name in _bare_entries(bare_parent)
            if name.endswith(".abandoned.git")]


def _assert_no_bypass_files(bare_path):
    for rel in ("objects/info/alternates", "objects/info/http-alternates",
                "info/grafts", "shallow", "commondir"):
        assert not os.path.exists(os.path.join(bare_path, rel)), rel
    pack = os.path.join(bare_path, "objects", "pack")
    if os.path.isdir(pack):
        assert not [n for n in os.listdir(pack) if n.endswith(".promisor")]


# --------------------------------------------------------------------------
# Green path
# --------------------------------------------------------------------------

def test_c01_installs_full_green_to_materials_staged(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    assert roles == ["w_items", "w_ledger"]
    layout, paths, base = _make_layout(tmp_path, roles)
    _create_case(paths["state"], core)

    receipt = install_materials(**_kwargs(package, core, layout))

    assert receipt.stage == "materials_staged"
    assert receipt.r2_status == "ok"
    assert receipt.backup_id == core["backup_id"]
    assert receipt.barrier_id == core["barrier_id"]
    bare = receipt.bare
    authority = core["authority"]
    assert (bare.object_format, bare.head_oid, bare.genesis_oid,
            bare.commit_count) == (
        "sha1", authority["commit_oid"], authority["genesis_oid"],
        authority["commit_count"])
    assert len(bare.head_oid) == 40 and bare.object_count >= 1
    assert bare.refs == (("refs/heads/facts", authority["commit_oid"]),)

    # Routed physical sets equal the package inventory, one file per route.
    expected = _expected_sets(core["payload"]["files"], "ok")
    by_name = {landing.name: landing for landing in receipt.roots}
    root_paths = {"r2": paths["r2"], "archive": paths["archive"],
                  "mapping": paths["mapping"],
                  **{"w:" + role: paths["w:" + role] for role in roles}}
    for name, root in root_paths.items():
        want = expected.get(name, set())
        assert {f[0] for f in by_name[name].files} == want
        assert _regular_files(root) == want
    assert len(by_name["archive"].files) == 2
    assert len(by_name["mapping"].files) == 3
    assert len(by_name["r2"].files) == 1
    # The bare root is 0700; git owns the modes of its internal objects/refs.
    _assert_private_tree(base, skip=("bare",),
                         skip_files=("state/recovery-state.sqlite3",))

    # The one fresh bare: create-only facts ref, fsync policy, no bypasses.
    names = _live_bare_names(paths["bare"])
    assert len(names) == 1 and not _abandoned_names(paths["bare"])
    bare_path = Path(bare.path)
    assert stat.S_IMODE(os.lstat(bare_path).st_mode) == 0o700
    assert (bare_path / "HEAD").read_text().strip() == "ref: refs/heads/facts"
    assert (bare_path / "refs/heads/facts").read_text().strip() == bare.head_oid
    config_text = (bare_path / "config").read_text()
    assert "fsync = objects,reference" in config_text
    assert "fsyncMethod = fsync" in config_text
    assert "[remote" not in config_text and "include" not in config_text
    _assert_no_bypass_files(bare_path)

    # State journal moved exactly quarantined -> package_verified ->
    # materials_staged; the lock file remains (no automatic resume).
    assert set(os.listdir(paths["state"])) == {
        "recovery-state.sqlite3", "recovery-materialize.lock"}
    lock_mode = stat.S_IMODE(os.lstat(paths["state"] / "recovery-materialize.lock").st_mode)
    assert lock_mode == 0o600
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        view = store.get(CASE)
        anchor = store.journal_anchor()
    assert view.phase == RecoveryPhase.MATERIALS_STAGED
    assert set(dict(view.proofs)) == {"package_verified", "materials_staged"}
    assert all(len(v) == 64 for v in dict(view.proofs).values())
    assert anchor == receipt.journal_anchor and anchor[0] == 3

    # Receipt proofs are exactly the two appended R5 proof values, in
    # phase order; receipt semantics are placement-only.
    proof_pairs = dict(view.proofs)
    assert receipt.proofs == (
        ("package_verified", proof_pairs["package_verified"]),
        ("materials_staged", proof_pairs["materials_staged"]))
    assert receipt.wal_preconditions == materializer._WAL_PRECONDITIONS
    assert receipt.advisories == materializer._ADVISORIES
    assert any("lazy_fetch_not_drilled" in a for a in receipt.advisories)
    assert any("placement_only" in a for a in receipt.advisories)
    assert len(receipt.wal_preconditions) == 3

    # Same-identity complete read-only re-verification from a fresh open.
    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is True
    assert report.phase == "materials_staged"
    assert report.proofs_match is True
    assert report.reasons == ()
    assert report.r2_status == "ok"
    assert report.bare_path == bare.path
    assert {landing.name: {f[0] for f in landing.files}
            for landing in report.roots} == {
        name: {f[0] for f in landing.files}
        for name, landing in by_name.items()}

    # No sidecars, no foreign material anywhere under the recovery root.
    leftovers = [str(p.relative_to(base)) for p in base.rglob("*")
                 if p.name.endswith(("-wal", "-shm"))]
    assert leftovers == []


def test_c03_s64_sha256_cut_installs_r2_skipped(world, tmp_path):
    cut = _s64_sha256_cut(world, "bkp-c-s64")
    try:
        assert assemble_backup(cut["plan"], cut["bid"]).status == "published"
        package = _copy_package(cut["package"], tmp_path, name="s64-package")
        core = _core(package)
        assert core["authority"]["object_format"] == "sha256"
        assert core["payload"]["r2"]["status"] == "skipped"
        roles = _roles(core)
        assert roles == ["w_items"]
        layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-s64")
        _create_case(paths["state"], core)

        receipt = install_materials(**_kwargs(package, core, layout))
        assert receipt.stage == "materials_staged"
        assert receipt.r2_status == "skipped"
        assert len(receipt.bare.head_oid) == 64
        assert receipt.bare.object_format == "sha256"
        expected = _expected_sets(core["payload"]["files"], "skipped")
        assert _regular_files(paths["r2"]) == set()
        assert _regular_files(paths["archive"]) == expected["archive"]
        assert _regular_files(paths["mapping"]) == expected["mapping"]
        assert len(expected["archive"]) == 1  # two logical, one physical
        report = reverify_materials(**_kwargs(package, core, layout))
        assert report.complete is True and report.r2_status == "skipped"
        assert report.proofs_match is True and report.reasons == ()
    finally:
        cut["broker_db"].close()
        cut["connection"].close()


def test_c04_r2_ok_with_only_ledger_backing_objects(world, tmp_path):
    # refs=() with the real ledger: R closure is empty, the three backing
    # objects ship as family=r2 ledger backing and are deduped into the
    # same mapping root; archive stays physically empty.
    package = _variant_package(
        world, tmp_path, "bkp-c-r2backing", refs=(),
        version_root=world["ledger_root"])
    core = _core(package)
    assert core["payload"]["r2"]["status"] == "ok"
    assert core["payload"]["r2"]["backing_files"] == 3
    families = {}
    for entry in core["payload"]["files"]:
        families.setdefault((entry["family"], entry.get("storage_class")), 0)
        families[(entry["family"], entry.get("storage_class"))] += 1
    assert families[("r2", "mapping_objects")] == 3
    assert ("r", None) not in families and ("r", "archive") not in families
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-r2b")
    _create_case(paths["state"], core)

    receipt = install_materials(**_kwargs(package, core, layout))
    assert receipt.r2_status == "ok"
    expected = _expected_sets(core["payload"]["files"], "ok")
    assert _regular_files(paths["archive"]) == set()
    assert len(_regular_files(paths["mapping"])) == 3
    assert _regular_files(paths["mapping"]) == expected["mapping"]
    assert _regular_files(paths["r2"]) == {"mapping-version-index.sqlite"}
    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is True and report.r2_status == "ok"


def test_c05_r2_skipped_installs_nothing_in_r2_root(world, tmp_path):
    package = _variant_package(
        world, tmp_path, "bkp-c-skip", refs=(), version_root=None)
    core = _core(package)
    assert core["payload"]["r2"] == {"status": "skipped"}
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-skip")
    _create_case(paths["state"], core)

    receipt = install_materials(**_kwargs(package, core, layout))
    assert receipt.r2_status == "skipped"
    assert _regular_files(paths["archive"]) == set()
    assert _regular_files(paths["mapping"]) == set()
    assert _regular_files(paths["r2"]) == set()
    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is True and report.r2_status == "skipped"


# --------------------------------------------------------------------------
# Admission gate: zero writes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [
    "missing", "denied", "wrong-digest", "bad-anchor", "malformed-anchors",
    "anchor-format", "tampered-package",
])
def test_c06_zero_writes_when_admission_not_admitted(world, tmp_path, mode):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    base, paths = _layout_paths(tmp_path, roles, base_name="rec-gate")
    paths["state"].mkdir(0o700, parents=True)
    _create_case(paths["state"], core)

    def fingerprint(root):
        return {p.relative_to(root): (p.lstat().st_ino, p.lstat().st_size,
                                      p.lstat().st_mtime_ns)
                for p in root.rglob("*")}

    before = fingerprint(package)
    evidence = {
        "missing": None,
        "denied": _evidence(core, verdict="denied"),
        "wrong-digest": _evidence(core, digest="sha256:" + "0" * 64),
        "bad-anchor": _evidence(core, anchors=(
            ("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
            ("payload_version", str(PAYLOAD_VERSION)),
            ("object_format", "sha256"))),
        "malformed-anchors": AdmissionEvidence(
            "admitted", compute_core_digest(core),
            (("envelope_version", "2"),)),
        "anchor-format": AdmissionEvidence(
            "admitted", compute_core_digest(core), (
                ("envelope_version", "2"),
                ("payload_schema", PAYLOAD_SCHEMA),
                ("payload_version", str(PAYLOAD_VERSION)),
                ("object_format", "sha256"))),
        "tampered-package": _evidence(core),
    }[mode]
    if mode == "tampered-package":
        leaf = package / "payload/w/w_items.sqlite"
        data = bytearray(leaf.read_bytes())
        data[0] ^= 1
        leaf.write_bytes(data)
    layout = InstallLayout(
        str(paths["state"]), str(paths["bare"]),
        tuple((role, str(paths["w:" + role])) for role in roles),
        str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout, evidence=evidence))
    assert raised.value.reason in {
        "admission_evidence_invalid", "admission_denied",
        "admission_binding_mismatch", "admission_anchors_mismatch",
        "package_verification_failed"}
    assert raised.value.stage_reached == "quarantined"

    # Nothing was created: only the pre-existing state journal, no lock,
    # no bare, no material roots. For the untampered modes the package is
    # byte-identical (the installer never opens it for writing).
    assert set(os.listdir(base)) == {"state"}
    assert set(os.listdir(paths["state"])) == {"recovery-state.sqlite3"}
    after = fingerprint(package)
    if mode == "tampered-package":
        assert after != before  # the test itself flipped one byte
    else:
        assert after == before
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        assert store.get(CASE).phase == RecoveryPhase.QUARANTINED


def test_c07_denied_evidence_with_everything_absent(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    base, paths = _layout_paths(tmp_path, roles, base_name="rec-nothing")
    layout = InstallLayout(
        str(paths["state"]), str(paths["bare"]),
        tuple((role, str(paths["w:" + role])) for role in roles),
        str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout,
                                    evidence=_evidence(core, verdict="denied")))
    assert raised.value.reason == "admission_denied"
    assert not base.exists()


# --------------------------------------------------------------------------
# Audit: every sqlite connect but the R5 journal is immutable mode=ro
# --------------------------------------------------------------------------

_INSTALL_CHIP = r"""
import json, os, signal, sys
from app.facts_tree.backup_assembly import PAYLOAD_SCHEMA, PAYLOAD_VERSION
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.recovery_materializer import (
    AdmissionEvidence, InstallLayout, install_materials)
pkg, state, bare, wa, wl, arch, mp, r2, case_id, at, options = sys.argv[1:12]
options = json.loads(options)
core = json.load(open(os.path.join(pkg, "manifest.json")))["core"]
anchors = (("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
           ("payload_version", str(PAYLOAD_VERSION)),
           ("object_format", core["authority"]["object_format"]))
evidence = AdmissionEvidence("admitted", compute_core_digest(core), anchors)
layout = InstallLayout(state, bare, (("w_items", wa), ("w_ledger", wl)),
                       arch, mp, r2)
counters = {"ro": 0, "journal": 0}
if options.get("audit"):
    def hook(event, args):
        if event == "sqlite3.connect":
            target = str(args[0])
            if target.endswith("recovery-state.sqlite3"):
                counters["journal"] += 1
                return
            assert "mode=ro" in target, target
            counters["ro"] += 1
    sys.addaudithook(hook)
def seam(root_key, relative):
    if options.get("kill_after_first_file"):
        os.kill(os.getpid(), signal.SIGKILL)
try:
    receipt = install_materials(
        package_dir=pkg, layout=layout, case_id=case_id,
        identity=core["identity"], head_oid=core["authority"]["commit_oid"],
        evidence=evidence, git_executable="/usr/bin/git", at=at,
        seam_after_file=seam if options.get("kill_after_first_file") else None)
    import dataclasses
    print("REPORT=" + json.dumps({"ok": True, "counters": counters,
        "receipt": dataclasses.asdict(receipt)}, default=str))
except Exception as exc:
    print("REPORT=" + json.dumps({"ok": False, "counters": counters,
        "error": type(exc).__name__ + ":" + str(exc)}))
"""

_REVERIFY_CHIP = r"""
import dataclasses, json, os, sys
from app.facts_tree.backup_assembly import PAYLOAD_SCHEMA, PAYLOAD_VERSION
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.recovery_materializer import (
    AdmissionEvidence, InstallLayout, reverify_materials)
pkg, state, bare, wa, wl, arch, mp, r2, case_id, at = sys.argv[1:11]
core = json.load(open(os.path.join(pkg, "manifest.json")))["core"]
anchors = (("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
           ("payload_version", str(PAYLOAD_VERSION)),
           ("object_format", core["authority"]["object_format"]))
evidence = AdmissionEvidence("admitted", compute_core_digest(core), anchors)
layout = InstallLayout(state, bare, (("w_items", wa), ("w_ledger", wl)),
                       arch, mp, r2)
report = reverify_materials(package_dir=pkg, layout=layout, case_id=case_id,
    identity=core["identity"], head_oid=core["authority"]["commit_oid"],
    evidence=evidence, git_executable="/usr/bin/git")
print("REPORT=" + json.dumps(dataclasses.asdict(report), default=str))
"""


def _chip_argv(package, paths, case_id, at, options):
    return [str(package), str(paths["state"]), str(paths["bare"]),
            str(paths["w:w_items"]), str(paths["w:w_ledger"]),
            str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]),
            case_id, at, json.dumps(options)]


def _run_chip(code, argv, *, env=None, bwrap_args=None, timeout=300):
    cmd = [sys.executable, "-c", code, *argv]
    if bwrap_args:
        cmd = ["/usr/bin/bwrap", "--die-with-parent", *bwrap_args, "--", *cmd]
    result = subprocess.run(cmd, env=env or dict(os.environ),
                            capture_output=True, text=True, timeout=timeout)
    lines = [line for line in result.stdout.splitlines()
             if line.startswith("REPORT=")]
    assert lines, "no report; rc=%r stderr=%s" % (result.returncode,
                                                   result.stderr[-3000:])
    return result, json.loads(lines[-1][7:])


def test_c08_sqlite_audit_all_ro_except_journal(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    layout, paths, _base = _make_layout(tmp_path, _roles(core),
                                        base_name="rec-audit")
    _create_case(paths["state"], core)
    _result, report = _run_chip(
        _INSTALL_CHIP, _chip_argv(package, paths, CASE, AT_INSTALL,
                                 {"audit": True}),
        env=dict(os.environ, HTTPS_PROXY="http://invalid:9"))
    assert report["ok"], report
    assert report["counters"]["journal"] >= 1
    assert report["counters"]["ro"] >= 3  # W pair plus R2 ledger, all immutable
    assert report["receipt"]["stage"] == "materials_staged"


# --------------------------------------------------------------------------
# AST import/write surfaces
# --------------------------------------------------------------------------

def _import_surface(tree):
    imports = {a.name for n in ast.walk(tree)
               if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return imports


def _enclosing_functions(tree):
    """Map each ast.Call node id() to the function (incl. method) enclosing it."""
    enclosing = {}

    def walk(node, stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + [node.name]
        if isinstance(node, ast.Call) and stack:
            enclosing[id(node)] = stack[-1]
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    walk(tree, [])
    return enclosing


def test_c09_repository_ast_surface():
    source = Path(repository.__file__).read_text()
    tree = ast.parse(source)
    assert _import_surface(tree) == {
        "__future__", "dataclasses", "json", "os", "stat", "subprocess",
        "app.facts_tree.git_repository"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden_names = {"Popen", "popen", "system", "socket", "pickle", "eval",
                       "exec", "compile", "__import__", "initialize"}
    forbidden_attrs = {"Popen", "popen", "system", "socket", "pickle",
                       "initialize", "open_gate", "authenticate", "merge",
                       "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
    assert not names & forbidden_names, names & forbidden_names
    assert not attrs & forbidden_attrs, attrs & forbidden_attrs
    assert not any(isinstance(n, ast.keyword) and n.arg == "shell"
                   for n in ast.walk(tree))
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"]
    assert [(n.func.attr) for n in runs] == ["run"]
    whitelist_heads = {"init", "symbolic-ref", "config", "bundle", "update-ref",
                       "for-each-ref", "cat-file", "rev-list", "fsck"}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "git" and n.func.attr == "run"):
            assert isinstance(n.args[0], ast.List)
            head = n.args[0].elts[0]
            assert isinstance(head, ast.Constant) and head.value in whitelist_heads
    for needle in ("--template=", "GIT_CONFIG_NOSYSTEM", "protocol.allow",
                   "/dev/null", "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS",
                   "update-ref", "create %s %s"):
        assert needle in source
    assert repository._TIMEOUT_SECONDS == 30


def _top_level_imports(tree):
    modules = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    return modules


def test_c10_materializer_ast_surface():
    source = Path(materializer.__file__).read_text()
    tree = ast.parse(source)
    assert _top_level_imports(tree) == {
        "__future__", "dataclasses", "datetime", "fcntl", "hashlib", "json",
        "os", "pathlib", "sqlite3", "stat",
        "app.facts_tree.backup_assembly",
        "app.facts_tree.backup_component_validation",
        "app.facts_tree.backup_manifest",
        "app.facts_tree.mapping_version_index",
        "app.facts_tree.recovery_epoch",
        "app.facts_tree.recovery_repository",
        "app.facts_tree.recovery_state_store",
        "app.facts_tree.recovery_verifier"}
    # The read-only adapter is imported lazily, inside reverify only, so a
    # bare never needs the mutating repository module to import at load time.
    assert [n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and n.module == "app.facts_tree.git_repository"] == [
        "app.facts_tree.git_repository"]
    reverify_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "reverify_materials")
    assert any(isinstance(n, ast.ImportFrom)
               and n.module == "app.facts_tree.git_repository"
               for n in ast.walk(reverify_fn))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden_names = {"subprocess", "Popen", "popen", "system", "socket",
                       "pickle", "eval", "exec", "MappingVersionLedger",
                       "VersionedMappingObjectStore", "ArchiveFileStore",
                       "assemble_backup", "create_case", "open_gate",
                       "authenticate"}
    forbidden_attrs = {"Popen", "socket", "pickle", "initialize", "system",
                       "O_TRUNC", "O_APPEND", "O_RDWR"}
    assert not names & forbidden_names, names & forbidden_names
    assert not attrs & forbidden_attrs, attrs & forbidden_attrs
    assert not any(isinstance(n, ast.keyword) and n.arg == "shell"
                   for n in ast.walk(tree))

    # rename/replace are banned only as filesystem operations; str.replace in
    # SQL identifier quoting is harmless.
    fs_moves = [(n.func.value.id, n.func.attr) for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in {"os", "Path"}
                and n.func.attr in {"rename", "replace"}]
    assert fs_moves == []

    enclosing = _enclosing_functions(tree)

    # Create-write flags appear at exactly one site: the O_EXCL lock file.
    for flag in ("O_WRONLY", "O_CREAT", "O_EXCL"):
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == flag]
        assert len(hits) == 1, flag

    # sqlite is reached through exactly one immutable mode=ro connector.
    connects = [(n, enclosing.get(id(n), "")) for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "sqlite3" and n.func.attr == "connect"]
    assert len(connects) == 1 and connects[0][1] == "_open_ro_sqlite"
    assert "mode=ro&immutable=1" in ast.get_source_segment(source, connects[0][0])

    mkdir_sites, unlink_sites, rmdir_sites, flock_sites = set(), set(), set(), set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        site = enclosing.get(id(n), "")
        if isinstance(n.func.value, ast.Name) and n.func.value.id == "os":
            if n.func.attr == "mkdir":
                mkdir_sites.add(site)
            elif n.func.attr == "unlink":
                unlink_sites.add(site)
            elif n.func.attr == "rmdir":
                rmdir_sites.add(site)
        if isinstance(n.func.value, ast.Name) and n.func.value.id == "fcntl":
            flock_sites.add(site)
    assert mkdir_sites == {"_shard"}
    assert unlink_sites == {"void", "_discard"} and rmdir_sites == {"void"}
    assert flock_sites == {"install_materials", "reverify_materials"}

    # R5 is consumed through its public append-only API only.
    store_attrs = {n.func.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == "store"}
    assert store_attrs <= {"get", "transition", "journal_anchor", "close"}
    assert {"get", "transition", "journal_anchor"} <= store_attrs

    for needle in ("mode=ro&immutable=1", "sam.f001.recovery.proof.v1",
                   "recovery-materialize.lock", "materials_staged"):
        assert needle in source
    for banned_phrase in ("掉电", "断电"):
        assert banned_phrase not in source


# --------------------------------------------------------------------------
# Runner whitelist and bootstrap preconditions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("args,stdin_kind", [
    (["clone", "https://invalid.example"], None),
    (["fetch", "origin"], None),
    (["push"], None),
    (["remote", "-v"], None),
    (["gc"], None),
    (["init", "--bare", "/tmp/x"], None),
    (["init", "--bare", "--object-format=sha1", "--template=",
      "--initial-branch=main", "/tmp/x"], None),
    (["init", "--bare", "--object-format=bad", "--template=",
      "--initial-branch=facts", "/tmp/x"], None),
    (["init", "--bare", "--object-format=sha1", "--template=",
      "--initial-branch=facts", "relative/x"], None),
    (["symbolic-ref", "refs/heads/x"], None),
    (["config", "--local", "remote.origin.url", "https://invalid"], None),
    (["config", "--local", "--get-all", "core.hooksPath"], None),
    (["bundle", "verify", "file.bundle"], "bundle"),
    (["bundle", "verify", "-"], None),
    (["bundle", "unbundle", "-"], None),
    (["bundle", "unbundle", "-"], "create"),
    (["update-ref", "--stdin"], None),
    (["update-ref", "refs/heads/facts", "HEAD"], "create"),
    (["cat-file", "--batch-all-objects"], None),
    (["rev-list", "--all"], None),
    (["fsck", "--full", "HEAD"], None),
])
def test_c11_runner_rejects_unapproved_shapes(args, stdin_kind):
    with pytest.raises(RecoveryRepositoryError) as raised:
        repository._check_shape(args, stdin_kind)
    assert raised.value.args[0] in {"git_argv_not_whitelisted", "git_argv_invalid"}


def test_c11_runner_accepts_exact_whitelist_shapes():
    good = [
        (["init", "--bare", "--object-format=sha256", "--template=",
          "--initial-branch=facts", "/tmp/x"], None),
        (["symbolic-ref", "HEAD"], None),
        (["config", "--local", "--get-all", "core.fsync"], None),
        (["config", "--local", "--get-all", "core.fsyncMethod"], None),
        (["config", "--local", "--list"], None),
        (["config", "--local", "core.fsync", "objects,reference"], None),
        (["config", "--local", "core.fsyncMethod", "fsync"], None),
        (["bundle", "list-heads", "-"], "bundle"),
        (["bundle", "verify", "-"], "bundle"),
        (["bundle", "unbundle", "-"], "bundle"),
        (["update-ref", "--stdin"], "create"),
        (["for-each-ref", "--format=%(objectname) %(refname)"], None),
        (["cat-file", "--batch-all-objects", "--batch-check"], None),
        (["cat-file", "-e", "a" * 40 + "^{commit}"], None),
        (["cat-file", "-e", "b" * 64 + "^{commit}"], None),
        (["cat-file", "blob", "a" * 40 + ":meta/repository.json"], None),
        (["rev-list", "a" * 40], None),
        (["rev-list", "--parents", "a" * 40], None),
        (["rev-list", "--max-parents=0", "a" * 64], None),
        (["rev-list", "--objects", "a" * 64], None),
        (["fsck", "--full", "--no-reflogs", "a" * 40], None),
    ]
    for args, kind in good:
        repository._check_shape(args, kind)


@pytest.mark.parametrize("field,mutate", [
    ("format-sha256-head40", None),
    ("head-short", None),
    ("head-nonhex", None),
    ("genesis-short", None),
    ("commit-count-zero", None),
    ("pin-no-refs", None),
    ("identity-empty", None),
    ("executable-relative", None),
    ("executable-missing", None),
    ("executable-regular-nonexec", None),
    ("seam-not-callable", None),
])
def test_c12_bootstrap_precondition_validation(world, tmp_path, field, mutate):
    package = _green_package(world, tmp_path)
    core = _core(package)
    parent = tmp_path / "bare-parent"
    parent.mkdir(0o700)
    kwargs = dict(
        isolated_parent=str(parent), bundle_fd=None,
        head_oid=core["authority"]["commit_oid"],
        pin_ref=core["authority"]["pin_ref"],
        genesis_oid=core["authority"]["genesis_oid"],
        commit_count=core["authority"]["commit_count"],
        object_format="sha1", identity=core["identity"], git_executable=GIT,
        post_unbundle_seam=None)
    if field == "format-sha256-head40":
        kwargs["object_format"] = "sha256"
    elif field == "head-short":
        kwargs["head_oid"] = "a" * 39
    elif field == "head-nonhex":
        kwargs["head_oid"] = "z" * 40
    elif field == "genesis-short":
        kwargs["genesis_oid"] = "a" * 39
    elif field == "commit-count-zero":
        kwargs["commit_count"] = 0
    elif field == "pin-no-refs":
        kwargs["pin_ref"] = "heads/x"
    elif field == "identity-empty":
        kwargs["identity"] = {}
    elif field == "executable-relative":
        kwargs["git_executable"] = "git"
    elif field == "executable-missing":
        kwargs["git_executable"] = "/nonexistent/git"
    elif field == "executable-regular-nonexec":
        marker = tmp_path / "notgit"
        marker.write_bytes(b"")
        os.chmod(marker, 0o600)
        kwargs["git_executable"] = str(marker)
    elif field == "seam-not-callable":
        kwargs["post_unbundle_seam"] = 1
    with pytest.raises(RecoveryRepositoryError):
        bootstrap_trusted_bare(**kwargs)
    assert _bare_entries(parent) == []


def test_c12_missing_parent_never_creates_one(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    fd = os.open(str(package / "authority.bundle"), os.O_RDONLY)
    try:
        # The fixed parent must pre-exist: pinning it raises a raw OSError
        # before any mkdir attempt, and nothing creates it on the way out.
        with pytest.raises(OSError):
            bootstrap_trusted_bare(
                isolated_parent=str(tmp_path / "missing-parent"),
                bundle_fd=fd, head_oid=core["authority"]["commit_oid"],
                pin_ref=core["authority"]["pin_ref"],
                genesis_oid=core["authority"]["genesis_oid"],
                commit_count=core["authority"]["commit_count"],
                object_format="sha1", identity=core["identity"],
                git_executable=GIT)
    finally:
        os.close(fd)
    assert not (tmp_path / "missing-parent").exists()


# --------------------------------------------------------------------------
# Direct bootstrap fault injection through the TEST-ONLY seam
# --------------------------------------------------------------------------

def _bootstrap_direct(package, core, parent, *, seam=None):
    fd = os.open(str(package / "authority.bundle"), os.O_RDONLY)
    try:
        return bootstrap_trusted_bare(
            isolated_parent=str(parent), bundle_fd=fd,
            head_oid=core["authority"]["commit_oid"],
            pin_ref=core["authority"]["pin_ref"],
            genesis_oid=core["authority"]["genesis_oid"],
            commit_count=core["authority"]["commit_count"],
            object_format=core["authority"]["object_format"],
            identity=core["identity"], git_executable=GIT,
            post_unbundle_seam=seam)
    finally:
        os.close(fd)


def test_c13_smuggled_unreachable_loose_object_rejected(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    parent = tmp_path / "bare-parent"
    parent.mkdir(0o700)

    def seam(bare_path):
        result = subprocess.run(
            [GIT, "--git-dir", bare_path, "hash-object", "-w", "--stdin"],
            input=b"smuggled-unreachable-loose-object",
            capture_output=True, check=True)
        smuggled = result.stdout.decode().strip()
        assert len(smuggled) in (40, 64)

    with pytest.raises(RecoveryRepositoryError) as raised:
        _bootstrap_direct(package, core, parent, seam=seam)
    assert raised.value.args[0] == "object_set_not_exact"
    assert not _live_bare_names(parent) and len(_abandoned_names(parent)) == 1
    abandoned = parent / _abandoned_names(parent)[0]
    assert not (abandoned / "refs/heads/facts").exists()


def test_c14_default_hook_canary_never_runs(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    parent = tmp_path / "bare-parent"
    parent.mkdir(0o700)
    marker = tmp_path / "hook-canary-fired"
    hook_script = "#!/bin/sh\nprintf fired > '%s'\n" % marker

    def seam(bare_path):
        hooks = Path(bare_path) / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("reference-transaction", "post-update"):
            hook = hooks / name
            hook.write_text(hook_script)
            os.chmod(hook, 0o755)

    report = _bootstrap_direct(package, core, parent, seam=seam)
    # Bootstrap succeeded with the hook scripts physically present, but
    # core.hooksPath=/dev/null meant neither hook ever executed.
    assert not marker.exists()
    bare_path = Path(report.path)
    assert (bare_path / "hooks/reference-transaction").exists()
    assert (bare_path / "refs/heads/facts").is_file()


def test_c15_include_hookspath_injection_rejected(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    parent = tmp_path / "bare-parent"
    parent.mkdir(0o700)
    marker = tmp_path / "include-canary-fired"
    hook_dir = tmp_path / "evil-hooks"
    hook_dir.mkdir()
    hook = hook_dir / "reference-transaction"
    hook.write_text("#!/bin/sh\nprintf fired > '%s'\n" % marker)
    os.chmod(hook, 0o755)
    evil_cfg = tmp_path / "evil.cfg"
    evil_cfg.write_text('[core]\n\thooksPath = %s\n' % hook_dir)

    def seam(bare_path):
        Path(bare_path, "hooks").mkdir(exist_ok=True)
        with open(os.path.join(bare_path, "config"), "a") as stream:
            stream.write('[include]\n\tpath = %s\n' % evil_cfg)

    with pytest.raises(RecoveryRepositoryError) as raised:
        _bootstrap_direct(package, core, parent, seam=seam)
    assert raised.value.args[0] == "git_forbidden_local_config"
    assert not marker.exists()
    assert not _live_bare_names(parent) and len(_abandoned_names(parent)) == 1
    abandoned = parent / _abandoned_names(parent)[0]
    assert not (abandoned / "refs/heads/facts").exists()


@pytest.mark.parametrize("mode,reason", [
    ("alternates", "git_offline_bypass_present"),
    ("grafts", "git_offline_bypass_present"),
    ("remote-config", "git_forbidden_local_config"),
    ("promisor", "git_promisor_pack_present"),
])
def test_c16_offline_bypass_sentinels_fail_closed(world, tmp_path, mode, reason):
    package = _green_package(world, tmp_path)
    core = _core(package)
    parent = tmp_path / "bare-parent"
    parent.mkdir(0o700)

    def seam(bare_path):
        if mode == "alternates":
            info = Path(bare_path, "objects", "info")
            info.mkdir(parents=True, exist_ok=True)
            (info / "alternates").write_text("/etc\n")
        elif mode == "grafts":
            info = Path(bare_path, "info")
            info.mkdir(parents=True, exist_ok=True)
            (info / "grafts").write_text("%s %s\n" % ("a" * 40, "b" * 40))
        elif mode == "remote-config":
            with open(os.path.join(bare_path, "config"), "a") as stream:
                stream.write('[remote "evil"]\n'
                             '\turl = https://invalid.example/x\n'
                             '\tfetch = +refs/*:refs/*\n')
        else:
            pack = Path(bare_path, "objects", "pack")
            pack.mkdir(parents=True, exist_ok=True)
            (pack / "evil.promisor").write_bytes(b"")

    with pytest.raises(RecoveryRepositoryError) as raised:
        _bootstrap_direct(package, core, parent, seam=seam)
    assert raised.value.args[0] == reason
    assert not _live_bare_names(parent) and len(_abandoned_names(parent)) == 1


# --------------------------------------------------------------------------
# Layout and state binding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,reason", [
    ("roots-overlap", "layout_roots_overlap"),
    ("role-missing", "layout_w_roots_mismatch"),
    ("role-extra", "layout_w_roots_mismatch"),
    ("relative-path", "layout_invalid"),
])
def test_c17_layout_rejection_writes_nothing(world, tmp_path, mode, reason):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    base, paths = _layout_paths(tmp_path, roles, base_name="rec-layout")
    paths["state"].mkdir(0o700, parents=True)
    _create_case(paths["state"], core)
    if mode == "roots-overlap":
        w_roots = tuple((role, str(paths["archive"])) for role in roles)
    elif mode == "role-missing":
        w_roots = (("w_items", str(paths["w:w_items"])),)
    elif mode == "role-extra":
        extra = base / "w-extra"
        w_roots = tuple((role, str(paths["w:" + role])) for role in roles) \
            + (("w_extra", str(extra)),)
    else:
        w_roots = tuple((role, "w-" + role) for role in roles)
    layout = InstallLayout(
        str(paths["state"]), str(paths["bare"]), w_roots,
        str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout))
    assert raised.value.reason == reason
    assert set(os.listdir(base)) == {"state"}
    assert set(os.listdir(paths["state"])) == {"recovery-state.sqlite3"}


@pytest.mark.parametrize("mode,reason", [
    ("unknown-case", "state_case_not_quarantined"),
    ("wrong-backup", "state_case_not_quarantined"),
    ("no-journal", "state_case_not_quarantined"),
])
def test_c18_case_binding_failures_write_nothing(world, tmp_path, mode, reason):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    layout, paths, base = _make_layout(tmp_path, roles, base_name="rec-case")
    case_id = CASE
    if mode == "unknown-case":
        _create_case(paths["state"], core)
        case_id = "case-never-created"
    elif mode == "wrong-backup":
        other = dict(core)
        other["backup_id"] = "bkp-other"
        _create_case(paths["state"], other)
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout, case_id=case_id))
    assert raised.value.reason == reason
    assert os.listdir(paths["bare"]) == []
    assert os.listdir(paths["archive"]) == []
    state_names = set(os.listdir(paths["state"]))
    assert "recovery-materialize.lock" not in state_names


# --------------------------------------------------------------------------
# Single writer, no automatic resume
# --------------------------------------------------------------------------

def test_c19_concurrent_second_writer_lock_refused(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-lock")
    _create_case(paths["state"], core, CASE, 1)

    # R5 only lets the newest epoch advance, so two concurrently advancing
    # cases cannot share a journal; the real second-writer scenario is
    # another installer holding the flock on this layout's lock file.
    state_fd = os.open(str(paths["state"]), os.O_RDONLY | os.O_DIRECTORY)
    try:
        holder_fd = materializer._create_locked_at(state_fd, "recovery-materialize.lock")
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
            with pytest.raises(MaterialInstallError) as raised:
                install_materials(**_kwargs(package, core, layout))
            assert raised.value.reason == "single_writer_lock_held"
        finally:
            release.set()
            thread.join(10)
    finally:
        os.close(holder_fd)
        os.close(state_fd)

    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        assert store.get(CASE).phase == RecoveryPhase.QUARANTINED
    assert paths["bare"] and os.listdir(paths["bare"]) == []
    assert os.listdir(paths["archive"]) == []
    assert os.listdir(paths["mapping"]) == []
    # The holder released; a fresh install now runs end to end.
    os.unlink(paths["state"] / "recovery-materialize.lock")
    receipt = install_materials(**_kwargs(package, core, layout))
    assert receipt.stage == "materials_staged"


def test_c20_success_is_terminal_and_reverifies(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    layout, paths, _base = _make_layout(tmp_path, _roles(core),
                                        base_name="rec-terminal")
    _create_case(paths["state"], core)
    receipt = install_materials(**_kwargs(package, core, layout))
    assert receipt.stage == "materials_staged"
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout))
    assert raised.value.reason == "state_case_not_quarantined"
    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is True and report.proofs_match is True


def test_c21_reverify_without_attempt_and_unknown_case(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-rev")
    _create_case(paths["state"], core)
    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is False
    assert report.reasons == ("no_install_attempt_recorded",)

    lock = paths["state"] / "recovery-materialize.lock"
    with lock.open("xb"):
        pass
    os.chmod(lock, 0o600)
    report = reverify_materials(**_kwargs(package, core, layout,
                                          case_id="case-never-created"))
    assert report.complete is False
    assert "state_case_unknown" in report.reasons


# --------------------------------------------------------------------------
# Half-install: foreign file breaks exact-set, roots voided, no resume
# --------------------------------------------------------------------------

def test_c22_foreign_file_fails_exact_set_and_voids(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-half")
    _create_case(paths["state"], core)
    foreign = paths["archive"] / "foreign.dat"
    foreign.write_bytes(b"not-from-package")

    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout))
    assert raised.value.reason == "root_set_not_exact:archive"
    assert raised.value.stage_reached == "package_verified"

    # Everything this call placed is gone; the foreign file is untouched;
    # the bare bootstrap itself had succeeded and stays quarantined.
    assert _regular_files(paths["archive"]) == {"foreign.dat"}
    assert _regular_files(paths["mapping"]) == set()
    assert _regular_files(paths["r2"]) == set()
    for role in roles:
        assert _regular_files(paths["w:" + role]) == set()
    assert len(_live_bare_names(paths["bare"])) == 1
    assert not _abandoned_names(paths["bare"])
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        assert store.get(CASE).phase == RecoveryPhase.PACKAGE_VERIFIED

    # First round offers no automatic resume; a fresh case hits the lock.
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        store.create_case(
            CASE_B, RecoveryEpoch("recovery-c", "epoch-c-2", 2),
            backup_id=core["backup_id"],
            barrier_digest=barrier_digest(core["barrier_id"]),
            prior_epochs=(RecoveryEpoch("recovery-c", "epoch-c-1", 1),),
            at=AT_INSTALL)
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout, case_id=CASE_B))
    assert raised.value.reason == "single_writer_lock_held"

    report = reverify_materials(**_kwargs(package, core, layout))
    assert report.complete is False
    assert "root_set_not_exact:archive" in report.reasons
    assert "phase_not_materials_staged" in report.reasons
    assert "proofs_mismatch" in report.reasons


# --------------------------------------------------------------------------
# Two real processes: SIGKILL after the fsync point, then read-only reopen
# --------------------------------------------------------------------------

def test_c23_sigkill_then_new_process_readonly_reverify(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    layout, paths, _base = _make_layout(tmp_path, roles, base_name="rec-kill")
    _create_case(paths["state"], core)

    argv = _chip_argv(package, paths, CASE, AT_INSTALL,
                      {"kill_after_first_file": True})
    result = subprocess.run(
        [sys.executable, "-c", _INSTALL_CHIP, *argv],
        capture_output=True, text=True, timeout=300)
    # Direct signal exit is -9; through the bwrap wrapper it is 128+9.
    assert result.returncode in (-9, 137), result.stderr[-2000:]

    # The first process is dead: its flock is released by the kernel but
    # the O_EXCL lock file records the attempt.
    assert (paths["state"] / "recovery-materialize.lock").is_file()
    with RecoveryStateStore(Path(str(paths["state"]))) as store:
        view = store.get(CASE)
        anchor = store.journal_anchor()
    assert view.phase == RecoveryPhase.PACKAGE_VERIFIED
    assert anchor[0] == 2

    live = _live_bare_names(paths["bare"])
    assert len(live) == 1 and not _abandoned_names(paths["bare"])
    placed = sum(len(_regular_files(p)) for p in (
        paths["archive"], paths["mapping"], paths["r2"],
        *(paths["w:" + role] for role in roles)))
    expected_total = len(core["payload"]["files"])
    assert 0 < placed < expected_total

    # A brand-new process performs only the same-identity read-only
    # re-verification; it never resumes, repairs or advances anything.
    _r, report = _run_chip(
        _REVERIFY_CHIP,
        [str(package), str(paths["state"]), str(paths["bare"]),
         str(paths["w:w_items"]), str(paths["w:w_ledger"]),
         str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]),
         CASE, AT_INSTALL])
    assert report["complete"] is False
    assert report["phase"] == "package_verified"
    assert report["proofs_match"] is False
    reasons = set(report["reasons"])
    assert "phase_not_materials_staged" in reasons
    assert "proofs_mismatch" in reasons
    assert any(r.startswith("root_set_not_exact") for r in reasons)


# --------------------------------------------------------------------------
# Real ENOSPC on a tiny tmpfs inside a user namespace
# --------------------------------------------------------------------------

_ENOSPC_CHIP = r"""
import errno, json, os, sys
from pathlib import Path
from app.facts_tree.backup_assembly import PAYLOAD_SCHEMA, PAYLOAD_VERSION
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.recovery_epoch import RecoveryEpoch
from app.facts_tree.recovery_materializer import (
    AdmissionEvidence, InstallLayout, MaterialInstallError,
    barrier_digest, install_materials)
from app.facts_tree.recovery_state_store import RecoveryStateStore
pkg, dest, case_id, at, headroom = sys.argv[1:6]
headroom = int(headroom)
report = {}
names = ("state", "bare", "archive", "mapping", "r2", "w-w_items", "w-w_ledger")
paths = {name: os.path.join(dest, name) for name in names}
for path in paths.values():
    os.mkdir(path, 0o700)
core = json.load(open(os.path.join(pkg, "manifest.json")))["core"]
with RecoveryStateStore(Path(paths["state"])) as store:
    store.create_case(case_id, RecoveryEpoch("recovery-c", "epoch-c-1", 1),
                      backup_id=core["backup_id"],
                      barrier_digest=barrier_digest(core["barrier_id"]),
                      prior_epochs=(), at="2026-11-02T00:00:00.000000+00:00")
# Fill the tmpfs, then hand exactly `headroom` bytes back: enough for the
# package_verified journal append, never enough for the full material set.
reserve = os.open(os.path.join(dest, ".reserve"),
                  os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
size = 0
saw_enospc = False
try:
    while True:
        try:
            os.write(reserve, b"x" * 4096)
            size += 4096
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                saw_enospc = True
                break
            raise
    os.ftruncate(reserve, max(0, size - headroom))
    st = os.statvfs(dest)
    report["free_bytes"] = st.f_bavail * st.f_bsize
finally:
    os.close(reserve)
report["saw_enospc"] = saw_enospc
anchors = (("envelope_version", "2"), ("payload_schema", PAYLOAD_SCHEMA),
           ("payload_version", str(PAYLOAD_VERSION)),
           ("object_format", core["authority"]["object_format"]))
evidence = AdmissionEvidence("admitted", compute_core_digest(core), anchors)
layout = InstallLayout(paths["state"], paths["bare"],
                       (("w_items", paths["w-w_items"]),
                        ("w_ledger", paths["w-w_ledger"])),
                       paths["archive"], paths["mapping"], paths["r2"])
try:
    install_materials(package_dir=pkg, layout=layout, case_id=case_id,
                      identity=core["identity"],
                      head_oid=core["authority"]["commit_oid"],
                      evidence=evidence, git_executable="/usr/bin/git", at=at)
    report["outcome"] = "unexpected-success"
except MaterialInstallError as exc:
    report["outcome"] = "material-error"
    report["reason"] = exc.reason
    report["stage_reached"] = exc.stage_reached
except Exception as exc:
    report["outcome"] = "other-error"
    report["reason"] = type(exc).__name__ + ":" + str(exc)
leftovers = {}
for name in ("archive", "mapping", "r2", "w-w_items", "w-w_ledger"):
    remaining = []
    for dirpath, _dirs, files in os.walk(paths[name]):
        for filename in files:
            remaining.append(os.path.relpath(
                os.path.join(dirpath, filename), paths[name]))
    leftovers[name] = sorted(remaining)
report["leftovers"] = leftovers
report["bare_entries"] = sorted(os.listdir(paths["bare"]))
report["lock_present"] = os.path.exists(
    os.path.join(paths["state"], "recovery-materialize.lock"))
try:
    with RecoveryStateStore(Path(paths["state"])) as store:
        report["phase"] = store.get(case_id).phase.value
except Exception as exc:
    report["phase"] = "unreadable:" + type(exc).__name__
print("REPORT=" + json.dumps(report, default=str))
"""


def test_c24_enospc_real_tmpfs_fails_closed(world, tmp_path):
    package = _green_package(world, tmp_path)
    dest = tmp_path / "enospc-dest"
    dest.mkdir(0o700)
    sam_root = os.environ["SAM_TEST_ROOT"]
    # 200 KiB tmpfs: seed journal fits, 24 KiB headroom cannot fit the
    # ~37 KiB green material set plus a complete bare bootstrap.
    bwrap_args = [
        "--unshare-net", "--unshare-user",
        "--ro-bind", "/", "/",
        "--bind", sam_root, sam_root,
        "--ro-bind", str(package), str(package),
        "--size", "204800", "--tmpfs", str(dest),
        "--proc", "/proc", "--dev", "/dev",
    ]
    _result, report = _run_chip(
        _ENOSPC_CHIP, [str(package), str(dest), CASE, AT_INSTALL, "24576"],
        bwrap_args=bwrap_args, timeout=300)
    assert report["saw_enospc"] is True, report
    assert 0 < report["free_bytes"] <= 40960, report
    assert report["outcome"] == "material-error", report
    assert report["reason"].startswith((
        "material_landing_failed", "material_reverify_failed",
        "material_install_failed", "git_command_failed",
        "bare_create_failed", "git_")), report
    assert report["stage_reached"] == "package_verified", report
    assert report["phase"] == "package_verified", report
    assert report["lock_present"] is True
    assert all(files == [] for files in report["leftovers"].values()), report
    bare = report["bare_entries"]
    live = [n for n in bare if n.endswith(".git")
            and not n.endswith(".abandoned.git")]
    abandoned = [n for n in bare if n.endswith(".abandoned.git")]
    assert len(live) + len(abandoned) == 1, report


# --------------------------------------------------------------------------
# Format collisions and hostile Git environments
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,head_mutator", [
    ("sha1", lambda c: "0" * 64),
    ("sha1", lambda c: "0" * 40),
    ("sha1", lambda c: "a" * 39),
    ("sha1", lambda c: "z" * 40),
])
def test_c25_sha1_head_oid_collisions_rejected(world, tmp_path, fmt, head_mutator):
    package = _green_package(world, tmp_path)
    core = _core(package)
    roles = _roles(core)
    base, paths = _layout_paths(tmp_path, roles, base_name="rec-collide")
    layout = InstallLayout(
        str(paths["state"]), str(paths["bare"]),
        tuple((role, str(paths["w:" + role])) for role in roles),
        str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
    with pytest.raises(MaterialInstallError) as raised:
        install_materials(**_kwargs(package, core, layout,
                                    head=head_mutator(core)))
    assert raised.value.reason == "head_anchor_mismatch"
    assert not base.exists()


def test_c25_sha256_package_rejects_sha1_head(world, tmp_path):
    cut = _s64_sha256_cut(world, "bkp-c-collide")
    try:
        assert assemble_backup(cut["plan"], cut["bid"]).status == "published"
        package = _copy_package(cut["package"], tmp_path, name="collide")
        core = _core(package)
        roles = _roles(core)
        base, paths = _layout_paths(tmp_path, roles, base_name="rec-collide256")
        layout = InstallLayout(
            str(paths["state"]), str(paths["bare"]),
            tuple((role, str(paths["w:" + role])) for role in roles),
            str(paths["archive"]), str(paths["mapping"]), str(paths["r2"]))
        with pytest.raises(MaterialInstallError) as raised:
            install_materials(**_kwargs(package, core, layout, head="0" * 40))
        assert raised.value.reason == "head_anchor_mismatch"
        assert not base.exists()
    finally:
        cut["broker_db"].close()
        cut["connection"].close()


def test_c26_install_inside_unshare_net_user_namespace(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    layout, paths, _base = _make_layout(tmp_path, _roles(core),
                                        base_name="rec-bwrap")
    _create_case(paths["state"], core)
    sam_root = os.environ["SAM_TEST_ROOT"]
    bwrap_args = [
        "--unshare-net", "--unshare-user",
        "--ro-bind", "/", "/",
        "--bind", sam_root, sam_root,
        "--ro-bind", str(package), str(package),
        "--proc", "/proc", "--dev", "/dev",
    ]
    _result, report = _run_chip(
        _INSTALL_CHIP,
        _chip_argv(package, paths, CASE, AT_INSTALL, {}),
        env=dict(os.environ, GIT_DIR="/nonexistent",
                 GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                 GIT_CONFIG_VALUE_0="/nonexistent",
                 HTTPS_PROXY="http://invalid:9"),
        bwrap_args=bwrap_args)
    assert report["ok"], report
    receipt = report["receipt"]
    assert receipt["stage"] == "materials_staged"
    assert receipt["bare"]["object_format"] == "sha1"
    assert len(receipt["bare"]["head_oid"]) == 40
    report2 = reverify_materials(**_kwargs(package, core, layout))
    assert report2.complete is True


def test_c27_sha1_explicit_wins_under_sha256_git_env(world, tmp_path):
    package = _green_package(world, tmp_path)
    core = _core(package)
    layout, paths, _base = _make_layout(tmp_path, _roles(core),
                                        base_name="rec-env")
    _create_case(paths["state"], core)
    hostile_env = dict(os.environ,
                       GIT_TEST_DEFAULT_HASH="sha256",
                       GIT_DEFAULT_HASH="sha256",
                       GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="core.hooksPath",
                       GIT_CONFIG_VALUE_0="/nonexistent",
                       GIT_TERMINAL_PROMPT="1",
                       HTTP_PROXY="http://invalid:9",
                       ALL_PROXY="socks5://invalid:9")
    _result, report = _run_chip(
        _INSTALL_CHIP,
        _chip_argv(package, paths, CASE, AT_INSTALL, {}),
        env=hostile_env)
    assert report["ok"], report
    receipt = report["receipt"]
    assert receipt["bare"]["object_format"] == "sha1"
    assert len(receipt["bare"]["head_oid"]) == 40
    assert receipt["stage"] == "materials_staged"
