"""P2 §5.3 execution preflight gate; synthetic acc-* data, tmp SQLite only.

The preflight verifies an approved plan against the immutable target
manifest archive and the live snapshot; it never executes a migration,
never writes executor states or receipts and never touches business tables.
Its single permitted write is the existing expired audit event, proven here
to be undoable by a caller rollback.
"""
import hashlib
import json
import sqlite3

import pytest

from app.db.schema import TEMPLATE_MANIFEST_SCHEMA
from app.facts_tree.migration_host import (
    MigrationApprovalCoordinator, MigrationHostUnavailable)
from app.facts_tree.migration_plan_builder import normalize_input_snapshot
from app.facts_tree.migration_plans import (
    SNAPSHOT_CATEGORIES, MigrationPlanStateLedger)


def coverage(**overrides):
    result = {category: {"status": "ok"} for category in SNAPSHOT_CATEGORIES}
    result.update(overrides)
    return result


def archive_manifest(db, pack_id, pack_version, *, tamper=False):
    document = {"pack_id": pack_id, "pack_version": pack_version,
                "trunks": [], "leaves": []}
    text = json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stored_text = text + " " if tamper else text
    db.execute(
        "INSERT INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
        (pack_id, pack_version, digest, stored_text, "published", "宿主",
         "synthetic", "2026-10-17T00:00:00+00:00"))
    return digest


def make_db(tmp_path):
    db = sqlite3.connect(tmp_path / "migration-preflight.db")
    ledger = MigrationPlanStateLedger(db)
    db.executescript(TEMPLATE_MANIFEST_SCHEMA)
    source_digest = archive_manifest(db, "generic", 1)
    target_digest = archive_manifest(db, "generic-v2", 1)
    db.commit()
    return db, ledger, source_digest, target_digest


def input_snapshot(*, snapshot=None, coverage_doc=None, company_id="acc-a"):
    snapshot = snapshot or {category: [] for category in SNAPSHOT_CATEGORIES}
    return normalize_input_snapshot(
        snapshot, coverage_doc or coverage(), company_id)


def plan(source_digest, target_digest, *, plan_id="plan:acc-a:exec-1",
         company_id="acc-a", snapshot=None, coverage_doc=None,
         target_version=1, target_digest_override=None):
    normalized = input_snapshot(
        snapshot=snapshot, coverage_doc=coverage_doc, company_id=company_id)
    return {
        "plan_id": plan_id,
        "company_id": company_id,
        "created_by": "preflight-test",
        "created_at": "2026-10-17T00:00:00+00:00",
        "source": {"pack_id": "generic", "pack_version": 1,
                   "manifest_sha256": source_digest},
        "target": {"pack_id": "generic-v2", "pack_version": target_version,
                   "manifest_sha256": target_digest_override or target_digest},
        "binding_revision": 1,
        "input_snapshot": normalized["input_snapshot"],
        "snapshot_fingerprint": normalized["snapshot_fingerprint"],
    }


class FakeSnapshotReader:
    def __init__(self, db, result):
        self.db = db
        self.result = result

    def read_in_transaction(self, *, company_id):
        assert self.db.in_transaction
        assert company_id == self.result["company_id"]
        return self.result


def live(source_digest, *, snapshot=None, coverage_doc=None, company_id="acc-a"):
    return {
        "company_id": company_id,
        "binding": {"pack_id": "generic", "pack_version": 1,
                    "manifest_sha256": source_digest, "binding_revision": 1},
        "snapshot": snapshot or {category: [] for category in SNAPSHOT_CATEGORIES},
        "coverage": coverage_doc or coverage(),
    }


@pytest.fixture
def setup(tmp_path):
    db, ledger, source_digest, target_digest = make_db(tmp_path)
    yield db, ledger, source_digest, target_digest
    db.close()


def make_coordinator(db, current, *, absence_verifier=None, company_id="acc-a"):
    return MigrationApprovalCoordinator(
        db, company_id=company_id,
        plan_validator=lambda _document, _snapshot: True,
        approval_verifier=lambda _document, _evidence: True,
        absence_verifier=absence_verifier,
        snapshot_reader=FakeSnapshotReader(db, current))


def approve(db, ledger, document):
    with db:
        ledger.register_plan(document)
        ledger.transition(document["plan_id"], "validated", "校验员")
        ledger.transition(document["plan_id"], "awaiting_user_approval", "宿主")
        ledger.decide(document["plan_id"], "approved", "用户",
                      "approval:1", "c" * 64, lambda _doc, _evidence: True)


def dump(db):
    return "\n".join(db.iterdump())


def non_event_row_counts(db):
    counts = {}
    for table in ("tree_migration_plans", "template_manifests"):
        counts[table] = db.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def test_success_returns_auditable_identity_without_any_writes(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    approve(db, ledger, document)
    coordinator = make_coordinator(db, live(source_digest))
    before = dump(db)

    result = coordinator.preflight_execution(document["plan_id"])

    assert result["preflight"] == "passed"
    assert result["state"] == "approved"
    assert result["plan_id"] == document["plan_id"]
    assert result["company_id"] == "acc-a"
    assert result["plan_sha256"] == ledger.status(document["plan_id"])["plan_sha256"]
    assert result["source"] == document["source"]
    assert result["target"] == document["target"]
    assert result["binding_revision"] == 1
    assert result["snapshot_fingerprint"] == document["snapshot_fingerprint"]
    assert result["fresh"] is True and result["changed_inputs"] == []
    assert not db.in_transaction
    assert dump(db) == before


def test_non_approved_unknown_and_cross_company_plans_rejected(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    with db:
        ledger.register_plan(document)
        ledger.transition(document["plan_id"], "validated", "校验员")
    coordinator = make_coordinator(db, live(source_digest))
    before = dump(db)

    with pytest.raises(ValueError, match="requires an approved plan"):
        coordinator.preflight_execution(document["plan_id"])
    with pytest.raises(ValueError, match="unknown migration plan"):
        coordinator.preflight_execution("plan:acc-a:missing")
    other = make_coordinator(db, live(source_digest), company_id="acc-b")
    with pytest.raises(PermissionError, match="outside the host company scope"):
        other.preflight_execution(document["plan_id"])

    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "validated"
    assert not db.in_transaction


def test_target_manifest_mismatch_fails_closed_without_writes(setup):
    db, ledger, source_digest, target_digest = setup
    coordinator = make_coordinator(db, live(source_digest))

    wrong_digest = plan(source_digest, target_digest,
                        plan_id="plan:acc-a:exec-2",
                        target_digest_override="d" * 64)
    approve(db, ledger, wrong_digest)
    with pytest.raises(ValueError, match="digest differs from the plan target"):
        coordinator.preflight_execution(wrong_digest["plan_id"])

    wrong_version = plan(source_digest, target_digest,
                         plan_id="plan:acc-a:exec-3", target_version=2)
    approve(db, ledger, wrong_version)
    with pytest.raises(ValueError, match="no immutable archive"):
        coordinator.preflight_execution(wrong_version["plan_id"])

    assert ledger.status(wrong_digest["plan_id"])["state"] == "approved"
    assert ledger.status(wrong_version["plan_id"])["state"] == "approved"
    assert not db.in_transaction


def test_tampered_or_absent_target_archive_fails_closed(tmp_path):
    db, ledger, source_digest, _ = make_db(tmp_path)
    tampered = archive_manifest(db, "generic-v3", 1, tamper=True)
    db.commit()
    document = plan(source_digest, tampered)
    document["target"] = {"pack_id": "generic-v3", "pack_version": 1,
                          "manifest_sha256": tampered}
    approve(db, ledger, document)
    coordinator = make_coordinator(db, live(source_digest))
    before = dump(db)

    with pytest.raises(ValueError, match="failed hash verification"):
        coordinator.preflight_execution(document["plan_id"])
    assert dump(db) == before
    db.close()

    bare = sqlite3.connect(tmp_path / "no-archive.db")
    bare_ledger = MigrationPlanStateLedger(bare)
    bare_document = plan("a" * 64, "b" * 64)
    approve(bare, bare_ledger, bare_document)
    bare_coordinator = MigrationApprovalCoordinator(
        bare, company_id="acc-a",
        plan_validator=lambda _document, _snapshot: True,
        approval_verifier=lambda _document, _evidence: True,
        snapshot_reader=FakeSnapshotReader(bare, live("a" * 64)))
    with pytest.raises(ValueError, match="archive table is absent"):
        bare_coordinator.preflight_execution(bare_document["plan_id"])
    bare.close()


def test_changed_snapshot_expires_plan_and_only_appends_audit_event(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    approve(db, ledger, document)
    current_snapshot = {category: [] for category in SNAPSHOT_CATEGORIES}
    current_snapshot["facts"] = [{"fact_row_id": 5, "company_id": "acc-a"}]
    current_snapshot["open_todos"] = [{"kind": "todo", "todo_id": 8}]
    coordinator = make_coordinator(
        db, live(source_digest, snapshot=current_snapshot))
    counts_before = non_event_row_counts(db)

    result = coordinator.preflight_execution(document["plan_id"])

    assert result["state"] == "expired"
    assert result["fresh"] is False
    assert "facts" in result["changed_inputs"]
    assert "open_todos" in result["changed_inputs"]
    events = ledger.history(document["plan_id"])
    assert [row["event_type"] for row in events] == [
        "draft", "validated", "awaiting_user_approval", "approved", "expired"]
    assert events[-1]["actor"] == "migration-execution-preflight"
    detail = json.loads(events[-1]["detail_json"])
    assert "facts" in detail["changed_inputs"]
    assert non_event_row_counts(db) == counts_before
    assert not db.in_transaction


def test_confirmed_absent_reverified_inside_transaction(setup):
    db, ledger, source_digest, target_digest = setup
    proof = {"statement": "无诊断引用", "confirmed_by": "宿主审计",
             "confirmed_at": "2026-10-17T08:00:00+08:00", "method": "精确只读查询"}
    coverage_doc = coverage(diagnostic_refs={"status": "confirmed_absent",
                                             "proof": proof})
    document = plan(source_digest, target_digest, coverage_doc=coverage_doc)
    approve(db, ledger, document)
    current = live(source_digest, coverage_doc=coverage(
        diagnostic_refs={"status": "not_implemented", "notes": ["当前表未实现"]}))

    coordinator = make_coordinator(db, current)
    before = dump(db)
    with pytest.raises(MigrationHostUnavailable, match="absence verifier"):
        coordinator.preflight_execution(document["plan_id"])
    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "approved"

    coordinator = make_coordinator(db, current, absence_verifier=lambda *_args: False)
    result = coordinator.preflight_execution(document["plan_id"])
    assert result["state"] == "expired"
    assert "diagnostic_refs:absence_proof_invalid" in result["changed_inputs"]

    seen = []

    def verifier(company, category, current_proof):
        seen.append((company, category, current_proof, db.in_transaction))
        return True

    fresh_document = plan(source_digest, target_digest,
                          plan_id="plan:acc-a:exec-4", coverage_doc=coverage_doc)
    approve(db, ledger, fresh_document)
    coordinator = make_coordinator(db, current, absence_verifier=verifier)
    result = coordinator.preflight_execution(fresh_document["plan_id"])
    assert result["preflight"] == "passed"
    assert seen == [("acc-a", "diagnostic_refs", proof, True)]


def test_in_transaction_version_preserves_caller_transaction(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    pending = plan(source_digest, target_digest, plan_id="plan:acc-a:exec-5")
    approve(db, ledger, document)
    with db:
        ledger.register_plan(pending)
        ledger.transition(pending["plan_id"], "validated", "校验员")
    coordinator = make_coordinator(db, live(source_digest))
    before = dump(db)

    with pytest.raises(ValueError, match="caller-owned transaction"):
        coordinator.preflight_execution_in_transaction(document["plan_id"])
    assert not db.in_transaction

    db.execute("BEGIN")
    result = coordinator.preflight_execution_in_transaction(document["plan_id"])
    assert result["preflight"] == "passed"
    assert db.in_transaction
    db.rollback()
    assert dump(db) == before

    db.execute("BEGIN")
    with pytest.raises(ValueError, match="requires an approved plan"):
        coordinator.preflight_execution_in_transaction(pending["plan_id"])
    assert db.in_transaction
    db.rollback()
    assert dump(db) == before


def test_caller_rollback_undoes_the_expired_audit_event(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    approve(db, ledger, document)
    current_snapshot = {category: [] for category in SNAPSHOT_CATEGORIES}
    current_snapshot["facts"] = [{"fact_row_id": 5, "company_id": "acc-a"}]
    coordinator = make_coordinator(
        db, live(source_digest, snapshot=current_snapshot))
    before = dump(db)

    db.execute("BEGIN IMMEDIATE")
    result = coordinator.preflight_execution_in_transaction(document["plan_id"])
    assert result["state"] == "expired"
    assert db.in_transaction
    db.rollback()

    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "approved"


def test_wrapper_refuses_busy_connection_and_never_commits_it(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    approve(db, ledger, document)
    coordinator = make_coordinator(db, live(source_digest))
    before = dump(db)

    db.execute("BEGIN")
    with pytest.raises(ValueError, match="idle connection"):
        coordinator.preflight_execution(document["plan_id"])
    db.rollback()

    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "approved"


def test_bool_target_pack_version_fails_closed(setup):
    # bool is an int subclass; an approved plan carrying True where a positive
    # integer version is required must be rejected exactly like the plan
    # builder's strict type(value) is int rule, with zero writes.
    db, ledger, source_digest, target_digest = setup
    coordinator = make_coordinator(db, live(source_digest))
    document = plan(source_digest, target_digest, plan_id="plan:acc-a:exec-6")
    document["target"] = {"pack_id": "generic-v2", "pack_version": True,
                          "manifest_sha256": target_digest}
    approve(db, ledger, document)
    before = dump(db)

    with pytest.raises(ValueError, match="pack_version must be a positive integer"):
        coordinator.preflight_execution(document["plan_id"])

    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "approved"
    assert not db.in_transaction


def test_bool_binding_revision_fails_closed(setup):
    # Same strict-int rule for the plan's binding revision: True is rejected.
    db, ledger, source_digest, target_digest = setup
    coordinator = make_coordinator(db, live(source_digest))
    document = plan(source_digest, target_digest, plan_id="plan:acc-a:exec-7")
    document["binding_revision"] = True
    approve(db, ledger, document)
    before = dump(db)

    with pytest.raises(ValueError,
                       match="binding_revision must be a positive integer"):
        coordinator.preflight_execution(document["plan_id"])

    assert dump(db) == before
    assert ledger.status(document["plan_id"])["state"] == "approved"
    assert not db.in_transaction


def test_repeated_preflight_has_no_side_effects_and_no_executor_states(setup):
    db, ledger, source_digest, target_digest = setup
    document = plan(source_digest, target_digest)
    approve(db, ledger, document)
    coordinator = make_coordinator(db, live(source_digest))

    first = coordinator.preflight_execution(document["plan_id"])
    second = coordinator.preflight_execution(document["plan_id"])

    assert first == second
    history = [row["event_type"] for row in ledger.history(document["plan_id"])]
    assert history == ["draft", "validated", "awaiting_user_approval", "approved"]
    assert not {"executing", "applied", "failed", "rolled_back"} & set(history)
    assert not db.in_transaction
