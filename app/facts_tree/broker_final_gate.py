"""Host-only final acceptance orchestration; no business decisions or I/O adapters.

The host supplies an already claimed lease, an idle same-connection work store,
immutable effect and a pure local assembler. The assembler receives this round's
verified snapshot and must return the COMPLETE tree, never a patch. It owns plan
and dependency construction; this module binds its effect to the requested digest.
All Git evidence uses the bound repository, never the SQLite accepted mirror.

The admission is released on every exit. The host retains ownership/lifetime of
its lease. ``accept`` stays the fresh entrance: an operation carrying any
in_flight/retry_wait/unknown history is refused there and must settle through
``reconcile_unknown``. Exceptions before CAS reject without retry; exceptions at
or after CAS park unknown. If even the unknown mirror cannot be persisted, the
storage exception propagates: a caller must never receive an invented durable
outcome.

B4-2 ``reconcile_unknown`` settles one operation currently mirrored unknown with
its one unknown attempt. It has exactly two evidence exits:

- Exit A (Git convergence, zero second CAS): the current legal chain already
  carries the operation at a first accepting commit C whose canonical effect
  equals the requested opaque SHA-256. One paired work transaction then mirrors
  unknown -> accepted on the operation and that same attempt, naming C. No
  repository CAS, tree/commit build or new attempt happens, and the call is
  idempotent once the mirror already names C. A different on-chain effect is
  rejected; locator/chain anomalies quarantine with no work-store write.
- Exit B (isolated same-attempt re-admission): only when the complete chain
  proves empty of the operation. The host injects one trusted predicate proving
  the old executor, descendants included, has actually terminated AND lost
  ref-write ability (a dead parent PID or a deadline is not such proof), plus a
  head-bound VerifiedReentryFacts object attesting authorization and immutable
  dependencies. The host predicate supplies isolation evidence; optional E2 host evidence
  independently rechecks current F-007 authority before any new CAS.
  Missing/non-True/raising proof,
  stale or malformed facts, or a broken old-base..head empty proof all fail
  closed with the pair staying unknown. Only then is the section-7 paired
  unknown -> in_flight / unknown -> accepting edge used ON THE SAME ATTEMPT,
  followed by a complete fresh gate (assemble, build/create, B3 validation,
  durable intent first, one CAS, the same three CAS outcomes), at most two
  rounds; the second definite loss parks the existing terminal retry_wait pair.
  A still-ambiguous CAS parks unknown again without retry.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from uuid import uuid4

from app.facts_tree import broker_tree_gate, git_format, operation_acceptance
from app.facts_tree.broker_coordination import FINAL_ACCEPT, CoordinationError
from app.facts_tree.git_repository import CasResult
from app.facts_tree.record_schema._primitives import APPROVAL_USE_FIELDS
from app.host_approval_consumptions import HostApprovalConsumptionService, _check_storage
from app.host_approval_reservations import _effective_now, _monotonic_ns


@dataclass(frozen=True)
class AssembledAttempt:
    records: object
    plan: dict
    effect: dict
    dependencies: dict


@dataclass(frozen=True)
class FinalGateResult:
    status: str
    accepted_commit: str | None = None
    attempt_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReentryEvidence:
    """Read-only context handed to the host's trusted isolation predicate.

    It identifies the operation, the single attempt parked unknown, that
    attempt's immutable unknown base commit and the fresh authoritative head
    the gate captured. It carries no credential and authorizes nothing by
    itself.
    """

    operation_id: str
    attempt_id: str
    unknown_base_commit: str
    head_oid: str


@dataclass(frozen=True)
class VerifiedReentryFacts:
    """Host-verified read-only facts gating an isolated re-admission.

    The host performs all identity/authorization/dependency verification and
    injects the already-verified result; this module checks only the exact
    shape, the literal True flags and freshness: ``head_oid`` must equal the
    fresh authoritative H this gate read itself. Nothing here is parsed as a
    business rule. With E2 enabled these attestations never replace the fresh
    host approval-chain and immutable effect checks inside the gate.
    """

    head_oid: str
    authorization_valid: bool
    dependencies_valid: bool


class FinalGateRefusal(ValueError):
    """Host input or fresh evidence does not authorize this acceptance."""


def _digest(effect):
    return sha256(git_format.dumps_record(effect)).hexdigest()


def approval_artifact_digests(records, baseline, operation_id):
    """Pure, explicit artifact contract for host prepared effect_manifest v1.

    Hash every added/changed canonical record, and bind removed paths to null.
    The current operation's path
    is represented by '@operation', the top-level operation_id of operation,
    event and consumed_candidate by '@operation', and the four families'
    top-level approval_use_refs by []. These mechanical
    substitutions break the host-issued-ID / approval-digest cycle; those
    metadata bindings are checked independently against the real host chain.
    All other bytes (including effect_digest, actors, base, events and business
    outputs) remain in the hash. Unchanged history is checked by tree closure
    and Git append-only retention, not re-authorized by this operation.
    """
    operation_path = git_format.record_path("operation", operation_id=operation_id)
    result = {}
    for path, content in sorted(records.items()):
        if baseline.get(path) == content:
            continue
        payload = git_format.loads_record(content)
        family = git_format.canonical_record_type(path)
        if family in {"operation", "event", "consumed_candidate"}:
            if payload.get("operation_id") == operation_id:
                payload["operation_id"] = "@operation"
        if family in {"operation", "event", "placement_revision", "source_lifecycle"}:
            if "approval_use_refs" in payload:
                payload["approval_use_refs"] = []
        result["@operation" if path == operation_path else path] = _digest(payload)
    for path in sorted(set(baseline) - set(records)):
        result[path] = None
    return result


_APPROVAL_CODES = frozenset({
    "approval_evidence_invalid", "approval_connection_mismatch",
    "approval_already_consumed", "approval_consumption_missing",
    "approval_reference_mismatch", "approval_effect_unexplained",
    "approval_scope_mismatch", "approval_base_changed", "approval_effect_changed",
    "approval_dependencies_changed", "approval_artifacts_changed",
    "approval_product_changed", "approval_attempt_history_invalid",
    "approval_evidence_unavailable", "approval_expired",
})


def _approval_reason(exc):
    return str(exc) if isinstance(exc, FinalGateRefusal) and str(exc) in _APPROVAL_CODES else "approval_evidence_invalid"


def _approval_safe(call):
    # Dependency/clock/storage exceptions may contain credentials or SQL values.
    # Only a fixed code leaves the handler, without an implicit exception chain.
    code = None
    try:
        return call()
    except FinalGateRefusal as exc:
        code = _approval_reason(exc)
    except Exception:
        code = "approval_evidence_invalid"
    raise FinalGateRefusal(code)


class HostApprovalEvidence:
    """Explicit trusted host injection, never an external evidence dictionary.

    Holds a real E1 service and task credential on the coordinator's connection.
    now is a trusted local clock, sampled AFTER each write lock is acquired.
    It is neither an executor nor an authorization token; no service is created
    here. Constructor and validation never commit or roll back caller state.
    """

    def __init__(self, consumptions, task_secret, *, now):
        if not isinstance(consumptions, HostApprovalConsumptionService) or not callable(now):
            raise FinalGateRefusal("approval_evidence_invalid")
        self.consumptions = consumptions
        self._task_secret = task_secret
        self._now = now

    def _verify(self, repo, store, op, records, baseline, assembled, head,
                commit, *, consume, readmit):
        def verify():
            service = self.consumptions
            if service.db is not store.db or not store.db.in_transaction:
                raise FinalGateRefusal("approval_connection_mismatch")
            _check_storage(service.db)
            now = self._now()
            origin = _monotonic_ns()
            operation_id = op["operation_id"]
            stored = service._fetch(operation_id)
            if consume and stored is not None and not readmit:
                raise FinalGateRefusal("approval_already_consumed")
            for ref in op["approval_use_refs"]:
                if consume:
                    fact = service._append_admission(
                        self._task_secret, now, origin, operation_id,
                        ref["reservation_id"], ref["approval_id"],
                        ref["effect_digest"], ref["preparation_digest"])
                    record = fact.record
                else:
                    reservation = service._current(self._task_secret, now, origin, operation_id)
                    fact = service._fetch(operation_id)
                    if fact is None or fact.reservation_id != reservation.reservation_id:
                        raise FinalGateRefusal("approval_consumption_missing")
                    record = fact.record
                expected = {key: record[key] for key in APPROVAL_USE_FIELDS
                            - {"schema_version", "audit_ref_type"}}
                expected.update(schema_version=1, audit_ref_type="approval_use")
                if ref != expected:
                    raise FinalGateRefusal("approval_reference_mismatch")
            effect = service.reservations.approval_decisions._load_prepared(operation_id)
            envelope = effect.to_dict()
            meta = repo._expected_meta
            manifest = envelope["effect_manifest"]
            if (set(manifest) != {"schema_version", "repo_id", "effect", "artifacts"}
                    or type(manifest["schema_version"]) is not int
                    or manifest["schema_version"] != 1):
                raise FinalGateRefusal("approval_effect_unexplained")
            if (envelope["installation_id"] != meta["installation_id"]
                    or envelope["company_id"] != meta["company_id"]
                    or manifest["repo_id"] != meta["repo_id"]
                    or store.get_operation(meta["repo_id"], meta["company_id"],
                                           operation_id)["task_id"] != envelope["task_id"]):
                raise FinalGateRefusal("approval_scope_mismatch")
            if envelope["base_commit"] != head or envelope["object_format"] != repo.object_format:
                raise FinalGateRefusal("approval_base_changed")
            if git_format.dumps_record(assembled.effect) != git_format.dumps_record(manifest["effect"]):
                raise FinalGateRefusal("approval_effect_changed")
            if git_format.dumps_record(assembled.dependencies) != git_format.dumps_record(envelope["dependency_versions"]):
                raise FinalGateRefusal("approval_dependencies_changed")
            if approval_artifact_digests(records, baseline, operation_id) != manifest["artifacts"]:
                raise FinalGateRefusal("approval_artifacts_changed")
            # Inspect the actual prepared Git objects, not just the assembler's
            # own record map. read_snapshot_at deliberately excludes dangling
            # prepared commits, so use the repository's controlled read leg.
            _, actual = repo._validate_commit_tree(commit, tuple(records))
            if actual != dict(records) or repo._commit_parents(commit) != [head]:
                raise FinalGateRefusal("approval_product_changed")
            if _effective_now(now, origin) >= fact.expires_at:
                raise FinalGateRefusal("approval_expired")
        return _approval_safe(verify)


class BrokerFinalGate:
    def __init__(self, repository, coordination, store, *, approval_evidence=None):
        if coordination.db is not store.db:
            raise ValueError("coordination and work store must share one connection")
        if repository._expected_meta["repo_id"] != coordination.repo_id:
            raise ValueError("coordination repository identity mismatch")
        self.repository = repository
        self.coordination = coordination
        self.store = store
        if approval_evidence is not None and (
                type(approval_evidence) is not HostApprovalEvidence
                or approval_evidence.consumptions.db is not store.db):
            raise FinalGateRefusal("approval_connection_mismatch")
        self.approval_evidence = approval_evidence

    def accept(self, *, lease, operation_id, effect, assemble, recorded_at):
        """Accept a prepared/reserved operation, at most two fresh CAS rounds.

        effect is an opaque canonical object; equality is its canonical SHA-256,
        matching operation.effect_digest. IDs/timestamps are host-owned; attempt
        and event IDs are minted locally. No external/model callback is allowed.
        """
        # Freeze caller-owned content before entering any Git/work operation.
        effect = git_format.loads_record(git_format.dumps_record(effect))
        if not isinstance(effect, dict) or not callable(assemble):
            raise TypeError("expected effect object and pure local assembler")
        try:
            with self.coordination.admit(FINAL_ACCEPT, lease=lease) as admission:
                return self._accept(admission, operation_id, effect, assemble, recorded_at)
        except (FinalGateRefusal, CoordinationError) as exc:
            return FinalGateResult("rejected", reason=_approval_reason(exc)
                if self.approval_evidence is not None else str(exc))

    def reconcile_unknown(self, *, lease, operation_id, effect, assemble,
                          recorded_at, prove_executor_isolated=None,
                          reentry_facts=None):
        """Settle an operation parked unknown: Git convergence or re-admission.

        Evidence exit A needs no predicate/facts. Evidence exit B calls
        ``prove_executor_isolated`` exactly once with a ReentryEvidence context
        and requires ``proved is True``; ``reentry_facts`` must be a
        VerifiedReentryFacts bound to the fresh head with both literal True
        flags. Every evidence failure is a rejected result with the unknown
        pair untouched; storage failures propagate.
        """
        effect = git_format.loads_record(git_format.dumps_record(effect))
        if not isinstance(effect, dict) or not callable(assemble):
            raise TypeError("expected effect object and pure local assembler")
        try:
            with self.coordination.admit(FINAL_ACCEPT, lease=lease) as admission:
                return self._reconcile_unknown(
                    admission, operation_id, effect, assemble, recorded_at,
                    prove_executor_isolated, reentry_facts)
        except (FinalGateRefusal, CoordinationError) as exc:
            return FinalGateResult("rejected", reason=_approval_reason(exc)
                if self.approval_evidence is not None else str(exc))

    def _evidence(self, head, operation_id):
        repo = self.repository
        origin = operation_acceptance.locate_operation_acceptance(
            repo, operation_id=operation_id, commit_oid=head)
        if origin.head_oid != head:
            raise FinalGateRefusal("head changed during evidence read")
        snapshot = repo.read_snapshot_at(head)
        if snapshot.head_oid != head:
            raise FinalGateRefusal("head changed during snapshot read")
        return origin, snapshot

    def _replay(self, origin, snapshot, operation_id, digest):
        if origin.accepted_commit_oid is None:
            return None
        path = git_format.record_path("operation", operation_id=operation_id)
        record = next(r for r in snapshot.records if r.path == path)
        if git_format.loads_record(record.content).get("effect_digest") != digest:
            return FinalGateResult("rejected", reason="accepted effect differs")
        return FinalGateResult("idempotent_replay", origin.accepted_commit_oid)

    def _accept(self, admission, operation_id, effect, assemble, recorded_at):
        repo, store = self.repository, self.store
        scope = (repo._expected_meta["repo_id"], repo._expected_meta["company_id"],
                 operation_id)
        digest = _digest(effect)
        # Evidence exceptions quarantine the request: no work-store writes or CAS.
        try:
            head = repo.head_oid()
            origin, snapshot = self._evidence(head, operation_id)
            replay = self._replay(origin, snapshot, operation_id, digest)
        except Exception as exc:
            return FinalGateResult("rejected", reason=_approval_reason(exc)
                if self.approval_evidence is not None else "Git evidence refused: " + str(exc))
        if replay is not None:
            return replay
        operation = store.get_operation(*scope)
        if operation is None or operation["current_state"] not in {"prepared", "reserved"}:
            return FinalGateResult("rejected", reason="operation requires fresh admission")
        # A previous round cannot be resumed by calling the fresh entrance again.
        if any(e["state"] == "in_flight" for e in store.list_operation_events(*scope)):
            return FinalGateResult("rejected", reason="prior final gate requires reconciliation")
        return self._gate_rounds(admission, scope, effect, assemble, recorded_at,
                                 head, snapshot, None)

    def _reconcile_unknown(self, admission, operation_id, effect, assemble,
                           recorded_at, prove_isolated, reentry_facts):
        repo, store = self.repository, self.store
        scope = (repo._expected_meta["repo_id"], repo._expected_meta["company_id"],
                 operation_id)
        digest = _digest(effect)
        # The same evidence preamble as the fresh gate. The locator either pins
        # the first accepting commit C on the current legal chain or proves the
        # complete chain empty; any chain/object/identity anomaly quarantines
        # before a single work-store write.
        try:
            head = repo.head_oid()
            origin, snapshot = self._evidence(head, operation_id)
            chain_effect_matches = True
            if origin.accepted_commit_oid is not None:
                path = git_format.record_path("operation", operation_id=operation_id)
                record = next(r for r in snapshot.records if r.path == path)
                chain_effect_matches = (
                    git_format.loads_record(record.content).get("effect_digest")
                    == digest)
        except Exception as exc:
            return FinalGateResult("rejected", reason=_approval_reason(exc)
                if self.approval_evidence is not None else "Git evidence refused: " + str(exc))
        # Mirror reads replay the full joint history: corrupted/half-recorded
        # pairs raise ValueError here and deliberately propagate.
        operation = store.get_operation(*scope)
        if operation is None:
            raise FinalGateRefusal("operation requires a recorded request")
        attempts = [store.get_attempt(*scope, row["attempt_id"])
                    for row in store.list_attempts(*scope)]
        # E-enabled crash recovery: SIGKILL may precede the exception handler
        # which normally parks in_flight/accepting as unknown. Keep the same
        # immutable attempt; no new intent or CAS is permitted by this repair.
        if self.approval_evidence is not None and operation["current_state"] == "in_flight":
            accepting = [a for a in attempts if a["current_state"] == "accepting"]
            if len(accepting) != 1:
                raise FinalGateRefusal("approval_attempt_history_invalid")
            with admission.work_transaction():
                store.mark_unknown(*scope, accepting[0]["attempt_id"],
                    uuid4().hex, uuid4().hex, recorded_at,
                    operation_detail={"reason": "interrupted_approval_admission"},
                    attempt_detail={"reason": "interrupted_approval_admission"})
            operation = store.get_operation(*scope)
            attempts = [store.get_attempt(*scope, row["attempt_id"])
                        for row in store.list_attempts(*scope)]
        if origin.accepted_commit_oid is not None:
            if not chain_effect_matches:
                return FinalGateResult("rejected", reason="accepted effect differs")
            return self._converge_unknown(admission, scope, digest, operation,
                                          attempts, origin.accepted_commit_oid,
                                          recorded_at)
        return self._re_admit_unknown(admission, scope, effect, digest, assemble,
                                      recorded_at, head, snapshot, operation,
                                      attempts, prove_isolated, reentry_facts)

    def _converge_unknown(self, admission, scope, digest, operation, attempts,
                          commit, recorded_at):
        # Evidence exit A: mirror the Git-proven acceptance as one paired
        # unknown -> accepted transition, or report it with zero writes when
        # the mirror already converges on the same C.
        store = self.store
        state = operation["current_state"]
        if state == "accepted":
            accepted = [a for a in attempts if a["current_state"] == "accepted"]
            if (operation["accepted_commit"] != commit or len(accepted) != 1
                    or accepted[0]["accepted_commit"] != commit):
                raise FinalGateRefusal(
                    "accepted mirror diverges from Git evidence")
            return FinalGateResult("accepted", commit, accepted[0]["attempt_id"])
        if state != "unknown":
            raise FinalGateRefusal(
                "Git acceptance cannot settle a %r operation" % state)
        unknown = [a for a in attempts if a["current_state"] == "unknown"]
        if (len(unknown) != 1
                or {a["current_state"] for a in attempts} - {"unknown", "stale"}):
            raise FinalGateRefusal(
                "an unknown operation must own exactly one unknown attempt")
        attempt_id = unknown[0]["attempt_id"]
        if unknown[0]["effect_sha256"] != digest:
            raise FinalGateRefusal(
                "unknown attempt effect differs from the Git-proven effect")
        detail = {"reason": "unknown_reconciled_on_chain"}
        with admission.work_transaction():
            store.mark_accepted(*scope, attempt_id, commit,
                                uuid4().hex, uuid4().hex, recorded_at,
                                operation_detail=detail, attempt_detail=dict(detail))
        if store.db.in_transaction:
            raise FinalGateRefusal("convergence transaction did not close")
        operation = store.get_operation(*scope)
        persisted = store.get_attempt(*scope, attempt_id)
        if (operation["current_state"] != "accepted"
                or operation["accepted_commit"] != commit
                or persisted is None
                or persisted["current_state"] != "accepted"
                or persisted["accepted_commit"] != commit):
            raise FinalGateRefusal("paired convergence was not persisted")
        return FinalGateResult("accepted", commit, attempt_id)

    def _re_admit_unknown(self, admission, scope, effect, digest, assemble,
                          recorded_at, head, snapshot, operation, attempts,
                          prove_isolated, reentry_facts):
        # Evidence exit B: every gate below must pass before the paired
        # unknown re-admission edge may be used; none of them performs I/O.
        repo = self.repository
        if operation["current_state"] != "unknown":
            raise FinalGateRefusal(
                "isolated re-admission requires an unknown operation; an "
                "accepted mirror without Git evidence is not acceptance proof")
        unknown = [a for a in attempts if a["current_state"] == "unknown"]
        if (len(unknown) != 1
                or {a["current_state"] for a in attempts} - {"unknown", "stale"}):
            raise FinalGateRefusal(
                "an unknown operation must own exactly one unknown attempt")
        attempt = unknown[0]
        if attempt["effect_sha256"] != digest:
            raise FinalGateRefusal(
                "unknown attempt effect differs from the requested effect")
        if not callable(prove_isolated):
            raise FinalGateRefusal(
                "a trusted host executor-isolation predicate is required")
        # The locator already proved the whole genesis..H chain healthy and
        # empty; additionally bind that empty proof to exactly the range the
        # unclear result raced: the attempt's immutable old base .. current H.
        try:
            chain = repo.list_commit_chain(attempt["base_commit"], head)
        except Exception as exc:
            return FinalGateResult("rejected", reason=_approval_reason(exc)
                if self.approval_evidence is not None else "Git evidence refused: " + str(exc))
        if (not chain or chain[0] != attempt["base_commit"]
                or chain[-1] != head):
            raise FinalGateRefusal(
                "the empty legal-chain proof does not cover the unknown base")
        if type(reentry_facts) is not VerifiedReentryFacts:
            raise FinalGateRefusal(
                "head-bound verified re-admission facts are required")
        if (reentry_facts.head_oid != head
                or type(reentry_facts.authorization_valid) is not bool
                or type(reentry_facts.dependencies_valid) is not bool
                or not reentry_facts.authorization_valid
                or not reentry_facts.dependencies_valid):
            raise FinalGateRefusal(
                "re-admission facts are stale or do not authorize")
        evidence = ReentryEvidence(scope[2], attempt["attempt_id"],
                                   attempt["base_commit"], head)
        try:
            proved = prove_isolated(evidence)
        except Exception as exc:
            raise FinalGateRefusal(
                "executor isolation proof failed: " + str(exc))
        if proved is not True:
            raise FinalGateRefusal(
                "old executor termination and loss of ref-write ability "
                "are not proven")
        detail = {"reason": "unknown_re_admission", "head_oid": head,
                  "unknown_base_commit": attempt["base_commit"]}
        return self._gate_rounds(admission, scope, effect, assemble,
                                 recorded_at, head, snapshot,
                                 (attempt["attempt_id"], detail))

    def _gate_rounds(self, admission, scope, effect, assemble, recorded_at,
                     head, snapshot, readmit):
        # The shared final gate. readmit is None for the fresh entrance, or
        # (attempt_id, detail) for the B4-2 same-attempt re-admission, whose
        # first round reuses the existing unknown attempt instead of
        # registering a new one.
        repo, store = self.repository, self.store
        digest = _digest(effect)
        readmit_attempt = None if readmit is None else readmit[0]
        readmit_detail = None if readmit is None else readmit[1]
        for round_index in (1, 2):
            reused = readmit_attempt if (readmit is not None
                                         and round_index == 1) else None
            attempt_id = reused or uuid4().hex
            def pair(method, **kwargs):
                with admission.work_transaction():
                    return method(*scope, attempt_id,
                                  operation_event_id=uuid4().hex,
                                  attempt_event_id=uuid4().hex,
                                  recorded_at=recorded_at, **kwargs)
            intent = False
            cas_started = False
            try:
                assembled = assemble(head, snapshot)
                if not isinstance(assembled, AssembledAttempt) or _digest(assembled.effect) != digest:
                    raise FinalGateRefusal("assembled effect differs")
                # Copy bytes/path associations before objects are built. The same
                # immutable byte instances reach build_tree and B3, even if the
                # host later mutates its dictionary.
                records = MappingProxyType(dict(assembled.records))
                path = git_format.record_path("operation", operation_id=scope[2])
                op = git_format.loads_record(records[path])
                if (op.get("operation_id") != scope[2]
                        or op.get("effect_digest") != digest
                        or op.get("base_commit") != head):
                    raise FinalGateRefusal("assembled operation binding differs")
                approval_refs = op.get("approval_use_refs", [])
                if approval_refs:
                    # Freeze all content used by the evidence bridge; caller
                    # mutations after capture cannot change approved intent.
                    assembled = AssembledAttempt(records,
                        git_format.loads_record(git_format.dumps_record(assembled.plan)),
                        git_format.loads_record(git_format.dumps_record(assembled.effect)),
                        git_format.loads_record(git_format.dumps_record(assembled.dependencies)))
                baseline = {r.path: r.content for r in snapshot.records} if approval_refs else None
                paths = tuple(records)
                tree = repo.build_tree(records.items(), expected_paths=paths)
                commit = repo.create_commit(tree, head)
                # Reject deterministic invalid trees before opening an intent.
                # Revalidate these identical bytes after durable admission too:
                # no CAS entrance can rely solely on a pre-intent check.
                def validate():
                    return broker_tree_gate.validate_broker_tree(
                        records, repo_id=scope[0], company_id=scope[1],
                        commit_oid=commit, head_oid=head, object_format=repo.object_format)
                validated = validate()
                historical_paths = {r.path for r in snapshot.records}
                new_approval_operations = [p for p in validated.families.get("operation", ())
                    if p not in historical_paths and validated.ordered[p]["approval_use_refs"]]
                if new_approval_operations:
                    if self.approval_evidence is None:
                        raise FinalGateRefusal("approval_evidence_unavailable")
                    # One gate invocation owns exactly this operation's intent.
                    # A cumulative tree cannot smuggle another NEW approval use
                    # through the historical-read exemption.
                    if new_approval_operations != [path]:
                        raise FinalGateRefusal("approval_reference_mismatch")
                if self.approval_evidence is not None and not approval_refs:
                    # [] makes no approval assertion. An operation explicitly
                    # requiring approval cannot erase its refs to bypass this
                    # opt-in boundary, including on same-attempt re-entry.
                    if any(e["state"] == "approval_required"
                           for e in store.list_operation_events(*scope)):
                        raise FinalGateRefusal("approval_reference_mismatch")
                with admission.work_transaction():
                    if approval_refs:
                        self.approval_evidence._verify(
                            repo, store, op, records, baseline, assembled, head, commit,
                            consume=True, readmit=readmit is not None)
                    if reused is None:
                        store.record_attempt(*scope, attempt_id, recorded_at, head,
                                             assembled.plan, effect, assembled.dependencies,
                                             object_format=repo.object_format,
                                             prepared_event_id=uuid4().hex, prepared_at=recorded_at)
                    admit_kwargs = {}
                    if reused is not None:
                        admit_kwargs["operation_detail"] = readmit_detail
                        admit_kwargs["attempt_detail"] = readmit_detail
                    store.admit_execution(*scope, attempt_id, uuid4().hex,
                                          uuid4().hex, recorded_at, **admit_kwargs)
                if store.db.in_transaction:
                    raise FinalGateRefusal("intent transaction did not close")
                persisted = store.get_attempt(*scope, attempt_id)
                if (persisted is None or persisted["current_state"] != "accepting"
                        or persisted["effect_sha256"] != digest
                        or (reused is None and persisted["base_commit"] != head)
                        or (reused is not None
                            and persisted["base_commit"]
                            != readmit_detail["unknown_base_commit"])
                        or store.get_operation(*scope)["current_state"] != "in_flight"):
                    raise FinalGateRefusal("paired intent was not persisted")
                intent = True
                validate()
                if approval_refs:
                    # A NEW post-intent write transaction: wait for the lock
                    # first, then sample time and reread current authorization.
                    # Hold ordering through the actual CAS; the component never
                    # commits/rolls back this coordinator-owned transaction.
                    with admission.work_transaction():
                        self.approval_evidence._verify(
                            repo, store, op, records, baseline, assembled, head, commit,
                            consume=False, readmit=readmit is not None)
                        cas_started = True
                        result = repo.accept(head, commit, expected_paths=paths)
                else:
                    cas_started = True
                    result = repo.accept(head, commit, expected_paths=paths)
                if (not isinstance(result, CasResult) or type(result.accepted) is not bool
                        or result.expected_old != head or result.new_oid != commit):
                    raise FinalGateRefusal("CAS result binding differs")
                if result.accepted:
                    if result.current_oid != commit:
                        raise FinalGateRefusal("CAS success head differs")
                    pair(store.mark_accepted, accepted_commit=commit)
                    return FinalGateResult("accepted", commit, attempt_id)
                # A definite false is insufficient alone. Prove a healthy strict
                # successor, absence on its ENTIRE chain, and unchanged effect.
                successor = result.current_oid
                chain = repo.list_commit_chain(head, successor)
                if successor == head or head not in chain:
                    raise FinalGateRefusal("CAS loss is not a healthy successor")
                origin, snapshot = self._evidence(successor, scope[2])
                if origin.accepted_commit_oid is not None:
                    raise FinalGateRefusal("operation appeared during CAS loss")
                if _digest(assembled.effect) != digest:
                    raise FinalGateRefusal("effect changed during CAS loss")
                method = (store.recycle_lost_cas_attempt if round_index == 1
                          else store.park_exhausted_cas_attempt)
                pair(method, lost_from_head=head, lost_to_head=successor,
                     round_index=round_index)
                if round_index == 2:
                    return FinalGateResult("retry_wait", attempt_id=attempt_id,
                                           reason="lost_cas_exhausted")
                head = successor
            except Exception as exc:
                if intent:
                    pair(store.mark_unknown, operation_detail={"reason": "final_gate_unresolved"},
                         attempt_detail={"reason": "final_gate_unresolved"})
                    return FinalGateResult("unknown", attempt_id=attempt_id,
                        reason=_approval_reason(exc)
                        if self.approval_evidence is not None else str(exc))
                if cas_started:  # defensive: no CAS may precede durable intent
                    raise
                return FinalGateResult("rejected", reason=_approval_reason(exc)
                    if self.approval_evidence is not None else str(exc))
        raise AssertionError("two-round budget escaped")
