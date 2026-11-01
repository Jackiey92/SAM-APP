"""F-001 vault assembly/publish slice A1 acceptance tests.

Real-machine drills only: a real Git authority repository (and a real
SHA-256 repository in the format-gap drill), real WAL SQLite freeze
checkpoints produced by the real in-barrier freeze coordinator, real
maintenance pins and real offline ``git bundle`` exports (with the
bundle primitive's own verify/empty-repo fetch/fsck suite plus an
independent ``git bundle list-heads``), a real ArchiveFileStore with
its SQLite metadata, a real VersionedMappingObjectStore and a real
append-only mapping version ledger. Nothing mocks the success path, a
hash or a digest chain; the only injected faults land on storage, path
or process boundaries (byte tampering, crash hooks, foreign scope).

Section 4 scenario map:

  4.1 green path ........... test_green_path_publishes_package_and_switches_current_atomically
  4.5 idempotency/coexist .. test_rerun_same_backup_id_is_refused_and_second_package_coexists
  4.2 R incomplete ......... test_missing_r_object_blocks_with_precise_gap_and_keeps_current
  4.3 family damage ........ test_bundle_tamper_after_export_blocks_publication
                             test_w_checkpoint_byte_tamper_blocks_publication
                             test_r_object_byte_corruption_is_corrupt_and_blocks
                             test_foreign_scope_refused_before_any_package_and_on_current_read
  4.4 atomic crashes ....... test_crash_after_bundle_export_leaves_partial_staging_and_rerun_publishes
                             test_crash_after_manifest_staged_leaves_verified_staging_and_rerun_publishes
                             test_crash_between_package_and_current_forward_recovers_current
                             test_crash_after_current_switched_is_recognized_as_already_published
  4.5 damaged position ..... test_damaged_package_position_is_conflict_and_never_overwritten
  4.6 stable digests ....... test_two_location_reinstall_reproduces_digests_without_secrets
  non-frozen refusal ....... test_non_frozen_and_absent_records_are_refused
  sha256 closure gap ....... test_sha256_cut_fails_closed_on_40_hex_closure_contract
  4.9 network sealed ....... test_assembly_succeeds_with_all_sockets_disabled
  4.7/A7 wiring ............ test_no_wiring_and_offline_import_graph
"""
from __future__ import annotations

import ast
import glob
import hashlib
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from app.db import init_db
from app.facts_tree import backup_assembly as assembly
from app.facts_tree.backup_assembly import (
    AlreadyPublishedError,
    AssemblyCitation,
    AssemblyPackageConflictError,
    AssemblyPlan,
    AssemblyReference,
    AssemblyScopeMismatchError,
    CRASH_PHASES,
    CutNotFrozenError,
    assemble_backup,
    read_current_backup,
    scan_assembly_state,
)
from app.facts_tree.backup_coordinator import (
    AssemblyIdentity,
    BarrierBudget,
    FreezeAssembly,
    WorkDatabaseSpec,
    freeze_consistent_cut,
)
from app.facts_tree.backup_manifest import verify_final_manifest
from app.facts_tree.backup_object_closure import (
    OBJECT_ATTACHMENT,
    OBJECT_MAPPING_VERSION,
    OBJECT_ORIGINAL,
    SCOPE_COMPANY,
    SCOPE_INSTALLATION,
    ClosureStores,
)
from app.facts_tree.broker_coordination import BrokerCoordination
from app.facts_tree.git_format import loads_record
from app.facts_tree.git_repository import (
    FactsGitRepository,
    maintenance_pin_ref,
)
from app.facts_tree.mapping_version_index import MappingVersionLedger
from app.storage.archive_store import ArchiveFileStore
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

_IDS = {
    "repo_id": "repo-a1-0001",
    "installation_id": "inst-a1-0001",
    "company_id": "co-a1-0001",
}
_IDS_B = {
    "repo_id": "repo-a1-9002",
    "installation_id": "inst-a1-9002",
    "company_id": "co-a1-9002",
}
_IDENTITY = AssemblyIdentity(**_IDS)
_IDENTITY_B = AssemblyIdentity(**_IDS_B)
_GREEN_BID = "bkp-a1-green"


class _InjectedCrash(Exception):
    """Test-only process-boundary crash raised from a crash hook."""


# ---------------------------------------------------------------------------
# Synthetic-but-real world: repo, freeze, stores, ledger, declared refs
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    repository = FactsGitRepository.initialize(
        tmp_path / "facts.git", object_format="sha1", **_IDS
    )
    retention = tmp_path / "retention"
    retention.mkdir(0o700)

    broker_db = sqlite3.connect(":memory:", check_same_thread=False)
    coordination = BrokerCoordination(
        tmp_path / "domain", _IDS["repo_id"], broker_db
    )

    connections = {}

    def work_spec(role):
        if role not in connections:
            source = tmp_path / (role + ".db")
            connection = sqlite3.connect(source)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE work_state ("
                "id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, payload TEXT)"
            )
            connection.execute(
                "INSERT INTO work_state VALUES (1, ?, 'payload-1')",
                (_IDS["company_id"],),
            )
            connection.commit()

            def scope_probe(conn):
                return tuple(row[0] for row in conn.execute(
                    "SELECT DISTINCT company_id FROM work_state ORDER BY 1"))

            connections[role] = (
                WorkDatabaseSpec(
                    role=role,
                    connection=connection,
                    scope_probe=scope_probe,
                    schema={"tables": ["work_state"]},
                    scope_watermark={"max_id": 1},
                ),
                connection,
            )
        return connections[role][0]

    def freeze(bid, *, roles=("w_items", "w_ledger"), identity=_IDENTITY,
               repo=None, coordination_obj=None, retention_root=None):
        repo = repo or repository
        coordination_obj = coordination_obj or coordination
        target_root = retention_root or retention
        if identity is _IDENTITY:
            specs = tuple(work_spec(role) for role in roles)
        else:
            specs = tuple(
                _foreign_spec(tmp_path, role, identity.company_id)
                for role in roles)
        evidence = freeze_consistent_cut(FreezeAssembly(
            backup_id=bid,
            identity=identity,
            coordination=coordination_obj,
            repository=repo,
            databases=specs,
            retention_root=target_root,
            budget=BarrierBudget(5.0, 30.0, 0.01),
        ))
        assert evidence.frozen, evidence.reason
        return evidence

    # Scope-B repository/retention are created lazily by the scope drill.
    foreign_state = {"retention": None, "repo": None, "coordination": None}

    def prepare_foreign():
        if foreign_state["repo"] is not None:
            return foreign_state
        foreign_state["repo"] = FactsGitRepository.initialize(
            tmp_path / "facts-b.git", object_format="sha1", **_IDS_B)
        foreign_state["retention"] = tmp_path / "retention-b"
        foreign_state["retention"].mkdir(0o700)
        broker_b = sqlite3.connect(":memory:", check_same_thread=False)
        foreign_state["coordination"] = BrokerCoordination(
            tmp_path / "domain-b", _IDS_B["repo_id"], broker_b)
        return foreign_state

    # Real content-addressed stores on a private 2A root.
    app_db = tmp_path / "app.db"
    init_db(app_db)
    with sqlite3.connect(app_db) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    two_a = tmp_path / "2a"
    archive = ArchiveFileStore(two_a / "bin", app_db)
    mapping_store = VersionedMappingObjectStore(two_a / "mapping-objects")
    stores = ClosureStores(archive=archive, mapping_objects=mapping_store)

    def put_blob(data, *, name="evidence.bin"):
        stored = archive.put_bytes(data, original_name=name)
        return stored.file_hash, stored.size_bytes

    # Freeze the green cut before registering R2 versions: the ledger
    # rows can cite the exact frozen 40-hex commit as their H root.
    evidence = freeze(_GREEN_BID)
    barrier = evidence.barrier_id
    commit = evidence.authority.commit_oid

    orig_digest, _ = put_blob(
        b"original-contract-v1-immutable-bytes", name="contract.txt")
    att_digest, _ = put_blob(
        b"attachment-scan-v1-immutable-bytes", name="scan.png")

    ledger_root = tmp_path / "mapping-version-index"
    mapping_versions = []  # (logical_source_id, digest, size, version label)
    with MappingVersionLedger(
            ledger_root,
            installation_id=_IDS["installation_id"],
            company_id=_IDS["company_id"],
            object_store=mapping_store) as ledger:
        for source, label, body in (
                ("src-contract-1", "v1", b"mapping-render-of-contract-v1"),
                ("src-contract-1", "v2", b"mapping-render-of-contract-v2"),
                ("src-contract-2", "v1", b"mapping-render-of-other-v1")):
            digest_hex = mapping_store.put(body)
            ledger.register_version(
                logical_source_id=source,
                digest=digest_hex,
                size_bytes=len(body),
                installation_id=_IDS["installation_id"],
                company_id=_IDS["company_id"],
                source_coordinate="harness/staging#%s=%s" % (source, label),
                h_commit_oid=commit,
            )
            mapping_versions.append((source, digest_hex, len(body), label))

    def green_references(bid=_GREEN_BID, barrier_id=None):
        barrier_id = barrier_id or barrier
        ref_specs = [
            AssemblyReference(
                logical_id="obj-orig-1",
                object_type=OBJECT_ORIGINAL,
                version="v1",
                scope_kind=SCOPE_COMPANY,
                citations=(
                    AssemblyCitation(
                        kind="H",
                        coordinate="facts:fact_current/obj-orig-1",
                    ),
                    AssemblyCitation(
                        kind="W",
                        coordinate="cuts/%s/%s/w_items.sqlite#row=1"
                                   % (bid, barrier_id),
                        root_id=barrier_id,
                    ),
                ),
                sha256_digest="sha256:" + orig_digest,
            ),
            AssemblyReference(
                logical_id="obj-att-1",
                object_type=OBJECT_ATTACHMENT,
                version="v1",
                scope_kind=SCOPE_INSTALLATION,
                citations=(
                    AssemblyCitation(
                        kind="W",
                        coordinate="cuts/%s/%s/w_ledger.sqlite#attachments"
                                   % (bid, barrier_id),
                        root_id=barrier_id,
                    ),
                ),
                sha256_digest="sha256:" + att_digest,
            ),
        ]
        for source, digest_hex, _size, label in mapping_versions:
            role = "w_items" if source == "src-contract-1" else "w_ledger"
            ref_specs.append(AssemblyReference(
                logical_id="map-%s-%s" % (source, label),
                object_type=OBJECT_MAPPING_VERSION,
                version=label,
                scope_kind=SCOPE_COMPANY,
                citations=(
                    AssemblyCitation(
                        kind="H",
                        coordinate="facts:fact_current/%s/%s" % (source, label),
                    ),
                    AssemblyCitation(
                        kind="W",
                        coordinate="cuts/%s/%s/%s.sqlite#mappings"
                                   % (bid, barrier_id, role),
                        root_id=barrier_id,
                    ),
                ),
                sha256_digest="sha256:" + digest_hex,
            ))
        return tuple(ref_specs)

    def make_plan(*, bid=_GREEN_BID, identity=_IDENTITY, repo_obj=None,
                  retention_root=None, closure_stores=None,
                  refs=None, version_root=ledger_root):
        return AssemblyPlan(
            identity=identity,
            repository=repo_obj or repository,
            retention_root=retention_root or retention,
            stores=closure_stores or stores,
            references=green_references(bid) if refs is None else tuple(refs),
            version_index_root=version_root,
        )

    yield {
        "tmp": tmp_path,
        "repo": repository,
        "retention": retention,
        "coordination": coordination,
        "stores": stores,
        "archive": archive,
        "mapping_store": mapping_store,
        "app_db": app_db,
        "two_a": two_a,
        "ledger_root": ledger_root,
        "freeze": freeze,
        "put_blob": put_blob,
        "green_bid": _GREEN_BID,
        "barrier": barrier,
        "commit": commit,
        "green_references": green_references,
        "make_plan": make_plan,
        "prepare_foreign": prepare_foreign,
    }

    broker_db.close()
    for _spec, connection in connections.values():
        connection.close()


def _foreign_spec(directory, role, company_id):
    """One work database for the scope-B repository."""
    source = directory / ("b-" + role + ".db")
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS work_state ("
        "id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, payload TEXT)")
    connection.execute(
        "INSERT OR IGNORE INTO work_state VALUES (1, ?, 'payload-b-1')",
        (company_id,))
    connection.commit()
    spec = WorkDatabaseSpec(
        role=role,
        connection=connection,
        scope_probe=lambda conn: (company_id,),
        schema={"tables": ["work_state"]},
    )
    return spec


# ---------------------------------------------------------------------------
# Small on-disk helpers
# ---------------------------------------------------------------------------


def _assembly_root(world_obj):
    return world_obj["retention"] / ".assembly"


def _package_dir(world_obj, bid, barrier):
    return (_assembly_root(world_obj) / "packages" / bid / barrier)


def _load_json(path):
    return loads_record(Path(path).read_bytes())


def _is_private_regular(path):
    result = path.stat()
    return (stat.S_ISREG(result.st_mode)
            and not path.is_symlink()
            and (result.st_mode & 0o777) == 0o600
            and result.st_uid == os.geteuid())


def _is_imported_bundle(path):
    # The bundle is produced verbatim by the existing export primitive:
    # git publishes it 0644. The contract refuses group/world WRITABLE
    # bits (0644 carries none); the 0700 parent chain denies any actual
    # read access, and assembly never chmods imported evidence.
    result = path.stat()
    return (stat.S_ISREG(result.st_mode)
            and not path.is_symlink()
            and not (result.st_mode & 0o022)
            and result.st_uid == os.geteuid())


def _is_private_dir(path):
    result = path.stat()
    return (stat.S_ISDIR(result.st_mode)
            and not path.is_symlink()
            and (result.st_mode & 0o777) == 0o700
            and result.st_uid == os.geteuid())


def _published_bundle_heads(bundle_path):
    """Test-side independent read: git bundle list-heads on the file."""
    result = subprocess.run(
        ["git", "bundle", "list-heads", os.fspath(bundle_path)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    heads = []
    for line in result.stdout.decode().splitlines():
        if line.strip():
            oid, ref = line.split(" ", 1)
            heads.append((oid, ref))
    return tuple(heads)


def _staging_bundles(world_obj):
    pattern = str(_assembly_root(world_obj) / "staging" / "*" / "authority.bundle")
    return [Path(name) for name in glob.glob(pattern)]


# ---------------------------------------------------------------------------
# 4.1 Green path: every family verifies, package + current atomically land
# ---------------------------------------------------------------------------


def test_green_path_publishes_package_and_switches_current_atomically(world):
    plan = world["make_plan"]()
    result = assemble_backup(plan, world["green_bid"])

    assert result.status == "published"
    assert result.recovered is False
    assert result.failures == ()
    assert result.outcome is None
    assert result.package == "packages/%s/%s" % (
        world["green_bid"], world["barrier"])
    assert result.current is not None
    assert result.current.backup_id == world["green_bid"]
    assert result.current.barrier_id == world["barrier"]
    assert result.current.identity == _IDENTITY

    # Reopening the pointer through the public reader reproduces it.
    reopened = read_current_backup(world["retention"], identity=_IDENTITY)
    assert reopened == result.current

    package = _package_dir(world, world["green_bid"], world["barrier"])
    bundle_path = package / "authority.bundle"
    manifest_path = package / "manifest.json"
    report_path = package / "report.json"
    assert _is_private_dir(package)
    assert _is_private_dir(package.parent)
    assert _is_imported_bundle(bundle_path)
    for path in (manifest_path, report_path):
        assert _is_private_regular(path)

    manifest = _load_json(manifest_path)
    report = _load_json(report_path)
    verify_final_manifest(manifest, report)
    assert manifest["verification"]["core_digest"] == result.core_digest
    assert manifest["verification"]["report_digest"] == result.report_digest

    core = manifest["core"]
    assert core["backup_id"] == world["green_bid"]
    assert core["barrier_id"] == world["barrier"]
    assert core["identity"] == {
        "repo_id": _IDS["repo_id"],
        "installation_id": _IDS["installation_id"],
        "company_id": _IDS["company_id"],
    }
    assert core["authority"]["commit_oid"] == world["commit"]
    assert core["authority"]["pin_ref"] == maintenance_pin_ref(
        world["green_bid"], world["barrier"])
    assert sorted(item["role"] for item in core["databases"]) == [
        "w_items", "w_ledger"]
    assert core["closure"]["status"] == "complete"
    assert core["closure"]["publishable"] is True
    assert core["closure"]["counts"]["present"] == 5
    assert core["closure"]["counts"]["missing"] == 0
    assert core["mapping_ledger"]["total_sources"] == 2
    assert core["mapping_ledger"]["total_versions"] == 3
    assert core["mapping_ledger"]["total_gaps"] == 0
    assert core["bundle"]["single_pin_ref"] is True
    assert core["bundle"]["advertised_refs"] == [
        maintenance_pin_ref(world["green_bid"], world["barrier"])]

    # The bytes inside the package hash to the manifest's declaration.
    bundle_bytes = bundle_path.read_bytes()
    assert ("sha256:" + hashlib.sha256(bundle_bytes).hexdigest()
            == core["bundle"]["sha256_digest"])
    assert core["bundle"]["size_bytes"] == len(bundle_bytes)

    # Independent heads proof: exactly one advertised ref, at frozen H.
    assert _published_bundle_heads(bundle_path) == ((
        world["commit"],
        maintenance_pin_ref(world["green_bid"], world["barrier"]),
    ),)

    # Every family check is present and green.
    for key in ("H.pin_readback", "H.bundle", "W.checkpoints",
                "R.closure", "R2.mapping_ledger"):
        assert report["checks"][key]["status"] == "ok"

    # No current temp debris, no staging leftover, no outcome recorded.
    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current == result.current
    assert len(state.packages) == 1
    assert state.packages[0].status == "complete"
    assert state.staging == ()
    assert state.outcomes == ()
    assert state.current_temp_debris == ()

    log_text = (_assembly_root(world) / "publish.log").read_text()
    assert '"event": "published"' in log_text


# ---------------------------------------------------------------------------
# 4.5 Idempotency: same backup_id refused; different backup_id coexists
# ---------------------------------------------------------------------------


def test_rerun_same_backup_id_is_refused_and_second_package_coexists(world):
    plan = world["make_plan"]()
    first = assemble_backup(plan, world["green_bid"])
    assert first.status == "published"
    package = _package_dir(world, world["green_bid"], world["barrier"])
    manifest_before = (package / "manifest.json").read_bytes()
    current_before = (_assembly_root(world) / "current").read_bytes()

    with pytest.raises(AlreadyPublishedError) as record:
        assemble_backup(plan, world["green_bid"])
    assert record.value.backup_id == world["green_bid"]
    assert record.value.coordinate == first.package

    # The first package and current are byte-untouched.
    assert (package / "manifest.json").read_bytes() == manifest_before
    assert (_assembly_root(world) / "current").read_bytes() == current_before

    # A second frozen cut for a different backup_id publishes beside the
    # first package without deleting or overwriting it.
    second_bid = "bkp-a1-next"
    second_evidence = world["freeze"](second_bid)
    plan_two = world["make_plan"](
        bid=second_bid,
        refs=world["green_references"](
            second_bid, second_evidence.barrier_id),
    )
    second = assemble_backup(plan_two, second_bid)
    assert second.status == "published"
    assert second.current.backup_id == second_bid

    first_package = _package_dir(world, world["green_bid"], world["barrier"])
    second_package = _package_dir(
        world, second_bid, second_evidence.barrier_id)
    assert first_package.is_dir() and second_package.is_dir()
    assert (first_package / "manifest.json").read_bytes() == manifest_before
    assert read_current_backup(
        world["retention"], identity=_IDENTITY).backup_id == second_bid

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert sorted(item.coordinate for item in state.packages) == sorted((
        first.package, second.package))


# ---------------------------------------------------------------------------
# 4.2 R incomplete: precise gap coordinates, outcome written, current held
# ---------------------------------------------------------------------------


def test_missing_r_object_blocks_with_precise_gap_and_keeps_current(world):
    first = assemble_backup(world["make_plan"](), world["green_bid"])
    assert first.status == "published"

    gap_bid = "bkp-a1-gap"
    evidence = world["freeze"](gap_bid)
    refs = list(world["green_references"](gap_bid, evidence.barrier_id))
    refs.append(AssemblyReference(
        logical_id="obj-vanished-1",
        object_type=OBJECT_ORIGINAL,
        version="v9",
        scope_kind=SCOPE_COMPANY,
        citations=(AssemblyCitation(
            kind="W",
            coordinate="cuts/%s/%s/w_items.sqlite#row=9"
                       % (gap_bid, evidence.barrier_id),
            root_id=evidence.barrier_id,
        ),),
        sha256_digest="sha256:" + ("a" * 64),  # never put into the archive
    ))
    plan = world["make_plan"](
        bid=gap_bid, refs=refs, version_root=None)
    result = assemble_backup(plan, gap_bid)

    assert result.status == "incomplete"
    assert result.package is None
    assert result.current == first.current
    assert result.outcome is not None
    assert {(f.family, f.check, f.reason, f.severity) for f in
            result.failures} == {(
        "R", "closure", "closure_not_publishable", "incomplete")}

    # No package position was claimed; an outcome leaf holds report+outcome.
    assert not (
        _assembly_root(world) / "packages" / gap_bid).exists()
    outcome_root = _assembly_root(world) / "outcomes" / gap_bid
    leaves = list(outcome_root.iterdir())
    assert len(leaves) == 1
    leaf = leaves[0]
    assert _is_private_regular(leaf / "report.json")
    assert _is_private_regular(leaf / "outcome.json")
    outcome_payload = _load_json(leaf / "outcome.json")
    assert outcome_payload["status"] == "incomplete"
    assert outcome_payload["backup_id"] == gap_bid
    assert outcome_payload["barrier_id"] == evidence.barrier_id
    assert outcome_payload["core_digest"] == result.core_digest

    report = _load_json(leaf / "report.json")
    closure_check = report["checks"]["R.closure"]
    assert closure_check["status"] == "incomplete"
    assert closure_check["publishable"] is False
    findings = closure_check["findings"]
    gap = [item for item in findings if item["logical_id"] == "obj-vanished-1"]
    assert len(gap) == 1
    assert gap[0]["entry_status"] == "missing"
    assert gap[0]["coordinates"][0]["coordinate"].endswith("#row=9")

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current == first.current
    assert len(state.outcomes) == 1
    assert state.outcomes[0].status == "incomplete"
    assert state.outcomes[0].backup_id == gap_bid


# ---------------------------------------------------------------------------
# 4.3 Damage: bundle bytes changed after export
# ---------------------------------------------------------------------------


def test_bundle_tamper_after_export_blocks_publication(world):
    plan = world["make_plan"]()

    def hook(phase):
        if phase == "bundle_exported":
            bundles = _staging_bundles(world)
            assert len(bundles) == 1
            with bundles[0].open("ab") as stream:
                stream.write(b"\x00tampered")

    result = assemble_backup(plan, world["green_bid"], crash_hook=hook)
    assert result.status == "failed"
    assert result.current is None
    assert result.package is None
    assert {(f.family, f.reason) for f in result.failures} == {(
        "H", "bundle_changed_after_export")}
    assert not (_assembly_root(world) / "packages").exists() or not any(
        (_assembly_root(world) / "packages").rglob("manifest.json"))


# ---------------------------------------------------------------------------
# 4.3 Damage: W checkpoint byte tamper
# ---------------------------------------------------------------------------


def test_w_checkpoint_byte_tamper_blocks_publication(world):
    cut_file = (world["retention"] / "cuts" / world["green_bid"]
                / world["barrier"] / "w_items.sqlite")
    with cut_file.open("ab") as stream:
        stream.write(b"extra-bytes-not-in-freeze-evidence")

    result = assemble_backup(world["make_plan"](), world["green_bid"])
    assert result.status == "failed"
    assert result.current is None
    assert result.package is None
    assert {(f.family, f.check, f.reason) for f in result.failures} == {(
        "W", "checkpoints", "component_verification_failed")}


# ---------------------------------------------------------------------------
# 4.3 Damage: R object sha mismatch (same-length byte swap) is corrupt
# ---------------------------------------------------------------------------


def test_r_object_byte_corruption_is_corrupt_and_blocks(world):
    ref = world["green_references"]()[0]
    digest_hex = ref.sha256_digest[len("sha256:"):]
    target = world["archive"].path_for(digest_hex)
    original = target.read_bytes()
    swapped = bytearray(original)
    swapped[0] = swapped[0] ^ 0xFF
    assert len(bytes(swapped)) == len(original)
    target.write_bytes(bytes(swapped))
    try:
        result = assemble_backup(world["make_plan"](), world["green_bid"])
    finally:
        target.write_bytes(original)

    assert result.status == "failed"
    assert result.current is None
    assert result.package is None
    assert any(
        f.family == "R" and f.reason == "closure_not_publishable"
        and f.severity == "failed"
        for f in result.failures)
    # The failed outcome report carries the corrupt coordinate.
    outcome_root = _assembly_root(world) / "outcomes" / world["green_bid"]
    leaf = next(outcome_root.iterdir())
    report = _load_json(leaf / "report.json")
    corrupt = [
        item for item in report["checks"]["R.closure"]["findings"]
        if item["logical_id"] == "obj-orig-1"]
    assert len(corrupt) == 1
    assert corrupt[0]["entry_status"] == "corrupt"


# ---------------------------------------------------------------------------
# 4.3/4.7 Scope: foreign identity refused, both for assembly and current
# ---------------------------------------------------------------------------


def test_foreign_scope_refused_before_any_package_and_on_current_read(world):
    # First publish the in-scope green backup.
    first = assemble_backup(world["make_plan"](), world["green_bid"])
    assert first.status == "published"

    foreign = world["prepare_foreign"]()
    foreign_bid = "bkp-a1-foreign"
    # The foreign-scope cut is physically frozen INTO the in-scope
    # retention tree, so every later refusal is an identity decision,
    # never a missing-cut accident.
    world["freeze"](
        foreign_bid,
        roles=("w_items",),
        identity=_IDENTITY_B,
        repo=foreign["repo"],
        coordination_obj=foreign["coordination"],
        retention_root=world["retention"],
    )

    # Gate A: the plan identity disagrees with the frozen record itself.
    # Refused at validation time, before the cut or layout is touched.
    wrong_identity_plan = AssemblyPlan(
        identity=_IDENTITY,
        repository=world["repo"],
        retention_root=world["retention"],
        stores=world["stores"],
        references=(),
        version_index_root=None,
    )
    with pytest.raises(AssemblyScopeMismatchError):
        assemble_backup(wrong_identity_plan, foreign_bid)
    assert not (
        _assembly_root(world) / "packages" / foreign_bid).exists()

    # Gate B: identities agree and every family verifies (empty R closure
    # is complete), but the in-scope current belongs to another scope.
    # The refusal must land in _publish_green before ANY byte moves, i.e.
    # before packages/<bid> is even created.
    foreign_plan = AssemblyPlan(
        identity=_IDENTITY_B,
        repository=foreign["repo"],
        retention_root=world["retention"],
        stores=world["stores"],
        references=(),
        version_index_root=None,
    )
    with pytest.raises(AssemblyScopeMismatchError):
        assemble_backup(foreign_plan, foreign_bid)
    assert not (
        _assembly_root(world) / "packages" / foreign_bid).exists()
    assert read_current_backup(
        world["retention"], identity=_IDENTITY) == first.current

    # Reading the existing pointer through a foreign identity is refused.
    with pytest.raises(AssemblyScopeMismatchError):
        read_current_backup(world["retention"], identity=_IDENTITY_B)
    # ... while the identity-less reader still sees the raw pointer.
    assert read_current_backup(world["retention"]) == first.current

    # A fresh foreign-scope cut in B's own retention tree assembles fine,
    # proving the earlier refusal was scope, not evidence quality.
    own_bid = "bkp-a1-foreign-own"
    own_evidence = world["freeze"](
        own_bid,
        roles=("w_items",),
        identity=_IDENTITY_B,
        repo=foreign["repo"],
        coordination_obj=foreign["coordination"],
        retention_root=foreign["retention"],
    )
    foreign_plan_own = AssemblyPlan(
        identity=_IDENTITY_B,
        repository=foreign["repo"],
        retention_root=foreign["retention"],
        stores=world["stores"],
        references=(),
        version_index_root=None,
    )
    foreign_result = assemble_backup(foreign_plan_own, own_bid)
    assert foreign_result.status == "published"
    assert foreign_result.barrier_id == own_evidence.barrier_id
    assert read_current_backup(
        foreign["retention"], identity=_IDENTITY_B).backup_id == own_bid


# ---------------------------------------------------------------------------
# 4.4 Atomic crash: after bundle export, before manifest staging
# ---------------------------------------------------------------------------


def test_crash_after_bundle_export_leaves_partial_staging_and_rerun_publishes(
        world):
    calls = {"n": 0}

    def hook(phase):
        if phase == "bundle_exported":
            calls["n"] += 1
            raise _InjectedCrash(phase)

    with pytest.raises(_InjectedCrash):
        assemble_backup(world["make_plan"](), world["green_bid"],
                        crash_hook=hook)
    assert calls["n"] == 1

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current is None
    assert state.packages == ()
    assert len(state.staging) == 1
    assert state.staging[0].status == "partial"
    assert "missing:manifest.json" in state.staging[0].reasons
    bundles = _staging_bundles(world)
    assert len(bundles) == 1 and bundles[0].is_file()

    # Rerun converges; the first partial staging stays as recognizable
    # debris (never deleted, never repaired).
    result = assemble_backup(world["make_plan"](), world["green_bid"])
    assert result.status == "published"
    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current.backup_id == world["green_bid"]
    partial = [item for item in state.staging if item.status == "partial"]
    assert len(partial) == 1


# ---------------------------------------------------------------------------
# 4.4 Atomic crash: after manifest staged, before the directory rename
# ---------------------------------------------------------------------------


def test_crash_after_manifest_staged_leaves_verified_staging_and_rerun_publishes(
        world):
    def hook(phase):
        if phase == "manifest_staged":
            raise _InjectedCrash(phase)

    with pytest.raises(_InjectedCrash):
        assemble_backup(world["make_plan"](), world["green_bid"],
                        crash_hook=hook)

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current is None
    assert state.packages == ()
    assert len(state.staging) == 1
    assert state.staging[0].status == "verified"

    result = assemble_backup(world["make_plan"](), world["green_bid"])
    assert result.status == "published"
    assert result.recovered is False
    # The verified staging leaf is not what current points at: current
    # names the packages/ coordinate.
    assert result.current.package.startswith("packages/")


# ---------------------------------------------------------------------------
# 4.4 Atomic crash: package published, current not switched yet
# ---------------------------------------------------------------------------


def test_crash_between_package_and_current_forward_recovers_current(world):
    def hook(phase):
        if phase == "package_published":
            raise _InjectedCrash(phase)

    with pytest.raises(_InjectedCrash):
        assemble_backup(world["make_plan"](), world["green_bid"],
                        crash_hook=hook)

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.current is None
    assert len(state.packages) == 1
    assert state.packages[0].status == "complete"

    # Rerun re-verifies every family, recognizes the byte-identical
    # package position and only finishes the current switch.
    result = assemble_backup(world["make_plan"](), world["green_bid"])
    assert result.status == "published"
    assert result.recovered is True
    assert result.current.backup_id == world["green_bid"]
    assert read_current_backup(
        world["retention"], identity=_IDENTITY) == result.current

    package_root = _assembly_root(world) / "packages" / world["green_bid"]
    assert len(list(package_root.iterdir())) == 1  # one barrier leaf only


# ---------------------------------------------------------------------------
# 4.4 Atomic crash: after current switched (idempotent recognition)
# ---------------------------------------------------------------------------


def test_crash_after_current_switched_is_recognized_as_already_published(world):
    def hook(phase):
        if phase == "current_switched":
            raise _InjectedCrash(phase)

    with pytest.raises(_InjectedCrash):
        assemble_backup(world["make_plan"](), world["green_bid"],
                        crash_hook=hook)

    # current is already durably in place.
    pointer = read_current_backup(world["retention"], identity=_IDENTITY)
    assert pointer is not None and pointer.backup_id == world["green_bid"]

    with pytest.raises(AlreadyPublishedError):
        assemble_backup(world["make_plan"](), world["green_bid"])
    assert read_current_backup(
        world["retention"], identity=_IDENTITY) == pointer


# ---------------------------------------------------------------------------
# 4.5 A damaged package at the position is a conflict, never overwritten
# ---------------------------------------------------------------------------


def test_damaged_package_position_is_conflict_and_never_overwritten(world):
    first = assemble_backup(world["make_plan"](), world["green_bid"])
    assert first.status == "published"
    package = _package_dir(world, world["green_bid"], world["barrier"])
    manifest_path = package / "manifest.json"
    tampered = manifest_path.read_bytes() + b"\x00damage"
    manifest_path.write_bytes(tampered)

    state = scan_assembly_state(world["retention"], identity=_IDENTITY)
    assert state.packages[0].status == "damaged"

    with pytest.raises(AssemblyPackageConflictError) as record:
        assemble_backup(world["make_plan"](), world["green_bid"])
    assert record.value.reason == "published_package_conflict"

    # The damaged bytes were not repaired or replaced.
    assert manifest_path.read_bytes() == tampered
    assert read_current_backup(
        world["retention"], identity=_IDENTITY) == first.current


# ---------------------------------------------------------------------------
# 4.6 Reinstall in two locations: identical digests, no secrets/paths
# ---------------------------------------------------------------------------


def test_two_location_reinstall_reproduces_digests_without_secrets(world):
    tmp = world["tmp"]

    # Second, independent installation: same cut journal, same stores,
    # same ledger, same (read-only) authority repository.
    retention_two = tmp / "retention-two"
    shutil.copytree(world["retention"], retention_two)
    two_a_two = tmp / "2a-two"
    shutil.copytree(world["two_a"], two_a_two)
    app_db_two = tmp / "app-two.db"
    shutil.copyfile(world["app_db"], app_db_two)
    ledger_two = tmp / "ledger-two"
    shutil.copytree(world["ledger_root"], ledger_two)

    archive_two = ArchiveFileStore(two_a_two / "bin", app_db_two)
    mapping_two = VersionedMappingObjectStore(two_a_two / "mapping-objects")
    stores_two = ClosureStores(
        archive=archive_two, mapping_objects=mapping_two)

    result_one = assemble_backup(world["make_plan"](), world["green_bid"])
    plan_two = world["make_plan"](
        retention_root=retention_two,
        closure_stores=stores_two,
        version_root=ledger_two,
    )
    result_two = assemble_backup(plan_two, world["green_bid"])

    assert result_one.status == result_two.status == "published"
    assert result_one.core_digest == result_two.core_digest
    assert result_one.report_digest == result_two.report_digest

    # Every digest-bound artifact on disk is free of absolute roots and
    # secret-shaped vocabulary.
    forbidden = [
        str(world["retention"]).encode(),
        str(retention_two).encode(),
        str(tmp).encode(),
        b"secret",
        b"password",
        b"token",
    ]
    roots = (
        _package_dir(world, world["green_bid"], world["barrier"]),
        retention_two / ".assembly" / "packages" / world["green_bid"]
        / world["barrier"],
    )
    for package_root in roots:
        for name in ("manifest.json", "report.json"):
            blob = (package_root / name).read_bytes().lower()
            for needle in forbidden:
                assert needle.lower() not in blob, (name, needle)
    for retention_root in (world["retention"], retention_two):
        current_blob = (
            retention_root / ".assembly" / "current").read_bytes().lower()
        for needle in forbidden:
            assert needle.lower() not in current_blob


# ---------------------------------------------------------------------------
# Frozen-state gate: preparing/failed/incomplete/absent are all refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_state", ["preparing", "failed", "incomplete"])
def test_non_frozen_and_absent_records_are_refused(world, bad_state):
    # The fixture's green freeze already seeded the real host journal.
    journal = world["retention"] / ".coordinator" / "freezes.sqlite"
    connection = sqlite3.connect(journal)
    try:
        with connection:
            connection.execute(
                "INSERT INTO freeze_journal (backup_id, state, barrier_id, "
                "created_at, updated_at, detail) VALUES "
                "('bkp-a1-%s', ?, 'b20260101T000000-dead', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'{\"reason\": \"%s\"}')" % (bad_state, bad_state),
                (bad_state,),
            )
    finally:
        connection.close()

    with pytest.raises(CutNotFrozenError) as record:
        assemble_backup(
            world["make_plan"](bid="bkp-a1-" + bad_state, refs=()),
            "bkp-a1-" + bad_state,
        )
    assert record.value.state == bad_state

    with pytest.raises(CutNotFrozenError) as record:
        assemble_backup(world["make_plan"](bid="bkp-a1-absent", refs=()),
                        "bkp-a1-absent")
    assert record.value.state is None


# ---------------------------------------------------------------------------
# Known closure seam: sha256 cuts fail closed on the 40-hex H contract
# ---------------------------------------------------------------------------


def test_sha256_cut_fails_closed_on_40_hex_closure_contract(world):
    repository = FactsGitRepository.initialize(
        world["tmp"] / "facts-256.git", object_format="sha256", **_IDS)
    broker_db = sqlite3.connect(":memory:")
    coordination = BrokerCoordination(
        world["tmp"] / "domain-256", _IDS["repo_id"], broker_db)

    # One real WAL work database carrying the in-scope company id.
    connection = sqlite3.connect(world["tmp"] / "w256.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE work_state ("
        "id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, payload TEXT)")
    connection.execute(
        "INSERT INTO work_state VALUES (1, ?, 'payload-256-1')",
        (_IDS["company_id"],))
    connection.commit()
    spec = WorkDatabaseSpec(
        role="w_items",
        connection=connection,
        scope_probe=lambda conn: (_IDS["company_id"],),
        schema={"tables": ["work_state"]},
        scope_watermark={"max_id": 1},
    )
    evidence = freeze_consistent_cut(FreezeAssembly(
        backup_id="bkp-a1-sha256",
        identity=_IDENTITY,
        coordination=coordination,
        repository=repository,
        databases=(spec,),
        retention_root=world["retention"],
        budget=BarrierBudget(5.0, 30.0, 0.01),
    ))
    assert evidence.frozen
    plan = AssemblyPlan(
        identity=_IDENTITY,
        repository=repository,
        retention_root=world["retention"],
        stores=world["stores"],
        references=(),
        version_index_root=None,
    )
    result = assemble_backup(plan, "bkp-a1-sha256")
    assert result.status == "failed"
    assert result.current is None
    assert any(
        f.family == "R"
        and f.reason == "closure_unsupported_object_format"
        for f in result.failures)
    broker_db.close()
    connection.close()


# ---------------------------------------------------------------------------
# 4.9 All sockets sealed: real local bundle verify/fetch still completes
# ---------------------------------------------------------------------------


def test_assembly_succeeds_with_all_sockets_disabled(world, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("backup assembly must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    result = assemble_backup(world["make_plan"](), world["green_bid"])
    assert result.status == "published"


# ---------------------------------------------------------------------------
# A7 zero wiring: production import graph cannot reach this module, and
# the module itself imports only offline, model-free dependencies.
# ---------------------------------------------------------------------------


def test_no_wiring_and_offline_import_graph():
    project_root = Path(assembly.__file__).resolve().parents[2]
    app_root = project_root / "app"

    offenders = []
    for path in app_root.rglob("*.py"):
        if path.name == "backup_assembly.py":
            continue
        text = path.read_text()
        if "backup_assembly" in text:
            offenders.append(path)
    assert offenders == []

    source = Path(assembly.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    allowed = {
        "__future__",
        "hashlib", "os", "re", "stat", "subprocess", "dataclasses",
        "pathlib",
        "app.facts_tree",
        "app.facts_tree.git_format",
        "app.facts_tree.backup_component_validation",
        "app.facts_tree.backup_coordinator",
        "app.facts_tree.backup_manifest",
        "app.facts_tree.backup_object_closure",
        "app.facts_tree.git_repository",
        "app.facts_tree.mapping_version_index",
    }
    assert imported <= allowed, imported - allowed

    for forbidden in ("socket", "urllib", "requests", "httpx",
                      "http.client", "ssl", "smtplib", "openai",
                      "anthropic", "paramiko"):
        assert forbidden not in imported
    assert "__import__" not in source

    # The single subprocess use runs only the local, offline
    # `git bundle list-heads` against a bundle file: no remote, no clone.
    run_calls = [
        node for node in ast.walk(tree)
        if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run")
    ]
    assert len(run_calls) == 1
    argv = run_calls[0].args[0]
    assert isinstance(argv, ast.List)
    head = [element.value for element in argv.elts[:3]]
    assert head == ["git", "bundle", "list-heads"]

    # Crash hook phases are the documented seam and nothing else.
    assert CRASH_PHASES == frozenset((
        "bundle_exported", "manifest_staged",
        "package_published", "current_switched"))
