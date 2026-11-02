"""Standalone append-only recovery journal. No startup/host/Broker integration.

The caller supplies an existing, private isolated root. Only recovery-state.sqlite3
inside it is opened; no business database or environment defaults are consulted.
Queries replay every event, verify canonical rows/digest chain/schema/watermark,
and fail closed with a quarantined safe_state on damage. Digests detect storage
inconsistency, not malicious coherent rewrites or whole-file rollback. Persist a
trusted external journal anchor and epoch history on the host for that threat.

Milestone proofs and gate decisions are references to trusted-host checks; this
module does not execute or authenticate them. Advancing a phase opens no gates.
Effects require separate per-operation decisions. External egress stays closed:
this slice has no authoritative external reconciliation/rotation implementation.
A newer epoch in this journal closes older cases locally; it cannot constrain
an old host using a separate journal. Views are snapshots, never execution tokens.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .recovery_epoch import (
    EvidenceKey, EvidenceState, IndependentEvidence, MergeDecision,
    OwnerRecoveryAuthentication, PHASES, PHASE_PROOFS, RecoveryEpoch,
    RecoveryPhase, merge_evidence, require_digest, require_label,
    require_sequence, validate_epoch, validate_transition,
)


_SCHEMA = """
CREATE TABLE recovery_events (
    seq INTEGER PRIMARY KEY CHECK (seq > 0),
    case_id TEXT NOT NULL,
    command TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    row_digest TEXT NOT NULL
);
CREATE TABLE recovery_watermark (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 0)
);
INSERT INTO recovery_watermark VALUES (1, 0);
CREATE TRIGGER recovery_events_no_update BEFORE UPDATE ON recovery_events BEGIN
    SELECT RAISE(ABORT, 'recovery events are append-only');
END;
CREATE TRIGGER recovery_events_no_delete BEFORE DELETE ON recovery_events BEGIN
    SELECT RAISE(ABORT, 'recovery events are append-only');
END;
CREATE TRIGGER recovery_events_no_replace BEFORE INSERT ON recovery_events
WHEN EXISTS (SELECT 1 FROM recovery_events WHERE seq = NEW.seq) BEGIN
    SELECT RAISE(ABORT, 'recovery events cannot be replaced');
END;
CREATE TRIGGER recovery_events_next BEFORE INSERT ON recovery_events
WHEN NEW.seq != (SELECT seq + 1 FROM recovery_watermark WHERE singleton = 1)
 OR NEW.seq != (SELECT coalesce(max(seq), 0) + 1 FROM recovery_events)
 OR NEW.previous_digest != coalesce(
    (SELECT row_digest FROM recovery_events ORDER BY seq DESC LIMIT 1), '') BEGIN
    SELECT RAISE(ABORT, 'recovery event sequence or chain mismatch');
END;
CREATE TRIGGER recovery_watermark_increment BEFORE UPDATE ON recovery_watermark
WHEN NEW.singleton != OLD.singleton OR typeof(NEW.seq) != 'integer'
 OR NEW.seq != OLD.seq + 1
 OR NEW.seq != (SELECT max(seq) FROM recovery_events) BEGIN
    SELECT RAISE(ABORT, 'recovery watermark must increase by exactly one event');
END;
CREATE TRIGGER recovery_watermark_no_delete BEFORE DELETE ON recovery_watermark BEGIN
    SELECT RAISE(ABORT, 'recovery watermark cannot be deleted');
END;
CREATE TRIGGER recovery_watermark_no_replace BEFORE INSERT ON recovery_watermark
WHEN EXISTS (SELECT 1 FROM recovery_watermark WHERE singleton = NEW.singleton) BEGIN
    SELECT RAISE(ABORT, 'recovery watermark cannot be replaced');
END;
"""


def _schema(db):
    return tuple(db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"))


with sqlite3.connect(":memory:") as _reference:
    _reference.executescript(_SCHEMA)
    _EXPECTED_SCHEMA = _schema(_reference)
_reference.close()
del _reference


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _row_digest(seq, case_id, command, previous):
    return hashlib.sha256(b"sam-recovery-state-row-v1\0" +
                          _canonical([seq, case_id, command, previous]).encode("ascii")).hexdigest()


def _time(value):
    if type(value) is not str:
        raise ValueError("invalid recovery time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo != timezone.utc or parsed.isoformat(timespec="microseconds") != value:
        raise ValueError("recovery time must be canonical UTC")
    return parsed


@dataclass(frozen=True)
class GateDecision:
    gate: str
    decision_digest: str
    operation_id: str | None = None

    def __post_init__(self):
        if type(self.gate) is not str or self.gate not in {"read", "tasks", "effects"}:
            raise ValueError("unsupported recovery gate")
        require_digest(self.decision_digest)
        if self.gate == "effects":
            require_label(self.operation_id)
        elif self.operation_id is not None:
            raise ValueError("only effects have operation decisions")


@dataclass(frozen=True)
class RecoveryView:
    case_id: str | None = None
    epoch: RecoveryEpoch | None = None
    backup_id: str | None = None
    barrier_digest: str | None = None
    phase: RecoveryPhase = RecoveryPhase.QUARANTINED
    proofs: tuple[tuple[str, str], ...] = ()
    authentication: OwnerRecoveryAuthentication | None = None
    merges: tuple[MergeDecision, ...] = ()
    decisions: tuple[GateDecision, ...] = ()
    updated_at: str | None = None
    is_current_epoch: bool = False

    @property
    def admission_category(self) -> str:
        if self.phase == RecoveryPhase.QUARANTINED:
            return "quarantined"
        if PHASES.index(self.phase) < PHASES.index(RecoveryPhase.READONLY_OPEN):
            return "evaluation"
        return {RecoveryPhase.READONLY_OPEN: "readonly", RecoveryPhase.TASKS_OPEN: "tasks",
                RecoveryPhase.EFFECTS_OPEN: "effects"}[self.phase]

    @property
    def read_open(self) -> bool:
        return self.is_current_epoch and any(d.gate == "read" for d in self.decisions)

    @property
    def tasks_open(self) -> bool:
        return self.read_open and not self.pending_count and any(d.gate == "tasks" for d in self.decisions)

    @property
    def effect_operations(self) -> tuple[str, ...]:
        # Later restrictive evidence closes an earlier decision, never clears it.
        denied = {m.key.operation_id for m in self.merges if m.restrictions}
        if not self.tasks_open or self.pending_count:
            return ()
        return tuple(d.operation_id for d in self.decisions
                     if d.gate == "effects" and d.operation_id not in denied)

    @property
    def effects_open(self) -> bool:
        return bool(self.effect_operations)

    @property
    def external_open(self) -> bool:
        return False

    @property
    def pending_count(self) -> int:
        return sum(m.pending for m in self.merges)

    @property
    def evidence_gaps(self) -> tuple[str, ...]:
        present = dict(self.proofs)
        gaps = [name for names in PHASE_PROOFS for name in names if name not in present]
        if self.epoch is not None and not self.is_current_epoch:
            gaps.append("superseded_epoch")
        if self.authentication is None:
            gaps.append("owner_recovery_authentication")
        if self.pending_count:
            gaps.append("unresolved_evidence")
        # Deliberately never claim old-host isolation/external rotation is done.
        gaps.append("external_isolation_and_rotation_unverified")
        return tuple(gaps)


class RecoveryStateError(RuntimeError):
    """Storage errors carry only a fixed message and an all-closed safe view."""
    safe_state = RecoveryView()

    def __init__(self):
        super().__init__("recovery state unavailable; quarantined")


def _keys(command, expected):
    if type(command) is not dict or set(command) != set(expected.split()):
        raise ValueError("invalid recovery command")


def _evidence(value):
    return IndependentEvidence(**{**value, "key": EvidenceKey(**value["key"]),
                                  "state": EvidenceState(value["state"])})


def _inherit_merges(states):
    merged = {}
    for state in states.values():
        for item in state.merges:
            merged[item.key] = merge_evidence(item.key, item.backup_states, item.evidence,
                                              merged.get(item.key, item))
    return tuple(sorted(merged.values(), key=lambda m: m.key))


def _apply(case_id, command, current, epochs, inherited):
    require_label(case_id)
    if type(command) is not dict:
        raise ValueError("invalid recovery command")
    now = _time(command.get("at"))
    kind = command.get("kind")
    if kind == "create":
        _keys(command, "kind at epoch backup_id barrier_digest prior_epochs")
        epoch = RecoveryEpoch(**command["epoch"])
        prior = tuple(RecoveryEpoch(**item) for item in command["prior_epochs"])
        validate_epoch(epoch, prior)
        validate_epoch(epoch, epochs)
        if current is not None:
            raise ValueError("recovery case already exists")
        require_label(command["backup_id"])
        require_digest(command["barrier_digest"])
        return RecoveryView(case_id, epoch, command["backup_id"], command["barrier_digest"],
                            merges=inherited, updated_at=command["at"], is_current_epoch=True)
    if current is None:
        raise ValueError("unknown recovery case")
    if not current.is_current_epoch and kind != "merge":
        raise ValueError("superseded recovery epoch")
    if now < _time(current.updated_at):
        raise ValueError("recovery event time rollback")
    updated = replace(current, updated_at=command["at"])
    if kind == "transition":
        _keys(command, "kind at target proofs")
        supplied = tuple(tuple(pair) for pair in command["proofs"])
        # Proofs can only be appended; no replacement of prior decisions.
        old = dict(current.proofs)
        for name, digest in supplied:
            if name in old and old[name] != digest:
                raise ValueError("recovery proof conflict")
        proofs = tuple(dict.fromkeys((*current.proofs, *supplied)))
        target = RecoveryPhase(command["target"])
        validate_transition(current.phase, target, proofs,
                            current.authentication is not None)
        return replace(updated, phase=target, proofs=proofs)
    if kind == "authenticate":
        _keys(command, "kind at authentication")
        auth = OwnerRecoveryAuthentication(**command["authentication"])
        if not auth.matches(current.epoch):
            raise ValueError("recovery authentication epoch mismatch")
        if current.authentication is not None and current.authentication != auth:
            raise ValueError("recovery authentication conflict")
        return replace(updated, authentication=auth)
    if kind == "merge":
        _keys(command, "kind at key backup evidence")
        key = EvidenceKey(**command["key"])
        previous = next((m for m in current.merges if m.key == key), None)
        merged = merge_evidence(key, tuple(EvidenceState(s) for s in command["backup"]),
                                tuple(_evidence(item) for item in command["evidence"]), previous)
        others = tuple(m for m in current.merges if m.key != key)
        return replace(updated, merges=tuple(sorted((*others, merged), key=lambda m: m.key)))
    if kind == "gate":
        _keys(command, "kind at decision")
        decision = GateDecision(**command["decision"])
        minimum = {"read": RecoveryPhase.READONLY_OPEN, "tasks": RecoveryPhase.TASKS_OPEN,
                   "effects": RecoveryPhase.EFFECTS_OPEN}[decision.gate]
        if PHASES.index(current.phase) < PHASES.index(minimum):
            raise ValueError("recovery gate phase prerequisite missing")
        if decision.gate != "read" and (not current.read_open or current.pending_count):
            raise ValueError("recovery gate evidence prerequisite missing")
        if decision.gate == "effects":
            if not current.tasks_open or any(m.key.operation_id == decision.operation_id
                                            for m in current.merges):
                raise ValueError("recovery effect has tombstone or missing task gate")
        if any((d.gate, d.operation_id) == (decision.gate, decision.operation_id)
               for d in current.decisions):
            raise ValueError("recovery gate decision already recorded")
        return replace(updated, decisions=(*current.decisions, decision))
    raise ValueError("invalid recovery command")


class RecoveryStateStore:
    """Each connection owns its transactions. Reopening never repairs damaged DDL.

    trusted_anchor is an optional (watermark, head digest) supplied by the host.
    It pins the exact journal at construction; callers must update their external
    anchor after writes. Trusted epoch history is required on case creation.
    """
    def __init__(self, isolated_root: Path, *, trusted_anchor: tuple[int, str] | None = None):
        root = Path(isolated_root)
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise ValueError("an existing isolated absolute recovery root is required")
        self.path = root / "recovery-state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("recovery state symlink refused")
        if trusted_anchor is not None:
            if type(trusted_anchor) is not tuple or len(trusted_anchor) != 2:
                raise ValueError("invalid recovery journal anchor")
            require_sequence(trusted_anchor[0])
            if trusted_anchor[0]:
                require_digest(trusted_anchor[1])
            elif trusted_anchor[1] != "":
                raise ValueError("invalid recovery journal anchor")
        self._db = None
        failed = False
        try:
            try:
                with self.path.open("xb"):
                    pass
                fresh = True
            except FileExistsError:
                fresh = False
            self._db = sqlite3.connect(self.path, isolation_level=None)
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA synchronous=FULL")
            if fresh:
                self._db.executescript("BEGIN IMMEDIATE;" + _SCHEMA + "COMMIT;")
            with self._snapshot():
                _, anchor = self._replay()
                if trusted_anchor is not None and anchor != trusted_anchor:
                    raise ValueError("recovery anchor mismatch")
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            failed = True
        if failed:
            self.close()
            raise RecoveryStateError()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _snapshot(self):
        return _Transaction(self._db, write=False)

    def _replay(self):
        if _schema(self._db) != _EXPECTED_SCHEMA:
            raise ValueError("recovery schema mismatch")
        if self._db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("recovery integrity mismatch")
        states = {}
        epochs = ()
        previous = ""
        count = 0
        for seq, case_id, blob, parent, digest in self._db.execute(
                "SELECT seq, case_id, command, previous_digest, row_digest FROM recovery_events ORDER BY seq"):
            count += 1
            if seq != count or parent != previous or digest != _row_digest(seq, case_id, blob, parent):
                raise ValueError("recovery history mismatch")
            command = json.loads(blob)
            if _canonical(command) != blob:
                raise ValueError("recovery canonical row mismatch")
            state = _apply(case_id, command, states.get(case_id), epochs, _inherit_merges(states))
            if command["kind"] == "create":
                epochs += (state.epoch,)
            states[case_id] = state
            inherited = _inherit_merges(states)
            latest = epochs[-1].ordinal
            states = {key: replace(value, merges=inherited,
                                   is_current_epoch=value.epoch.ordinal == latest)
                      for key, value in states.items()}
            previous = digest
        if self._db.execute("SELECT singleton, seq FROM recovery_watermark").fetchall() != [(1, count)]:
            raise ValueError("recovery watermark mismatch")
        return states, (count, previous)

    def _read(self):
        failed = False
        try:
            with self._snapshot():
                result = self._replay()
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            failed = True
        if failed:
            raise RecoveryStateError()
        return result

    def get(self, case_id: str) -> RecoveryView:
        require_label(case_id)
        states, _ = self._read()
        if case_id not in states:
            raise ValueError("unknown recovery case")
        return states[case_id]

    def journal_anchor(self) -> tuple[int, str]:
        return self._read()[1]

    def _record(self, case_id, command):
        require_label(case_id)
        _time(command["at"])
        # Canonical serialization also detaches mutable caller inputs.
        command = json.loads(_canonical(command))
        failed = False
        result = None
        try:
            with _Transaction(self._db, write=True):
                try:
                    states, (seq, previous) = self._replay()
                except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    raise RecoveryStateError() from None
                current = states.get(case_id)
                if command["kind"] == "create" and current is not None:
                    # Reopening the same case is a zero-write replay even after
                    # advancement. Compare original metadata, not current phase.
                    original = json.loads(self._db.execute(
                        "SELECT command FROM recovery_events WHERE case_id=? ORDER BY seq LIMIT 1",
                        (case_id,)).fetchone()[0])
                    if {k: v for k, v in original.items() if k != "at"} != {
                            k: v for k, v in command.items() if k != "at"}:
                        raise ValueError("recovery case definition conflict")
                    return current
                epochs = tuple(s.epoch for s in states.values())
                result = _apply(case_id, command, current, epochs, _inherit_merges(states))
                blob = _canonical(command)
                digest = _row_digest(seq + 1, case_id, blob, previous)
                self._db.execute("INSERT INTO recovery_events VALUES (?, ?, ?, ?, ?)",
                                 (seq + 1, case_id, blob, previous, digest))
                self._db.execute("UPDATE recovery_watermark SET seq=seq+1 WHERE singleton=1")
        except sqlite3.Error:
            failed = True
        if failed:
            raise RecoveryStateError()
        return result

    def create_case(self, case_id: str, epoch: RecoveryEpoch, *, backup_id: str,
                    barrier_digest: str, prior_epochs: tuple[RecoveryEpoch, ...], at: str) -> RecoveryView:
        if type(epoch) is not RecoveryEpoch or type(prior_epochs) is not tuple:
            raise ValueError("invalid epoch history")
        validate_epoch(epoch, prior_epochs)
        return self._record(case_id, dict(kind="create", at=at, epoch=asdict(epoch),
                            backup_id=backup_id, barrier_digest=barrier_digest,
                            prior_epochs=[asdict(item) for item in prior_epochs]))

    def transition(self, case_id: str, target: RecoveryPhase, *,
                   proofs: tuple[tuple[str, str], ...], at: str) -> RecoveryView:
        if type(target) is not RecoveryPhase:
            raise ValueError("invalid recovery phase")
        return self._record(case_id, dict(kind="transition", at=at, target=target.value, proofs=proofs))

    def authenticate(self, case_id: str, authentication: OwnerRecoveryAuthentication, *, at: str) -> RecoveryView:
        if type(authentication) is not OwnerRecoveryAuthentication:
            raise ValueError("invalid recovery authentication")
        return self._record(case_id, dict(kind="authenticate", at=at, authentication=asdict(authentication)))

    def merge(self, case_id: str, key: EvidenceKey, *, backup: tuple[EvidenceState, ...],
              evidence: tuple[IndependentEvidence, ...], at: str) -> RecoveryView:
        # Validate before serialization so raw strings cannot masquerade as enums.
        merge_evidence(key, backup, evidence)
        return self._record(case_id, dict(kind="merge", at=at, key=asdict(key),
                                         backup=backup, evidence=[asdict(item) for item in evidence]))

    def open_gate(self, case_id: str, decision: GateDecision, *, at: str) -> RecoveryView:
        if type(decision) is not GateDecision:
            raise ValueError("invalid recovery gate decision")
        return self._record(case_id, dict(kind="gate", at=at, decision=asdict(decision)))


class _Transaction:
    def __init__(self, db, *, write):
        self.db, self.write = db, write

    def __enter__(self):
        if self.db is None:
            raise RecoveryStateError()
        if self.db.in_transaction:
            raise ValueError("recovery store requires an idle connection")
        self.db.execute("BEGIN IMMEDIATE" if self.write else "BEGIN")

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.execute("COMMIT" if self.write and exc_type is None else "ROLLBACK")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.rollback()
            raise
