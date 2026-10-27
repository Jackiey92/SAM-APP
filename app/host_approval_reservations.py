"""Irreversible approval reservations for the SAM host (F-007 API-008).

Approved design baseline a13abdb4116714fea7f81c23897fcb038d1be41a,
F-007 P02 section 7: the immutable reserved record that binds a fully
verified approved decision to the current valid task. This module
records the first-binding fact only; it never consumes, never executes
and never marks an operation as accepted, and reservation_state
'reserved' is not the operation's current execution state. There is no
approval-use audit reference here, no unreserve API and no renewal.

This module implements reservation persistence and read-back only:

- HostApprovalReservationService(db, identity, authorization,
  task_credentials, prepared_effects, approval_decisions) binds actual
  service instances that all share the very same sqlite3.Connection
  object and the very same identity / authorization / task-credential /
  prepared-effect / decision instances. Every binding is checked BEFORE
  any DDL runs, and the connection must be idle at construction: task
  resolution, decision and prepared-effect re-verification and the
  reservation write must live in one SQLite transaction on one
  connection. The module never opens a database, never chooses a data
  path, never reads environment variables and never touches the default
  app.db; it creates only its own table.
- reserve_approval(task_secret, ...) takes the current real task
  credential - never a caller-fabricated context, never a credential
  digest and never the owner's session. The worker does not hold the
  approver's secret: the decision's original session_ref is historical
  audit data only, and the original session being revoked afterwards
  does not block a reservation while the task itself is still valid.
  The secret is validated (str, non-empty, UTF-8 encodable) before it
  is handed to the task credential resolver, with a fixed error whose
  exception chain never carries the original input. The trusted caller
  time and the monotonic origin are captured at entry before any
  expensive verification, and the effective time is re-derived inside
  the BEGIN IMMEDIATE write transaction, exactly as in the sibling
  host modules; reservation.created_at is that post-lock effective
  time.
- Inside that transaction the task credential is resolved for real
  (event history, installation, membership, bound policy
  revision/status/validity, stored cap against the current policy
  scope, revocation). Because resolution reports the ISSUANCE-time
  member revision, the member record is additionally re-read through
  the identity service's internal _fetch_member: the member must be
  active and its CURRENT revision must exactly equal the revision the
  task, the prepared effect and the decision were recorded under. The
  prepared effect is loaded through the decision service's internal
  _load_prepared (full re-verification), the decision through
  _fetch_by_operation (canonical bytes, row digest, index agreement)
  and their mutual binding through _check_decision_binding; the public
  get methods are never called inside the write transaction because
  they refuse caller transactions. The literal scope fit is re-checked
  through the prepared module's _recheck_scope_fit, and the real
  installation binding's owner_principal_id must still equal the
  decision's subject principal.
- The caller supplies the operation_id, the approval_id and BOTH
  digests; all three must exactly equal the fully verified decision
  and prepared effect. Only an 'approved' decision can be reserved; a
  denial is refused. The caller never supplies a validity period: the
  reservation's expires_at is fixed to the decision's expires_at and
  can never be re-expired or reopened. The full time chain is enforced
  in this module because the decision service's internal binding check
  does not cover creation-time ordering: prepared.created_at <=
  decision.created_at <= reservation.created_at <= the effective
  verification time < reservation.expires_at = decision.expires_at <=
  prepared.expires_at <= the current task scope not_after. The "current
  validity" of the real Git head, source, candidate/overlay and
  dependency versions is re-checked by the Broker's final coordination
  domain; this slice verifies only the canonical prepared record and
  the declarations inside it, and never accesses Git or side effects.
- The stored row is immutable: UPDATE, DELETE and all three INSERT OR
  REPLACE classes (by reservation_id, by approval_id and by
  operation_id) are refused by triggers without relying on
  recursive_triggers. reservation_id, approval_id and operation_id are
  each unique. reservation_id is host-generated randomness (the record
  id, not an authorization token and not approval-use proof); the
  caller can never supply it. The canonical record blob holds exactly
  schema_version, reservation_state, reservation_id, approval_id,
  operation_id, effect_digest, preparation_digest, installation_id,
  company_id, principal_id, member_revision, task_id, run_id,
  policy_id, policy_revision, action, target, created_at and
  expires_at, with fixed UTC six-digit-microsecond timestamps; action
  and target are copied verbatim from the fully verified prepared
  envelope. The row digest is SHA-256 over the domain-separated UTF-8
  text 'sam-host-approval-reservation row-v1', one single NUL byte and
  the blob. It proves storage consistency only; it is not a signature.
  The digests already bind the object, base and declared dependencies;
  no sensitive manifest content, credential secret or credential
  digest is copied into this module's table.
- Idempotency: a replay over the same current valid task with the same
  operation, the same approval, both digests and the complete binding
  returns the FIRST reservation_id and created_at and writes nothing;
  any changed content conflicts and never overwrites. The replayed row
  is accepted only after its stored created_at re-passes the complete
  time chain, so a forged old row never masquerades as the first
  binding. Concurrent
  identical requests return the same row; a divergent binding can
  never win over the unique legitimate one; every failure rolls back
  completely and reopening a connection never resets the binding.
- get_approval_reservation refuses any existing caller transaction and
  always opens its own fresh short read snapshot; the effective time
  is re-derived only after the first real read has actually
  established the snapshot and absorbed any lock wait, so a wait
  crossing a validity boundary can never pass on the stale entry time.
  The task credential is authenticated first, then the reservation
  row; a missing operation and one belonging to another task produce
  the identical unavailable error, so other tasks' reservations are
  never exposed. The complete binding (task, run, installation,
  company, principal, member revision, policy and revision, decision,
  prepared effect, both digests, action and target) is re-verified
  against the freshly re-verified decision, prepared effect and
  re-resolved task credential, the decision must still be an approval
  (a row bound to a denial is simply unavailable) and the full time
  chain is re-checked.
- Expiry, revocation, denied execution, unknown or timeout never
  delete, move or auto-release a reservation: an expired or revoked
  reservation simply stops being returned as currently valid, and the
  old row is kept permanently as a tombstone. There is no unreserve
  and no renewal API. A first reserve call whose outcome is unclear
  must be re-checked with the same operation; a new approval must
  never be created to bypass an existing occupation.
- ApprovalReservation is plain frozen data: reservation_id,
  approval_id, operation_id, both digests, created_at, expires_at, the
  deep-frozen canonical record and its canonical bytes. to_dict()
  returns a brand-new mutable deep copy. The result carries no session
  or task secret and no credential digest, and has no authorization,
  consumption or execution methods.

Scope of this slice (explicitly NOT implemented here): consumption and
approval-use audit references, execution, the operation/attempt state
machine, accepted/unknown reconciliation, the Commit Broker's final
coordination domain, Git access, signatures, whole-database rollback
protection, recovery admission, trusted history reconciliation reads,
HTTP routes, UI, default feature gates and any change to the global
app.db schema or migrations. The original prepared and decision rows
are never modified. Atomicity is one same-connection transaction with
real verification ordering; actual power-loss durability still
requires trustworthy host storage, a sound SQLite deployment and
backup verification. Error messages never copy caller input or
secrets, and exception chains never retain them: handlers record only
the failure kind or fact and raise fixed exceptions after leaving the
handler, so __cause__ and __context__ stay empty of input-carrying
exceptions.

Connection and transaction contract: identical to
app.host_approval_decisions. The constructor takes only a
caller-provided sqlite3.Connection, idle at construction. The write
method owns exactly one BEGIN IMMEDIATE transaction, commits on
success and rolls back on any exception; calling it inside a caller
transaction is refused and leaves that transaction untouched. The
read method refuses any existing caller transaction outright, opens
its own fresh short snapshot and closes it with ROLLBACK; the refused
caller transaction is never closed, committed or rolled back by this
module.

Internal-interface reuse (sanctioned by the F-007 card): boundary
validators (_require_time, _LABEL_MAX) come from
app.host_authorization, canonical record serialization and ID rules
from app.facts_tree.git_format, the prepared module contributes the
secret/canonical/freeze/time primitives (_require_secret,
_canonical_bytes, _freeze, _thaw, _store_time, _LOWER_HEX) and the
literal scope re-check _recheck_scope_fit, and the decision service
contributes its internal loaders _load_prepared/_fetch_by_operation
and its binding check _check_decision_binding. The identity service's
internal _fetch_member/_fetch_installation re-verify the current
membership revision and the owner binding. No dependency module's
source is modified and no secret or credential digest is copied into
this module's table.
"""
import hashlib
import secrets
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from app.facts_tree import git_format
from app.host_approval_decisions import HostApprovalDecisionService
from app.host_authorization import (
    HostAuthorizationService,
    _LABEL_MAX,
    _require_time,
)
from app.host_identity import HostIdentityService
from app.host_prepared_effects import (
    HostPreparedEffectService,
    _LOWER_HEX,
    _canonical_bytes,
    _freeze,
    _require_secret,
    _store_time,
    _thaw,
)
from app.host_task_credentials import HostTaskCredentialService

# Monotonic time source for the in-transaction effective time; a plain
# module attribute so deterministic tests can simulate a contended
# write-lock wait, exactly as in the sibling host modules.
_monotonic_ns = time.monotonic_ns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_approval_reservations (
    reservation_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    effect_digest TEXT NOT NULL,
    preparation_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    record_blob BLOB NOT NULL,
    row_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS host_approval_reservations_no_update
BEFORE UPDATE ON host_approval_reservations BEGIN
    SELECT RAISE(ABORT, 'host approval reservations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_approval_reservations_no_delete
BEFORE DELETE ON host_approval_reservations BEGIN
    SELECT RAISE(ABORT, 'host approval reservations are append-only');
END;
-- INSERT OR REPLACE would implicitly delete the stored row before
-- inserting, and that delete does not fire the BEFORE DELETE trigger
-- while recursive_triggers is off (the SQLite default). BEFORE INSERT
-- runs before any conflict resolution while the old row is still
-- present, so these three guards make all three REPLACE classes - by
-- reservation_id, by approval_id and by the operation_id unique key -
-- unable to overwrite stored reservation history.
CREATE TRIGGER IF NOT EXISTS host_approval_reservations_no_replace_reservation
BEFORE INSERT ON host_approval_reservations
WHEN EXISTS (
    SELECT 1 FROM host_approval_reservations
    WHERE reservation_id = NEW.reservation_id
) BEGIN
    SELECT RAISE(ABORT, 'host approval reservations are append-only: INSERT OR REPLACE cannot overwrite an existing reservation');
END;
CREATE TRIGGER IF NOT EXISTS host_approval_reservations_no_replace_approval
BEFORE INSERT ON host_approval_reservations
WHEN EXISTS (
    SELECT 1 FROM host_approval_reservations
    WHERE approval_id = NEW.approval_id
) BEGIN
    SELECT RAISE(ABORT, 'host approval reservations are append-only: INSERT OR REPLACE cannot overwrite an existing approval reservation');
END;
CREATE TRIGGER IF NOT EXISTS host_approval_reservations_no_replace_operation
BEFORE INSERT ON host_approval_reservations
WHEN EXISTS (
    SELECT 1 FROM host_approval_reservations
    WHERE operation_id = NEW.operation_id
) BEGIN
    SELECT RAISE(ABORT, 'host approval reservations are append-only: INSERT OR REPLACE cannot overwrite an existing operation reservation');
END;
"""

_COLUMNS = ("reservation_id, approval_id, operation_id, task_id, "
            "run_id, effect_digest, preparation_digest, created_at, "
            "expires_at, record_blob, row_sha256")

# Domain-separated digest prefix: the exact UTF-8 domain text plus one
# single NUL byte, per the approved design.
_ROW_DOMAIN = b"sam-host-approval-reservation row-v1\x00"

_SCHEMA_VERSION = 1
_RESERVATION_STATE = "reserved"

_RECORD_KEYS = frozenset({
    "schema_version", "reservation_state", "reservation_id",
    "approval_id", "operation_id", "effect_digest",
    "preparation_digest", "installation_id", "company_id",
    "principal_id", "member_revision", "task_id", "run_id",
    "policy_id", "policy_revision", "action", "target", "created_at",
    "expires_at",
})

_UNAVAILABLE_MESSAGE = ("no approval reservation is available for this "
                        "operation reference")


class HostApprovalReservationError(Exception):
    """Base class for every approval-reservation failure; fails closed."""


class ApprovalReservationUnavailable(HostApprovalReservationError):
    """No approval reservation is available for this operation
    reference: it does not exist, it has no approved decision or it
    does not belong to the current task. The same error covers all
    cases so other tasks' reservations are never exposed."""


class ApprovalReservationMismatch(HostApprovalReservationError):
    """The approval_id or the expected effect/preparation digests do
    not equal the fully verified decision and prepared effect; an
    operation reference alone never reserves anything."""


class ApprovalReservationDenied(HostApprovalReservationError):
    """The decision on this operation is a denial; a denied decision is
    never reserved."""


class ApprovalReservationConflict(HostApprovalReservationError):
    """The operation, approval or reservation reference is already
    bound to a different reservation; a stored reservation is never
    overwritten, moved or re-expired."""


class ApprovalReservationExpired(HostApprovalReservationError):
    """The decision validity period the reservation binds to has
    passed; replays never restore an expired reservation and no new
    expiry ever reopens one."""


@dataclass(frozen=True)
class ApprovalReservation:
    """One immutable approval reservation as persisted by the host.

    Plain frozen data: no secret material, no credential digest and
    deliberately no methods that grant consumption, execution or any
    other authority. reservation_state 'reserved' records the
    first-binding fact only; it is not the operation's current
    execution state. The record mapping and everything nested in it
    are deep-frozen (mapping proxies and tuples); record_bytes are the
    exact canonical record bytes. to_dict() returns a brand-new
    mutable deep copy, so a caller can never mutate the stored shape.
    """

    reservation_id: str
    approval_id: str
    operation_id: str
    effect_digest: str
    preparation_digest: str
    created_at: datetime
    expires_at: datetime
    record: Mapping
    record_bytes: bytes

    def to_dict(self):
        """Return a brand-new mutable deep copy of the canonical
        record; mutating the copy never affects this
        ApprovalReservation."""
        return _thaw(self.record)


def _effective_now(base, origin):
    """In-transaction effective time, identical in shape to the
    sibling modules': the trusted base advanced by the monotonic
    elapsed time since call entry, floored to whole microseconds, so a
    lock wait crossing an expiry boundary can never decide on a stale
    pre-lock time and the effective time can never lead the observed
    monotonic elapsed time."""
    elapsed_ns = _monotonic_ns() - origin
    if elapsed_ns < 0:
        elapsed_ns = 0
    return base + timedelta(microseconds=elapsed_ns // 1000)


def _load_time(name, value):
    if not isinstance(value, str) or not value:
        raise ValueError(
            "stored approval reservation %s is not a non-empty string; "
            "refusing to trust possibly tampered data" % name)
    parsed = None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # The parser error echoes the offending text; only the failure
        # fact leaves this handler, never the exception object.
        parsed = None
    if parsed is None:
        raise ValueError(
            "stored approval reservation %s failed to parse; refusing "
            "to trust possibly tampered data" % name)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "stored approval reservation %s is naive; refusing to "
            "trust possibly tampered data" % name)
    parsed = parsed.astimezone(timezone.utc)
    if _store_time(parsed) != value:
        raise ValueError(
            "stored approval reservation %s is not in the canonical "
            "UTC six-digit-microsecond format; refusing to trust "
            "possibly tampered data" % name)
    return parsed


def _digest_row(blob):
    return hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()


def _require_digest(name, value):
    if not isinstance(value, str):
        raise TypeError("%s must be the str SHA-256 hex digest of the "
                        "prepared effect being reserved" % name)
    if len(value) != 64 \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError("%s must be exactly 64 lowercase hex "
                         "characters" % name)
    return value


# -- stored-data re-validation (all fail closed) ------------------------------


def _recheck_id(name, value):
    valid = True
    try:
        git_format.encode_id(value)
    except (TypeError, ValueError, UnicodeEncodeError):
        # The library error may carry the stored value; only the
        # failure fact leaves this handler.
        valid = False
    if not valid:
        raise ValueError(
            "stored approval reservation %s failed re-validation; "
            "refusing to trust possibly tampered data" % name)
    return value


def _recheck_label(name, value):
    if not isinstance(value, str) or not value or len(value) > _LABEL_MAX:
        raise ValueError(
            "stored approval reservation %s is not a valid label; "
            "refusing to trust possibly tampered data" % name)
    return value


def _recheck_revision(name, value):
    # Only the built-in int type itself; a stored bool never masquerades
    # as a revision.
    if type(value) is not int or value < 1:
        raise ValueError(
            "stored approval reservation %s is not a positive built-in "
            "int; refusing to trust possibly tampered data" % name)
    return value


def _recheck_digest(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError(
            "stored approval reservation digest is malformed; refusing "
            "to trust possibly tampered data")
    return value


def _check_record(payload):
    """Re-verify one parsed canonical reservation record, field by
    field."""
    if not isinstance(payload, dict) \
            or frozenset(payload) != _RECORD_KEYS:
        raise ValueError(
            "stored approval reservation record has an unexpected "
            "shape; refusing to trust possibly tampered data")
    if type(payload["schema_version"]) is not int \
            or payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(
            "stored approval reservation schema_version is not the "
            "built-in int 1; refusing to trust possibly tampered data")
    if payload["reservation_state"] != _RESERVATION_STATE:
        raise ValueError(
            "stored approval reservation state is not 'reserved'; "
            "refusing to trust possibly tampered data")
    for name in ("reservation_id", "approval_id", "operation_id",
                 "installation_id", "company_id", "principal_id",
                 "task_id", "run_id", "policy_id"):
        _recheck_id(name, payload[name])
    _recheck_revision("member_revision", payload["member_revision"])
    _recheck_revision("policy_revision", payload["policy_revision"])
    _recheck_digest(payload["effect_digest"])
    _recheck_digest(payload["preparation_digest"])
    _recheck_label("action", payload["action"])
    _recheck_label("target", payload["target"])
    created = _load_time("created_at", payload["created_at"])
    expires = _load_time("expires_at", payload["expires_at"])
    if not created < expires:
        raise ValueError(
            "stored approval reservation created_at is not before "
            "expires_at; refusing to trust possibly tampered data")
    return payload


def _loads_record(blob):
    """Parse stored record bytes and fully re-verify them.

    The stored bytes must BE the canonical serialization of their
    payload: the parsed content is re-serialized and compared byte for
    byte, so a non-canonical (even if self-consistently hashed) blob
    fails closed.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError(
            "stored approval reservation blob is not bytes; refusing "
            "to trust possibly tampered data")
    blob = bytes(blob)
    parsed = True
    try:
        payload = git_format.loads_record(blob)
    except (TypeError, ValueError, UnicodeDecodeError):
        # JSONDecodeError.doc and UnicodeDecodeError.object would carry
        # the stored bytes; only the failure fact leaves this handler.
        parsed = False
    if not parsed:
        raise ValueError(
            "stored approval reservation blob failed to parse; "
            "refusing to trust possibly tampered data")
    serialized = True
    try:
        canonical = git_format.dumps_record(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        serialized = False
    if not serialized:
        raise ValueError(
            "stored approval reservation blob failed re-serialization; "
            "refusing to trust possibly tampered data")
    if canonical != blob:
        raise ValueError(
            "stored approval reservation blob is not in canonical "
            "record form; refusing to trust possibly tampered data")
    return _check_record(payload)


class HostApprovalReservationService:
    """Irreversible approval reservations on one shared connection.

    See the module docstring for the full scope list and the
    connection, transaction, identity, digest and idempotency
    contracts.
    """

    def __init__(self, db, identity, authorization, task_credentials,
                 prepared_effects, approval_decisions):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host approval reservations require a caller-provided "
                "sqlite3.Connection; the module never opens or chooses "
                "a database path itself")
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host approval reservations require the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        if not isinstance(authorization, HostAuthorizationService):
            raise TypeError(
                "host approval reservations require the bound "
                "HostAuthorizationService; caller-reported policy "
                "state is never accepted")
        if not isinstance(task_credentials, HostTaskCredentialService):
            raise TypeError(
                "host approval reservations require the bound "
                "HostTaskCredentialService; caller-reported task "
                "context is never accepted")
        if not isinstance(prepared_effects, HostPreparedEffectService):
            raise TypeError(
                "host approval reservations require the bound "
                "HostPreparedEffectService; caller-reported "
                "preparation state is never accepted")
        if not isinstance(approval_decisions, HostApprovalDecisionService):
            raise TypeError(
                "host approval reservations require the bound "
                "HostApprovalDecisionService; caller-reported decision "
                "state is never accepted")
        # Same-object checks, before any DDL: task resolution, decision
        # and prepared-effect re-verification and the reservation write
        # must share one connection so they share one SQLite
        # transaction. Any second connection - even to the same file -
        # could read a different database or a stale snapshot, so it is
        # refused.
        if identity.db is not db or authorization.db is not db \
                or task_credentials.db is not db \
                or prepared_effects.db is not db \
                or approval_decisions.db is not db:
            raise ValueError(
                "host approval reservations require every bound "
                "service's own connection object; combining separate "
                "connections breaks the single-transaction "
                "verification guarantee")
        if authorization.identity is not identity \
                or task_credentials.identity is not identity \
                or task_credentials.authorization is not authorization \
                or prepared_effects.identity is not identity \
                or prepared_effects.authorization is not authorization \
                or prepared_effects.task_credentials is not task_credentials \
                or approval_decisions.identity is not identity \
                or approval_decisions.authorization is not authorization \
                or approval_decisions.task_credentials is not task_credentials \
                or approval_decisions.prepared_effects is not prepared_effects:
            raise ValueError(
                "host approval reservations require the ledger, the "
                "credential service, the prepared-effect service and "
                "the decision service to be bound to the very same "
                "identity, authorization, credential and prepared "
                "instances; combining divergent instances breaks the "
                "single-transaction verification guarantee")
        # Checked before any DDL: a caller transaction is left
        # untouched, never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host approval reservations require an idle connection "
                "at construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.identity = identity
        self.authorization = authorization
        self.task_credentials = task_credentials
        self.prepared_effects = prepared_effects
        self.approval_decisions = approval_decisions
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- boundary validation ------------------------------------------------

    @staticmethod
    def _require_id(name, value):
        # git_format.encode_id enforces the host-issued ID bounds: str,
        # non-empty, length-capped. Pure boundary validation; the
        # library error may carry the offending text, so only the
        # failure kind leaves the handler and the raised error is fixed.
        failure = None
        try:
            git_format.encode_id(value)
        except UnicodeEncodeError:
            failure = "encoding"
        except (TypeError, ValueError):
            failure = "invalid"
        if failure == "encoding":
            raise ValueError(
                "%s must be UTF-8 encodable host-issued id text"
                % name)
        if failure is not None:
            raise ValueError("%s is not a valid host-issued id" % name)
        return value

    # -- transaction helpers --------------------------------------------------

    def _write_tx(self, core):
        if self.db.in_transaction:
            raise ValueError(
                "host approval reservation writes own their "
                "transaction; the connection must be idle at call time")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = core()
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            return result

    def _read(self, fetch):
        # A caller-held transaction could be an old snapshot that
        # predates a revocation committed by another connection, so
        # reads never join one: they always open a fresh short snapshot
        # of their own. The refused caller transaction is never closed,
        # committed or rolled back by this module.
        if self.db.in_transaction:
            raise ValueError(
                "host approval reservation reads open their own fresh "
                "snapshot; refusing to read through a caller "
                "transaction")
        with self.lock:
            self.db.execute("BEGIN")
            try:
                return fetch()
            finally:
                # Read-only snapshot owned by this service: close it
                # with ROLLBACK on success and failure alike.
                self.db.rollback()

    # -- shared verification core ------------------------------------------------

    def _resolve_task(self, task_secret, tx_now):
        """Really resolve the task credential and require the CURRENT
        membership revision.

        Resolution reports the issuance-time stored revision, so the
        member record is re-read for real: the member must still be
        active and its current revision must exactly equal the revision
        the task (and therefore the prepared effect and the decision)
        was recorded under.
        """
        ctx = self.task_credentials.resolve_task_credential(
            task_secret, now=tx_now)
        member = self.identity._fetch_member(ctx.principal_id)
        if member is None or member["status"] != "active":
            raise ValueError(
                "the resolved task credential's principal membership "
                "is not active; refusing to trust possibly tampered "
                "data")
        if member["revision"] != ctx.member_revision:
            raise ValueError(
                "the current membership revision does not match the "
                "revision the task credential was issued under; "
                "refusing to trust possibly tampered or stale data")
        return ctx

    def _check_installation_owner(self, ctx, principal_id):
        """The real installation binding must match the resolved task,
        and its owner_principal_id must still be the decision's
        subject principal."""
        installation = self.identity._fetch_installation()
        if installation is None \
                or installation["installation_id"] != ctx.installation_id \
                or installation["company_id"] != ctx.company_id:
            raise ValueError(
                "stored installation binding does not match the "
                "resolved task credential; refusing to trust possibly "
                "tampered data")
        if installation["owner_principal_id"] != principal_id:
            raise ValueError(
                "the approval decision subject is not the bound "
                "installation owner; refusing to trust possibly "
                "tampered data")

    def _load_verified_pair(self, operation_id):
        """Load and fully re-verify the prepared effect and the
        decision for operation_id through the decision service's own
        internal loaders, and re-verify their mutual binding. The
        public get methods are never called here: they refuse caller
        transactions."""
        effect = self.approval_decisions._load_prepared(operation_id)
        if effect is None:
            return None, None
        decision = self.approval_decisions._fetch_by_operation(
            operation_id)
        if decision is None:
            return effect, None
        self.approval_decisions._check_decision_binding(decision, effect)
        return effect, decision

    @staticmethod
    def _check_validity_chain(ctx, effect, decision, created_at, tx_now):
        """Enforce the complete time chain in this module: the decision
        service's internal binding check does not cover creation-time
        ordering.

        prepared.created_at <= decision.created_at <=
        reservation.created_at <= tx_now < reservation.expires_at =
        decision.expires_at <= prepared.expires_at <=
        task.scope.not_after. Stored-data violations fail closed with
        ValueError; only the current-time expiry boundary raises
        ApprovalReservationExpired.
        """
        if decision.created_at < effect.created_at:
            raise ValueError(
                "stored approval decision predates its prepared "
                "effect; refusing to trust possibly tampered data")
        if created_at < decision.created_at:
            raise ValueError(
                "stored approval reservation predates its approval "
                "decision; refusing to trust possibly tampered data")
        if tx_now < created_at:
            raise ValueError(
                "stored approval reservation was created after the "
                "current verification time; refusing to trust possibly "
                "tampered data")
        if tx_now >= decision.expires_at:
            raise ApprovalReservationExpired(
                "the decision validity period has passed; an expired "
                "decision is never reserved and replays never restore "
                "an expired reservation")
        if decision.expires_at > effect.expires_at:
            raise ValueError(
                "stored approval decision validity exceeds its "
                "prepared effect validity; refusing to trust possibly "
                "tampered data")
        if effect.expires_at > ctx.scope.not_after:
            raise ValueError(
                "stored prepared effect validity exceeds the resolved "
                "task credential scope validity; refusing to trust "
                "possibly tampered data")

    # -- row loading ------------------------------------------------------------

    def _reservation_from_payload(self, payload, blob):
        return ApprovalReservation(
            reservation_id=payload["reservation_id"],
            approval_id=payload["approval_id"],
            operation_id=payload["operation_id"],
            effect_digest=payload["effect_digest"],
            preparation_digest=payload["preparation_digest"],
            created_at=_load_time("created_at", payload["created_at"]),
            expires_at=_load_time("expires_at", payload["expires_at"]),
            record=_freeze(payload),
            record_bytes=blob)

    def _reservation_from_row(self, row):
        """Re-verify one stored row end to end: index columns, the row
        digest, the canonical blob and the exact blob/index-column
        agreement. The row digest alone is never treated as a full-row
        check."""
        (reservation_id, approval_id, operation_id, task_id, run_id,
         effect_digest, preparation_digest, created_at, expires_at,
         record_blob, row_sha256) = row
        _recheck_id("reservation_id", reservation_id)
        _recheck_id("approval_id", approval_id)
        _recheck_id("operation_id", operation_id)
        _recheck_id("task_id", task_id)
        _recheck_id("run_id", run_id)
        _recheck_digest(effect_digest)
        _recheck_digest(preparation_digest)
        _load_time("created_at", created_at)
        _load_time("expires_at", expires_at)
        _recheck_digest(row_sha256)
        payload = _loads_record(record_blob)
        blob = bytes(record_blob)
        if _digest_row(blob) != row_sha256:
            raise ValueError(
                "stored approval reservation row digest does not match "
                "its blob; refusing to trust possibly tampered data")
        index = (payload["reservation_id"], payload["approval_id"],
                 payload["operation_id"], payload["task_id"],
                 payload["run_id"], payload["effect_digest"],
                 payload["preparation_digest"], payload["created_at"],
                 payload["expires_at"])
        if index != (reservation_id, approval_id, operation_id,
                     task_id, run_id, effect_digest, preparation_digest,
                     created_at, expires_at):
            raise ValueError(
                "stored approval reservation index columns do not "
                "match its record; refusing to trust possibly tampered "
                "data")
        return self._reservation_from_payload(payload, blob)

    def _fetch_by_operation(self, operation_id):
        row = self.db.execute(
            "SELECT " + _COLUMNS + " FROM host_approval_reservations "
            "WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            return None
        return self._reservation_from_row(row)

    @staticmethod
    def _check_reservation_binding(stored, decision, effect):
        """The stored reservation must bind the very decision and
        prepared effect that were just fully re-verified; any drift
        fails closed."""
        envelope = effect.envelope
        record = stored.record
        decision_record = decision.record
        if record["approval_id"] != decision.approval_id \
                or record["operation_id"] != effect.operation_id \
                or record["effect_digest"] != effect.effect_digest \
                or record["preparation_digest"] \
                != effect.preparation_digest \
                or record["installation_id"] != envelope["installation_id"] \
                or record["company_id"] != envelope["company_id"] \
                or record["principal_id"] != envelope["principal_id"] \
                or record["member_revision"] \
                != envelope["member_revision"] \
                or record["task_id"] != envelope["task_id"] \
                or record["run_id"] != envelope["run_id"] \
                or record["policy_id"] != envelope["policy_id"] \
                or record["policy_revision"] \
                != envelope["policy_revision"] \
                or record["action"] != envelope["action"] \
                or record["target"] != envelope["target"] \
                or record["expires_at"] != decision_record["expires_at"]:
            raise ValueError(
                "stored approval reservation binding does not match "
                "its approval decision and prepared effect; refusing "
                "to trust possibly tampered data")

    # -- reservation (F-007-API-008 persistence slice) ---------------------------

    def reserve_approval(self, task_secret, *, now, operation_id,
                         approval_id, expected_effect_digest,
                         expected_preparation_digest):
        """Record the irreversible reservation of one approved decision
        for the current valid task.

        The identity is resolved from the real task credential inside
        the write transaction; a caller-supplied context, credential
        digest or owner session is never accepted, and the worker never
        holds the approver's secret. The approval_id and both expected
        digests must equal the fully verified decision and prepared
        effect, the decision must be 'approved', and the complete time
        chain must hold at the post-lock effective time. The
        reservation's expires_at is fixed to the decision's expires_at;
        the caller never supplies a validity period and no expiry can
        ever be reopened. On success exactly one row exists for the
        operation: an identical replay (same operation, approval,
        digests and complete binding) returns the original
        reservation_id and created_at and writes nothing after its
        stored created_at re-passes the complete time chain, while any
        changed content is refused with ApprovalReservationConflict and
        never overwrites. A revoked task credential, membership or
        policy, an advanced membership revision, an expired decision or
        a cross-task/cross-company operation all fail closed with zero
        writes; a missing operation, a missing decision and a foreign
        operation share the identical ApprovalReservationUnavailable
        error. This never consumes or executes the effect, and the
        stored reservation_state is the first-binding fact only, not
        the operation's current execution state.
        """
        # The prepared module's secret validator refuses lone surrogates
        # with a fixed, chain-empty error BEFORE the secret reaches the
        # task credential resolver, so no exception chain ever carries
        # the original input.
        task_secret = _require_secret("task_secret", task_secret)
        now = _require_time("now", now)
        # Captured immediately next to the validated trusted host time,
        # before ANY further validation or verification work: the
        # in-transaction effective time must account for all of that
        # elapsed time, not only for the write-lock wait.
        origin = _monotonic_ns()
        operation_id = self._require_id("operation_id", operation_id)
        approval_id = self._require_id("approval_id", approval_id)
        expected_effect_digest = _require_digest(
            "expected_effect_digest", expected_effect_digest)
        expected_preparation_digest = _require_digest(
            "expected_preparation_digest", expected_preparation_digest)

        def core():
            # Everything below runs strictly ordered inside this one
            # transaction on the shared connection: real task
            # resolution and current-membership verification first,
            # then the fully verified prepared effect and decision,
            # then the binding and time-chain checks, then the
            # idempotency lookup and the write. A pre-lock context is
            # never reused.
            tx_now = _effective_now(now, origin)
            ctx = self._resolve_task(task_secret, tx_now)
            effect, decision = self._load_verified_pair(operation_id)
            if effect is None or decision is None:
                # A missing preparation, a missing decision and a
                # foreign operation share one error, so other tasks'
                # content is never exposed.
                raise ApprovalReservationUnavailable(
                    _UNAVAILABLE_MESSAGE)
            envelope = effect.envelope
            if envelope["task_id"] != ctx.task_id \
                    or envelope["run_id"] != ctx.run_id:
                # A reservation for another task's operation is
                # indistinguishable from a missing one.
                raise ApprovalReservationUnavailable(
                    _UNAVAILABLE_MESSAGE)
            if envelope["installation_id"] != ctx.installation_id \
                    or envelope["company_id"] != ctx.company_id \
                    or envelope["principal_id"] != ctx.principal_id \
                    or envelope["member_revision"] != ctx.member_revision \
                    or envelope["policy_id"] != ctx.policy_id \
                    or envelope["policy_revision"] != ctx.policy_revision:
                raise ValueError(
                    "stored prepared effect binding does not match the "
                    "resolved task credential; refusing to trust "
                    "possibly tampered data")
            if approval_id != decision.approval_id \
                    or expected_effect_digest != effect.effect_digest \
                    or expected_preparation_digest \
                    != effect.preparation_digest:
                raise ApprovalReservationMismatch(
                    "the approval reference or expected digests do not "
                    "equal the verified decision and prepared effect; "
                    "an operation reference alone never records a "
                    "reservation")
            if decision.decision != "approved":
                raise ApprovalReservationDenied(
                    "the decision on this operation is a denial; a "
                    "denied decision is never reserved")
            self._check_installation_owner(ctx, envelope["principal_id"])
            self.prepared_effects._recheck_scope_fit(ctx, envelope)
            self._check_validity_chain(ctx, effect, decision, tx_now,
                                       tx_now)
            stored = self._fetch_by_operation(operation_id)
            if stored is not None:
                # Idempotent replay: the stored row is fully re-verified
                # and must bind the very decision and prepared effect
                # verified above, so it IS the identical request; the
                # first reservation_id and created_at stand and nothing
                # is written.
                self._check_reservation_binding(stored, decision, effect)
                # The complete time chain is re-checked against the
                # STORED created_at, not only against the newly
                # proposed tx_now: a forged old row predating its
                # decision (or postdating the verification time) is
                # refused, never replayed.
                self._check_validity_chain(ctx, effect, decision,
                                           stored.created_at, tx_now)
                return stored
            # Host-generated randomness; the caller can never supply a
            # reservation_id. It is the record id, not a token and not
            # approval-use proof.
            reservation_id = "har_" + secrets.token_hex(8)
            record = {
                "schema_version": _SCHEMA_VERSION,
                "reservation_state": _RESERVATION_STATE,
                "reservation_id": reservation_id,
                "approval_id": decision.approval_id,
                "operation_id": operation_id,
                "effect_digest": effect.effect_digest,
                "preparation_digest": effect.preparation_digest,
                "installation_id": envelope["installation_id"],
                "company_id": envelope["company_id"],
                "principal_id": envelope["principal_id"],
                "member_revision": envelope["member_revision"],
                "task_id": envelope["task_id"],
                "run_id": envelope["run_id"],
                "policy_id": envelope["policy_id"],
                "policy_revision": envelope["policy_revision"],
                "action": envelope["action"],
                "target": envelope["target"],
                "created_at": _store_time(tx_now),
                "expires_at": _store_time(decision.expires_at),
            }
            blob = _canonical_bytes(record)
            insert_failed = False
            try:
                self.db.execute(
                    "INSERT INTO host_approval_reservations ("
                    + _COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (reservation_id, decision.approval_id, operation_id,
                     envelope["task_id"], envelope["run_id"],
                     effect.effect_digest, effect.preparation_digest,
                     record["created_at"], record["expires_at"], blob,
                     _digest_row(blob)))
            except sqlite3.IntegrityError:
                # Only the failure fact leaves this handler; the
                # database exception object is never re-raised or
                # chained into a caller-visible error.
                insert_failed = True
            if insert_failed:
                # Backstop for a cross-connection race that committed a
                # reservation for the same operation between our lookup
                # and our insert: apply the very same replay-or-conflict
                # rule instead of failing the caller. A bare
                # reservation_id or approval_id collision (no such
                # operation row) becomes a fixed conflict error.
                raced = self._fetch_by_operation(operation_id)
                if raced is None:
                    raise ApprovalReservationConflict(
                        "the reservation, approval or operation "
                        "reference is already bound to another "
                        "reservation; a stored reservation is never "
                        "overwritten")
                self._check_reservation_binding(raced, decision, effect)
                # Same consistency rule as the plain replay path
                # above: the raced row's stored created_at must
                # re-pass the complete time chain before it is
                # returned.
                self._check_validity_chain(ctx, effect, decision,
                                           raced.created_at, tx_now)
                return raced
            # Return through the same fully verifying parse used by the
            # read path, so the returned object is exactly what a later
            # read-back will return.
            return self._reservation_from_payload(_loads_record(blob),
                                                  blob)
        return self._write_tx(core)

    # -- read-back -----------------------------------------------------------------

    def get_approval_reservation(self, task_secret, *, now, operation_id):
        """Return the still-valid reservation of the current task for
        operation_id.

        Any existing caller transaction is refused at entry (a
        caller-held snapshot could predate a revocation committed by
        another connection); this method always opens its own fresh
        short read snapshot and never closes, commits or rolls back the
        refused caller transaction. Establishing that snapshot may wait
        on the database lock, so the effective time is re-derived from
        the entry-time monotonic origin only after the first real read
        returns, and the task credential is really resolved with that
        effective time inside the same snapshot: a wait crossing the
        task, policy, decision or reservation validity boundary can
        never pass on the stale entry time. Authentication comes first;
        a missing operation and one belonging to another task then
        produce the identical ApprovalReservationUnavailable error, so
        other tasks' reservations are never exposed. The stored row is
        re-verified end to end (canonical bytes, row digest, field
        types, blob/index agreement), its complete binding is
        re-verified against the fully re-verified decision, prepared
        effect and re-resolved task credential, the current membership
        revision must exactly match the revision everything was
        recorded under, the re-verified decision must still be an
        approval - a stored row bound to a denial fails with the
        identical ApprovalReservationUnavailable as a missing
        operation, so forged history never upgrades a denial - and the
        full time chain is re-checked. An expired reservation fails
        with ApprovalReservationExpired and
        the old row is kept as a tombstone; there is no history entry
        point that reads it back as valid. This read grants no
        consumption, execution or enumeration authority.
        """
        task_secret = _require_secret("task_secret", task_secret)
        now = _require_time("now", now)
        # Captured immediately next to the validated trusted host time:
        # establishing the fresh read snapshot may wait on the database
        # lock, and that wait must count against every validity period
        # re-checked below.
        origin = _monotonic_ns()
        operation_id = self._require_id("operation_id", operation_id)

        def fetch():
            # The first real read is what actually establishes the
            # snapshot and absorbs any lock wait; the effective time is
            # derived only after it returns, so the wait counts.
            row = self.db.execute(
                "SELECT " + _COLUMNS
                + " FROM host_approval_reservations "
                "WHERE operation_id = ?", (operation_id,)).fetchone()
            tx_now = _effective_now(now, origin)
            ctx = self._resolve_task(task_secret, tx_now)
            if row is None:
                raise ApprovalReservationUnavailable(
                    _UNAVAILABLE_MESSAGE)
            reservation = self._reservation_from_row(row)
            record = reservation.record
            if record["task_id"] != ctx.task_id \
                    or record["run_id"] != ctx.run_id:
                # A foreign operation is indistinguishable from a
                # missing one.
                raise ApprovalReservationUnavailable(
                    _UNAVAILABLE_MESSAGE)
            if record["installation_id"] != ctx.installation_id \
                    or record["company_id"] != ctx.company_id \
                    or record["principal_id"] != ctx.principal_id \
                    or record["member_revision"] != ctx.member_revision \
                    or record["policy_id"] != ctx.policy_id \
                    or record["policy_revision"] != ctx.policy_revision:
                raise ValueError(
                    "stored approval reservation binding does not "
                    "match the resolved task credential; refusing to "
                    "trust possibly tampered data")
            effect = self.approval_decisions._load_prepared(
                operation_id)
            if effect is None:
                raise ValueError(
                    "stored approval reservation references a missing "
                    "prepared effect; refusing to trust possibly "
                    "tampered data")
            decision = self.approval_decisions._fetch_by_operation(
                operation_id)
            if decision is None:
                raise ValueError(
                    "stored approval reservation references a missing "
                    "approval decision; refusing to trust possibly "
                    "tampered data")
            self.approval_decisions._check_decision_binding(decision,
                                                            effect)
            if decision.decision != "approved":
                # Only an approved decision can ever back a
                # reservation; a stored row bound to a denial is
                # forged history and fails exactly like a missing
                # operation, so the denial is never upgraded into
                # availability.
                raise ApprovalReservationUnavailable(
                    _UNAVAILABLE_MESSAGE)
            self._check_reservation_binding(reservation, decision,
                                            effect)
            self._check_installation_owner(ctx, record["principal_id"])
            self.prepared_effects._recheck_scope_fit(
                ctx, effect.envelope)
            self._check_validity_chain(ctx, effect, decision,
                                       reservation.created_at, tx_now)
            return reservation
        return self._read(fetch)
