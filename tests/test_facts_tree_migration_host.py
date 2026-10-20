"""Host approval/freshness integration; no network or real company data."""
import sqlite3

import pytest

from app.facts_tree.migration_host import (
    MigrationApprovalCoordinator, MigrationHostUnavailable)
from app.facts_tree.migration_plan_builder import normalize_input_snapshot
from app.facts_tree.migration_plans import (
    SNAPSHOT_CATEGORIES, MigrationPlanStateLedger)


def coverage(**overrides):
    result = {category: {"status": "ok"} for category in SNAPSHOT_CATEGORIES}
    result.update(overrides)
    return result


def input_snapshot(*, snapshot=None, coverage_doc=None, company_id="acc-a"):
    snapshot = snapshot or {category: [] for category in SNAPSHOT_CATEGORIES}
    return normalize_input_snapshot(
        snapshot, coverage_doc or coverage(), company_id)


def plan(*, plan_id="plan:acc-a:host-1", company_id="acc-a",
         snapshot=None, coverage_doc=None):
    normalized = input_snapshot(
        snapshot=snapshot, coverage_doc=coverage_doc, company_id=company_id)
    return {
        "plan_id": plan_id,
        "company_id": company_id,
        "created_by": "host-test",
        "created_at": "2026-10-17T00:00:00+00:00",
        "source": {"pack_id": "generic", "pack_version": 1,
                   "manifest_sha256": "a" * 64},
        "target": {"pack_id": "generic-v2", "pack_version": 1,
                   "manifest_sha256": "b" * 64},
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


def live(*, snapshot=None, coverage_doc=None, company_id="acc-a"):
    return {
        "company_id": company_id,
        "binding": {"pack_id": "generic", "pack_version": 1,
                    "manifest_sha256": "a" * 64, "binding_revision": 1},
        "snapshot": snapshot or {category: [] for category in SNAPSHOT_CATEGORIES},
        "coverage": coverage_doc or coverage(),
    }


@pytest.fixture
def setup(tmp_path):
    db = sqlite3.connect(tmp_path / "migration-host.db")
    ledger = MigrationPlanStateLedger(db)
    yield db, ledger
    db.close()


def make_coordinator(db, current, *, verifier=None, absence_verifier=None,
                     company_id="acc-a"):
    return MigrationApprovalCoordinator(
        db, company_id=company_id,
        plan_validator=lambda _document, _snapshot: True,
        approval_verifier=verifier or (lambda _document, _evidence: True),
        absence_verifier=absence_verifier,
        snapshot_reader=FakeSnapshotReader(db, current))


def register_validated(db, ledger, document):
    with db:
        ledger.register_plan(document)
        ledger.transition(document["plan_id"], "validated", "校验员")


def test_request_approval_rechecks_snapshot_inside_write_transaction(setup):
    db, ledger = setup
    document = plan()
    register_validated(db, ledger, document)
    coordinator = make_coordinator(db, live())

    result = coordinator.request_approval(document["plan_id"], actor="宿主")

    assert result["state"] == "awaiting_user_approval"
    assert result["fresh"] is True
    assert not db.in_transaction
    assert [row["event_type"] for row in ledger.history(document["plan_id"])] == [
        "draft", "validated", "awaiting_user_approval"]


def test_changed_snapshot_expires_validated_plan_without_queuing_approval(setup):
    db, ledger = setup
    document = plan()
    register_validated(db, ledger, document)
    current_snapshot = {category: [] for category in SNAPSHOT_CATEGORIES}
    current_snapshot["facts"] = [{"fact_row_id": 5, "company_id": "acc-a"}]
    coordinator = make_coordinator(db, live(snapshot=current_snapshot))

    result = coordinator.request_approval(document["plan_id"], actor="宿主")

    assert result["state"] == "expired"
    assert result["fresh"] is False
    assert "facts" in result["changed_inputs"]
    assert "awaiting_user_approval" not in [
        event["event_type"] for event in ledger.history(document["plan_id"])]
    assert not db.in_transaction


def test_fresh_user_approval_is_bound_to_plan_and_committed_atomically(setup):
    db, ledger = setup
    document = plan()
    register_validated(db, ledger, document)
    with db:
        ledger.transition(document["plan_id"], "awaiting_user_approval", "宿主")
    seen = []

    def verifier(plan_doc, evidence):
        seen.append((plan_doc, evidence, db.in_transaction))
        return (evidence["company_id"] == "acc-a"
                and evidence["plan_id"] == document["plan_id"]
                and evidence["plan_sha256"]
                and evidence["decision"] == "approved"
                and evidence["target"] == document["target"])

    coordinator = make_coordinator(db, live(), verifier=verifier)
    result = coordinator.decide(
        document["plan_id"], "approved", actor="coze-user-7",
        approval_ref="coze-approval:42", approval_doc_sha256="f" * 64)

    assert result["state"] == "approved"
    assert result["fresh"] is True
    assert seen and seen[0][2] is True
    assert result["event"]["actor"] == "coze-user-7"
    assert not db.in_transaction


def test_stale_plan_is_expired_before_untrusted_approval_verifier_runs(setup):
    db, ledger = setup
    document = plan()
    register_validated(db, ledger, document)
    with db:
        ledger.transition(document["plan_id"], "awaiting_user_approval", "宿主")
    current_snapshot = {category: [] for category in SNAPSHOT_CATEGORIES}
    current_snapshot["open_todos"] = [{"kind": "todo", "todo_id": 8}]
    called = []
    coordinator = make_coordinator(
        db, live(snapshot=current_snapshot),
        verifier=lambda *_args: called.append(True) or True)

    result = coordinator.decide(
        document["plan_id"], "approved", actor="coze-user-7",
        approval_ref="coze-approval:43", approval_doc_sha256="e" * 64)

    assert result["state"] == "expired"
    assert "open_todos" in result["changed_inputs"]
    assert called == []


def test_host_company_scope_cannot_be_overridden_by_plan_or_request(setup):
    db, ledger = setup
    document = plan()
    with db:
        ledger.register_plan(document)
    coordinator = make_coordinator(db, live(), company_id="acc-b")

    with pytest.raises(PermissionError, match="outside the host company scope"):
        coordinator.request_approval(document["plan_id"], actor="宿主")
    assert ledger.status(document["plan_id"])["state"] == "draft"
    assert not db.in_transaction


def test_confirmed_absence_requires_trusted_host_reverification(setup):
    db, ledger = setup
    proof = {"statement": "无诊断引用", "confirmed_by": "宿主审计",
             "confirmed_at": "2026-10-17T08:00:00+08:00", "method": "精确只读查询"}
    coverage_doc = coverage(diagnostic_refs={"status": "confirmed_absent", "proof": proof})
    document = plan(coverage_doc=coverage_doc)
    register_validated(db, ledger, document)
    current_coverage = coverage(
        diagnostic_refs={"status": "not_implemented", "notes": ["当前表未实现"]})
    current = live(coverage_doc=current_coverage)

    coordinator = make_coordinator(db, current)
    with pytest.raises(MigrationHostUnavailable, match="absence verifier"):
        coordinator.request_approval(document["plan_id"], actor="宿主")
    assert ledger.status(document["plan_id"])["state"] == "validated"

    coordinator = make_coordinator(
        db, current, absence_verifier=lambda company, category, current_proof: (
            company == "acc-a" and category == "diagnostic_refs" and current_proof == proof))
    result = coordinator.request_approval(document["plan_id"], actor="宿主")
    assert result["state"] == "awaiting_user_approval"
    assert result["fresh"] is True


def test_failed_absence_reverification_expires_the_plan(setup):
    db, ledger = setup
    proof = {"statement": "无诊断引用", "confirmed_by": "宿主审计",
             "confirmed_at": "2026-10-17T08:00:00+08:00", "method": "精确只读查询"}
    coverage_doc = coverage(diagnostic_refs={"status": "confirmed_absent", "proof": proof})
    document = plan(coverage_doc=coverage_doc)
    register_validated(db, ledger, document)
    current_coverage = coverage(
        diagnostic_refs={"status": "not_implemented", "notes": ["当前表未实现"]})
    coordinator = make_coordinator(
        db, live(coverage_doc=current_coverage), absence_verifier=lambda *_args: False)

    result = coordinator.request_approval(document["plan_id"], actor="宿主")

    assert result["state"] == "expired"
    assert "diagnostic_refs:absence_proof_invalid" in result["changed_inputs"]


def test_constructor_requires_trusted_host_validators(setup):
    db, _ledger = setup
    with pytest.raises(MigrationHostUnavailable, match="plan validator is not configured"):
        MigrationApprovalCoordinator(
            db, company_id="acc-a", plan_validator=None,
            approval_verifier=lambda _document, _evidence: True)
    with pytest.raises(MigrationHostUnavailable, match="approval verifier is not configured"):
        MigrationApprovalCoordinator(
            db, company_id="acc-a",
            plan_validator=lambda _document, _snapshot: True,
            approval_verifier=None)


def test_plan_validator_must_approve_before_queueing(setup):
    db, ledger = setup
    document = plan()
    with db:
        ledger.register_plan(document)
    coordinator = MigrationApprovalCoordinator(
        db, company_id="acc-a",
        plan_validator=lambda _document, _snapshot: False,
        approval_verifier=lambda _document, _evidence: True,
        snapshot_reader=FakeSnapshotReader(db, live()))

    with pytest.raises(ValueError, match="did not authorize"):
        coordinator.request_approval(document["plan_id"], actor="宿主")
    assert ledger.status(document["plan_id"])["state"] == "draft"
