"""Single-host coordination primitives for F-001 Broker design sections 3/7.

TRUST BOUNDARY: host-internal API, never a worker-facing authorization API.
The host must route *every* registered participant for one repo through one
canonical, private directory on a local filesystem supporting flock, SQLite
transactions and durable fsync. Only the Broker/controlled executors may have
ref-write permission; this module neither grants nor revokes OS permissions.
It provides single-host exclusion, NOT independent multi-host isolation.

Lock order is maintenance admission -> repo acceptance gate -> host work DB
transaction. Thread-local ranks reject nesting/reversal across instances too;
flock serializes threads, processes and separate instances. Every entrance also
rejects a pre-existing transaction on the bound host connection. Other services
must use this same connection and ordering; arbitrary SQL on other untracked
connections cannot be detected. F-007 public APIs own their transactions, so
they must NOT be called inside work_transaction; final same-connection F-007
verification is a later integration. OperationWorkStore can join this transaction.

The singleton file descriptor lives with BrokerLease. Before launching an
actual CAS executor, register_executor durably records an unresolved fence,
then calls a trusted launcher and retains its process handle. Register the
actual update-ref executor, never just a shell/wrapper that can exit before
its writing descendants. A launcher must not detach unregistered writers.
An unresolved registration fences ALL ordinary participants, including after
the parent dies or its locks close. Thus the persistent owner/executor fence
extends the logical critical section beyond the lifetime of Python contexts
and OS lock descriptors. Exiting a context, a PID, elapsed time or a launcher
exception never clears it. Recovery alone can record observed termination or
host-proven isolation of that executor AND all possible writing descendants.
Isolation/remote termination verifiers are trusted host evidence adapters, not
user booleans or this module's own process-tree/permission verification.

The private SQLite ledger is coordination metadata, not operation/attempt
state, acceptance proof, projection or backup. Checksums detect accidental
damage, not a malicious administrator or whole-directory rollback/deletion.
Do not delete/reinitialize the directory to recover. Unavailable, missing or
damaged ledger/anchor/audit data fail closed. Audit admission/completion is
durable but is not atomic with host callback storage: a crash can leave an
admitted event without completion, which later reconciliation must inspect.
No Git, company DB, HTTP, model, business validation or timeout takeover lives
here. Opaque detail strings must contain no credentials or private reasoning.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading


# Fixed coordination protocol labels from section 3.1, not business decisions.
FINAL_ACCEPT = "final_accept"
AUTHORIZATION_CHANGE = "authorization_change"
AUTHORIZATION_REVOKE = "authorization_revoke"
MEMBERSHIP_CHANGE = "membership_change"
MEMBERSHIP_REVOKE = "membership_revoke"
TASK_CANCEL = "task_cancel"
TOKEN_TRANSFER = "token_transfer"
CANDIDATE_REVISE = "candidate_revise"
CANDIDATE_WITHDRAW = "candidate_withdraw"
COVERAGE_INVALIDATE = "coverage_invalidate"
APPROVAL_VALIDATE = "approval_validate"
APPROVAL_BIND = "approval_bind"
BACKUP_BARRIER = "backup_barrier"
GIT_GC = "git_gc"
EXTERNAL_OBJECT_GC = "external_object_gc"
PARTICIPANTS = frozenset({
    FINAL_ACCEPT, AUTHORIZATION_CHANGE, AUTHORIZATION_REVOKE,
    MEMBERSHIP_CHANGE, MEMBERSHIP_REVOKE, TASK_CANCEL, TOKEN_TRANSFER,
    CANDIDATE_REVISE, CANDIDATE_WITHDRAW, COVERAGE_INVALIDATE,
    APPROVAL_VALIDATE, APPROVAL_BIND, BACKUP_BARRIER, GIT_GC,
    EXTERNAL_OBJECT_GC,
})
_SAFE = frozenset({"terminated", "isolated"})
_LOCAL = threading.local()


class CoordinationError(Exception):
    """Coordination was denied; no acceptance is implied."""


class UnregisteredParticipant(CoordinationError):
    """The operation is outside the fixed participant registry."""


class LockOrderError(CoordinationError):
    """A caller holds a transaction or requests a reversed/nested lock."""


class CoordinationStateError(CoordinationError):
    """Current coordination state cannot be established reliably."""


class BrokerOccupied(CoordinationError):
    """A Broker or unresolved actual executor still owns the domain."""


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("expected nonempty host text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("expected valid host text") from None
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutorRecord:
    owner_id: str
    executor_id: str
    state: str
    pid: int | None
    evidence: str | None


class BrokerLease:
    """Process-bound singleton handle; close never erases unresolved writers."""

    def __init__(self, domain, owner_id, generation, fd):
        self.domain = domain
        self.owner_id = owner_id
        self.generation = generation
        self._fd = fd
        self._pid = os.getpid()

    def close(self):
        if self._fd is None:
            return
        if os.getpid() != self._pid:
            raise BrokerOccupied("lease belongs to another process")
        # Keep the descriptor when called in reverse order, so callers can
        # retry close after leaving their current coordinated scope.
        with self.domain._gates():
            try:
                state = self.domain._load()
                self.domain._require_lease(self, state)
                unresolved = self.domain._unresolved(state)
                if not unresolved:
                    state["owner"] = None
                self.domain._save(state, "broker_abandoned" if unresolved
                                  else "broker_released", owner=self.owner_id)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Admission:
    """A live, thread-bound participant admission; never acceptance proof."""

    def __init__(self, domain, operation, lease):
        self.domain = domain
        self.operation = operation
        self.lease = lease
        self._thread = threading.get_ident()
        self._pid = os.getpid()
        self._active = True
        self._committed = False

    def _check(self):
        if (not self._active or self._thread != threading.get_ident()
                or self._pid != os.getpid()):
            raise LockOrderError("admission is no longer held by this thread")

    @contextmanager
    def work_transaction(self):
        self._check()
        self._committed = False
        with self.domain.work_transaction() as db:
            yield db
        self._committed = True

    def register_executor(self, executor_id, launch):
        """Persist a fence BEFORE launch(); keep returned actual-process handle.

        Call only after the fresh host intent transaction commits. launch is
        trusted host code starting precisely the actual writer, with no network
        or model wait. Even if it spawns and then raises, the fence remains.
        """
        self._check()
        if (self.operation != FINAL_ACCEPT or not self._committed
                or self.domain.db.in_transaction):
            raise LockOrderError("executor requires committed final-gate intent")
        _text(executor_id)
        if not callable(launch):
            raise TypeError("expected trusted executor launcher")
        state = self.domain._load()
        self.domain._require_lease(self.lease, state)
        if executor_id in state["executors"] or self.domain._unresolved(state):
            raise BrokerOccupied("executor registration is already occupied")
        row = {"owner_id": self.lease.owner_id, "executor_id": executor_id,
               "state": "registered", "pid": None, "evidence": None}
        state["executors"][executor_id] = row
        self.domain._save(state, "executor_registered", operation=self.operation,
                          owner=self.lease.owner_id, executor=executor_id)
        handle = launch()
        if (not callable(getattr(handle, "poll", None))
                or type(getattr(handle, "pid", None)) is not int
                or handle.pid <= 0):
            raise CoordinationStateError("actual executor handle unavailable")
        self.domain._handles[executor_id] = handle
        row["pid"] = handle.pid
        row["state"] = "running"
        self.domain._save(state, "executor_attached", operation=self.operation,
                          owner=self.lease.owner_id, executor=executor_id)
        return ExecutorRecord(**row)

    def confirm_executor_terminated(self, executor_id):
        self._check()
        if self.lease is None:
            raise BrokerOccupied("executor requires its Broker lease")
        return self.domain._resolve_executor(self.lease.owner_id, executor_id,
                                              "terminated", None)


class BrokerCoordination:
    """One canonical repo domain bound to one host SQLite work connection.

    Independent threads use their own connection/domain instance pointing to
    the SAME directory. Construct while idle, before entering any host locks.
    The two lock-stage methods are ordering primitives; use admit for actual
    participant work so durable state checks and audit cannot be skipped.
    """

    def __init__(self, directory, repo_id, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("expected host SQLite connection")
        self.db = db
        self.repo_id = _text(repo_id)
        self.directory = Path(directory).resolve()
        self.database_path = self.directory / "coordination.sqlite"
        self._anchor = self.directory / "initialized"
        self._handles = {}
        self._idle()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._gates():
            if (not self._anchor.exists() and not self.database_path.exists()
                    and not self._anchor.is_symlink()
                    and not self.database_path.is_symlink()):
                # Anchor is durable first: interrupted initialization refuses
                # on reopen instead of silently inventing a fresh owner.
                fd = os.open(self._anchor, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
                try:
                    os.write(fd, _canonical({"repo_id": repo_id}).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                with closing(sqlite3.connect(self.database_path)) as ledger:
                    with ledger:
                        ledger.execute("PRAGMA synchronous=FULL")
                        ledger.execute("CREATE TABLE state (id INTEGER PRIMARY KEY "
                                       "CHECK(id=1), payload TEXT, sha256 TEXT)")
                        ledger.execute("CREATE TABLE audit (seq INTEGER PRIMARY KEY, "
                                       "payload TEXT, sha256 TEXT)")
                        payload = _canonical({"version": 1, "repo_id": repo_id,
                                              "generation": 0, "owner": None,
                                              "executors": {}, "seq": 0,
                                              "tail": "0" * 64})
                        ledger.execute("INSERT INTO state VALUES (1, ?, ?)",
                                       (payload, _digest(payload)))
            self._load()

    def _idle(self):
        if self.db.in_transaction:
            raise LockOrderError("host transaction precedes coordination locks")
        if getattr(_LOCAL, "frames", []):
            raise LockOrderError("nested or reversed coordination entrance")

    @contextmanager
    def _file_lock(self, name, rank):
        frames = getattr(_LOCAL, "frames", [])
        if self.db.in_transaction or (rank == 1 and frames) or (
                rank == 2 and frames != [(self.directory, 1)]):
            raise LockOrderError("maintenance -> repo gate -> work transaction required")
        fd = os.open(self.directory / name,
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _LOCAL.frames = frames + [(self.directory, rank)]
            try:
                yield
            finally:
                _LOCAL.frames = frames
        finally:
            os.close(fd)

    def maintenance_admission(self):
        return self._file_lock("maintenance.lock", 1)

    def repo_acceptance_gate(self):
        return self._file_lock("acceptance.lock", 2)

    @contextmanager
    def work_transaction(self):
        """Own a fresh transaction; callbacks must not commit/rollback it.

        Storage callbacks may join via SAVEPOINT, as OperationWorkStore does.
        Their effects must be confined to this DB; audit is a separate ledger.
        Explicit SQL also supports Python connections with autocommit=True.
        """
        if (self.db.in_transaction or getattr(_LOCAL, "frames", []) != [
                (self.directory, 1), (self.directory, 2)]):
            raise LockOrderError("work transaction requires ordered repo gate")
        frames = _LOCAL.frames
        _LOCAL.frames = frames + [(self.directory, 3)]
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                if not self.db.in_transaction:
                    raise CoordinationStateError("callback closed the host transaction")
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        finally:
            _LOCAL.frames = frames

    @contextmanager
    def _gates(self):
        self._idle()
        with self.maintenance_admission(), self.repo_acceptance_gate():
            try:
                yield
            finally:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                    raise LockOrderError("host transaction escaped coordinated scope")

    @contextmanager
    def _ledger(self):
        try:
            if (self._anchor.is_symlink() or self.database_path.is_symlink()
                    or self._anchor.read_text() != _canonical({"repo_id": self.repo_id})
                    or not self.database_path.is_file()):
                raise CoordinationStateError("coordination ledger or anchor unavailable")
            ledger = sqlite3.connect(self.database_path.as_uri() + "?mode=rw", uri=True)
            try:
                ledger.execute("PRAGMA synchronous=FULL")
                yield ledger
            finally:
                ledger.close()
        except (sqlite3.Error, OSError, UnicodeError):
            raise CoordinationStateError("coordination ledger unavailable or damaged") from None

    def _load(self):
        with self._ledger() as ledger:
            rows = ledger.execute("SELECT id, payload, sha256 FROM state").fetchall()
            audits = ledger.execute("SELECT seq, payload, sha256 FROM audit ORDER BY seq").fetchall()
        try:
            if len(rows) != 1 or rows[0][0] != 1:
                raise ValueError
            payload, checksum = rows[0][1:]
            state = json.loads(payload)
            if (payload != _canonical(state) or checksum != _digest(payload)
                    or set(state) != {"version", "repo_id", "generation", "owner",
                                      "executors", "seq", "tail"}
                    or type(state["version"]) is not int or state["version"] != 1
                    or state["repo_id"] != self.repo_id
                    or type(state["generation"]) is not int or state["generation"] < 0
                    or type(state["seq"]) is not int or state["seq"] < 0
                    or not isinstance(state["executors"], dict)):
                raise ValueError
            if state["owner"] is not None:
                _text(state["owner"])
                if state["generation"] == 0:
                    raise ValueError
            for key, row in state["executors"].items():
                if set(row) != {"owner_id", "executor_id", "state", "pid", "evidence"}:
                    raise ValueError
                _text(key)
                _text(row["owner_id"])
                if key != row["executor_id"] or row["state"] not in {
                        "registered", "running", "terminated", "isolated"}:
                    raise ValueError
                if row["pid"] is not None and (type(row["pid"]) is not int or row["pid"] <= 0):
                    raise ValueError
                if row["state"] in _SAFE:
                    _text(row["evidence"])
                elif (row["evidence"] is not None or row["owner_id"] != state["owner"]):
                    raise ValueError
                if row["state"] == "running" and row["pid"] is None:
                    raise ValueError
            tail = "0" * 64
            for expected, (seq, event, checksum) in enumerate(audits, 1):
                parsed = json.loads(event)
                if (type(seq) is not int or seq != expected or event != _canonical(parsed)
                        or checksum != _digest(tail + event)):
                    raise ValueError
                tail = checksum
            if state["seq"] != len(audits) or state["tail"] != tail:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CoordinationStateError("coordination occupancy or audit is damaged") from None
        return state

    def _save(self, state, action, *, operation=None, owner=None,
              executor=None, detail=None):
        event = _canonical({"action": action, "operation": operation,
                            "owner": owner, "executor": executor, "detail": detail})
        seq = state["seq"] + 1
        tail = _digest(state["tail"] + event)
        updated = dict(state, seq=seq, tail=tail)
        payload = _canonical(updated)
        with self._ledger() as ledger:
            with ledger:
                ledger.execute("BEGIN IMMEDIATE")
                ledger.execute("INSERT INTO audit VALUES (?, ?, ?)", (seq, event, tail))
                ledger.execute("UPDATE state SET payload=?, sha256=? WHERE id=1",
                               (payload, _digest(payload)))
        state.update(seq=seq, tail=tail)

    @staticmethod
    def _unresolved(state):
        return any(row["state"] not in _SAFE for row in state["executors"].values())

    def _require_lease(self, lease, state):
        if (not isinstance(lease, BrokerLease) or lease.domain is not self
                or lease._fd is None or lease._pid != os.getpid()
                or lease.owner_id != state["owner"]
                or lease.generation != state["generation"]):
            raise BrokerOccupied("current process-bound Broker lease required")

    def claim_broker(self, *, takeover=False):
        """Claim once, or explicitly take over only fully resolved old writers.

        There is deliberately no timeout/expiry/force option. Takeover proves
        executor exclusion only, never unknown-operation re-admission (§7).
        """
        if type(takeover) is not bool:
            raise TypeError("expected explicit takeover flag")
        with self._gates():
            fd = os.open(self.directory / "broker.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BrokerOccupied("Broker singleton is still held") from None
                state = self._load()
                if self._unresolved(state) or (state["owner"] is not None and not takeover):
                    raise BrokerOccupied("old Broker/executor requires explicit recovery")
                state["owner"] = secrets.token_hex(24)
                state["generation"] += 1
                self._save(state, "broker_claimed", owner=state["owner"])
                lease = BrokerLease(self, state["owner"], state["generation"], fd)
            except BaseException:
                os.close(fd)
                raise
        return lease

    @contextmanager
    def admit(self, operation, *, lease=None, detail=None):
        if not isinstance(operation, str) or operation not in PARTICIPANTS:
            raise UnregisteredParticipant("operation is not a coordination participant")
        if detail is not None:
            _text(detail)
        with self._gates():
            state = self._load()
            if self._unresolved(state):
                raise BrokerOccupied("actual executor state is unresolved")
            if operation == FINAL_ACCEPT:
                self._require_lease(lease, state)
            admission = Admission(self, operation, lease)
            self._save(state, "admitted", operation=operation, detail=detail)
            try:
                yield admission
                if self.db.in_transaction:
                    raise LockOrderError("host transaction escaped participant scope")
            except BaseException:
                self._save(self._load(), "failed", operation=operation, detail=detail)
                raise
            else:
                self._save(self._load(), "completed", operation=operation, detail=detail)
            finally:
                admission._active = False
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")

    def _resolve_executor(self, owner_id, executor_id, outcome, prove):
        _text(owner_id)
        _text(executor_id)
        state = self._load()
        row = state["executors"].get(executor_id)
        if row is None or row["owner_id"] != owner_id or state["owner"] != owner_id:
            raise BrokerOccupied("executor occupation does not match current owner")
        if row["state"] in _SAFE:
            raise BrokerOccupied("executor already resolved")
        handle = self._handles.get(executor_id)
        if outcome == "terminated" and handle is not None:
            code = handle.poll()
            if type(code) is not int:
                raise BrokerOccupied("actual executor is not proven terminated")
            evidence = "observed actual executor exit: %d" % code
        else:
            if not callable(prove):
                raise BrokerOccupied("trusted host executor proof required")
            evidence = prove(ExecutorRecord(**row))
            _text(evidence)
        if self.db.in_transaction:
            raise LockOrderError("executor proof left a host transaction open")
        row["state"] = outcome
        row["evidence"] = evidence
        self._save(state, "executor_" + outcome, owner=owner_id, executor=executor_id,
                   detail=evidence)
        return ExecutorRecord(**row)

    def confirm_executor_terminated(self, owner_id, executor_id, *, prove=None):
        """Observe the actual retained handle, or request trusted host evidence.

        A recovered host adapter must prove actual executor/descendant exit;
        checking the dead parent PID or a deadline is not such evidence.
        """
        with self._gates():
            return self._resolve_executor(owner_id, executor_id, "terminated", prove)

    def isolate_executor(self, owner_id, executor_id, *, prove):
        """Host verifier must establish loss of ref-write ability, including descendants."""
        with self._gates():
            return self._resolve_executor(owner_id, executor_id, "isolated", prove)

    def audit_records(self):
        with self._gates():
            self._load()
            with self._ledger() as ledger:
                return tuple(dict(seq=seq, **json.loads(payload)) for seq, payload in
                             ledger.execute("SELECT seq, payload FROM audit ORDER BY seq"))

    def _candidate_change(self, operation, callback, detail):
        if not callable(callback):
            raise TypeError("expected trusted storage callback")
        with self.admit(operation, detail=detail) as admission:
            with admission.work_transaction() as db:
                return callback(db)

    def revise_candidate(self, callback, *, detail=None):
        return self._candidate_change(CANDIDATE_REVISE, callback, detail)

    def withdraw_candidate(self, callback, *, detail=None):
        return self._candidate_change(CANDIDATE_WITHDRAW, callback, detail)

    def invalidate_coverage(self, callback, *, detail=None):
        return self._candidate_change(COVERAGE_INVALIDATE, callback, detail)
