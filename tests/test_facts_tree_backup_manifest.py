"""Pinned checks for the F-001 minimal backup manifest digest chain.

Covers the approved F-001 backup design section 5 (manifest and
verification report) at the smallest in-memory kernel: digest stability
under key reordering, detection of core changes, rejection of a wrong
core reference in the report, rejection of a wrong report digest in the
final manifest, absence of circular digest fields in a successful chain,
and rejection of illegal inputs.
"""
import pytest

from app.facts_tree import backup_manifest
from app.facts_tree.backup_manifest import (
    BackupManifestError,
    build_final_manifest,
    build_verification_report,
    compute_core_digest,
    compute_report_digest,
    verify_final_manifest,
)
from app.facts_tree.git_format import MAX_DEPTH


def _core(backup_id="bk-0001"):
    return {
        "manifest_version": 1,
        "backup_id": backup_id,
        "barrier_id": "bar-0001",
        "exact_h": "808a9deeb759e2ff30ac0001737e8a372640f89b",
        "components": [
            {"path": "git/export.bundle", "role": "H", "size": 10, "hash": "sha256:" + "a1" * 32},
            {"path": "w/app.db-checkpoint", "role": "W", "size": 20, "hash": "sha256:" + "b2" * 32},
        ],
    }


def _chain():
    core = _core()
    report = build_verification_report(core, checks={"h_reachable": True})
    manifest = build_final_manifest(core, report)
    return core, report, manifest


def test_digest_stable_across_key_insertion_order():
    first = {"a": 1, "b": {"x": 2, "y": 3}, "c": [1, 2]}
    second = {"c": [1, 2], "b": {"y": 3, "x": 2}, "a": 1}
    assert compute_core_digest(first) == compute_core_digest(second)
    report_first = build_verification_report(first)
    report_second = build_verification_report(second)
    assert compute_report_digest(report_first) == compute_report_digest(report_second)


def test_core_change_is_detected():
    core, report, manifest = _chain()
    assert compute_core_digest(core) != compute_core_digest(_core(backup_id="bk-0002"))
    # Tampering with the embedded core breaks the chain at verification.
    manifest["core"]["backup_id"] = "bk-9999"
    with pytest.raises(BackupManifestError, match="core digest"):
        verify_final_manifest(manifest, report)


def test_report_with_wrong_core_reference_is_rejected():
    core = _core()
    foreign_report = build_verification_report(_core(backup_id="bk-other"))
    with pytest.raises(BackupManifestError, match="different core digest"):
        build_final_manifest(core, foreign_report)
    # A hand-tampered core reference is rejected as well.
    report = build_verification_report(core)
    report["core_digest"] = "sha256:" + "ff" * 32
    with pytest.raises(BackupManifestError, match="different core digest"):
        build_final_manifest(core, report)


def test_final_manifest_with_wrong_report_digest_is_rejected():
    core, report, manifest = _chain()
    manifest["verification"]["report_digest"] = "sha256:" + "ee" * 32
    with pytest.raises(BackupManifestError, match="report digest"):
        verify_final_manifest(manifest, report)
    # Changing the report after the manifest was built is also detected.
    _, fresh_report, fresh_manifest = _chain()
    fresh_report["checks"]["h_reachable"] = False
    with pytest.raises(BackupManifestError, match="report digest"):
        verify_final_manifest(fresh_manifest, fresh_report)


def test_successful_chain_has_no_circular_fields():
    core, report, manifest = _chain()
    assert verify_final_manifest(manifest, report) is None
    assert "report_digest" not in report
    assert "manifest_digest" not in report
    assert "manifest_digest" not in manifest
    assert set(manifest["verification"]) == {"core_digest", "report_digest"}
    assert manifest["verification"]["core_digest"] == report["core_digest"]
    assert manifest["verification"]["report_digest"] == compute_report_digest(report)


def test_illegal_inputs_are_rejected():
    core, report, manifest = _chain()
    # Malformed digest strings.
    for bad in ("a1" * 32, "SHA256:" + "a1" * 32, "sha256:" + "a1" * 31, "sha256:" + "g1" * 32, None, 7):
        broken_report = dict(report, core_digest=bad)
        with pytest.raises(BackupManifestError):
            build_final_manifest(core, broken_report)
        broken_manifest = {
            "schema": manifest["schema"],
            "version": manifest["version"],
            "core": manifest["core"],
            "verification": {"core_digest": bad, "report_digest": manifest["verification"]["report_digest"]},
        }
        with pytest.raises(BackupManifestError):
            verify_final_manifest(broken_manifest, report)
    # Non-dict payloads.
    with pytest.raises(BackupManifestError):
        compute_core_digest(["not", "a", "dict"])
    with pytest.raises(BackupManifestError):
        compute_report_digest("not-a-dict")
    with pytest.raises(BackupManifestError):
        verify_final_manifest(None, report)
    # Self-referencing digest fields are refused anywhere in the chain.
    with pytest.raises(BackupManifestError, match="report_digest"):
        build_verification_report(core, checks={"report_digest": "sha256:" + "aa" * 32})
    circular_report = dict(report, report_digest=compute_report_digest(report))
    with pytest.raises(BackupManifestError, match="report_digest"):
        compute_report_digest(circular_report)
    circular_manifest = dict(manifest, manifest_digest="sha256:" + "cc" * 32)
    with pytest.raises(BackupManifestError, match="manifest_digest"):
        verify_final_manifest(circular_manifest, report)


def test_compute_report_digest_validates_report_shape_first():
    _, report, _ = _chain()
    # A well-formed report digests fine.
    assert compute_report_digest(report).startswith("sha256:")
    # Missing schema / version / core_digest are all refused.
    for field in ("schema", "version", "core_digest"):
        broken = {k: v for k, v in report.items() if k != field}
        with pytest.raises(BackupManifestError):
            compute_report_digest(broken)
    # Wrong schema, wrong version and invalid core_digest are refused.
    with pytest.raises(BackupManifestError, match="schema"):
        compute_report_digest(dict(report, schema="sam.evil.other_report"))
    with pytest.raises(BackupManifestError, match="version"):
        compute_report_digest(dict(report, version=999))
    for bad in ("a1" * 32, "SHA256:" + "a1" * 32, "sha256:" + "a1" * 31, None, 7):
        with pytest.raises(BackupManifestError, match="core_digest"):
            compute_report_digest(dict(report, core_digest=bad))


def test_nested_self_digest_fields_in_core_are_rejected():
    bad = "sha256:" + "aa" * 32
    for field in ("report_digest", "manifest_digest"):
        # Nested inside a core component (list of dicts).
        core = _core()
        core["components"][0][field] = bad
        with pytest.raises(BackupManifestError, match=field):
            compute_core_digest(core)
        # Deeply nested inside an arbitrary core sub-object.
        core = _core()
        core["scope"] = {"installation": {"deep": [{"leaf": {field: bad}}]}}
        with pytest.raises(BackupManifestError, match=field):
            build_verification_report(core)


def test_nested_self_digest_fields_in_report_checks_are_rejected():
    bad = "sha256:" + "aa" * 32
    core = _core()
    for field in ("report_digest", "manifest_digest"):
        # Nested inside checks, through a list level.
        with pytest.raises(BackupManifestError, match=field):
            build_verification_report(core, checks={"summary": {"rows": [1, {field: bad}]}})
        # Tampering an already-built report's nested checks.
        report = build_verification_report(core)
        report["checks"]["nested"] = {"deeper": {field: bad}}
        with pytest.raises(BackupManifestError, match=field):
            compute_report_digest(report)
        with pytest.raises(BackupManifestError, match=field):
            build_final_manifest(core, report)


def test_final_manifest_rejects_nested_manifest_digest_but_keeps_report_reference():
    core, report, manifest = _chain()
    # The legitimate verification.report_digest reference still verifies.
    assert verify_final_manifest(manifest, report) is None
    bad = "sha256:" + "cc" * 32
    # manifest_digest at a nested position in the final manifest.
    broken = _chain()[2]
    broken["verification"]["detail"] = {"nested": [{"manifest_digest": bad}]}
    with pytest.raises(BackupManifestError, match="manifest_digest"):
        verify_final_manifest(broken, report)
    # report_digest outside the single allowed verification path.
    leaked = _chain()[2]
    leaked["extra"] = {"report_digest": bad}
    with pytest.raises(BackupManifestError, match="report_digest"):
        verify_final_manifest(leaked, report)
    buried = _chain()[2]
    buried["verification"]["detail"] = {"report_digest": bad}
    with pytest.raises(BackupManifestError, match="report_digest"):
        verify_final_manifest(buried, report)


def test_unknown_top_level_manifest_keys_are_rejected():
    _, report, manifest = _chain()
    tampered = dict(manifest, signature="sha256:" + "ab" * 32)
    with pytest.raises(BackupManifestError, match="unknown key"):
        verify_final_manifest(tampered, report)
    # A missing envelope key is refused as well (no implicit optional keys).
    incomplete = {key: value for key, value in manifest.items() if key != "core"}
    with pytest.raises(BackupManifestError, match="required key"):
        verify_final_manifest(incomplete, report)


def test_mixed_type_unknown_envelope_keys_fail_closed():
    _, report, manifest = _chain()
    # An int key beside a string key used to leak TypeError from sorted()
    # while formatting the unknown-key error.
    tampered = {**manifest, 7: True, "rogue": True}
    with pytest.raises(BackupManifestError, match="non-string key"):
        verify_final_manifest(tampered, report)
    # Same edge inside the verification block.
    bad_block = {
        **manifest,
        "verification": {**manifest["verification"], 3: "x", "extra": 1},
    }
    with pytest.raises(BackupManifestError, match="non-string key"):
        verify_final_manifest(bad_block, report)
    # A lone non-string key at the report envelope fails closed as well.
    with pytest.raises(BackupManifestError, match="non-string key"):
        compute_report_digest({**report, 9: 1})
    # A normal string unknown key still produces the original message.
    with pytest.raises(BackupManifestError, match="unknown key: 'rogue'"):
        verify_final_manifest({**manifest, "rogue": True}, report)


def test_unknown_verification_block_keys_are_rejected():
    _, report, manifest = _chain()
    tampered = {**manifest, "verification": {**manifest["verification"], "extra": True}}
    with pytest.raises(BackupManifestError, match="unknown key"):
        verify_final_manifest(tampered, report)
    missing = {**manifest, "verification": {"core_digest": manifest["verification"]["core_digest"]}}
    with pytest.raises(BackupManifestError, match="required key"):
        verify_final_manifest(missing, report)


def test_unknown_report_envelope_keys_are_rejected():
    core, report, _ = _chain()
    with pytest.raises(BackupManifestError, match="unknown key"):
        compute_report_digest(dict(report, rogue=1))
    with pytest.raises(BackupManifestError, match="unknown key"):
        build_final_manifest(core, dict(report, rogue=1))
    # The checks envelope key is mandatory; fields inside it stay caller-defined.
    missing_checks = {key: value for key, value in report.items() if key != "checks"}
    with pytest.raises(BackupManifestError, match="required key"):
        compute_report_digest(missing_checks)
    rich_report = build_verification_report(
        core, checks={"h_reachable": True, "summary": {"rows": [1, 2], "ok": None}}
    )
    assert compute_report_digest(rich_report).startswith("sha256:")


def test_cyclic_containers_fail_closed_as_backup_manifest_error():
    cyclic_core = {}
    cyclic_core["self"] = cyclic_core
    with pytest.raises(BackupManifestError, match="cyclic"):
        compute_core_digest(cyclic_core)
    # A self-referencing list nested in checks fails at construction time.
    loop = []
    loop.append(loop)
    with pytest.raises(BackupManifestError, match="cyclic"):
        build_verification_report(_core(), checks={"loop": loop})
    # A cyclic core embedded in a presented manifest fails at verification.
    _, report, manifest = _chain()
    cyclic_manifest = dict(manifest)
    cyclic_manifest["core"] = cyclic_core
    with pytest.raises(BackupManifestError, match="cyclic"):
        verify_final_manifest(cyclic_manifest, report)


def test_nesting_deeper_than_max_depth_fails_closed():
    deep = "leaf"
    for _ in range(MAX_DEPTH + 1):
        deep = {"n": deep}
    with pytest.raises(BackupManifestError, match="MAX_DEPTH"):
        compute_core_digest(deep)
    with pytest.raises(BackupManifestError, match="MAX_DEPTH"):
        build_verification_report(deep)
    # Exactly at the serializer bound stays valid.
    at_limit = "leaf"
    for _ in range(MAX_DEPTH):
        at_limit = {"n": at_limit}
    assert compute_core_digest(at_limit).startswith("sha256:")


def test_unserializable_values_fail_closed_as_backup_manifest_error():
    # float, arbitrary objects and non-str keys are serializer TypeErrors.
    for bad in (1.5, object()):
        with pytest.raises(BackupManifestError, match="serialization"):
            compute_core_digest({"bad": bad})
    with pytest.raises(BackupManifestError, match="serialization"):
        compute_core_digest({("tuple", "key"): 1})
    # Same normalization on the report path, including post-build tampering.
    with pytest.raises(BackupManifestError, match="serialization"):
        build_verification_report(_core(), checks={"ratio": 0.5})
    _, report, _ = _chain()
    report["checks"]["object"] = object()
    with pytest.raises(BackupManifestError, match="serialization"):
        compute_report_digest(report)


def test_shared_but_acyclic_nested_containers_stay_valid():
    shared = {"x": 1, "y": [1, 2]}
    core = _core()
    core["refs"] = {"a": shared, "b": shared}
    report = build_verification_report(core, checks={"s1": shared, "s2": shared})
    manifest = build_final_manifest(core, report)
    assert verify_final_manifest(manifest, report) is None


def test_digest_format_and_schema_constants():
    digest = compute_core_digest(_core())
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert backup_manifest.DIGEST_ALGORITHM == "sha256"
    assert backup_manifest.SCHEMA_VERSION == 1
    _, report, manifest = _chain()
    assert report["schema"] == backup_manifest.REPORT_SCHEMA
    assert manifest["schema"] == backup_manifest.MANIFEST_SCHEMA
