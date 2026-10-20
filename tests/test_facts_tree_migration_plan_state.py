"""Append-only migration plan state audit; temporary SQLite databases only."""
import json
import sqlite3

import pytest

from app.facts_tree.migration_plans import (
    MigrationPlanStateLedger, MigrationPlanStore, SNAPSHOT_CATEGORIES)

APPROVAL_DOC = "e" * 64


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db")
    yield conn
    conn.close()


@pytest.fixture
def ledger(db):
    return MigrationPlanStateLedger(db)


def coverage_ok(**overrides):
    coverage = {category: {"status": "ok"} for category in SNAPSHOT_CATEGORIES}
    coverage.update(overrides)
    return coverage


def plan(**overrides):
    document = {
        "plan_id": "plan:acc-a:0001",
        "company_id": "acc-a",
        "created_by": "范范",
        "created_at": "2026-10-17T00:00:00+00:00",
        "source": {"pack_id": "generic", "pack_version": 1, "manifest_sha256": "a" * 64},
        "target": {"pack_id": "generic-v2", "pack_version": 1, "manifest_sha256": "b" * 64},
        "binding_revision": 1,
        "snapshot": {"facts": "c" * 64, "placements": "d" * 64},
        "input_snapshot": {"coverage": coverage_ok()},
        "dispositions": [{"fact_row_id": 1, "action": "keep"}],
    }
    document.update(overrides)
    return document


def make_verifier(plan_document, plan_sha256, expected):
    """Explicitly synthetic test verifier: checks the full evidence binding
    and returns True only when every expected field matches. This is NOT a
    real identity/approval channel."""
    calls = []

    def verifier(document, evidence):
        calls.append((document, evidence))
        assert document == plan_document
        assert evidence["company_id"] == "acc-a"
        assert evidence["plan_sha256"] == plan_sha256
        assert evidence["target"] == plan_document["target"]
        for key, value in expected.items():
            assert evidence[key] == value
        return True

    verifier.calls = calls
    return verifier


def test_constructor_requires_idle_caller_connection(db):
    with pytest.raises(TypeError):
        MigrationPlanStateLedger("state.db")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="idle connection"):
            MigrationPlanStateLedger(db)
        assert db.in_transaction
    finally:
        db.rollback()


def test_register_plan_writes_snapshot_and_draft_in_caller_transaction(ledger, db):
    with db:
        result = ledger.register_plan(plan())
    assert result["state"] == "draft"
    assert result["event"]["event_seq"] == 1
    assert result["event"]["actor"] == "范范"
    assert result["event"]["plan_sha256"] == result["plan_sha256"]
    assert db.execute("SELECT count(*) FROM tree_migration_plans").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM tree_migration_plan_events").fetchone()[0] == 1
    # Identical replay is idempotent: no new snapshot, no new event.
    with db:
        again = ledger.register_plan(plan())
    assert again == result
    assert db.execute("SELECT count(*) FROM tree_migration_plan_events").fetchone()[0] == 1
    # Conflicting plan_id is hard-rejected; nothing changes.
    with pytest.raises(ValueError, match="conflicting migration plan"):
        with db:
            ledger.register_plan(plan(created_by="别人"))
    assert ledger.status(plan()["plan_id"]) == result


def test_plan_without_draft_cannot_advance(db):
    store = MigrationPlanStore(db)
    with db:
        store.create(plan())
    # Default constructor: the state event table must exist even though the
    # snapshot table was already ensured by the snapshot store.
    ledger = MigrationPlanStateLedger(db)
    with pytest.raises(ValueError, match="no draft event"):
        ledger.status(plan()["plan_id"])
    with pytest.raises(ValueError, match="no draft event"):
        ledger.transition(plan()["plan_id"], "validated", "校验员")
    # register_plan repairs the missing draft event for the identical plan.
    with db:
        assert ledger.register_plan(plan())["state"] == "draft"


def test_unknown_plan_and_actor_validation(ledger, db):
    with pytest.raises(ValueError, match="unknown migration plan"):
        ledger.status("plan:missing")
    with db:
        ledger.register_plan(plan())
    for bad in ("", "  ", None, 1):
        with pytest.raises(ValueError, match="actor"):
            ledger.transition(plan()["plan_id"], "validated", bad)


def test_legal_chain_and_terminal_expiry(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
    with db:
        assert ledger.transition(pid, "validated", "校验员")["state"] == "validated"
    with db:
        assert ledger.transition(pid, "awaiting_user_approval", "协调员")["state"] \
            == "awaiting_user_approval"
    record = MigrationPlanStore(db, ensure_schema=False).get(pid)
    document = json.loads(record["plan_json"])
    verifier = make_verifier(document, record["plan_sha256"], {
        "decision": "approved", "actor": "范范", "approval_ref": "appr:1",
        "approval_doc_sha256": APPROVAL_DOC})
    with db:
        result = ledger.decide(pid, "approved", "范范", "appr:1", APPROVAL_DOC, verifier)
    assert result["state"] == "approved"
    assert verifier.calls  # the synthetic verifier actually ran
    detail = json.loads(result["event"]["detail_json"])
    assert detail == {"approval_ref": "appr:1", "approval_doc_sha256": APPROVAL_DOC}
    assert result["event"]["event_seq"] == 4
    with db:
        assert ledger.transition(pid, "expired", "协调员", reason="窗口过期")["state"] == "expired"
    # Terminal: no outgoing edge.
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "validated", "校验员")
    assert [e["event_type"] for e in ledger.history(pid)] == [
        "draft", "validated", "awaiting_user_approval", "approved", "expired"]


def test_expiry_from_each_pre_execution_stage_requires_reason(ledger, db):
    # P2 §5.2: a change of plan input version or digest expires the plan, so
    # validated, awaiting_user_approval and approved can all expire — always
    # with a mandatory reason, and expired stays closed.
    def reach(state_target):
        pid = f"plan:acc-a:exp:{state_target}"
        stage = plan(plan_id=pid)
        with db:
            ledger.register_plan(stage)
            ledger.transition(pid, "validated", "校验员")
        if state_target == "validated":
            return pid
        with db:
            ledger.transition(pid, "awaiting_user_approval", "协调员")
        if state_target == "awaiting_user_approval":
            return pid
        record = MigrationPlanStore(db, ensure_schema=False).get(pid)
        verifier = make_verifier(json.loads(record["plan_json"]),
                                 record["plan_sha256"], {"decision": "approved"})
        with db:
            ledger.decide(pid, "approved", "范范", "appr:exp", APPROVAL_DOC, verifier)
        return pid

    for stage in ("validated", "awaiting_user_approval", "approved"):
        pid = reach(stage)
        assert ledger.status(pid)["state"] == stage
        with pytest.raises(ValueError, match="reason"):
            ledger.transition(pid, "expired", "协调员", reason="  ")
        with db:
            result = ledger.transition(pid, "expired", "协调员",
                                       reason="目标模板版本变化")
        assert result["state"] == "expired"
        assert result["event"]["reason"] == "目标模板版本变化"
        with pytest.raises(ValueError, match="illegal plan state transition"):
            ledger.transition(pid, "validated", "校验员")
        assert ledger.status(pid)["event"]["event_type"] == "expired"


def test_draft_and_failed_plans_can_expire_after_snapshot_change(ledger, db):
    draft_id = "plan:acc-a:stale-draft"
    with db:
        ledger.register_plan(plan(plan_id=draft_id))
        result = ledger.transition(
            draft_id, "expired", "宿主", reason="首次校验前输入已变化")
    assert result["state"] == "expired"

    failed_id = "plan:acc-a:stale-failed"
    with db:
        ledger.register_plan(plan(plan_id=failed_id))
        ledger.transition(failed_id, "validated", "校验员")
        ledger.transition(failed_id, "awaiting_user_approval", "协调员")
    record = MigrationPlanStore(db, ensure_schema=False).get(failed_id)
    with db:
        ledger.decide(
            failed_id, "approved", "范范", "appr:stale-failed", APPROVAL_DOC,
            make_verifier(json.loads(record["plan_json"]), record["plan_sha256"],
                          {"decision": "approved"}))
    with db:
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (failed_id, 5, "executing", record["plan_sha256"], "执行器",
             "synthetic start", "{}", "2026-10-17T01:00:00+00:00"))
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (failed_id, 6, "failed", record["plan_sha256"], "执行器",
             "synthetic rollback", "{}", "2026-10-17T01:01:00+00:00"))
        result = ledger.transition(
            failed_id, "expired", "宿主", reason="重试前输入快照已变化")
    assert result["state"] == "expired"


def test_illegal_edges_and_same_state_repeat_rejected(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "blocked", "协调员", reason="x")
    with db:
        ledger.transition(pid, "validated", "校验员")
    # Same-state duplicate writes are illegal edges (no self-loops).
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "validated", "校验员")
    with db:
        ledger.transition(pid, "blocked", "协调员", reason="模板缺字段")
    # blocked is closed this slice (no ruled recovery edge yet).
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "validated", "校验员")
    assert ledger.status(pid)["state"] == "blocked"
    assert ledger.status(pid)["event_count"] == 3


def test_rejection_requires_reason_and_is_terminal(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
        ledger.transition(pid, "validated", "校验员")
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    record = MigrationPlanStore(db, ensure_schema=False).get(pid)
    verifier = make_verifier(json.loads(record["plan_json"]), record["plan_sha256"],
                             {"decision": "rejected"})
    with pytest.raises(ValueError, match="reason"):
        ledger.decide(pid, "rejected", "范范", "appr:2", APPROVAL_DOC, verifier, reason=" ")
    with db:
        result = ledger.decide(pid, "rejected", "范范", "appr:2", APPROVAL_DOC, verifier,
                               reason="目标模板变化")
    assert result["state"] == "rejected"
    assert result["event"]["reason"] == "目标模板变化"
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.transition(pid, "validated", "校验员")


def test_executor_states_are_never_written_by_this_service(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
        ledger.transition(pid, "validated", "校验员")
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    record = MigrationPlanStore(db, ensure_schema=False).get(pid)
    verifier = make_verifier(json.loads(record["plan_json"]), record["plan_sha256"],
                             {"decision": "approved"})
    with db:
        ledger.decide(pid, "approved", "范范", "appr:3", APPROVAL_DOC, verifier)
    for state in ("executing", "applied", "failed", "rolled_back"):
        with pytest.raises(ValueError, match="migration executor"):
            ledger.transition(pid, state, "执行器")
        assert ledger.status(pid)["state"] == "approved"
    with pytest.raises(ValueError, match="decision must be"):
        ledger.decide(pid, "failed", "范范", "appr:3", APPROVAL_DOC, verifier)


def test_failed_recovers_via_revalidation_and_fresh_approval(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
        ledger.transition(pid, "validated", "校验员")
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    record = MigrationPlanStore(db, ensure_schema=False).get(pid)
    document = json.loads(record["plan_json"])
    verifier = make_verifier(document, record["plan_sha256"], {"decision": "approved"})
    with db:
        ledger.decide(pid, "approved", "范范", "appr:4", APPROVAL_DOC, verifier)
    # A future executor would record approved -> executing -> failed;
    # simulate both events at SQL level (the guards allow the enum values
    # with correct seq and digest; the service API still refuses them).
    with db:
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (pid, 5, "executing", record["plan_sha256"], "执行器",
             "synthetic executor start", "{}", "2026-10-17T01:00:00+00:00"))
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (pid, 6, "failed", record["plan_sha256"], "执行器",
             "synthetic executor failure", "{}", "2026-10-17T01:05:00+00:00"))
    assert ledger.status(pid)["state"] == "failed"
    # failed is not terminal: re-validate the same immutable snapshot and
    # obtain a fresh user approval.
    with db:
        assert ledger.transition(pid, "validated", "校验员")["state"] == "validated"
    with db:
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    with db:
        assert ledger.decide(pid, "approved", "范范", "appr:5", APPROVAL_DOC,
                             verifier)["state"] == "approved"
    assert [e["event_type"] for e in ledger.history(pid)] == [
        "draft", "validated", "awaiting_user_approval", "approved",
        "executing", "failed",
        "validated", "awaiting_user_approval", "approved"]


def test_decide_requires_trusted_verifier_and_valid_evidence(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
        ledger.transition(pid, "validated", "校验员")
        ledger.transition(pid, "awaiting_user_approval", "协调员")
    with pytest.raises(ValueError, match="trusted approval verifier"):
        ledger.decide(pid, "approved", "范范", "appr:6", APPROVAL_DOC, None)
    with pytest.raises(ValueError, match="trusted approval verifier"):
        ledger.decide(pid, "approved", "范范", "appr:6", APPROVAL_DOC, "not-callable")
    with pytest.raises(ValueError, match="not confirmed"):
        ledger.decide(pid, "approved", "范范", "appr:6", APPROVAL_DOC,
                      lambda document, evidence: False)
    record = MigrationPlanStore(db, ensure_schema=False).get(pid)
    document = json.loads(record["plan_json"])

    def binding_mismatch(document_seen, evidence):
        return evidence["plan_sha256"] == "0" * 64

    with pytest.raises(ValueError, match="not confirmed"):
        ledger.decide(pid, "approved", "范范", "appr:6", APPROVAL_DOC, binding_mismatch)
    with pytest.raises(ValueError, match="approval_ref"):
        ledger.decide(pid, "approved", "范范", " ", APPROVAL_DOC,
                      lambda d, e: True)
    with pytest.raises(ValueError, match="approval_doc_sha256"):
        ledger.decide(pid, "approved", "范范", "appr:6", "xyz", lambda d, e: True)
    with pytest.raises(ValueError, match="decision must be"):
        ledger.decide(pid, "maybe", "范范", "appr:6", APPROVAL_DOC, lambda d, e: True)
    # A decision is only possible from awaiting_user_approval.
    other = plan(plan_id="plan:acc-a:0002")
    with db:
        ledger.register_plan(other)
    with pytest.raises(ValueError, match="illegal plan state transition"):
        ledger.decide(other["plan_id"], "approved", "范范", "appr:6", APPROVAL_DOC,
                      lambda d, e: True)
    # Nothing was written.
    assert ledger.status(pid)["state"] == "awaiting_user_approval"
    assert ledger.status(pid)["event_count"] == 3


def test_sql_guards_reject_seq_gaps_and_digest_mismatch(ledger, db):
    pid = plan()["plan_id"]
    with db:
        result = ledger.register_plan(plan())
    digest = result["plan_sha256"]
    with pytest.raises(sqlite3.IntegrityError, match="sequence gap"):
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (pid, 3, "validated", digest, "x", None, "{}", "t"))
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="digest differs"):
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (pid, 2, "validated", "0" * 64, "x", None, "{}", "t"))
    db.rollback()
    # Event for a plan row that does not exist is rejected as well.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            ("plan:ghost", 1, "draft", "0" * 64, "x", None, "{}", "t"))
    db.rollback()
    assert ledger.status(pid)["event_count"] == 1


def test_events_are_immutable_and_tamper_fails_closed(ledger, db):
    pid = plan()["plan_id"]
    with db:
        ledger.register_plan(plan())
        ledger.transition(pid, "validated", "校验员")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE tree_migration_plan_events SET actor='x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM tree_migration_plan_events")
    # Bypass the guard and introduce a sequence gap: reads must fail closed.
    with db:
        db.execute("DROP TRIGGER tree_migration_plan_events_no_update")
        db.execute("UPDATE tree_migration_plan_events SET event_seq=5 WHERE event_seq=2")
    with pytest.raises(ValueError, match="sequence gap"):
        ledger.status(pid)
    with pytest.raises(ValueError, match="sequence gap"):
        ledger.history(pid)
    with db:
        db.execute("UPDATE tree_migration_plan_events SET event_seq=2 WHERE event_seq=5")
        db.execute("UPDATE tree_migration_plan_events SET plan_sha256=? WHERE event_seq=2",
                   ("0" * 64,))
    with pytest.raises(ValueError, match="digest differs"):
        ledger.status(pid)


def test_validated_gate_fails_closed_on_blocking_coverage(ledger, db):
    # A not_implemented coverage registers as a draft preview but can never
    # be validated; the rejected attempt writes no event.
    blocked_plan = plan(input_snapshot={"coverage": coverage_ok(
        diagnostic_refs={"status": "not_implemented",
                         "notes": ["诊断/专题无权威存储"]})})
    pid = blocked_plan["plan_id"]
    with db:
        assert ledger.register_plan(blocked_plan)["state"] == "draft"
    with pytest.raises(ValueError, match="coverage blocks validation"):
        ledger.transition(pid, "validated", "校验员")
    assert ledger.status(pid)["state"] == "draft"
    assert ledger.status(pid)["event_count"] == 1
    # Every blocking status is rejected by name.
    for status in ("table_absent", "ambiguous_provenance",
                   "partial_identity_ledger", "identity_mismatch",
                   "active_tasks_present", "unidentified_checkpoint",
                   "pending_source_withdrawal"):
        variant = plan(plan_id=f"plan:acc-a:{status}",
                       input_snapshot={"coverage": coverage_ok(
                           facts={"status": status})})
        with db:
            ledger.register_plan(variant)
        with pytest.raises(ValueError, match=status):
            ledger.transition(variant["plan_id"], "validated", "校验员")
        assert ledger.status(variant["plan_id"])["event_count"] == 1


def test_validated_gate_fails_closed_on_structural_problems(ledger, db):
    # No coverage section at all.
    missing = plan(plan_id="plan:acc-a:no-coverage", input_snapshot={})
    with db:
        ledger.register_plan(missing)
    with pytest.raises(ValueError, match="no input_snapshot coverage"):
        ledger.transition(missing["plan_id"], "validated", "校验员")
    # Missing category key.
    short = coverage_ok()
    del short["open_todos"]
    broken = plan(plan_id="plan:acc-a:short",
                  input_snapshot={"coverage": short})
    with db:
        ledger.register_plan(broken)
    with pytest.raises(ValueError, match="invalid coverage"):
        ledger.transition(broken["plan_id"], "validated", "校验员")
    # Unknown status fails closed even though it is not in the blocking list.
    unknown = plan(plan_id="plan:acc-a:unknown",
                   input_snapshot={"coverage": coverage_ok(
                       facts={"status": "probably_fine"})})
    with db:
        ledger.register_plan(unknown)
    with pytest.raises(ValueError, match="invalid coverage"):
        ledger.transition(unknown["plan_id"], "validated", "校验员")
    # confirmed_absent with an incomplete proof is rejected; a complete one
    # passes the gate and the normal flow continues.
    proof = {"statement": "该公司无诊断/专题对象", "confirmed_by": "范范",
             "confirmed_at": "2026-10-17T01:00:00+00:00", "method": "人工巡检"}
    incomplete = plan(plan_id="plan:acc-a:bad-proof",
                      input_snapshot={"coverage": coverage_ok(
                          diagnostic_refs={"status": "confirmed_absent",
                                           "proof": {"statement": "缺字段"}})})
    with db:
        ledger.register_plan(incomplete)
    with pytest.raises(ValueError, match="invalid coverage"):
        ledger.transition(incomplete["plan_id"], "validated", "校验员")
    lifted = plan(plan_id="plan:acc-a:lifted",
                  input_snapshot={"coverage": coverage_ok(
                      diagnostic_refs={"status": "confirmed_absent",
                                       "proof": proof})})
    with db:
        ledger.register_plan(lifted)
        assert ledger.transition(lifted["plan_id"], "validated", "校验员")["state"] \
            == "validated"


def test_validated_gate_also_covers_revalidation_after_failed(ledger, db):
    # Executor states are simulated at SQL level (guards allow enum values
    # with correct seq/digest); the re-validation edge must pass the same
    # coverage gate as the first validation.
    blocked_plan = plan(input_snapshot={"coverage": coverage_ok(
        active_tasks={"status": "active_tasks_present",
                      "notes": ["parse_job job-1 status=parsing"]})})
    pid = blocked_plan["plan_id"]
    with db:
        record = ledger.register_plan(blocked_plan)
    digest = record["plan_sha256"]
    with db:
        for seq, event_type in ((2, "validated"), (3, "awaiting_user_approval"),
                                (4, "approved"), (5, "executing"), (6, "failed")):
            db.execute(
                "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
                (pid, seq, event_type, digest, "执行器", "synthetic", "{}",
                 "2026-10-17T01:00:00+00:00"))
    assert ledger.status(pid)["state"] == "failed"
    with pytest.raises(ValueError, match="coverage blocks validation"):
        ledger.transition(pid, "validated", "校验员")
    assert ledger.status(pid)["state"] == "failed"
    assert ledger.status(pid)["event_count"] == 6


def test_caller_transaction_is_preserved(ledger, db):
    with db:
        ledger.register_plan(plan())
    db.execute("BEGIN IMMEDIATE")
    try:
        # A failure raised inside a caller-owned transaction must neither be
        # committed nor rolled back by the service. "approved" via transition()
        # fails the user-decision guard before any edge check.
        with pytest.raises(ValueError, match="trusted verifier"):
            ledger.transition(plan()["plan_id"], "approved", "范范")
        assert db.in_transaction
    finally:
        db.rollback()
    assert ledger.status(plan()["plan_id"])["state"] == "draft"
    # And a successful call inside a caller transaction rolls back with it.
    db.execute("BEGIN IMMEDIATE")
    try:
        ledger.transition(plan()["plan_id"], "validated", "校验员")
        assert ledger.status(plan()["plan_id"])["state"] == "validated"
    finally:
        db.rollback()
    assert ledger.status(plan()["plan_id"])["state"] == "draft"
