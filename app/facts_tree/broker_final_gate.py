"""Host-only final acceptance orchestration; no business decisions or I/O adapters.

The host supplies an already claimed lease, an idle same-connection work store,
immutable effect and a pure local assembler. The assembler receives this round's
verified snapshot and must return the COMPLETE tree, never a patch. It owns plan
and dependency construction; this module binds its effect to the requested digest.
All Git evidence uses the bound repository, never the SQLite accepted mirror.

The admission is released on every exit. The host retains ownership/lifetime of
its lease. Unknown re-admission is deliberately refused here: B4-2 can reconcile
via a separate evidence entry without changing this fresh-acceptance contract.
Exceptions before CAS reject without retry; exceptions at/after CAS park unknown.
If even the unknown mirror cannot be persisted, the storage exception propagates:
a caller must never receive an invented durable outcome.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from uuid import uuid4

from app.facts_tree import broker_tree_gate, git_format, operation_acceptance
from app.facts_tree.broker_coordination import FINAL_ACCEPT, CoordinationError
from app.facts_tree.git_repository import CasResult


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


class FinalGateRefusal(ValueError):
    """Host input or fresh evidence does not authorize this acceptance."""


def _digest(effect):
    return sha256(git_format.dumps_record(effect)).hexdigest()


class BrokerFinalGate:
    def __init__(self, repository, coordination, store):
        if coordination.db is not store.db:
            raise ValueError("coordination and work store must share one connection")
        if repository._expected_meta["repo_id"] != coordination.repo_id:
            raise ValueError("coordination repository identity mismatch")
        self.repository = repository
        self.coordination = coordination
        self.store = store

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
            return FinalGateResult("rejected", reason=str(exc))

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
            return FinalGateResult("rejected", reason="Git evidence refused: " + str(exc))
        if replay is not None:
            return replay
        operation = store.get_operation(*scope)
        if operation is None or operation["current_state"] not in {"prepared", "reserved"}:
            return FinalGateResult("rejected", reason="operation requires fresh admission")
        # A previous round cannot be resumed by calling the fresh entrance again.
        if any(e["state"] == "in_flight" for e in store.list_operation_events(*scope)):
            return FinalGateResult("rejected", reason="prior final gate requires reconciliation")

        for round_index in (1, 2):
            attempt_id = uuid4().hex
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
                path = git_format.record_path("operation", operation_id=operation_id)
                op = git_format.loads_record(records[path])
                if (op.get("operation_id") != operation_id
                        or op.get("effect_digest") != digest
                        or op.get("base_commit") != head):
                    raise FinalGateRefusal("assembled operation binding differs")
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
                validate()
                with admission.work_transaction():
                    store.record_attempt(*scope, attempt_id, recorded_at, head,
                                         assembled.plan, effect, assembled.dependencies,
                                         object_format=repo.object_format,
                                         prepared_event_id=uuid4().hex, prepared_at=recorded_at)
                    store.admit_execution(*scope, attempt_id, uuid4().hex,
                                          uuid4().hex, recorded_at)
                if store.db.in_transaction:
                    raise FinalGateRefusal("intent transaction did not close")
                persisted = store.get_attempt(*scope, attempt_id)
                if (persisted is None or persisted["current_state"] != "accepting"
                        or persisted["base_commit"] != head
                        or persisted["effect_sha256"] != digest
                        or store.get_operation(*scope)["current_state"] != "in_flight"):
                    raise FinalGateRefusal("paired intent was not persisted")
                intent = True
                validate()
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
                origin, snapshot = self._evidence(successor, operation_id)
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
                    return FinalGateResult("unknown", attempt_id=attempt_id, reason=str(exc))
                if cas_started:  # defensive: no CAS may precede durable intent
                    raise
                return FinalGateResult("rejected", reason=str(exc))
        raise AssertionError("two-round budget escaped")
