"""Immutable migration plan snapshots (P2 §5.1 storage contract).

Caller-provided sqlite3.Connection only; methods never open their own
transactions — the caller owns the transaction boundary so a plan write
commits or rolls back together with whatever accompanies it. Storage only:
no plan generation, approval, expiry, execution or rollback here.

The digest always covers the canonical serialization of the complete plan
document and lives only in the database column / return value. `plan_sha256`
is a reserved key: it is never part of the plan document (a digest cannot
reference itself) and is rejected on input.

Snapshot coverage contract (P2 §5.1, rulings of 2026-10-17): the constants
and ``normalize_coverage``/``coverage_blocking_reasons`` helpers below are
shared by the plan builder (build-time shape validation) and the state
ledger, which applies ONE mechanical fail-closed gate on the `validated`
edge: a frozen plan whose coverage has any blocking status (or an
unverifiable/absent coverage section) can never advance. This is a records-
keeping ledger otherwise; the gate derives blocking only from the fixed
status table, never from caller-supplied flags, and a `confirmed_absent`
proof is shape-checked here while the identity behind it remains the
responsibility of a future trusted host layer.
"""
import hashlib
import json
import sqlite3

from ..db.schema import TREE_MIGRATION_EVENT_SCHEMA, TREE_MIGRATION_PLAN_SCHEMA

_REQUIRED_FIELDS = ("plan_id", "company_id", "created_by", "created_at")
_RESERVED_FIELDS = frozenset({"plan_sha256"})

# P2 §5.2 state machine. The executor states executing/applied/failed/
# rolled_back are part of the enum (no future DDL change) but are NEVER
# written by this slice: only a migration executor may record them.
# `failed` is deliberately not terminal: a failed attempt recovers via
# re-validation and a fresh user approval of the same immutable snapshot.
_STATE_EDGES = {
    # A snapshot can change even before first validation; host coordination
    # must be able to close that immutable plan instead of leaving a stale
    # draft that looks reusable.
    "draft": {"validated", "rejected", "expired"},
    # P2 §5.2: any change of plan input version or digest expires the plan,
    # so validated/awaiting_user_approval/approved may all go to expired.
    "validated": {"awaiting_user_approval", "blocked", "rejected", "expired"},
    "awaiting_user_approval": {"approved", "rejected", "expired"},
    "approved": {"expired", "executing"},
    # A failed transaction is retryable only while its frozen input remains
    # current; the host may expire it after detecting changed inputs.
    "failed": {"validated", "expired"},
    "executing": {"applied", "failed"},
    "applied": {"rolled_back"},
}
# Hard-closed states: no outgoing edge exists.
_TERMINAL_STATES = frozenset({"rejected", "expired", "rolled_back"})
# States owned by a future executor; the service refuses to write them.
_EXECUTOR_STATES = frozenset({"executing", "applied", "failed", "rolled_back"})
# Non-decision states recordable through transition().
_TRANSITION_STATES = frozenset({"validated", "awaiting_user_approval", "blocked", "expired"})

# P2 §5.1 snapshot coverage categories and statuses. Shared with the plan
# builder; the state ledger gate derives blocking solely from this table.
SNAPSHOT_CATEGORIES = (
    "facts", "placements", "evidence_links", "source_lifecycle",
    "leaf_revisions", "open_todos", "diagnostic_refs", "active_tasks")
# Non-blocking statuses. `confirmed_absent` additionally requires a complete
# proof; `legacy_bootstrap_derived` is valid for the facts category only.
COVERAGE_PASS_STATUSES = frozenset({"ok", "confirmed_absent", "legacy_bootstrap_derived"})
COVERAGE_BLOCKING_STATUSES = frozenset({
    "table_absent", "not_implemented", "ambiguous_provenance",
    "partial_identity_ledger", "identity_mismatch", "active_tasks_present",
    "unidentified_checkpoint", "pending_source_withdrawal"})
COVERAGE_STATUSES = COVERAGE_PASS_STATUSES | COVERAGE_BLOCKING_STATUSES
COVERAGE_FACTS_ONLY_STATUSES = frozenset({"legacy_bootstrap_derived"})
COVERAGE_PROOF_FIELDS = ("statement", "confirmed_by", "confirmed_at", "method")
# Statuses asserting that no records can exist; the builder cross-checks the
# snapshot lists are actually empty for these.
COVERAGE_EMPTY_RECORD_STATUSES = frozenset(
    {"not_implemented", "table_absent", "confirmed_absent"})
_COVERAGE_FIELDS = frozenset({"status", "notes", "proof"})


def normalize_coverage(coverage):
    """Validate and canonicalize one plan's snapshot coverage section.

    Pure shape/status validation shared by the builder and the ledger gate.
    Blocking is derived from the fixed status table — there is deliberately
    no caller-settable blocking flag. Notes are sorted so the normalized
    form is deterministic regardless of caller ordering. Unknown statuses
    fail closed (rejected here, treated as blocking by the gate).
    """
    if not isinstance(coverage, dict):
        raise ValueError("coverage must be a JSON object")
    keys = set(coverage)
    expected = set(SNAPSHOT_CATEGORIES)
    if keys != expected:
        raise ValueError(
            f"coverage must contain exactly the categories {sorted(expected)}; "
            f"missing {sorted(expected - keys)}, unexpected {sorted(keys - expected)}")
    normalized = {}
    for category in SNAPSHOT_CATEGORIES:
        entry = coverage[category]
        what = f"coverage.{category}"
        if not isinstance(entry, dict):
            raise ValueError(f"{what} must be a JSON object")
        unknown = set(entry) - _COVERAGE_FIELDS
        if unknown:
            raise ValueError(f"{what} has unknown fields {sorted(unknown)}")
        status = entry.get("status")
        if status not in COVERAGE_STATUSES:
            raise ValueError(f"{what}.status {status!r} is not a known coverage status")
        if status in COVERAGE_FACTS_ONLY_STATUSES and category != "facts":
            raise ValueError(f"{what}.status {status!r} is only valid for facts")
        notes = entry.get("notes", [])
        if (not isinstance(notes, list)
                or any(not isinstance(note, str) or not note.strip() for note in notes)):
            raise ValueError(f"{what}.notes must be a list of nonempty text")
        result = {"status": status}
        if notes:
            result["notes"] = sorted(notes)
        proof = entry.get("proof")
        if status == "confirmed_absent":
            if not isinstance(proof, dict) or set(proof) != set(COVERAGE_PROOF_FIELDS):
                raise ValueError(
                    f"{what}.proof must contain exactly {list(COVERAGE_PROOF_FIELDS)}")
            for field in COVERAGE_PROOF_FIELDS:
                if not isinstance(proof[field], str) or not proof[field].strip():
                    raise ValueError(f"{what}.proof.{field} must be nonempty text")
            result["proof"] = dict(proof)
        elif proof is not None:
            raise ValueError(f"{what}.proof is only allowed for confirmed_absent")
        normalized[category] = result
    return normalized


def coverage_blocking_reasons(plan_document):
    """Mechanical validated-gate check over a frozen plan document.

    Returns a sorted list of blocking reasons; an empty list allows the
    `validated` edge. Any structural problem — missing coverage section,
    wrong keys, unknown status, incomplete proof — is itself a blocking
    reason: the gate fails closed and never inspects live data.
    """
    snapshot = plan_document.get("input_snapshot") if isinstance(plan_document, dict) else None
    coverage = snapshot.get("coverage") if isinstance(snapshot, dict) else None
    if coverage is None:
        return ["plan has no input_snapshot coverage section"]
    try:
        normalized = normalize_coverage(coverage)
    except ValueError as exc:
        return [f"invalid coverage: {exc}"]
    return sorted(f"{category}: {entry['status']}"
                  for category, entry in normalized.items()
                  if entry["status"] in COVERAGE_BLOCKING_STATUSES)


def _canonical_json(document) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("migration plan must be a JSON object")
    for field in _REQUIRED_FIELDS:
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"migration plan field {field!r} must be nonempty text")
    reserved = _RESERVED_FIELDS & set(plan)
    if reserved:
        raise ValueError(
            f"{sorted(reserved)} are reserved and not part of the plan document; "
            "the digest is stored separately")
    try:
        canonical = _canonical_json(plan)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"migration plan must be JSON serializable: {exc}") from exc
    return canonical


class MigrationPlanStore:
    def __init__(self, db, *, ensure_schema=True):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("migration plan store requires a caller-provided sqlite3.Connection")
        self.db = db
        if ensure_schema:
            # Checked before any DDL: a caller transaction is never joined or
            # committed. With ensure_schema=False no DDL runs, so a lightweight
            # read-only wrapper may be created inside a caller transaction
            # (the state ledger uses this to verify plans mid-transaction).
            if db.in_transaction:
                raise ValueError("migration plan store requires an idle connection")
            self.db.executescript(TREE_MIGRATION_PLAN_SCHEMA)

    def create(self, plan):
        """Store a plan snapshot; idempotent replay for identical content.

        The same plan_id with a different canonical document is hard-rejected
        and the original row is left untouched.
        """
        canonical = _validate_plan(plan)
        digest = _digest(canonical)
        plan_id, company_id = plan["plan_id"], plan["company_id"]
        cursor = self.db.execute(
            "SELECT company_id,plan_sha256,plan_json FROM tree_migration_plans WHERE plan_id=?",
            (plan_id,))
        row = cursor.fetchone()
        if row is not None:
            if row[0] != company_id or row[1] != digest or row[2] != canonical:
                raise ValueError(f"conflicting migration plan already stored as {plan_id!r}")
            return {"plan_id": plan_id, "company_id": company_id,
                    "plan_sha256": digest, "plan_json": canonical}
        self.db.execute(
            "INSERT INTO tree_migration_plans VALUES(?,?,?,?)",
            (plan_id, company_id, digest, canonical))
        return {"plan_id": plan_id, "company_id": company_id,
                "plan_sha256": digest, "plan_json": canonical}

    def get(self, plan_id):
        """Exact lookup that re-verifies the stored snapshot.

        Returns None only when no plan exists; a digest/content/column
        mismatch fails closed instead of returning a tampered plan.
        """
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("plan_id must be nonempty text")
        cursor = self.db.execute(
            "SELECT company_id,plan_sha256,plan_json FROM tree_migration_plans WHERE plan_id=?",
            (plan_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        company_id, digest, canonical = row
        if _digest(canonical) != digest:
            raise ValueError(f"migration plan {plan_id!r} failed hash verification")
        try:
            document = json.loads(canonical)
        except ValueError as exc:
            raise ValueError(f"migration plan {plan_id!r} is not valid JSON") from exc
        if document.get("plan_id") != plan_id or document.get("company_id") != company_id:
            raise ValueError(f"migration plan {plan_id!r} columns disagree with its content")
        return {"plan_id": plan_id, "company_id": company_id,
                "plan_sha256": digest, "plan_json": canonical}


class MigrationPlanStateLedger:
    """Append-only state audit for migration plans (P2 §5.2).

    Caller-provided connection; like MigrationPlanStore the methods never
    open their own transactions — the caller owns the boundary, so a plan
    snapshot and its first draft event commit or roll back together.
    Current state is always rebuilt from the ordered event log with full
    read-time validation (contiguous sequence, first event is draft, every
    event binds the stored plan digest, every edge is legal). Executor
    states are never written here.
    """

    def __init__(self, db, *, ensure_schema=True):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("plan state ledger requires a caller-provided sqlite3.Connection")
        # Checked before any DDL: a caller transaction is never joined or committed.
        if db.in_transaction:
            raise ValueError("plan state ledger requires an idle connection")
        self.db = db
        if ensure_schema:
            self.db.executescript(TREE_MIGRATION_PLAN_SCHEMA)
            self.db.executescript(TREE_MIGRATION_EVENT_SCHEMA)

    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _store(self):
        return MigrationPlanStore(self.db, ensure_schema=False)

    def _events(self, plan_id):
        cursor = self.db.execute(
            "SELECT event_seq,event_type,plan_sha256,actor,reason,detail_json,created_at"
            " FROM tree_migration_plan_events WHERE plan_id=? ORDER BY event_seq",
            (plan_id,))
        return [{"plan_id": plan_id, "event_seq": row[0], "event_type": row[1],
                 "plan_sha256": row[2], "actor": row[3], "reason": row[4],
                 "detail_json": row[5], "created_at": row[6]}
                for row in cursor.fetchall()]

    def _validated_events(self, plan_id):
        record = self._store().get(plan_id)  # re-verifies the stored snapshot
        if record is None:
            raise ValueError(f"unknown migration plan {plan_id!r}")
        events = self._events(plan_id)
        if not events:
            raise ValueError(f"migration plan {plan_id!r} has no draft event")
        seqs = [event["event_seq"] for event in events]
        if seqs != list(range(1, len(seqs) + 1)):
            raise ValueError(f"migration plan {plan_id!r} has a plan event sequence gap")
        for event in events:
            if event["plan_sha256"] != record["plan_sha256"]:
                raise ValueError(
                    f"migration plan {plan_id!r} event digest differs from the stored plan")
        if events[0]["event_type"] != "draft":
            raise ValueError(f"migration plan {plan_id!r} first event must be draft")
        for previous, current in zip(events, events[1:]):
            if current["event_type"] not in _STATE_EDGES.get(previous["event_type"], set()):
                raise ValueError(
                    f"migration plan {plan_id!r} has illegal transition "
                    f"{previous['event_type']} -> {current['event_type']}")
        return record, events

    def _append(self, record, seq, event_type, actor, reason, detail):
        self.db.execute(
            "INSERT INTO tree_migration_plan_events VALUES(?,?,?,?,?,?,?,?)",
            (record["plan_id"], seq, event_type, record["plan_sha256"],
             actor, reason, _canonical_json(detail), self._now()))

    @staticmethod
    def _require_actor(actor):
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be nonempty text")

    def register_plan(self, plan):
        """Store the plan snapshot and its first draft event atomically.

        Idempotent replay for an identical plan (the existing event log is
        left untouched); a conflicting plan_id is hard-rejected by the
        snapshot store. A plan row without any draft event is not a valid
        ledger plan and is repaired here by appending its draft event.
        """
        record = self._store().create(plan)
        if not self._events(record["plan_id"]):
            self._append(record, 1, "draft", plan["created_by"], None, {})
        return self.status(record["plan_id"])

    def status(self, plan_id):
        """Current state rebuilt from the fully validated event log."""
        record, events = self._validated_events(plan_id)
        latest = events[-1]
        return {"plan_id": record["plan_id"], "company_id": record["company_id"],
                "plan_sha256": record["plan_sha256"], "state": latest["event_type"],
                "event": latest, "event_count": len(events)}

    def history(self, plan_id):
        """The full validated event log."""
        _, events = self._validated_events(plan_id)
        return events

    def _next_state(self, plan_id, target, actor):
        self._require_actor(actor)
        record, events = self._validated_events(plan_id)
        current = events[-1]["event_type"]
        if target not in _STATE_EDGES.get(current, set()):
            raise ValueError(f"illegal plan state transition {current} -> {target}")
        return record, len(events) + 1

    def transition(self, plan_id, target, actor, *, reason=None, detail=None):
        """Record a non-decision state edge (validated/awaiting_user_approval/
        blocked/expired). Executor states and user decisions are rejected.

        The `validated` edge — the single entry to the approval/execution
        chain, including re-validation after `failed` — passes one mechanical
        fail-closed gate: the frozen, digest-verified plan document must
        carry a complete snapshot coverage section with no blocking status.
        No event is written when the gate rejects. Every other edge only
        records that trusted upper layers completed their checks."""
        if target in _EXECUTOR_STATES:
            raise ValueError(
                f"state {target!r} belongs to the migration executor and cannot be "
                "written by this service")
        if target in {"approved", "rejected"}:
            raise ValueError("user decisions go through decide() with a trusted verifier")
        if target not in _TRANSITION_STATES:
            raise ValueError(f"unknown transition target {target!r}")
        if target in {"blocked", "expired"} and not (isinstance(reason, str) and reason.strip()):
            raise ValueError(f"state {target!r} requires a nonempty reason")
        record, next_seq = self._next_state(plan_id, target, actor)
        if target == "validated":
            document = json.loads(record["plan_json"])
            reasons = coverage_blocking_reasons(document)
            if reasons:
                raise ValueError(
                    "plan coverage blocks validation: " + "; ".join(reasons))
        self._append(record, next_seq, target, actor, reason, detail or {})
        return self.status(plan_id)

    def decide(self, plan_id, decision, actor, approval_ref, approval_doc_sha256,
               verifier, *, reason=None, detail=None):
        """Record a user decision (approved/rejected).

        A trusted approval_verifier is mandatory: it receives the frozen plan
        document and the decision evidence and confirms a trusted host, the
        binding of company_id/plan_sha256/target and the actual user decision.
        This callback is an interface contract only — it does not mean a real
        identity/approval channel is connected. Only the verified reference,
        digest and details are stored.
        """
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
        self._require_actor(actor)
        if not callable(verifier):
            raise ValueError(
                "a trusted approval verifier is required for user decisions")
        if not isinstance(approval_ref, str) or not approval_ref.strip():
            raise ValueError("approval_ref must be nonempty text")
        if not (isinstance(approval_doc_sha256, str) and len(approval_doc_sha256) == 64
                and all(c in "0123456789abcdef" for c in approval_doc_sha256)):
            raise ValueError("approval_doc_sha256 must be a 64-char lowercase hex digest")
        if decision == "rejected" and not (isinstance(reason, str) and reason.strip()):
            raise ValueError("rejected decisions require a nonempty reason")
        record, next_seq = self._next_state(plan_id, decision, actor)
        document = json.loads(record["plan_json"])
        evidence = {"decision": decision, "actor": actor, "approval_ref": approval_ref,
                    "approval_doc_sha256": approval_doc_sha256,
                    "plan_id": record["plan_id"],
                    "company_id": record["company_id"],
                    "plan_sha256": record["plan_sha256"],
                    "target": document.get("target")}
        if not verifier(document, evidence):
            raise ValueError("approval evidence was not confirmed by the trusted verifier")
        payload = {"approval_ref": approval_ref,
                   "approval_doc_sha256": approval_doc_sha256}
        if detail:
            payload["detail"] = detail
        self._append(record, next_seq, decision, actor, reason, payload)
        return self.status(plan_id)
