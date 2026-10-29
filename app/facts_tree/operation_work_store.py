"""Runtime work-record primitive for F-001/CHG-064 operation/attempt records.

Approved design: CHG-064 (DEC-030), ``96-Commit-Broker提交与恢复设计.md``
sections 4 (准备记录与摘要分工), 7 (状态机、故障与恢复) and 9 (施工拆分);
F-001 P01 sections 10.2-10.5; F-007 prepare_effect/accept_effect state
names. This module is a small standalone SQLite persistence primitive for
the *runtime work store* only: it keeps the durable operation request
snapshot, its append-only state-event history, immutable attempt snapshots
and their append-only state-event history. Per P01 section 10 the runtime
work store is NOT the formal fact Git repository and NOT the rebuildable
query projection; a row here, including an ``accepted`` mirror event, only
means "the host durably recorded this work state", never that an operation
was formally accepted into the authoritative ref F.

The accepted-state caveat (CHG-064 section 7): ``accepted`` recorded here
is a work-store mirror, not proof of acceptance. Only the later Broker
reconciliation that finds the immutable operation content on the current
legal Git chain and locates the first accepting commit C may confirm it.
This module never performs that reconciliation: it stores the
host-supplied accepted_commit as an OID-shaped mirror reference without
checking any ref, chain or reachability, and a state record here is never
an authorization, an approval or a business success.

Record model
------------

- ``fact_operation_work_records``: one immutable request snapshot per
  (repo_id, company_id, operation_id). It additionally binds the
  host-provided task_id, idempotency key and host created_at string, and
  stores the opaque request object as canonical bytes plus its digest.
- ``fact_operation_work_events``: append-only operation state events with
  a store-assigned gapless seq per operation and a host event_id unique
  in the scope.
- ``fact_operation_attempts``: one immutable attempt snapshot per
  (repo_id, company_id, operation_id, attempt_id), binding the fixed
  base commit/object format, separate opaque plan and effect objects and
  the opaque precise dependency version snapshot, each canonicalized and
  digested.
- ``fact_operation_attempt_events``: append-only attempt state events with
  a store-assigned gapless seq per attempt and a host event_id unique in
  the scope. Accepted attempt/operation events carry accepted_commit,
  which is the only state that may carry it.

All identity values (repo_id, company_id, task_id, operation_id,
idempotency_key, attempt_id, event_id) are host-owned values passed as
separate arguments; they are never read out of the request, plan/effect,
dependency or detail objects, so a payload field named "company_id" (or
similar) can never set stored identity.

Canonical serialization and digests (documented choice): every content
digest is the SHA-256 hex digest of the exact canonical
``git_format.dumps_record`` (git_format FORMAT_VERSION = 1) bytes of the
host object: request_sha256 over request, plan_sha256 over plan,
effect_sha256 over effect, dependencies_sha256 over dependencies and
detail_sha256 over each event detail (None detail is recorded as the
canonical empty object {}). Callers never supply digests; the component
computes them. Each row additionally carries a row_sha256 checksum
binding every identity/metadata column together with the validated
content digests; it is an unkeyed drift checksum, NOT authentication, and
cannot protect against a malicious database administrator who can
recompute it.

Request identity (CHG-064 section 4). The request digest is immutable
*within one operation*: the same operation_id with different canonical
request content is rejected. Section 4 freezes nothing across different
operations, so this store does NOT make request_sha256 unique across a
(repo_id, company_id) scope: the identical canonical request may be
recorded under different operation_ids (and idempotency keys). What stays
unique is the operation identity (primary key) and the idempotency key;
the host decides when two operations genuinely share a request shape.

State machines. The operation edge set is exactly the finite table of
CHG-064 section 7 (F-007 state names):

    prepared -> approval_required | in_flight | rejected
    approval_required -> reserved
    reserved -> in_flight | rejected
    in_flight -> accepted | unknown
    unknown -> accepted | in_flight
    accepted and rejected are terminal

The approved design does not freeze attempt states beyond
accepting/accepted/unknown and the scheduling note on stale/retry_wait.
Implementation conventions of THIS store (not formal Git semantics):

- The first operation state is ``prepared`` and is recorded explicitly;
  between the request snapshot and that event the operation only carries
  its task input (CHG-064 section 5 step 1: "尚未形成准确方案时只登记任务
  输入"). The first attempt state is ``prepared``; its event is inserted
  atomically with the immutable attempt snapshot, so an attempt row can
  never exist without its prepared event.
- Attempt edges:
      prepared -> accepting | stale
      accepting -> accepted | unknown
      unknown -> accepted | accepting
      accepted and stale are terminal
  There is deliberately no accepting -> stale edge and no unknown ->
  stale edge. ``stale`` means a *definite* pre-acceptance expiry of an
  attempt that never entered the final gate (CHG-064 section 7
  interruption rows: object persisted but C absent, candidate revised or
  coverage invalidated while Git H may not even have moved): it is
  reachable only from prepared, and only while the operation itself is
  still in prepared/approval_required/reserved. Once an attempt is
  accepting the operation is in_flight, whose only section-7 exits are
  the paired accepted/unknown outcomes; a gate-time H change or a CAS
  that is not a clear success is parked as unknown and recovered through
  the section-7 re-admission proof, never retroactively turned into a
  definite failure. ``retry_wait`` is deliberately not a stored state:
  section 7 assigns it to scheduling, and neither stale nor retry_wait
  may clear unknown.

Paired transitions are one call and one SAVEPOINT. CHG-064 section 7
defines three states only as joint operation/attempt outcomes, so this
store exposes no public single-side event append at all; the only write
entries are semantic transitions:

- ``mark_operation_prepared`` / ``mark_approval_required`` /
  ``mark_reserved`` / ``reject_operation`` append one operation event;
  they move states section 7 lets the operation reach alone, and each
  leaves a legal stable joint state.
- ``mark_attempt_stale`` appends one attempt event (prepared -> stale,
  operation still pre-flight).
- ``admit_execution`` writes BOTH the attempt accepting event and the
  operation in_flight event, in that order, inside one SAVEPOINT: from
  prepared/reserved the named attempt must currently be prepared; from
  unknown (same-operation re-admission) that same attempt must currently
  be unknown.
- ``mark_unknown`` writes BOTH the attempt unknown event and the
  operation in_flight -> unknown event, in that order, in one SAVEPOINT.
- ``mark_accepted`` writes BOTH the attempt accepted event (with the
  OID-shaped accepted_commit mirror) and the operation accepted event
  naming the same commit, in that order, in one SAVEPOINT; the current
  pair is (in_flight, accepting) or (unknown, unknown).

A failed paired call rolls back to its savepoint, so neither half is
visible or committable. Reads and ``verify_integrity`` replay BOTH event
chains structurally (the two event tables have independent rowid
counters, so cross-table rowid order is meaningless): the operation's
paired-state subsequence must match the single live attempt's
paired-state subsequence event by event (accepting/in_flight, unknown,
accepted with the same mirror commit), all other attempts must end
stale (or be the one still-prepared attempt of a pre-flight operation),
and the canonical legal interleaving the write path can produce must
pass through the stable joint states below at every prefix. There is no
committed moment at which an operation is in_flight while its attempt is
still prepared, an unknown operation has no unknown attempt, or an
accepted operation names a commit no accepted attempt named.

Stable joint states (current operation state vs every attempt's current
state):

    (no events)            no attempts
    prepared               attempts prepared or stale, at most one live
    approval_required      attempts prepared or stale, at most one live
    reserved               attempts prepared or stale, at most one live
    in_flight              exactly one attempt accepting, others stale
    unknown                exactly one attempt unknown, others stale
    accepted               exactly one attempt accepted (same commit),
                           others stale
    rejected               no attempt accepting or unknown

Cross-record admission conventions applied on the write path (they order
host calls; they do not verify any external fact):

- An attempt may be recorded only for an operation currently in
  prepared/approval_required/reserved, and only while the operation has
  no attempt still live (prepared/accepting/unknown). A retry therefore
  cannot dodge an unknown result by registering a new attempt: unknown
  is reconciled on the same operation and attempt.
- An unknown result is never rejected away: reject_operation is legal
  only from prepared/reserved with no accepting/unknown attempt.

What this module deliberately does NOT check (the host/Broker owns
these; no call or return value here claims they held):

- unknown -> in_flight re-admission additionally requires, per
  CHG-064 section 7, proof that the old CAS executor is stopped/isolated,
  that the complete current legal Git chain contains no accepted
  operation content, that current authorization and every immutable
  dependency are still valid, and that the original approval binding is
  retained. admit_execution records the paired mirror only; it verifies
  none of those.
- unknown/in_flight -> accepted additionally requires, per section 7,
  the operation content on the current complete legal chain and the
  first accepting commit C (never a dangling commit). mark_accepted
  records the host-supplied OID mirror; it verifies no Git fact.
- host identity/actor authentication, policy/delegation/revocation,
  approval binding or token storage, process isolation and old-Broker
  takeover, Git ref/CAS verification and legal-chain reconciliation,
  candidate revision or coverage validity, candidate consumption
  records, query projections, HTTP/API entry points, F-007/Broker
  wiring, app.db schema setup and feature gates.

Callers must never place session keys, approval tokens, credentials or
private model reasoning in request/plan/dependency/detail objects; the
store keeps them as opaque canonical content and cannot redact secrets
after the fact.

Transaction contract (same documented choice as the sibling
candidate_work_store / candidate_revision_store modules):

- The constructor takes only a caller-provided sqlite3.Connection. It
  never opens a database, never chooses a data path and never migrates
  app.db. While the connection is idle it creates its own tables, indexes
  and triggers; construction with an already-active caller transaction is
  refused.
- Every write REQUIRES an active caller transaction and joins it inside
  one SAVEPOINT: on success the savepoint is released; on any exception
  the store rolls back only to that savepoint, releases it and re-raises,
  so a failed call leaves no partial rows and the caller transaction
  stays open. This store never issues BEGIN/COMMIT/ROLLBACK against the
  caller transaction.
- Reads are read-only and never issue COMMIT; on an idle connection they
  open a short read snapshot and always close it with ROLLBACK, on success
  and on failure alike; inside a caller transaction they read through it
  and never close, commit or roll it back.

Tamper resistance: all four tables are append-only at the database level.
BEFORE UPDATE / BEFORE DELETE triggers reject any rewrite or removal, and
BEFORE INSERT triggers reject an insert whose primary or unique key, or
the operation's idempotency key, already exists (including INSERT OR
REPLACE, whose implicit delete does not fire the DELETE trigger while
recursive triggers are off); parent-existence triggers additionally
reject events or attempts without their recorded parent. The Python
checks stay in place; triggers are the last line of defence for SQL that
bypasses this module. Reads re-parse every blob through
git_format.loads_record, require the stored bytes to equal the exact
canonical git_format.dumps_record serialization of the parsed value
(parseable-with-matching-digest is not enough), re-verify every digest,
scope column and row checksum, and replay the joint operation/attempt
history; any drift is a hard failure, never a silent repair.
verify_integrity() runs the same checks over every stored row and chain.
"""
import hashlib
import sqlite3
from threading import RLock

from app.facts_tree import git_format

# -- states and the finite migration tables (CHG-064 section 7) -------------

OPERATION_PREPARED = "prepared"
OPERATION_APPROVAL_REQUIRED = "approval_required"
OPERATION_RESERVED = "reserved"
OPERATION_IN_FLIGHT = "in_flight"
OPERATION_ACCEPTED = "accepted"
OPERATION_UNKNOWN = "unknown"
OPERATION_REJECTED = "rejected"

_OPERATION_STATES = frozenset({
    OPERATION_PREPARED,
    OPERATION_APPROVAL_REQUIRED,
    OPERATION_RESERVED,
    OPERATION_IN_FLIGHT,
    OPERATION_ACCEPTED,
    OPERATION_UNKNOWN,
    OPERATION_REJECTED,
})

_OPERATION_EDGES = {
    OPERATION_PREPARED: frozenset({
        OPERATION_APPROVAL_REQUIRED, OPERATION_IN_FLIGHT,
        OPERATION_REJECTED}),
    OPERATION_APPROVAL_REQUIRED: frozenset({OPERATION_RESERVED}),
    OPERATION_RESERVED: frozenset({
        OPERATION_IN_FLIGHT, OPERATION_REJECTED}),
    OPERATION_IN_FLIGHT: frozenset({OPERATION_ACCEPTED, OPERATION_UNKNOWN}),
    # unknown -> in_flight is the section-7 same-operation re-admission.
    OPERATION_UNKNOWN: frozenset({OPERATION_ACCEPTED, OPERATION_IN_FLIGHT}),
    OPERATION_ACCEPTED: frozenset(),
    OPERATION_REJECTED: frozenset(),
}

ATTEMPT_PREPARED = "prepared"
ATTEMPT_ACCEPTING = "accepting"
ATTEMPT_ACCEPTED = "accepted"
ATTEMPT_UNKNOWN = "unknown"
ATTEMPT_STALE = "stale"

_ATTEMPT_STATES = frozenset({
    ATTEMPT_PREPARED,
    ATTEMPT_ACCEPTING,
    ATTEMPT_ACCEPTED,
    ATTEMPT_UNKNOWN,
    ATTEMPT_STALE,
})

_ATTEMPT_EDGES = {
    ATTEMPT_PREPARED: frozenset({ATTEMPT_ACCEPTING, ATTEMPT_STALE}),
    # An accepting attempt exits only through a paired section-7 outcome.
    # There is deliberately no accepting -> stale edge: while the final
    # gate is open, non-acceptance is not definite, and an expired gate is
    # parked as unknown and recovered through re-admission.
    ATTEMPT_ACCEPTING: frozenset({ATTEMPT_ACCEPTED, ATTEMPT_UNKNOWN}),
    # unknown -> accepting is the section-7 re-admission path; there is
    # deliberately no unknown -> stale edge: stale/retry_wait are
    # scheduling states after a DEFINITE non-acceptance and can never
    # clear an unknown result.
    ATTEMPT_UNKNOWN: frozenset({ATTEMPT_ACCEPTED, ATTEMPT_ACCEPTING}),
    ATTEMPT_ACCEPTED: frozenset(),
    ATTEMPT_STALE: frozenset(),
}

# Operation states before the final gate is ever opened. An attempt may be
# registered or expired (stale) only in one of these.
_PRE_FLIGHT_STATES = frozenset({
    OPERATION_PREPARED,
    OPERATION_APPROVAL_REQUIRED,
    OPERATION_RESERVED,
})

# Attempt states that still own the operation's execution: no new attempt
# may be recorded while one of these is the attempt's current state.
_ATTEMPT_LIVE_STATES = frozenset({
    ATTEMPT_PREPARED, ATTEMPT_ACCEPTING, ATTEMPT_UNKNOWN})

# Operation states reached ONLY together with the matching attempt event
# (attempt half first, operation half second, one SAVEPOINT).
_PAIRED_OPERATION_STATES = frozenset({
    OPERATION_IN_FLIGHT, OPERATION_UNKNOWN, OPERATION_ACCEPTED})

_OID_HEX_LENGTHS = {"sha1": 40, "sha256": 64}
_HEX_DIGITS = frozenset("0123456789abcdef")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_operation_work_records (
    repo_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_blob BLOB NOT NULL,
    request_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (repo_id, company_id, operation_id),
    UNIQUE (repo_id, company_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS fact_operation_work_events (
    repo_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL,
    state_commit TEXT,
    recorded_at TEXT NOT NULL,
    detail_blob BLOB NOT NULL,
    detail_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (repo_id, company_id, operation_id, seq),
    UNIQUE (repo_id, company_id, event_id),
    CHECK (state IN ('prepared', 'approval_required', 'reserved',
                    'in_flight', 'accepted', 'unknown', 'rejected')),
    CHECK ((state = 'accepted') = (state_commit IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS fact_operation_attempts (
    repo_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    object_format TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    plan_blob BLOB NOT NULL,
    plan_sha256 TEXT NOT NULL,
    effect_blob BLOB NOT NULL,
    effect_sha256 TEXT NOT NULL,
    deps_blob BLOB NOT NULL,
    deps_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (repo_id, company_id, operation_id, attempt_id),
    CHECK (object_format IN ('sha1', 'sha256'))
);

CREATE TABLE IF NOT EXISTS fact_operation_attempt_events (
    repo_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL,
    state_commit TEXT,
    recorded_at TEXT NOT NULL,
    detail_blob BLOB NOT NULL,
    detail_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (repo_id, company_id, operation_id, attempt_id, seq),
    UNIQUE (repo_id, company_id, event_id),
    CHECK (state IN ('prepared', 'accepting', 'accepted',
                    'unknown', 'stale')),
    CHECK ((state = 'accepted') = (state_commit IS NOT NULL))
);

-- Append-only enforcement at the database level: once an operation
-- request snapshot is recorded it is never rewritten or removed.
CREATE TRIGGER IF NOT EXISTS fact_operation_work_records_no_update
BEFORE UPDATE ON fact_operation_work_records
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_records is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_work_records_no_delete
BEFORE DELETE ON fact_operation_work_records
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_records is append-only: DELETE rejected');
END;
-- INSERT OR REPLACE on the primary key or idempotency key would otherwise
-- delete the original row; the implicit REPLACE delete does not fire the
-- DELETE trigger while recursive_triggers is off, so block it on the
-- insert side while the original row still exists. CHG-064 section 4
-- freezes request_sha256 only within one operation, so a repeated request
-- digest under a different operation id is deliberately NOT blocked.
CREATE TRIGGER IF NOT EXISTS fact_operation_work_records_no_replace
BEFORE INSERT ON fact_operation_work_records
WHEN EXISTS (
    SELECT 1 FROM fact_operation_work_records
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND (operation_id = NEW.operation_id
           OR idempotency_key = NEW.idempotency_key)
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_records is append-only: operation identity or idempotency key already recorded; refusing to overwrite');
END;

CREATE TRIGGER IF NOT EXISTS fact_operation_work_events_no_update
BEFORE UPDATE ON fact_operation_work_events
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_events is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_work_events_no_delete
BEFORE DELETE ON fact_operation_work_events
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_events is append-only: DELETE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_work_events_no_replace
BEFORE INSERT ON fact_operation_work_events
WHEN EXISTS (
    SELECT 1 FROM fact_operation_work_events
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND ((operation_id = NEW.operation_id AND seq = NEW.seq)
           OR event_id = NEW.event_id)
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_events is append-only: event seq or event_id already recorded; refusing to overwrite');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_work_events_require_parent
BEFORE INSERT ON fact_operation_work_events
WHEN NOT EXISTS (
    SELECT 1 FROM fact_operation_work_records
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND operation_id = NEW.operation_id
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_work_events requires a recorded operation in the same scope');
END;

CREATE TRIGGER IF NOT EXISTS fact_operation_attempts_no_update
BEFORE UPDATE ON fact_operation_attempts
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempts is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempts_no_delete
BEFORE DELETE ON fact_operation_attempts
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempts is append-only: DELETE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempts_no_replace
BEFORE INSERT ON fact_operation_attempts
WHEN EXISTS (
    SELECT 1 FROM fact_operation_attempts
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND operation_id = NEW.operation_id
      AND attempt_id = NEW.attempt_id
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempts is append-only: attempt identity already recorded; refusing to overwrite');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempts_require_parent
BEFORE INSERT ON fact_operation_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM fact_operation_work_records
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND operation_id = NEW.operation_id
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempts requires a recorded operation in the same scope');
END;

CREATE TRIGGER IF NOT EXISTS fact_operation_attempt_events_no_update
BEFORE UPDATE ON fact_operation_attempt_events
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempt_events is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempt_events_no_delete
BEFORE DELETE ON fact_operation_attempt_events
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempt_events is append-only: DELETE rejected');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempt_events_no_replace
BEFORE INSERT ON fact_operation_attempt_events
WHEN EXISTS (
    SELECT 1 FROM fact_operation_attempt_events
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND operation_id = NEW.operation_id
      AND attempt_id = NEW.attempt_id
      AND ((seq = NEW.seq) OR event_id = NEW.event_id)
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempt_events is append-only: event seq or event_id already recorded; refusing to overwrite');
END;
CREATE TRIGGER IF NOT EXISTS fact_operation_attempt_events_require_parent
BEFORE INSERT ON fact_operation_attempt_events
WHEN NOT EXISTS (
    SELECT 1 FROM fact_operation_attempts
    WHERE repo_id = NEW.repo_id AND company_id = NEW.company_id
      AND operation_id = NEW.operation_id
      AND attempt_id = NEW.attempt_id
)
BEGIN
    SELECT RAISE(ABORT,
        'fact_operation_attempt_events requires a recorded attempt in the same scope');
END;
"""

_SAVEPOINT = "sam_operation_work_record_write"

_RECORD_COLUMNS = (
    "repo_id, company_id, task_id, operation_id, idempotency_key, "
    "created_at, request_blob, request_sha256, row_sha256")
_OP_EVENT_COLUMNS = (
    "repo_id, company_id, operation_id, seq, event_id, state, "
    "state_commit, recorded_at, detail_blob, detail_sha256, row_sha256")
_ATTEMPT_COLUMNS = (
    "repo_id, company_id, operation_id, attempt_id, created_at, "
    "object_format, base_commit, plan_blob, plan_sha256, effect_blob, "
    "effect_sha256, deps_blob, deps_sha256, row_sha256")
_ATTEMPT_EVENT_COLUMNS = (
    "repo_id, company_id, operation_id, attempt_id, seq, event_id, state, "
    "state_commit, recorded_at, detail_blob, detail_sha256, row_sha256")


class OperationConflict(Exception):
    """An operation identity/key was reused with different content."""


class OperationEventConflict(Exception):
    """An operation event_id was reused with different content."""


class AttemptConflict(Exception):
    """An attempt identity was reused with different immutable content."""


class AttemptEventConflict(Exception):
    """An attempt event_id was reused with different content."""


class UnknownOperation(Exception):
    """The referenced operation has no recorded request in this scope."""


class UnknownAttempt(Exception):
    """The referenced attempt has no recorded snapshot for this operation."""


class IllegalStateTransition(Exception):
    """A state event is not reachable from the current recorded state."""


class OperationWorkStore:
    """Append-only operation/attempt work-record store.

    See the module docstring for the state machines, the paired-transition
    API, recorded conventions, caller obligations (including the explicit
    list of host/Broker-only proofs), the transaction contract and the
    explicit list of what this slice does not do.
    """

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "operation work store requires a caller-provided "
                "sqlite3.Connection")
        # Checked before any DDL: a caller transaction is left untouched,
        # never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "operation work store requires an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.lock = RLock()
        # Row rows support both name and integer indexing, so this does
        # not disturb sibling stores sharing the caller connection.
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _require_id(name, value):
        # git_format.encode_id enforces the host-issued ID bounds: str,
        # non-empty, length-capped. Its result is not needed for storage;
        # this is pure validation of the boundary values.
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s is not a valid host-issued id: %s"
                             % (name, exc)) from exc
        return value

    @staticmethod
    def _require_timestamp(name, value):
        if not isinstance(value, str) or not value:
            raise ValueError("%s must be a non-empty host string" % name)
        return value

    @staticmethod
    def _require_object(name, value, *, allow_none=False):
        if value is None and allow_none:
            value = {}
        if not isinstance(value, dict):
            raise TypeError(
                "%s must be a JSON object (dict); this store keeps host "
                "content as opaque canonical objects" % name)
        return value

    @staticmethod
    def _canonical(name, value):
        blob = git_format.dumps_record(value)
        return blob, hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _require_state(name, value, states):
        if not isinstance(value, str):
            raise TypeError("%s must be a str state" % name)
        if value not in states:
            raise ValueError(
                "%s %r is not a recorded state; legal states: %s"
                % (name, value, sorted(states)))
        return value

    @staticmethod
    def _require_oid(name, value, object_format):
        # A full-length lowercase hex OID for the repository object
        # format. The all-zero OID is deliberately accepted as a base
        # commit (genesis boundary), mirroring operation_event_closure.
        # Shape only: this store never verifies object existence or any
        # Git chain.
        if not isinstance(value, str):
            raise TypeError("%s must be a str OID" % name)
        length = _OID_HEX_LENGTHS[object_format]
        if len(value) != length:
            raise ValueError(
                "%s must be exactly %d hex characters for object format "
                "%r, got %d" % (name, length, object_format, len(value)))
        if any(char not in _HEX_DIGITS for char in value):
            raise ValueError(
                "%s must be lowercase hexadecimal, got %r" % (name, value))
        return value

    @staticmethod
    def _operation_row_digest(repo_id, company_id, task_id, operation_id,
                              idempotency_key, created_at, request_sha256):
        """Unkeyed drift checksum over the bound operation identity."""
        return hashlib.sha256(git_format.dumps_record({
            "repo_id": repo_id,
            "company_id": company_id,
            "task_id": task_id,
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "created_at": created_at,
            "request_sha256": request_sha256,
        })).hexdigest()

    @staticmethod
    def _attempt_row_digest(repo_id, company_id, operation_id, attempt_id,
                            created_at, object_format, base_commit,
                            plan_sha256, effect_sha256, deps_sha256):
        """Unkeyed drift checksum over the bound attempt identity."""
        return hashlib.sha256(git_format.dumps_record({
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "created_at": created_at,
            "object_format": object_format,
            "base_commit": base_commit,
            "plan_sha256": plan_sha256,
            "effect_sha256": effect_sha256,
            "deps_sha256": deps_sha256,
        })).hexdigest()

    @staticmethod
    def _event_row_digest(columns):
        """Unkeyed drift checksum binding one event row's columns."""
        return hashlib.sha256(git_format.dumps_record(columns)).hexdigest()

    # -- write path: operation request snapshot -----------------------------

    def record_operation(self, repo_id, company_id, operation_id, task_id,
                         idempotency_key, created_at, request):
        """Persist one immutable operation request snapshot, or replay it.

        Identity comes from separate host arguments, never from request.
        Returns the durable record with replayed=False on first receipt.
        The exact same (repo_id, company_id, operation_id) with the same
        task_id, idempotency key and byte-identical canonical request
        replays the original record (replayed=True, original created_at
        kept) and writes nothing. Same operation id with different
        content, or the same idempotency key under another operation id,
        raises OperationConflict and leaves prior rows unchanged. The
        same canonical request under a different operation id is
        deliberately allowed: CHG-064 section 4 freezes the request
        digest only within one operation, never across operations.

        Requires an active caller transaction; see the module docstring.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        task_id = self._require_id("task_id", task_id)
        idempotency_key = self._require_id("idempotency_key",
                                           idempotency_key)
        created_at = self._require_timestamp("created_at", created_at)
        request = self._require_object("request", request)
        request_blob, request_sha256 = self._canonical("request", request)

        def core():
            return self._record_operation_core(
                repo_id, company_id, operation_id, task_id,
                idempotency_key, created_at, request_blob, request_sha256)
        return self._write(core)

    def _record_operation_core(self, repo_id, company_id, operation_id,
                               task_id, idempotency_key, created_at,
                               request_blob, request_sha256):
        row = self._fetch_operation_row(repo_id, company_id, operation_id)
        if row is not None:
            stored = self._load_operation_row(row)
            if (stored["task_id"] != task_id
                    or stored["idempotency_key"] != idempotency_key
                    or stored["request_sha256"] != request_sha256
                    or stored["request"]
                    != git_format.loads_record(request_blob)):
                raise OperationConflict(
                    "operation %r in scope (%r, %r) was already recorded "
                    "with different task, idempotency key or request "
                    "content; refusing to overwrite the original record"
                    % (operation_id, repo_id, company_id))
            stored["replayed"] = True
            return stored
        key_row = self._fetch_operation_by_key(repo_id, company_id,
                                               idempotency_key)
        if key_row is not None:
            existing = self._load_operation_row(key_row)
            raise OperationConflict(
                "idempotency key %r is already bound to operation %r in "
                "scope (%r, %r); a different operation id cannot reuse it"
                % (idempotency_key, existing["operation_id"], repo_id,
                   company_id))
        row_sha256 = self._operation_row_digest(
            repo_id, company_id, task_id, operation_id, idempotency_key,
            created_at, request_sha256)
        self.db.execute(
            "INSERT INTO fact_operation_work_records (" + _RECORD_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, company_id, task_id, operation_id, idempotency_key,
             created_at, request_blob, request_sha256, row_sha256))
        return self._operation_result(repo_id, company_id, task_id,
                                      operation_id, idempotency_key,
                                      created_at, request_blob,
                                      request_sha256, replayed=False)

    # -- write path: operation-only state events ----------------------------

    def mark_operation_prepared(self, repo_id, company_id, operation_id,
                                event_id, recorded_at, detail=None):
        """Record the first operation state event: snapshot -> prepared.

        Legal only while the operation has no state events yet. Between
        the request snapshot and this call only the task input is
        registered (CHG-064 section 5 step 1). Attempts cannot be
        recorded before the operation is prepared.
        """
        return self._operation_only_transition(
            repo_id, company_id, operation_id, event_id, recorded_at,
            detail, OPERATION_PREPARED, frozenset({None}),
            "the first operation state event must be %r"
            % OPERATION_PREPARED)

    def mark_approval_required(self, repo_id, company_id, operation_id,
                               event_id, recorded_at, detail=None):
        """prepared -> approval_required (real approval needed, no exec)."""
        return self._operation_only_transition(
            repo_id, company_id, operation_id, event_id, recorded_at,
            detail, OPERATION_APPROVAL_REQUIRED,
            frozenset({OPERATION_PREPARED}), None)

    def mark_reserved(self, repo_id, company_id, operation_id, event_id,
                      recorded_at, detail=None):
        """approval_required -> reserved once the one-shot approval bound.

        The binding itself happens in F-007's unique consumption
        service; this store records the host mirror only and verifies no
        approval or token.
        """
        return self._operation_only_transition(
            repo_id, company_id, operation_id, event_id, recorded_at,
            detail, OPERATION_RESERVED,
            frozenset({OPERATION_APPROVAL_REQUIRED}), None)

    def reject_operation(self, repo_id, company_id, operation_id, event_id,
                         recorded_at, detail=None):
        """prepared/reserved -> rejected: definite non-acceptance.

        CHG-064 section 7 allows rejection only from prepared/reserved
        (dependency invalidated or request illegal before execution), and
        only while no attempt is accepting or unknown: an unknown result
        must be reconciled, never rejected away. A different accurate
        effect needs a new operation explicitly linked to this one.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        event_id = self._require_id("event_id", event_id)
        recorded_at = self._require_timestamp("recorded_at", recorded_at)
        detail = self._require_object("detail", detail, allow_none=True)
        detail_blob, detail_sha256 = self._canonical("detail", detail)

        def core():
            return self._reject_operation_core(
                repo_id, company_id, operation_id, event_id, recorded_at,
                detail_blob, detail_sha256)
        return self._write(core)

    def _reject_operation_core(self, repo_id, company_id, operation_id,
                               event_id, recorded_at, detail_blob,
                               detail_sha256):
        existing = self._fetch_op_event_by_event_id(repo_id, company_id,
                                                    event_id)
        if existing is not None:
            return self._replay_or_conflict_op_event(
                existing, operation_id, OPERATION_REJECTED, detail_blob,
                None)
        if self._fetch_operation_row(repo_id, company_id,
                                     operation_id) is None:
            raise UnknownOperation(
                "operation %r has no recorded request in scope (%r, %r); "
                "register the immutable request before appending state "
                "events" % (operation_id, repo_id, company_id))
        events = self._fetch_op_event_rows(repo_id, company_id,
                                           operation_id)
        current = events[-1]["state"] if events else None
        if self._attempts_currently_in(
                repo_id, company_id, operation_id,
                {ATTEMPT_ACCEPTING, ATTEMPT_UNKNOWN}):
            raise IllegalStateTransition(
                "operation %r cannot be rejected while an attempt is "
                "accepting or unknown: an unknown result must be "
                "reconciled, never rejected away" % operation_id)
        if current not in {OPERATION_PREPARED, OPERATION_RESERVED}:
            raise IllegalStateTransition(
                "operation %r can be rejected only from %r/%r per "
                "CHG-064 section 7; current operation state is %r"
                % (operation_id, OPERATION_PREPARED, OPERATION_RESERVED,
                   current))
        seq = events[-1]["seq"] + 1
        return self._insert_op_event(
            repo_id, company_id, operation_id, seq, event_id,
            OPERATION_REJECTED, None, recorded_at, detail_blob,
            detail_sha256, replayed=False)

    def _operation_only_transition(self, repo_id, company_id, operation_id,
                                   event_id, recorded_at, detail, state,
                                   allowed_current, first_state_message):
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        event_id = self._require_id("event_id", event_id)
        recorded_at = self._require_timestamp("recorded_at", recorded_at)
        detail = self._require_object("detail", detail, allow_none=True)
        detail_blob, detail_sha256 = self._canonical("detail", detail)

        def core():
            return self._operation_only_core(
                repo_id, company_id, operation_id, event_id, state,
                recorded_at, detail_blob, detail_sha256, allowed_current,
                first_state_message)
        return self._write(core)

    def _operation_only_core(self, repo_id, company_id, operation_id,
                             event_id, state, recorded_at, detail_blob,
                             detail_sha256, allowed_current,
                             first_state_message):
        existing = self._fetch_op_event_by_event_id(repo_id, company_id,
                                                    event_id)
        if existing is not None:
            return self._replay_or_conflict_op_event(
                existing, operation_id, state, detail_blob, None)
        if self._fetch_operation_row(repo_id, company_id,
                                     operation_id) is None:
            raise UnknownOperation(
                "operation %r has no recorded request in scope (%r, %r); "
                "register the immutable request before appending state "
                "events" % (operation_id, repo_id, company_id))
        events = self._fetch_op_event_rows(repo_id, company_id,
                                           operation_id)
        current = events[-1]["state"] if events else None
        if current not in allowed_current:
            if current is None:
                message = "the first operation event must be prepared"
            elif first_state_message is not None:
                message = first_state_message
            else:
                message = (
                    "operation %r: illegal migration %r -> %r; legal "
                    "next states: %s"
                    % (operation_id, current, state,
                       sorted(_OPERATION_EDGES.get(current, frozenset()))))
            raise IllegalStateTransition(message)
        seq = (events[-1]["seq"] + 1) if events else 1
        return self._insert_op_event(
            repo_id, company_id, operation_id, seq, event_id, state, None,
            recorded_at, detail_blob, detail_sha256, replayed=False)

    # -- write path: attempt snapshot + its prepared event ------------------

    def record_attempt(self, repo_id, company_id, operation_id, attempt_id,
                       created_at, base_commit, plan, effect, dependencies,
                       *, object_format, prepared_event_id, prepared_at,
                       prepared_detail=None):
        """Persist one immutable attempt snapshot AND its prepared event.

        The snapshot row and the attempt's first (prepared) state event
        are inserted in one SAVEPOINT, so an attempt can never be seen
        without its prepared event. Binds attempt_id, its owning
        operation scope, the fixed base commit (full-length lowercase hex
        OID for object_format; the all-zero genesis OID is allowed),
        separate opaque plan and effect objects and the opaque precise
        dependency version snapshot. All three content digests are
        computed by this store.

        A new attempt requires the operation currently in
        prepared/approval_required/reserved and no live sibling attempt
        (prepared/accepting/unknown), so an unknown result cannot be
        dodged with a fresh id. A replay with the same attempt id,
        byte-identical canonical content and the same prepared-event
        arguments returns the original record and event (replayed=True);
        the same attempt id with different immutable content, or a
        different prepared event, raises AttemptConflict.

        Requires an active caller transaction.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        attempt_id = self._require_id("attempt_id", attempt_id)
        created_at = self._require_timestamp("created_at", created_at)
        prepared_event_id = self._require_id("prepared_event_id",
                                             prepared_event_id)
        prepared_at = self._require_timestamp("prepared_at", prepared_at)
        if object_format not in _OID_HEX_LENGTHS:
            raise ValueError(
                "unsupported object_format %r: only 'sha1' and 'sha256' "
                "are supported" % (object_format,))
        base_commit = self._require_oid("base_commit", base_commit,
                                        object_format)
        plan = self._require_object("plan", plan)
        effect = self._require_object("effect", effect)
        dependencies = self._require_object("dependencies", dependencies)
        prepared_detail = self._require_object(
            "prepared_detail", prepared_detail, allow_none=True)
        plan_blob, plan_sha256 = self._canonical("plan", plan)
        effect_blob, effect_sha256 = self._canonical("effect", effect)
        deps_blob, deps_sha256 = self._canonical("dependencies",
                                                 dependencies)
        prepared_blob, prepared_sha256 = self._canonical(
            "prepared_detail", prepared_detail)

        def core():
            return self._record_attempt_core(
                repo_id, company_id, operation_id, attempt_id, created_at,
                object_format, base_commit, plan_blob, plan_sha256,
                effect_blob, effect_sha256, deps_blob, deps_sha256,
                prepared_event_id, prepared_at, prepared_blob,
                prepared_sha256)
        return self._write(core)

    def _record_attempt_core(self, repo_id, company_id, operation_id,
                             attempt_id, created_at, object_format,
                             base_commit, plan_blob, plan_sha256,
                             effect_blob, effect_sha256, deps_blob,
                             deps_sha256, prepared_event_id, prepared_at,
                             prepared_blob, prepared_sha256):
        if self._fetch_operation_row(repo_id, company_id,
                                     operation_id) is None:
            raise UnknownOperation(
                "operation %r has no recorded request in scope (%r, %r); "
                "an attempt belongs to a recorded operation"
                % (operation_id, repo_id, company_id))
        row = self._fetch_attempt_row(repo_id, company_id, operation_id,
                                      attempt_id)
        if row is not None:
            stored = self._load_attempt_row(row)
            content_conflict = (
                stored["object_format"] != object_format
                or stored["base_commit"] != base_commit
                or stored["plan_sha256"] != plan_sha256
                or stored["effect_sha256"] != effect_sha256
                or stored["dependencies_sha256"] != deps_sha256
                or stored["plan"] != git_format.loads_record(plan_blob)
                or stored["effect"]
                != git_format.loads_record(effect_blob)
                or stored["dependencies"]
                != git_format.loads_record(deps_blob))
            prepared_rows = self._fetch_att_event_rows(
                repo_id, company_id, operation_id, attempt_id)
            if not prepared_rows or prepared_rows[0]["state"] != \
                    ATTEMPT_PREPARED:
                raise ValueError(
                    "attempt %r of operation %r is stored without its "
                    "prepared event; refusing to trust corrupted state "
                    "history" % (attempt_id, operation_id))
            first = self._load_att_event_row(prepared_rows[0])
            event_conflict = (
                first["event_id"] != prepared_event_id
                or first["recorded_at"] != prepared_at
                or first["detail_sha256"] != prepared_sha256
                or first["detail"]
                != git_format.loads_record(prepared_blob))
            if content_conflict or event_conflict:
                raise AttemptConflict(
                    "attempt %r of operation %r was already recorded with "
                    "different base commit, plan, effect, dependency or "
                    "prepared-event content; refusing to overwrite the "
                    "immutable attempt" % (attempt_id, operation_id))
            result = self._attempt_result_from_row(stored, replayed=True)
            first["replayed"] = True
            result["prepared_event"] = first
            return result
        state = self._current_operation_state(repo_id, company_id,
                                              operation_id)
        if state not in _PRE_FLIGHT_STATES:
            raise IllegalStateTransition(
                "a new attempt can be recorded for operation %r only "
                "while it is in prepared/approval_required/reserved; "
                "current operation state is %r. Mark the operation "
                "prepared first; an in-flight or unknown operation is "
                "reconciled on its existing attempt"
                % (operation_id, state))
        if self._attempts_currently_in(
                repo_id, company_id, operation_id, _ATTEMPT_LIVE_STATES):
            live = self._current_attempt_states(
                repo_id, company_id, operation_id)
            raise IllegalStateTransition(
                "operation %r already has a live attempt in state %s; "
                "finish, expire (stale before the gate) or reconcile it "
                "before recording another attempt"
                % (operation_id,
                   sorted(set(live.values()) & _ATTEMPT_LIVE_STATES)))
        existing_event = self._fetch_att_event_by_event_id(
            repo_id, company_id, prepared_event_id)
        if existing_event is not None:
            # A fresh snapshot cannot reuse another event's id.
            self._replay_or_conflict_att_event(
                existing_event, operation_id, attempt_id,
                ATTEMPT_PREPARED, prepared_blob, None)
        row_sha256 = self._attempt_row_digest(
            repo_id, company_id, operation_id, attempt_id, created_at,
            object_format, base_commit, plan_sha256, effect_sha256,
            deps_sha256)
        # Snapshot row first (the parent trigger requires it), then the
        # prepared event: both inside this one SAVEPOINT.
        self.db.execute(
            "INSERT INTO fact_operation_attempts (" + _ATTEMPT_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, company_id, operation_id, attempt_id, created_at,
             object_format, base_commit, plan_blob, plan_sha256,
             effect_blob, effect_sha256, deps_blob, deps_sha256,
             row_sha256))
        prepared_event = self._insert_att_event(
            repo_id, company_id, operation_id, attempt_id, 1,
            prepared_event_id, ATTEMPT_PREPARED, None, prepared_at,
            prepared_blob, prepared_sha256, replayed=False)
        result = self._attempt_result(
            repo_id, company_id, operation_id, attempt_id, created_at,
            object_format, base_commit, plan_blob, plan_sha256,
            effect_blob, effect_sha256, deps_blob, deps_sha256,
            replayed=False)
        result["prepared_event"] = prepared_event
        return result

    # -- write path: pre-flight attempt expiry ------------------------------

    def mark_attempt_stale(self, repo_id, company_id, operation_id,
                           attempt_id, event_id, recorded_at, detail=None):
        """prepared -> stale while the operation is still pre-flight.

        Per CHG-064 section 7 ``stale`` is a scheduling state after a
        DEFINITE non-acceptance: the attempt never entered the final gate
        (its object was never C-at-F, or its candidate/coverage was
        revised/invalidated before admission). It is unreachable from
        accepting and from unknown; once the gate is open an uncertain
        outcome is parked unknown and recovered through the section-7
        proof, never turned into a definite failure here.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        attempt_id = self._require_id("attempt_id", attempt_id)
        event_id = self._require_id("event_id", event_id)
        recorded_at = self._require_timestamp("recorded_at", recorded_at)
        detail = self._require_object("detail", detail, allow_none=True)
        detail_blob, detail_sha256 = self._canonical("detail", detail)

        def core():
            return self._stale_core(
                repo_id, company_id, operation_id, attempt_id, event_id,
                recorded_at, detail_blob, detail_sha256)
        return self._write(core)

    def _stale_core(self, repo_id, company_id, operation_id, attempt_id,
                    event_id, recorded_at, detail_blob, detail_sha256):
        existing = self._fetch_att_event_by_event_id(repo_id, company_id,
                                                     event_id)
        if existing is not None:
            return self._replay_or_conflict_att_event(
                existing, operation_id, attempt_id, ATTEMPT_STALE,
                detail_blob, None)
        attempt_row = self._fetch_attempt_row(repo_id, company_id,
                                              operation_id, attempt_id)
        if attempt_row is None:
            raise UnknownAttempt(
                "attempt %r has no recorded snapshot for operation %r in "
                "scope (%r, %r)"
                % (attempt_id, operation_id, repo_id, company_id))
        events = self._fetch_att_event_rows(repo_id, company_id,
                                            operation_id, attempt_id)
        current = events[-1]["state"] if events else None
        if current != ATTEMPT_PREPARED:
            raise IllegalStateTransition(
                "attempt %r can expire as stale only from its prepared "
                "state; current attempt state is %r. There is no "
                "accepting/unknown -> stale edge: post-gate uncertainty "
                "is parked unknown and recovered under CHG-064 "
                "section 7, never turned into a definite failure"
                % (attempt_id, current))
        op_state = self._current_operation_state(repo_id, company_id,
                                                 operation_id)
        if op_state not in _PRE_FLIGHT_STATES:
            raise IllegalStateTransition(
                "attempt %r can be marked stale only while operation %r "
                "is still pre-flight (prepared/approval_required/"
                "reserved); current operation state is %r"
                % (attempt_id, operation_id, op_state))
        seq = events[-1]["seq"] + 1
        return self._insert_att_event(
            repo_id, company_id, operation_id, attempt_id, seq, event_id,
            ATTEMPT_STALE, None, recorded_at, detail_blob, detail_sha256,
            replayed=False)

    # -- write path: paired section-7 transitions ---------------------------

    def admit_execution(self, repo_id, company_id, operation_id,
                        attempt_id, operation_event_id, attempt_event_id,
                        recorded_at, operation_detail=None,
                        attempt_detail=None):
        """Open the final gate: write the paired in_flight/accepting events.

        One SAVEPOINT writes the attempt ``accepting`` event first and the
        operation ``in_flight`` event second. From operation
        prepared/reserved the named attempt must currently be prepared;
        from operation unknown (CHG-064 section 7 same-operation
        re-admission) that same attempt must currently be unknown.
        approval_required is not an admitting state (reserve first).

        Host-only preconditions this call does NOT verify and nobody may
        claim it verified: for the unknown path, the old CAS executor must
        be stopped/isolated, the complete current legal Git chain must
        contain no accepted operation content, current authorization and
        every immutable dependency must still be valid, and the original
        approval binding stays in force; for both paths the fresh
        transaction must have re-read H, identity/policy/delegation/
        revocation/approval, candidate revision/digest and coverage.
        Recording the mirror is none of that proof.

        Returns ``{"operation_event": ..., "attempt_event": ...}``. An
        exact replay (both event ids with identical content) returns the
        original paired events with replayed=True and writes nothing.
        """
        return self._paired_transition(
            repo_id, company_id, operation_id, attempt_id,
            operation_event_id, attempt_event_id, recorded_at,
            operation_detail, attempt_detail, OPERATION_IN_FLIGHT,
            ATTEMPT_ACCEPTING, None, "admit_execution")

    def mark_unknown(self, repo_id, company_id, operation_id, attempt_id,
                     operation_event_id, attempt_event_id, recorded_at,
                     operation_detail=None, attempt_detail=None):
        """Park an uncertain gate: paired in_flight->unknown outcomes.

        One SAVEPOINT writes attempt accepting -> unknown first and
        operation in_flight -> unknown second. Per CHG-064 section 7 the
        occupation is retained: no new attempt may be registered while
        this one is unknown, stale/retry_wait can never clear it, and the
        only exits are the paired mark_accepted Git proof or the paired
        admit_execution re-admission with the host-owned isolation /
        legal-chain / authorization evidence.
        """
        return self._paired_transition(
            repo_id, company_id, operation_id, attempt_id,
            operation_event_id, attempt_event_id, recorded_at,
            operation_detail, attempt_detail, OPERATION_UNKNOWN,
            ATTEMPT_UNKNOWN, None, "mark_unknown")

    def mark_accepted(self, repo_id, company_id, operation_id, attempt_id,
                      accepted_commit, operation_event_id,
                      attempt_event_id, recorded_at,
                      operation_detail=None, attempt_detail=None):
        """Record the paired accepted mirror with one accepted_commit.

        One SAVEPOINT writes attempt accepting/unknown -> accepted first
        (with the OID-shaped accepted_commit mirror) and operation
        in_flight/unknown -> accepted second naming the same commit; the
        current pair must be (in_flight, accepting) or (unknown,
        unknown). This is a work-store mirror, not acceptance proof: the
        host/Broker must separately establish the operation content on
        the current complete legal Git chain and the first accepting
        commit C, never a dangling commit. This method verifies only the
        OID shape against the attempt object format and the pair
        coupling; it checks no ref, chain or reachability.
        """
        return self._paired_transition(
            repo_id, company_id, operation_id, attempt_id,
            operation_event_id, attempt_event_id, recorded_at,
            operation_detail, attempt_detail, OPERATION_ACCEPTED,
            ATTEMPT_ACCEPTED, accepted_commit, "mark_accepted")

    def _paired_transition(self, repo_id, company_id, operation_id,
                           attempt_id, operation_event_id,
                           attempt_event_id, recorded_at,
                           operation_detail, attempt_detail,
                           operation_state, attempt_state,
                           accepted_commit, transition_name):
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        attempt_id = self._require_id("attempt_id", attempt_id)
        operation_event_id = self._require_id("operation_event_id",
                                              operation_event_id)
        attempt_event_id = self._require_id("attempt_event_id",
                                            attempt_event_id)
        recorded_at = self._require_timestamp("recorded_at", recorded_at)
        operation_detail = self._require_object(
            "operation_detail", operation_detail, allow_none=True)
        attempt_detail = self._require_object(
            "attempt_detail", attempt_detail, allow_none=True)
        op_blob, op_sha = self._canonical("operation_detail",
                                          operation_detail)
        att_blob, att_sha = self._canonical("attempt_detail",
                                            attempt_detail)
        mirror_commit = None
        if operation_state == OPERATION_ACCEPTED:
            if accepted_commit is None:
                raise ValueError(
                    "an accepted transition requires the host-supplied "
                    "accepted_commit mirror reference")
        elif accepted_commit is not None:
            raise ValueError(
                "accepted_commit is recorded only on accepted events")

        def core():
            return self._paired_core(
                repo_id, company_id, operation_id, attempt_id,
                operation_event_id, attempt_event_id, recorded_at,
                op_blob, op_sha, att_blob, att_sha, operation_state,
                attempt_state, accepted_commit, mirror_commit,
                transition_name)
        return self._write(core)

    def _paired_core(self, repo_id, company_id, operation_id, attempt_id,
                     operation_event_id, attempt_event_id, recorded_at,
                     op_blob, op_sha, att_blob, att_sha, operation_state,
                     attempt_state, accepted_commit, mirror_commit,
                     transition_name):
        op_existing = self._fetch_op_event_by_event_id(
            repo_id, company_id, operation_event_id)
        att_existing = self._fetch_att_event_by_event_id(
            repo_id, company_id, attempt_event_id)
        if (op_existing is None) != (att_existing is None):
            # A pair is atomic; exactly one half on disk can only come
            # from out-of-band SQL or a call that reused one event id.
            # Fail closed either way.
            raise ValueError(
                "%s for operation %r: paired event ids %r/%r are half "
                "recorded (one exists, one missing); refusing to leave "
                "or trust a torn paired transition"
                % (transition_name, operation_id, operation_event_id,
                   attempt_event_id))
        if op_existing is not None:
            op_event = self._replay_or_conflict_op_event(
                op_existing, operation_id, operation_state, op_blob,
                accepted_commit)
            att_event = self._replay_or_conflict_att_event(
                att_existing, operation_id, attempt_id, attempt_state,
                att_blob, accepted_commit)
            return {"operation_event": op_event, "attempt_event": att_event}
        attempt_row = self._fetch_attempt_row(repo_id, company_id,
                                              operation_id, attempt_id)
        if attempt_row is None:
            raise UnknownAttempt(
                "attempt %r has no recorded snapshot for operation %r in "
                "scope (%r, %r)"
                % (attempt_id, operation_id, repo_id, company_id))
        attempt = self._load_attempt_row(attempt_row)
        if accepted_commit is not None:
            mirror_commit = self._require_oid(
                "accepted_commit", accepted_commit,
                attempt["object_format"])
        op_events = self._fetch_op_event_rows(repo_id, company_id,
                                              operation_id)
        op_current = op_events[-1]["state"] if op_events else None
        att_events = self._fetch_att_event_rows(
            repo_id, company_id, operation_id, attempt_id)
        att_current = att_events[-1]["state"] if att_events else None
        self._require_paired_admission(
            transition_name, operation_id, attempt_id, op_current,
            att_current, operation_state, attempt_state, repo_id,
            company_id)
        # Attempt half first, operation half second: the operation mirror
        # follows the attempt result, and both rows are released together.
        att_seq = (att_events[-1]["seq"] + 1) if att_events else 1
        op_seq = (op_events[-1]["seq"] + 1) if op_events else 1
        att_event = self._insert_att_event(
            repo_id, company_id, operation_id, attempt_id, att_seq,
            attempt_event_id, attempt_state, mirror_commit, recorded_at,
            att_blob, att_sha, replayed=False)
        op_event = self._insert_op_event(
            repo_id, company_id, operation_id, op_seq,
            operation_event_id, operation_state, mirror_commit,
            recorded_at, op_blob, op_sha, replayed=False)
        return {"operation_event": op_event, "attempt_event": att_event}

    def _require_paired_admission(self, transition_name, operation_id,
                                  attempt_id, op_current, att_current,
                                  operation_state, attempt_state, repo_id,
                                  company_id):
        # Validate the joint move against the CHG-064 section 7 table and
        # the one-live-attempt invariant BEFORE either row is inserted.
        if operation_state == OPERATION_IN_FLIGHT:
            if op_current in (OPERATION_PREPARED, OPERATION_RESERVED):
                expected = ATTEMPT_PREPARED
            elif op_current == OPERATION_UNKNOWN:
                expected = ATTEMPT_UNKNOWN
            else:
                raise IllegalStateTransition(
                    "%s: operation %r can enter in_flight only from "
                    "prepared, reserved or unknown (section 7); current "
                    "operation state is %r"
                    % (transition_name, operation_id, op_current))
            if att_current != expected:
                raise IllegalStateTransition(
                    "%s: operation %r entering in_flight from %r requires "
                    "attempt %r currently %r, got %r; the paired events "
                    "must be written together"
                    % (transition_name, operation_id, op_current,
                       attempt_id, expected, att_current))
        elif operation_state == OPERATION_UNKNOWN:
            if op_current != OPERATION_IN_FLIGHT or \
                    att_current != ATTEMPT_ACCEPTING:
                raise IllegalStateTransition(
                    "%s: unknown is the paired transition "
                    "in_flight/accepting -> unknown/unknown; operation "
                    "%r is %r and attempt %r is %r"
                    % (transition_name, operation_id, op_current,
                       attempt_id, att_current))
        elif operation_state == OPERATION_ACCEPTED:
            pair = (op_current, att_current)
            if pair not in ((OPERATION_IN_FLIGHT, ATTEMPT_ACCEPTING),
                            (OPERATION_UNKNOWN, ATTEMPT_UNKNOWN)):
                raise IllegalStateTransition(
                    "%s: accepted is the paired transition from "
                    "(in_flight, accepting) or (unknown, unknown); "
                    "operation %r is %r and attempt %r is %r"
                    % (transition_name, operation_id, op_current,
                       attempt_id, att_current))
        currents = self._current_attempt_states(
            repo_id, company_id, operation_id)
        others = {state for other, state in currents.items()
                  if other != attempt_id}
        if others - {ATTEMPT_STALE}:
            raise IllegalStateTransition(
                "%s: operation %r has another live attempt in state %s; "
                "at most one attempt may own the execution"
                % (transition_name, operation_id,
                   sorted(others - {ATTEMPT_STALE})))

    def _write(self, core):
        if not self.db.in_transaction:
            raise ValueError(
                "operation work store writes require an active caller "
                "transaction; this store never begins, commits or rolls "
                "back the caller transaction itself")
        with self.lock:
            self.db.execute("SAVEPOINT " + _SAVEPOINT)
            try:
                result = core()
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
                self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
                raise
            self.db.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            return result

    # -- read path ----------------------------------------------------------

    def get_operation(self, repo_id, company_id, operation_id):
        """Return the immutable operation record plus current state.

        current_state is None when only the request snapshot is
        registered (CHG-064 section 5 step 1: task input may be recorded
        before a precise plan exists). accepted_commit is the mirror
        reference from the accepted event, still not acceptance proof.
        Returns None when no operation exists in the exact scope.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)

        def fetch():
            row = self._fetch_operation_row(repo_id, company_id,
                                            operation_id)
            return self._load_operation_row(row) if row is not None else None
        result = self._read(fetch)
        if result is None:
            return None
        events = self.list_operation_events(repo_id, company_id,
                                            operation_id)
        result["current_state"] = events[-1]["state"] if events else None
        result["current_seq"] = events[-1]["seq"] if events else None
        accepted = [event for event in events
                    if event["state"] == OPERATION_ACCEPTED]
        result["accepted_commit"] = (accepted[-1]["accepted_commit"]
                                     if accepted else None)
        result["replayed"] = False
        return result

    def get_operation_by_idempotency_key(self, repo_id, company_id,
                                         idempotency_key):
        """Return the operation record bound to one idempotency key."""
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        idempotency_key = self._require_id("idempotency_key",
                                           idempotency_key)

        def fetch():
            row = self._fetch_operation_by_key(repo_id, company_id,
                                               idempotency_key)
            return self._load_operation_row(row) if row is not None else None
        result = self._read(fetch)
        if result is not None:
            result["replayed"] = False
        return result

    def list_operation_events(self, repo_id, company_id, operation_id):
        """Return all operation state events, seq order, fully re-validated.

        Re-verifies the gapless seq chain, the prepared first state, the
        finite CHG-064 section 7 migrations, the accepted-commit coupling
        and the joint operation/attempt stable state; any drift fails
        closed.
        """
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)

        def fetch():
            self._verify_operation_full(repo_id, company_id, operation_id)
            rows = self._fetch_op_event_rows(repo_id, company_id,
                                             operation_id)
            return [self._load_op_event_row(row) for row in rows]
        return self._read(fetch)

    def get_attempt(self, repo_id, company_id, operation_id, attempt_id):
        """Return one immutable attempt record plus its current state."""
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        attempt_id = self._require_id("attempt_id", attempt_id)

        def fetch():
            row = self._fetch_attempt_row(repo_id, company_id,
                                          operation_id, attempt_id)
            return self._load_attempt_row(row) if row is not None else None
        result = self._read(fetch)
        if result is None:
            return None
        events = self.list_attempt_events(repo_id, company_id,
                                          operation_id, attempt_id)
        result["current_state"] = events[-1]["state"] if events else None
        result["current_seq"] = events[-1]["seq"] if events else None
        accepted = [event for event in events
                    if event["state"] == ATTEMPT_ACCEPTED]
        result["accepted_commit"] = (accepted[-1]["accepted_commit"]
                                     if accepted else None)
        result["replayed"] = False
        return result

    def list_attempts(self, repo_id, company_id, operation_id):
        """Return all stored attempts of one operation in insert order."""
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)

        def fetch():
            rows = self.db.execute(
                "SELECT " + _ATTEMPT_COLUMNS + " FROM "
                "fact_operation_attempts WHERE repo_id=? AND company_id=? "
                "AND operation_id=? ORDER BY rowid ASC",
                (repo_id, company_id, operation_id)).fetchall()
            return [self._load_attempt_row(row) for row in rows]
        results = self._read(fetch)
        for result in results:
            result["replayed"] = False
        return results

    def list_attempt_events(self, repo_id, company_id, operation_id,
                            attempt_id):
        """Return all attempt state events, seq order, re-validated."""
        repo_id = self._require_id("repo_id", repo_id)
        company_id = self._require_id("company_id", company_id)
        operation_id = self._require_id("operation_id", operation_id)
        attempt_id = self._require_id("attempt_id", attempt_id)

        def fetch():
            attempt_row = self._fetch_attempt_row(repo_id, company_id,
                                                  operation_id, attempt_id)
            if attempt_row is None:
                return None
            attempt = self._load_attempt_row(attempt_row)
            self._verify_operation_full(repo_id, company_id, operation_id)
            rows = self._fetch_att_event_rows(repo_id, company_id,
                                              operation_id, attempt_id)
            events = [self._load_att_event_row(row) for row in rows]
            self._verify_attempt_chain(events, attempt["object_format"])
            return events
        result = self._read(fetch)
        if result is None:
            raise UnknownAttempt(
                "attempt %r has no recorded snapshot for operation %r in "
                "scope (%r, %r)"
                % (attempt_id, operation_id, repo_id, company_id))
        return result

    def verify_integrity(self):
        """Re-validate every stored row and every joint event history.

        Scans all four tables regardless of scope: canonical blobs,
        content digests, row checksums and bound scope columns, parent
        existence, gapless event seqs, first states, legal migrations,
        the element-wise pairing of operation in_flight/unknown/accepted
        events with the single live attempt's accepting/unknown/accepted
        events, and every stable joint state of CHG-064 section 7.
        Returns None on success and raises ValueError on the first
        inconsistency; never repairs anything.
        """
        def scan():
            op_rows = self.db.execute(
                "SELECT " + _RECORD_COLUMNS
                + " FROM fact_operation_work_records ORDER BY rowid ASC"
            ).fetchall()
            operations = {}
            for row in op_rows:
                record = self._load_operation_row(row)
                operations[(record["repo_id"], record["company_id"],
                            record["operation_id"])] = record
            op_events = {}
            op_event_rows = self.db.execute(
                "SELECT " + _OP_EVENT_COLUMNS
                + " FROM fact_operation_work_events ORDER BY seq ASC"
            ).fetchall()
            for row in op_event_rows:
                event = self._load_op_event_row(row)
                key = (event["repo_id"], event["company_id"],
                       event["operation_id"])
                if key not in operations:
                    raise ValueError(
                        "operation event %r has no recorded operation; "
                        "refusing to trust orphan state history"
                        % event["event_id"])
                op_events.setdefault(key, []).append(event)
            attempts = {}
            attempt_rows = self.db.execute(
                "SELECT " + _ATTEMPT_COLUMNS
                + " FROM fact_operation_attempts ORDER BY rowid ASC"
            ).fetchall()
            for row in attempt_rows:
                attempt = self._load_attempt_row(row)
                key = (attempt["repo_id"], attempt["company_id"],
                       attempt["operation_id"])
                if key not in operations:
                    raise ValueError(
                        "attempt %r has no recorded operation; refusing "
                        "to trust orphan attempt snapshot"
                        % attempt["attempt_id"])
                attempts.setdefault(key, []).append(attempt)
            att_events = {}
            att_event_rows = self.db.execute(
                "SELECT " + _ATTEMPT_EVENT_COLUMNS
                + " FROM fact_operation_attempt_events ORDER BY seq ASC"
            ).fetchall()
            for row in att_event_rows:
                event = self._load_att_event_row(row)
                parent_row = self._fetch_attempt_row(
                    event["repo_id"], event["company_id"],
                    event["operation_id"], event["attempt_id"])
                if parent_row is None:
                    raise ValueError(
                        "attempt event %r has no recorded attempt; "
                        "refusing to trust orphan state history"
                        % event["event_id"])
                key = (event["repo_id"], event["company_id"],
                       event["operation_id"], event["attempt_id"])
                att_events.setdefault(key, []).append(event)
            for key in sorted(operations):
                self._verify_operation_full(
                    key[0], key[1], key[2],
                    operation=operations[key],
                    attempts=attempts.get(key, []),
                    op_events=op_events.get(key, []),
                    att_events=att_events)
            return None
        return self._read(scan)

    def _verify_operation_full(self, repo_id, company_id, operation_id,
                               *, operation=None, attempts=None,
                               op_events=None, att_events=None):
        # Shared between read entry points (loads the scope itself) and
        # the whole-store scan (which passes the already-loaded rows).
        if operation is None:
            row = self._fetch_operation_row(repo_id, company_id,
                                            operation_id)
            if row is None:
                raise UnknownOperation(
                    "operation %r has no recorded request in scope "
                    "(%r, %r)" % (operation_id, repo_id, company_id))
            operation = self._load_operation_row(row)
        if attempts is None:
            attempts = [
                self._load_attempt_row(row)
                for row in self.db.execute(
                    "SELECT " + _ATTEMPT_COLUMNS
                    + " FROM fact_operation_attempts"
                    " WHERE repo_id=? AND company_id=? AND operation_id=? "
                    "ORDER BY rowid ASC",
                    (repo_id, company_id, operation_id)).fetchall()]
        if op_events is None:
            op_events = [
                self._load_op_event_row(row)
                for row in self._fetch_op_event_rows(
                    repo_id, company_id, operation_id)]
        if att_events is None:
            att_events = {}
            for attempt in attempts:
                att_events[(repo_id, company_id, operation_id,
                            attempt["attempt_id"])] = [
                    self._load_att_event_row(row)
                    for row in self._fetch_att_event_rows(
                        repo_id, company_id, operation_id,
                        attempt["attempt_id"])]
        formats = {attempt["attempt_id"]: attempt["object_format"]
                   for attempt in attempts}
        chains = {}
        for attempt in attempts:
            akey = (repo_id, company_id, operation_id,
                    attempt["attempt_id"])
            chain = att_events.get(akey)
            if not chain:
                raise ValueError(
                    "attempt %r has a snapshot row but no state events; "
                    "refusing to trust corrupted state history"
                    % attempt["attempt_id"])
            chains[attempt["attempt_id"]] = chain
            self._verify_attempt_chain(chain, attempt["object_format"])
        self._verify_operation_chain(repo_id, company_id, operation_id,
                                     op_events, formats, chains)
        self._verify_joint_history(operation_id, attempts, op_events,
                                   chains)

    def _read(self, fetch):
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                return fetch()
            finally:
                if owns_snapshot:
                    # Read-only snapshot owned by this store: close it
                    # with ROLLBACK on success and failure alike. A
                    # caller-owned transaction is never touched.
                    self.db.rollback()

    # -- joint history replay (CHG-064 section 7 stable states) -------------

    # The two event tables have independent rowid counters, so a
    # cross-table rowid merge is meaningless. The write path itself
    # constrains the legal interleavings tightly (pre-flight events, then
    # indivisible paired units), so joint history is verified
    # structurally: classify attempt chains, match the paired
    # subsequences element by element, then walk the one canonical legal
    # ordering the write path could have produced and require every
    # prefix to be an enumerated stable state.
    _PAIRED_ATTEMPT_TO_OPERATION = {
        ATTEMPT_ACCEPTING: OPERATION_IN_FLIGHT,
        ATTEMPT_UNKNOWN: OPERATION_UNKNOWN,
        ATTEMPT_ACCEPTED: OPERATION_ACCEPTED,
    }

    def _verify_joint_history(self, operation_id, attempts, op_events,
                              chains):
        if not op_events:
            if chains:
                raise ValueError(
                    "operation %r has attempt state events before its "
                    "own prepared event; refusing to trust corrupted "
                    "state history" % operation_id)
            return

        currents = {attempt["attempt_id"]: chain[-1]["state"]
                    for attempt, chain in
                    ((attempt, chains[attempt["attempt_id"]])
                     for attempt in attempts)}
        op_pairs = [event for event in op_events
                    if event["state"] in _PAIRED_OPERATION_STATES]

        live = [attempt_id for attempt_id, state in currents.items()
                if state in (ATTEMPT_ACCEPTING, ATTEMPT_UNKNOWN,
                             ATTEMPT_ACCEPTED)]
        waiting = [attempt_id for attempt_id, state in currents.items()
                   if state == ATTEMPT_PREPARED]
        if len(live) > 1:
            raise ValueError(
                "operation %r has more than one attempt past the gate "
                "(%s); at most one attempt may own the execution; "
                "refusing to trust corrupted state history"
                % (operation_id, sorted(live)))
        if len(waiting) > 1:
            raise ValueError(
                "operation %r has more than one attempt still prepared "
                "(%s); at most one attempt may be live pre-flight; "
                "refusing to trust corrupted state history"
                % (operation_id, sorted(waiting)))
        op_final = op_events[-1]["state"]
        if waiting and op_final not in _PRE_FLIGHT_STATES:
            raise ValueError(
                "operation %r is mirrored %r but an attempt is still "
                "prepared; a still-prepared attempt is legal only while "
                "the operation is pre-flight; refusing to trust "
                "corrupted state history"
                % (operation_id, op_final))

        att_pairs = []
        if live:
            live_chain = chains[live[0]]
            att_pairs = [event for event in live_chain
                         if event["state"]
                         in self._PAIRED_ATTEMPT_TO_OPERATION]
        if len(att_pairs) != len(op_pairs):
            raise ValueError(
                "operation %r records %d paired operation events but its "
                "attempt history records %d paired events; paired "
                "transitions must be written together; refusing to "
                "trust corrupted state history"
                % (operation_id, len(op_pairs), len(att_pairs)))
        for op_event, att_event in zip(op_pairs, att_pairs):
            expected = self._PAIRED_ATTEMPT_TO_OPERATION[
                att_event["state"]]
            if op_event["state"] != expected:
                raise ValueError(
                    "operation %r paired event %r does not match attempt "
                    "event %r (expected operation state %r); refusing "
                    "to trust corrupted state history"
                    % (operation_id, op_event["event_id"],
                       att_event["event_id"], expected))
            if op_event["state"] == OPERATION_ACCEPTED and \
                    op_event["state_commit"] != att_event["state_commit"]:
                raise ValueError(
                    "operation %r accepted event commit %r does not "
                    "match the paired attempt accepted commit %r; "
                    "refusing to trust corrupted state history"
                    % (operation_id, op_event["state_commit"],
                       att_event["state_commit"]))

        # Walk the canonical legal ordering; every prefix must be a
        # stable joint state. Dead attempts (prepared -> stale) come
        # first, then a still-waiting prepared attempt, then the live
        # attempt's prepared event, then any remaining pre-flight
        # operation events (approval_required/reserved are mutually
        # order-insensitive with pre-flight attempt events: every such
        # combination is stable), then the paired units.
        current = {}
        self._assert_stable_joint_state(operation_id, None, current)
        # The operation edge/chain checks already guarantee the first
        # event is prepared and only pre-flight states precede the first
        # paired event; walk those operation events in seq order,
        # interleaving the attempt pre-flight events at their only legal
        # position.
        pre_flight_op = [event for event in op_events
                         if event["state"] in _PRE_FLIGHT_STATES]
        paired = list(zip(op_pairs, att_pairs)) if live else []
        dead_ids = [attempt["attempt_id"] for attempt in attempts
                    if currents[attempt["attempt_id"]] == ATTEMPT_STALE]
        # op prepared first and alone.
        op_state = pre_flight_op[0]["state"] if pre_flight_op else None
        if op_state != OPERATION_PREPARED:
            raise ValueError(
                "operation %r first state must be prepared; refusing to "
                "trust corrupted state history" % operation_id)
        self._assert_stable_joint_state(operation_id, op_state, current)
        for attempt_id in dead_ids:
            current[attempt_id] = ATTEMPT_PREPARED
            self._assert_stable_joint_state(
                operation_id, op_state, current)
            current[attempt_id] = ATTEMPT_STALE
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        for attempt_id in waiting:
            current[attempt_id] = ATTEMPT_PREPARED
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        if live:
            current[live[0]] = ATTEMPT_PREPARED
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        # Remaining pre-flight operation events (approval_required,
        # reserved), in seq order, skipping the first prepared.
        for event in pre_flight_op[1:]:
            op_state = event["state"]
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        for op_event, att_event in paired:
            current[live[0]] = att_event["state"]
            op_state = op_event["state"]
            # Stability is asserted only after BOTH halves: the window
            # between halves exists solely inside one SAVEPOINT and is
            # never a committable stable state.
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        # A rejected terminal event (only reachable pre-flight per the
        # edge table) is the final operation event and not part of the
        # paired walk.
        if op_final == OPERATION_REJECTED:
            op_state = OPERATION_REJECTED
            self._assert_stable_joint_state(
                operation_id, op_state, current)
        if op_state != op_final:
            raise ValueError(
                "operation %r replayed joint history ends in %r but the "
                "event chain ends in %r; refusing to trust corrupted "
                "state history"
                % (operation_id, op_state, op_final))
        if set(current) != set(currents) or \
                any(current[aid] != currents[aid] for aid in current):
            raise ValueError(
                "operation %r replayed attempt currents do not match the "
                "stored chain tails; refusing to trust corrupted state "
                "history" % operation_id)

    def _assert_stable_joint_state(self, operation_id, op_state, current):
        states = set(current.values())
        if op_state is None:
            if current:
                raise ValueError(
                    "operation %r has attempt state events before its "
                    "own prepared event; refusing to trust corrupted "
                    "state history" % operation_id)
            return
        if op_state in _PRE_FLIGHT_STATES or op_state == OPERATION_REJECTED:
            allowed = {ATTEMPT_PREPARED, ATTEMPT_STALE}
            exact = None
        elif op_state == OPERATION_IN_FLIGHT:
            allowed = {ATTEMPT_ACCEPTING, ATTEMPT_STALE}
            exact = ATTEMPT_ACCEPTING
        elif op_state == OPERATION_UNKNOWN:
            allowed = {ATTEMPT_UNKNOWN, ATTEMPT_STALE}
            exact = ATTEMPT_UNKNOWN
        elif op_state == OPERATION_ACCEPTED:
            allowed = {ATTEMPT_ACCEPTED, ATTEMPT_STALE}
            exact = ATTEMPT_ACCEPTED
        else:  # pragma: no cover - every operation state is handled
            raise ValueError(
                "operation %r has unknown mirrored state %r"
                % (operation_id, op_state))
        foreign = states - allowed
        if foreign:
            raise ValueError(
                "operation %r is mirrored %r but an attempt is %s; this "
                "is not a CHG-064 section 7 stable state; refusing to "
                "trust corrupted state history"
                % (operation_id, op_state, sorted(foreign)))
        live_states = [state for state in current.values()
                       if state != ATTEMPT_STALE]
        if len(live_states) > 1:
            raise ValueError(
                "operation %r has more than one non-stale attempt (%s); "
                "at most one attempt may own the execution; refusing to "
                "trust corrupted state history"
                % (operation_id, sorted(live_states)))
        if exact is not None and live_states != [exact]:
            raise ValueError(
                "operation %r is mirrored %r but requires exactly one "
                "%r attempt; current attempt states are %s; refusing to "
                "trust corrupted state history"
                % (operation_id, op_state, exact,
                   sorted(states)))

    # -- chain verification --------------------------------------------------

    def _verify_operation_chain(self, repo_id, company_id, operation_id,
                                events, formats, chains):
        for index, event in enumerate(events, start=1):
            if event["seq"] != index:
                raise ValueError(
                    "operation %r event seq is not gapless from 1: "
                    "expected %r, got %r"
                    % (operation_id, index, event["seq"]))
        if events:
            if events[0]["state"] != OPERATION_PREPARED:
                raise ValueError(
                    "operation %r first state must be prepared, got %r"
                    % (operation_id, events[0]["state"]))
            for previous, event in zip(events, events[1:]):
                if event["state"] not in _OPERATION_EDGES[previous["state"]]:
                    raise ValueError(
                        "operation %r state history contains illegal "
                        "migration %r -> %r; refusing to trust corrupted "
                        "state history"
                        % (operation_id, previous["state"],
                           event["state"]))
        for event in events:
            if event["state"] == OPERATION_ACCEPTED:
                commit = event["state_commit"]
                matching = [
                    chain_event for chain in chains.values()
                    for chain_event in chain
                    if chain_event["state"] == ATTEMPT_ACCEPTED
                    and chain_event["state_commit"] == commit]
                if not matching:
                    raise ValueError(
                        "operation %r accepted event commit %r does not "
                        "match an accepted attempt commit; refusing to "
                        "trust corrupted state history"
                        % (operation_id, commit))
                # Shape-validate against the format of the attempt that
                # carries that commit.
                for attempt_id, chain in chains.items():
                    for chain_event in chain:
                        if chain_event["state"] == ATTEMPT_ACCEPTED and \
                                chain_event["state_commit"] == commit:
                            self._require_oid(
                                "stored accepted_commit", commit,
                                formats[attempt_id])
            elif event["state_commit"] is not None:
                raise ValueError(
                    "operation %r carries a mirror commit on non-accepted "
                    "state %r; refusing to trust corrupted state history"
                    % (operation_id, event["state"]))

    def _verify_attempt_chain(self, events, object_format):
        for index, event in enumerate(events, start=1):
            if event["seq"] != index:
                raise ValueError(
                    "attempt %r event seq is not gapless from 1: "
                    "expected %r, got %r"
                    % (event["attempt_id"], index, event["seq"]))
        if not events:
            return
        if events[0]["state"] != ATTEMPT_PREPARED:
            raise ValueError(
                "attempt %r first state must be prepared, got %r"
                % (events[0]["attempt_id"], events[0]["state"]))
        for previous, event in zip(events, events[1:]):
            if event["state"] not in _ATTEMPT_EDGES[previous["state"]]:
                raise ValueError(
                    "attempt %r state history contains illegal migration "
                    "%r -> %r; refusing to trust corrupted state history"
                    % (event["attempt_id"], previous["state"],
                       event["state"]))
        for event in events:
            commit = event["state_commit"]
            if event["state"] == ATTEMPT_ACCEPTED:
                if commit is None:
                    raise ValueError(
                        "attempt %r accepted event lacks its mirror "
                        "commit; refusing to trust corrupted state "
                        "history" % event["attempt_id"])
                self._require_oid("stored accepted_commit", commit,
                                  object_format)
            elif commit is not None:
                raise ValueError(
                    "attempt %r carries a mirror commit on non-accepted "
                    "state %r; refusing to trust corrupted state history"
                    % (event["attempt_id"], event["state"]))

    # -- raw SQL fetch helpers ----------------------------------------------

    def _fetch_operation_row(self, repo_id, company_id, operation_id):
        return self.db.execute(
            "SELECT " + _RECORD_COLUMNS + " FROM fact_operation_work_records"
            " WHERE repo_id=? AND company_id=? AND operation_id=?",
            (repo_id, company_id, operation_id)).fetchone()

    def _fetch_operation_by_key(self, repo_id, company_id, idempotency_key):
        return self.db.execute(
            "SELECT " + _RECORD_COLUMNS + " FROM fact_operation_work_records"
            " WHERE repo_id=? AND company_id=? AND idempotency_key=?",
            (repo_id, company_id, idempotency_key)).fetchone()

    def _fetch_op_event_rows(self, repo_id, company_id, operation_id):
        return self.db.execute(
            "SELECT " + _OP_EVENT_COLUMNS + " FROM fact_operation_work_events"
            " WHERE repo_id=? AND company_id=? AND operation_id=? "
            "ORDER BY seq ASC",
            (repo_id, company_id, operation_id)).fetchall()

    def _fetch_op_event_by_event_id(self, repo_id, company_id, event_id):
        return self.db.execute(
            "SELECT " + _OP_EVENT_COLUMNS + " FROM fact_operation_work_events"
            " WHERE repo_id=? AND company_id=? AND event_id=?",
            (repo_id, company_id, event_id)).fetchone()

    def _fetch_attempt_row(self, repo_id, company_id, operation_id,
                           attempt_id):
        return self.db.execute(
            "SELECT " + _ATTEMPT_COLUMNS + " FROM fact_operation_attempts"
            " WHERE repo_id=? AND company_id=? AND operation_id=? "
            "AND attempt_id=?",
            (repo_id, company_id, operation_id, attempt_id)).fetchone()

    def _fetch_att_event_rows(self, repo_id, company_id, operation_id,
                              attempt_id):
        return self.db.execute(
            "SELECT " + _ATTEMPT_EVENT_COLUMNS
            + " FROM fact_operation_attempt_events"
            " WHERE repo_id=? AND company_id=? AND operation_id=? "
            "AND attempt_id=? ORDER BY seq ASC",
            (repo_id, company_id, operation_id, attempt_id)).fetchall()

    def _fetch_att_event_by_event_id(self, repo_id, company_id, event_id):
        return self.db.execute(
            "SELECT " + _ATTEMPT_EVENT_COLUMNS
            + " FROM fact_operation_attempt_events"
            " WHERE repo_id=? AND company_id=? AND event_id=?",
            (repo_id, company_id, event_id)).fetchone()

    def _current_operation_state(self, repo_id, company_id, operation_id):
        row = self.db.execute(
            "SELECT state FROM fact_operation_work_events"
            " WHERE repo_id=? AND company_id=? AND operation_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (repo_id, company_id, operation_id)).fetchone()
        return row[0] if row is not None else None

    def _current_attempt_states(self, repo_id, company_id, operation_id):
        rows = self.db.execute(
            "SELECT e.attempt_id, e.state FROM fact_operation_attempt_events e"
            " WHERE e.repo_id=? AND e.company_id=? AND e.operation_id=?"
            "  AND e.seq = ("
            "    SELECT MAX(seq) FROM fact_operation_attempt_events x"
            "    WHERE x.repo_id=e.repo_id AND x.company_id=e.company_id"
            "      AND x.operation_id=e.operation_id"
            "      AND x.attempt_id=e.attempt_id)",
            (repo_id, company_id, operation_id)).fetchall()
        return {row[0]: row[1] for row in rows}

    def _attempts_currently_in(self, repo_id, company_id, operation_id,
                               states):
        return bool(set(self._current_attempt_states(
            repo_id, company_id, operation_id).values()) & set(states))

    # -- replay / insert / row loading --------------------------------------

    def _replay_or_conflict_op_event(self, row, operation_id, state,
                                     detail_blob, accepted_commit):
        stored = self._load_op_event_row(row)
        if (stored["operation_id"] != operation_id
                or stored["state"] != state
                or stored["detail"] != git_format.loads_record(detail_blob)
                or stored["state_commit"] != accepted_commit):
            raise OperationEventConflict(
                "operation event_id %r is already recorded with different "
                "operation, state, detail or accepted_commit; refusing to "
                "overwrite the append-only event" % stored["event_id"])
        stored["replayed"] = True
        return stored

    def _replay_or_conflict_att_event(self, row, operation_id, attempt_id,
                                      state, detail_blob, accepted_commit):
        stored = self._load_att_event_row(row)
        if (stored["operation_id"] != operation_id
                or stored["attempt_id"] != attempt_id
                or stored["state"] != state
                or stored["detail"] != git_format.loads_record(detail_blob)
                or stored["state_commit"] != accepted_commit):
            raise AttemptEventConflict(
                "attempt event_id %r is already recorded with different "
                "attempt, state, detail or accepted_commit; refusing to "
                "overwrite the append-only event" % stored["event_id"])
        stored["replayed"] = True
        return stored

    def _insert_op_event(self, repo_id, company_id, operation_id, seq,
                         event_id, state, state_commit, recorded_at,
                         detail_blob, detail_sha256, *, replayed):
        row_digest = self._event_row_digest({
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "state_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
        })
        self.db.execute(
            "INSERT INTO fact_operation_work_events (" + _OP_EVENT_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, company_id, operation_id, seq, event_id, state,
             state_commit, recorded_at, detail_blob, detail_sha256,
             row_digest))
        result = {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "accepted_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
            "detail": git_format.loads_record(detail_blob),
            "replayed": replayed,
        }
        return result

    def _insert_att_event(self, repo_id, company_id, operation_id,
                          attempt_id, seq, event_id, state, state_commit,
                          recorded_at, detail_blob, detail_sha256, *,
                          replayed):
        row_digest = self._event_row_digest({
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "state_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
        })
        self.db.execute(
            "INSERT INTO fact_operation_attempt_events ("
            + _ATTEMPT_EVENT_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, company_id, operation_id, attempt_id, seq, event_id,
             state, state_commit, recorded_at, detail_blob, detail_sha256,
             row_digest))
        return {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "accepted_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
            "detail": git_format.loads_record(detail_blob),
            "replayed": replayed,
        }

    @staticmethod
    def _operation_result(repo_id, company_id, task_id, operation_id,
                          idempotency_key, created_at, request_blob,
                          request_sha256, *, replayed):
        return {
            "repo_id": repo_id,
            "company_id": company_id,
            "task_id": task_id,
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "created_at": created_at,
            "request_sha256": request_sha256,
            "request": git_format.loads_record(request_blob),
            "replayed": replayed,
        }

    @staticmethod
    def _attempt_result(repo_id, company_id, operation_id, attempt_id,
                        created_at, object_format, base_commit, plan_blob,
                        plan_sha256, effect_blob, effect_sha256, deps_blob,
                        deps_sha256, *, replayed):
        return {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "created_at": created_at,
            "object_format": object_format,
            "base_commit": base_commit,
            "plan_sha256": plan_sha256,
            "plan": git_format.loads_record(plan_blob),
            "effect_sha256": effect_sha256,
            "effect": git_format.loads_record(effect_blob),
            "dependencies_sha256": deps_sha256,
            "dependencies": git_format.loads_record(deps_blob),
            "replayed": replayed,
        }

    def _attempt_result_from_row(self, stored, *, replayed):
        result = dict(stored)
        result["replayed"] = replayed
        return result

    @staticmethod
    def _load_operation_row(row):
        (repo_id, company_id, task_id, operation_id, idempotency_key,
         created_at, request_blob, request_sha256, row_sha256) = row
        try:
            request = git_format.loads_record(request_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored operation request blob failed canonical "
                "re-validation; refusing to read possibly tampered data") \
                from exc
        if hashlib.sha256(bytes(request_blob)).hexdigest() != request_sha256:
            raise ValueError(
                "stored operation request digest does not match the "
                "stored request; refusing to read possibly tampered data")
        if git_format.dumps_record(request) != bytes(request_blob):
            raise ValueError(
                "stored operation request bytes are not the exact "
                "canonical record format; refusing to read possibly "
                "tampered data")
        for name, value in (
                ("repo_id", repo_id), ("company_id", company_id),
                ("task_id", task_id), ("operation_id", operation_id),
                ("idempotency_key", idempotency_key)):
            try:
                git_format.encode_id(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "stored %s is not a legal host-issued id; refusing "
                    "to read possibly tampered data: %s" % (name, exc)) \
                    from exc
        if not isinstance(created_at, str) or not created_at:
            raise ValueError(
                "stored operation created_at is not a non-empty host "
                "string; refusing to read possibly tampered data")
        if OperationWorkStore._operation_row_digest(
                repo_id, company_id, task_id, operation_id,
                idempotency_key, created_at, request_sha256) != row_sha256:
            raise ValueError(
                "stored operation row-integrity digest does not match the "
                "stored identity/metadata columns; refusing to read "
                "possibly tampered data")
        return {
            "repo_id": repo_id,
            "company_id": company_id,
            "task_id": task_id,
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "created_at": created_at,
            "request_sha256": request_sha256,
            "request": request,
        }

    @staticmethod
    def _load_attempt_row(row):
        (repo_id, company_id, operation_id, attempt_id, created_at,
         object_format, base_commit, plan_blob, plan_sha256, effect_blob,
         effect_sha256, deps_blob, deps_sha256, row_sha256) = row
        try:
            plan = git_format.loads_record(plan_blob)
            effect = git_format.loads_record(effect_blob)
            dependencies = git_format.loads_record(deps_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored attempt blobs failed canonical re-validation; "
                "refusing to read possibly tampered data") from exc
        for label, blob, digest in (
                ("plan", plan_blob, plan_sha256),
                ("effect", effect_blob, effect_sha256),
                ("dependency", deps_blob, deps_sha256)):
            if hashlib.sha256(bytes(blob)).hexdigest() != digest:
                raise ValueError(
                    "stored %s digest does not match the stored blob; "
                    "refusing to read possibly tampered data" % label)
        for label, value, blob in (
                ("plan", plan, plan_blob),
                ("effect", effect, effect_blob),
                ("dependency", dependencies, deps_blob)):
            if git_format.dumps_record(value) != bytes(blob):
                raise ValueError(
                    "stored %s bytes are not the exact canonical record "
                    "format; refusing to read possibly tampered data"
                    % label)
        for name, value in (
                ("repo_id", repo_id), ("company_id", company_id),
                ("operation_id", operation_id), ("attempt_id", attempt_id)):
            try:
                git_format.encode_id(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "stored %s is not a legal host-issued id; refusing "
                    "to read possibly tampered data: %s" % (name, exc)) \
                    from exc
        if object_format not in _OID_HEX_LENGTHS:
            raise ValueError(
                "stored attempt object_format %r is unsupported; "
                "refusing to read possibly tampered data"
                % (object_format,))
        OperationWorkStore._require_oid("stored base_commit", base_commit,
                                        object_format)
        if not isinstance(created_at, str) or not created_at:
            raise ValueError(
                "stored attempt created_at is not a non-empty host "
                "string; refusing to read possibly tampered data")
        if OperationWorkStore._attempt_row_digest(
                repo_id, company_id, operation_id, attempt_id, created_at,
                object_format, base_commit, plan_sha256, effect_sha256,
                deps_sha256) != row_sha256:
            raise ValueError(
                "stored attempt row-integrity digest does not match the "
                "stored identity/metadata columns; refusing to read "
                "possibly tampered data")
        return {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "created_at": created_at,
            "object_format": object_format,
            "base_commit": base_commit,
            "plan_sha256": plan_sha256,
            "plan": plan,
            "effect_sha256": effect_sha256,
            "effect": effect,
            "dependencies_sha256": deps_sha256,
            "dependencies": dependencies,
        }

    @staticmethod
    def _load_event_row(row, kind):
        if kind == "operation":
            (repo_id, company_id, operation_id, seq, event_id, state,
             state_commit, recorded_at, detail_blob, detail_sha256,
             row_sha256) = row
            attempt_id = None
            states = _OPERATION_STATES
            id_names = ("repo_id", "company_id", "operation_id")
            id_values = (repo_id, company_id, operation_id)
        else:
            (repo_id, company_id, operation_id, attempt_id, seq, event_id,
             state, state_commit, recorded_at, detail_blob,
             detail_sha256, row_sha256) = row
            states = _ATTEMPT_STATES
            id_names = ("repo_id", "company_id", "operation_id",
                        "attempt_id")
            id_values = (repo_id, company_id, operation_id, attempt_id)
        try:
            detail = git_format.loads_record(detail_blob)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored %s event detail blob failed canonical "
                "re-validation; refusing to read possibly tampered data"
                % kind) from exc
        if hashlib.sha256(bytes(detail_blob)).hexdigest() != detail_sha256:
            raise ValueError(
                "stored %s event detail digest does not match the stored "
                "blob; refusing to read possibly tampered data" % kind)
        if git_format.dumps_record(detail) != bytes(detail_blob):
            raise ValueError(
                "stored %s event detail bytes are not the exact canonical "
                "record format; refusing to read possibly tampered data"
                % kind)
        for name, value in zip(id_names, id_values):
            try:
                git_format.encode_id(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "stored %s %s is not a legal host-issued id; refusing "
                    "to read possibly tampered data: %s"
                    % (kind, name, exc)) from exc
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValueError(
                "stored %s event seq is not a positive int; refusing to "
                "read possibly tampered data" % kind)
        if state not in states:
            raise ValueError(
                "stored %s event state %r is not a recorded state; "
                "refusing to read possibly tampered data" % (kind, state))
        if (state == "accepted") != (state_commit is not None):
            raise ValueError(
                "stored %s event accepted_commit presence does not match "
                "state %r; refusing to read possibly tampered data"
                % (kind, state))
        if not isinstance(recorded_at, str) or not recorded_at:
            raise ValueError(
                "stored %s event recorded_at is not a non-empty host "
                "string; refusing to read possibly tampered data" % kind)
        digest_columns = {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "state_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
        }
        if attempt_id is not None:
            digest_columns["attempt_id"] = attempt_id
        if OperationWorkStore._event_row_digest(
                digest_columns) != row_sha256:
            raise ValueError(
                "stored %s event row-integrity digest does not match the "
                "stored identity/metadata columns; refusing to read "
                "possibly tampered data" % kind)
        result = {
            "repo_id": repo_id,
            "company_id": company_id,
            "operation_id": operation_id,
            "seq": seq,
            "event_id": event_id,
            "state": state,
            "state_commit": state_commit,
            "accepted_commit": state_commit,
            "recorded_at": recorded_at,
            "detail_sha256": detail_sha256,
            "detail": detail,
        }
        if attempt_id is not None:
            result["attempt_id"] = attempt_id
        return result

    def _load_op_event_row(self, row):
        return self._load_event_row(row, "operation")

    def _load_att_event_row(self, row):
        return self._load_event_row(row, "attempt")
