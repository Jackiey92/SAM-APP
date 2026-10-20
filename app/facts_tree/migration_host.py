"""Host-side approval coordination for immutable FactsTree migration plans.

This module is deliberately an adapter boundary, not a web route. The host
must inject its company scope and validators backed by its real plan and
identity / approval systems. They must be local and deterministic: no network
calls while the SQLite write lock is held. No caller-supplied company ID or
actor string is treated as authenticated.

The execution preflight (P2 §5.3) also lives here so a future migration
executor can run the full gate inside its own ``BEGIN IMMEDIATE`` transaction
with no TOCTOU window between the check and the first write. The preflight
verifies only: approved state bound to the stored digest, the exact archived
target manifest, and the live freshness of all eight input categories plus
the company binding. It never writes executor states, execution receipts,
facts, placements, bindings or projections; the single permitted write is
the existing ``expired`` audit event when the plan inputs have changed.
"""
import hashlib
import json
import sqlite3

from .migration_plan_builder import normalize_input_snapshot
from .migration_plans import (
    SNAPSHOT_CATEGORIES, MigrationPlanStateLedger, MigrationPlanStore)
from .migration_snapshot import MigrationSnapshotReader

_EXPIRABLE_STATES = frozenset({
    "draft", "validated", "awaiting_user_approval", "approved", "failed"})
# Fixed audit actor for the preflight's only permitted write (the expired
# event). No caller parameter can supply or override an actor or company.
_PREFLIGHT_ACTOR = "migration-execution-preflight"


class MigrationHostUnavailable(RuntimeError):
    """A trusted host proof required to continue has not been configured."""


class MigrationApprovalCoordinator:
    """Recheck live inputs and record approval under one SQLite write lock.

    ``company_id`` is injected by the host when constructing this service.
    The plan validator and approval verifier are also host-owned and are not
    accepted per request, preventing an API caller from replacing them with
    permissive callbacks. The caller owns schema setup, but this coordinator
    owns each short ``BEGIN IMMEDIATE``/commit boundary so the live snapshot
    and audit event cannot race a concurrent writer.
    """

    def __init__(self, db, *, company_id, plan_validator, approval_verifier,
                 absence_verifier=None, snapshot_reader=None):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("migration approval coordinator requires a SQLite connection")
        if not isinstance(company_id, str) or not company_id.strip():
            raise ValueError("company_id must be injected by the trusted host")
        if not callable(plan_validator):
            raise MigrationHostUnavailable(
                "trusted host migration-plan validator is not configured")
        if not callable(approval_verifier):
            raise MigrationHostUnavailable(
                "trusted host approval verifier is not configured")
        if absence_verifier is not None and not callable(absence_verifier):
            raise TypeError("absence_verifier must be callable when configured")
        self.db = db
        self.company_id = company_id
        self.plan_validator = plan_validator
        self.approval_verifier = approval_verifier
        self.absence_verifier = absence_verifier
        self.reader = snapshot_reader or MigrationSnapshotReader(db)
        self.store = MigrationPlanStore(db, ensure_schema=False)
        self.ledger = MigrationPlanStateLedger(db, ensure_schema=False)

    def request_approval(self, plan_id, *, actor):
        """Move a fresh validated plan into the user-approval queue.

        A stale snapshot is terminally expired and returned as such. For a
        fresh draft, the host's locally configured validator checks the plan
        and target against the exact live snapshot before the state advances.
        """
        self._require_text(plan_id, "plan_id")
        self._require_text(actor, "actor")
        with self._immediate_transaction():
            record = self._scoped_record(plan_id)
            status = self.ledger.status(plan_id)
            fresh, changed, live = self._fresh(record)
            if not fresh:
                return self._expire(status, actor, changed)
            if status["state"] == "awaiting_user_approval":
                return {**status, "fresh": True, "changed_inputs": []}
            if status["state"] not in {"draft", "validated"}:
                raise ValueError(
                    "only a draft or validated migration plan can enter user approval")
            plan = json.loads(record["plan_json"])
            if self.plan_validator(plan, live) is not True:
                raise ValueError(
                    "host migration-plan validation did not authorize user approval")
            if status["state"] == "draft":
                self.ledger.transition(
                    plan_id, "validated", actor,
                    detail={"host_validation": "passed"})
            result = self.ledger.transition(
                plan_id, "awaiting_user_approval", actor,
                detail={"snapshot_rechecked": True})
            return {**result, "fresh": True, "changed_inputs": []}

    def decide(self, plan_id, decision, *, actor, approval_ref,
               approval_doc_sha256, reason=None, detail=None):
        """Record a trusted user's decision only while the plan remains fresh.

        Snapshot comparison, expiry or decision append all happen under the
        same SQLite write lock. The configured verifier must bind the signed
        host receipt to actor, company, plan ID/digest, target and decision.
        """
        self._require_text(plan_id, "plan_id")
        self._require_text(actor, "actor")
        with self._immediate_transaction():
            record = self._scoped_record(plan_id)
            status = self.ledger.status(plan_id)
            if status["state"] != "awaiting_user_approval":
                raise ValueError(
                    "a migration decision requires awaiting_user_approval state")
            fresh, changed, _live = self._fresh(record)
            if not fresh:
                return self._expire(status, actor, changed)
            result = self.ledger.decide(
                plan_id, decision, actor, approval_ref, approval_doc_sha256,
                self.approval_verifier, reason=reason, detail=detail)
            return {**result, "fresh": True, "changed_inputs": []}

    def preflight_execution(self, plan_id):
        """Short-transaction convenience wrapper over the in-transaction gate.

        Refuses a busy connection: it never commits or rolls back a caller
        transaction. The future executor should instead call
        ``preflight_execution_in_transaction`` inside its own
        ``BEGIN IMMEDIATE`` boundary so the gate and the first migration
        write share one lock.
        """
        self._require_text(plan_id, "plan_id")
        with self._immediate_transaction():
            return self.preflight_execution_in_transaction(plan_id)

    def preflight_execution_in_transaction(self, plan_id):
        """P2 §5.3 mechanical pre-execution gate, verification only.

        Requires a caller-owned transaction and never starts, commits or
        rolls it back, so a caller rollback also undoes the single permitted
        audit write. Checks, in order and fail-closed: the plan exists inside
        the host company scope; its current state is exactly ``approved``
        (the event log validation already binds the approval event to the
        stored plan digest); the plan's source/target/binding_revision are
        well-formed; the target manifest is archived, hash-consistent and
        published; and the live snapshot plus binding still match the frozen
        plan inputs (same rules as approval-time freshness, including the
        per-call confirmed_absent reverification). A stale plan is expired
        with the changed inputs recorded and returned; any other failure
        raises with zero writes. A passing result only means "pre-execution
        conditions hold" — no executor state, receipt or business data is
        ever written here.
        """
        self._require_text(plan_id, "plan_id")
        if not self.db.in_transaction:
            raise ValueError(
                "preflight_execution_in_transaction requires a caller-owned "
                "transaction; use preflight_execution on an idle connection")
        record = self._scoped_record(plan_id)
        status = self.ledger.status(plan_id)
        if status["state"] != "approved":
            raise ValueError(
                "migration execution preflight requires an approved plan; "
                f"current state is {status['state']!r}")
        plan = json.loads(record["plan_json"])
        source = self._binding_ref(plan.get("source"), "plan.source")
        target = self._binding_ref(plan.get("target"), "plan.target")
        binding_revision = plan.get("binding_revision")
        if type(binding_revision) is not int or binding_revision < 1:
            raise ValueError("plan.binding_revision must be a positive integer")
        self._verify_target_manifest(target)
        fresh, changed, _live = self._fresh(record)
        if not fresh:
            return self._expire(status, _PREFLIGHT_ACTOR, changed)
        return {"plan_id": record["plan_id"], "company_id": self.company_id,
                "plan_sha256": record["plan_sha256"],
                "source": source, "target": target,
                "binding_revision": binding_revision,
                "snapshot_fingerprint": plan.get("snapshot_fingerprint"),
                "state": status["state"], "preflight": "passed",
                "fresh": True, "changed_inputs": []}

    @classmethod
    def _binding_ref(cls, ref, what):
        """Fail-closed shape check of a plan's source/target identity."""
        if not isinstance(ref, dict) \
                or set(ref) != {"pack_id", "pack_version", "manifest_sha256"}:
            raise ValueError(f"{what} must be a pack/version/manifest identity")
        cls._require_text(ref["pack_id"], f"{what}.pack_id")
        # type() is int, not isinstance(): bool is an int subclass and True
        # must fail closed exactly like the plan builder's strict check.
        if type(ref["pack_version"]) is not int or ref["pack_version"] < 1:
            raise ValueError(f"{what}.pack_version must be a positive integer")
        digest = ref["manifest_sha256"]
        if not (isinstance(digest, str) and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest)):
            raise ValueError(f"{what}.manifest_sha256 must be a 64-char "
                             "lowercase hex digest")
        return {"pack_id": ref["pack_id"], "pack_version": ref["pack_version"],
                "manifest_sha256": digest}

    def _verify_target_manifest(self, target):
        """Exact immutable-archive verification of the plan's target manifest.

        Verified from the registry table only; the target is never required
        to be loadable as a template pack, so this neither enables a new pack
        nor touches the binding service. Runs inside the caller transaction.
        """
        if not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='template_manifests'").fetchone():
            raise ValueError("target manifest archive table is absent")
        row = self.db.execute(
            "SELECT manifest_sha256,manifest_json,status FROM template_manifests "
            "WHERE pack_id=? AND pack_version=?",
            (target["pack_id"], target["pack_version"])).fetchone()
        if row is None:
            raise ValueError(
                "target manifest has no immutable archive for "
                f"{target['pack_id']!r}@{target['pack_version']}")
        if hashlib.sha256(row[1].encode("utf-8")).hexdigest() != row[0]:
            raise ValueError("archived target manifest failed hash verification")
        if row[0] != target["manifest_sha256"]:
            raise ValueError(
                "archived target manifest digest differs from the plan target")
        if row[2] != "published":
            raise ValueError("target manifest is not published")

    def _scoped_record(self, plan_id):
        record = self.store.get(plan_id)
        if record is None:
            raise ValueError(f"unknown migration plan {plan_id!r}")
        if record["company_id"] != self.company_id:
            raise PermissionError("migration plan is outside the host company scope")
        return record

    def _fresh(self, record):
        plan = json.loads(record["plan_json"])
        expected_input = plan.get("input_snapshot")
        if not isinstance(expected_input, dict):
            raise ValueError("migration plan has no canonical input snapshot")
        try:
            expected_categories = {
                category: expected_input[category]["records"]
                for category in SNAPSHOT_CATEGORIES}
            expected = normalize_input_snapshot(
                expected_categories, expected_input["coverage"], self.company_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "migration plan input snapshot is incomplete or malformed") from exc
        if (expected["input_snapshot"] != expected_input
                or expected["snapshot_fingerprint"] != plan.get("snapshot_fingerprint")):
            raise ValueError("migration plan input snapshot failed internal verification")

        live = self.reader.read_in_transaction(company_id=self.company_id)
        if live.get("company_id") != self.company_id:
            raise ValueError("snapshot reader returned a different company scope")
        binding = live.get("binding")
        source = plan.get("source")
        if not isinstance(binding, dict) or not isinstance(source, dict):
            binding_changed = True
        else:
            binding_changed = any((
                binding.get("pack_id") != source.get("pack_id"),
                binding.get("pack_version") != source.get("pack_version"),
                binding.get("manifest_sha256") != source.get("manifest_sha256"),
                binding.get("binding_revision") != plan.get("binding_revision"),
            ))

        live_coverage = dict(live.get("coverage") or {})
        planned_coverage = expected["coverage"]
        changed = ["company_binding"] if binding_changed else []
        for category, entry in planned_coverage.items():
            if entry["status"] != "confirmed_absent":
                continue
            if live.get("snapshot", {}).get(category):
                changed.append(f"{category}:absence_no_longer_true")
                continue
            if self.absence_verifier is None:
                raise MigrationHostUnavailable(
                    f"trusted absence verifier is required for {category}")
            proof_ok = self.absence_verifier(
                self.company_id, category, dict(entry["proof"]))
            if proof_ok is not True:
                changed.append(f"{category}:absence_proof_invalid")
                continue
            # The reader may report not_implemented/table_absent because it
            # cannot itself prove absence. Only this trusted host proof lifts
            # that one category, and only for this exact company and plan.
            live_coverage[category] = entry

        current = normalize_input_snapshot(
            live["snapshot"], live_coverage, self.company_id)
        current_section = current["input_snapshot"]
        for category in expected["snapshot"]:
            wanted = expected["input_snapshot"][category]
            actual = current_section[category]
            if wanted["count"] != actual["count"] or wanted["sha256"] != actual["sha256"]:
                changed.append(category)
            if expected["coverage"][category] != current["coverage"][category]:
                changed.append(f"{category}:coverage")
        if current["snapshot_fingerprint"] != plan["snapshot_fingerprint"]:
            changed.append("snapshot_fingerprint")
        blocking = [category for category, entry in current["coverage"].items()
                    if entry["status"] in {
                        "table_absent", "not_implemented", "ambiguous_provenance",
                        "partial_identity_ledger", "identity_mismatch",
                        "active_tasks_present", "unidentified_checkpoint",
                        "pending_source_withdrawal"}]
        if blocking:
            changed.extend(f"{category}:coverage_blocked" for category in blocking)
        return not changed, sorted(set(changed)), live

    def _expire(self, prior_status, actor, changed):
        if prior_status["state"] not in _EXPIRABLE_STATES:
            raise ValueError(
                f"stale migration plan in non-expirable state {prior_status['state']!r}")
        result = self.ledger.transition(
            prior_status["plan_id"], "expired", actor,
            reason="输入快照或公司绑定已变化，必须重新生成计划",
            detail={"changed_inputs": changed})
        return {**result, "fresh": False, "changed_inputs": changed}

    class _Transaction:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            if self.db.in_transaction:
                raise ValueError(
                    "approval coordination requires an idle connection")
            self.db.execute("BEGIN IMMEDIATE")
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                try:
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            else:
                self.db.rollback()
            return False

    def _immediate_transaction(self):
        return self._Transaction(self.db)

    @staticmethod
    def _require_text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be nonempty text")
