"""One-time approval/denial decision ledger for the SAM host (F-007 API-007).

Approved design baseline a5db11fbccbe86bd5bdce617e4b3ee7b8b13ae02,
F-007 P02 section 6: the one-time owner decision that binds to an
already verified prepared effect. This module records the decision
only; it never consumes, never reserves and never executes anything,
and it never marks an operation as accepted. A stored decision is an
immutable approval/denial fact, not an execution grant, and there is
no approval-use audit reference here.

This module implements decision persistence and read-back only:

- HostApprovalDecisionService(db, identity, authorization,
  task_credentials, prepared_effects) binds actual service instances
  that all share the very same sqlite3.Connection object and the very
  same identity / authorization / task-credential / prepared-effect
  instances. Every binding is checked BEFORE any DDL runs, and the
  connection must be idle at construction: session resolution, owner
  verification, prepared-effect re-verification and the decision write
  must live in one SQLite transaction on one connection. The module
  never opens a database, never chooses a data path, never reads
  environment variables and never touches the default app.db; it
  creates only its own table.
- approve_effect(session_secret, ...) takes the operator's session
  secret - never a task credential and never a caller-fabricated
  context. It validates the secret (str, non-empty, UTF-8 encodable)
  before handing it to the identity service, captures the trusted
  caller time and the monotonic origin at entry before any expensive
  verification, and re-derives the effective time inside the BEGIN
  IMMEDIATE write transaction, exactly as in app.host_prepared_effects.
  Inside that transaction it re-resolves the session for real, then
  re-reads the installation through the identity service's internal
  loader: only the session whose principal IS the installation's
  owner_principal_id may decide. The owner role label is never
  interpreted; installation binding alone decides eligibility.
- The prepared effect is loaded through the prepared module's own
  internal row loader (SELECT by operation_id, then the fully
  re-verifying prepared_effects._effect_from_row): the public
  get_prepared_effect is never called inside the write transaction,
  because it refuses caller transactions and would need the task
  secret. The task credential behind the prepared envelope is fetched
  by its task_id and re-resolved for real through the credential
  service's internal _resolve_core (event history, installation,
  membership, bound policy revision/status/validity, stored cap
  against the current policy scope, revocation). The stored credential
  digest is only read for this internal resolution; it is never copied
  into this module's table. Every identity/task/run/policy/revision
  field of the resolved context must equal the prepared envelope, the
  session principal must equal the preparation principal, and the
  literal scope fit is re-checked.
- A decision binds the exact operation_id plus BOTH digests of the
  fully verified prepared effect: expected_effect_digest and
  expected_preparation_digest must equal them exactly; an operation_id
  alone never approves anything. decision is exactly 'approved' or
  'denied'. The validity chain is tx_now < expires_at <=
  prepared.expires_at <= the current task scope not_after, with the
  prepared envelope's own created_at < expires_at re-verified by the
  prepared row loader.
- The stored row is immutable: UPDATE, DELETE and both INSERT OR
  REPLACE classes (by approval_id and by operation_id) are refused by
  triggers without relying on recursive_triggers, exactly as in the
  prepared module. approval_id is host-generated randomness (the
  record id, not a token); the caller can never supply it. The
  canonical record blob holds exactly schema_version, approval_id,
  operation_id, effect_digest, preparation_digest, installation_id,
  company_id, principal_id, member_revision, task_id, run_id,
  policy_id, policy_revision, session_ref, decision, created_at and
  expires_at, with fixed UTC six-digit-microsecond timestamps; the row
  digest is SHA-256 over the domain-separated UTF-8 text
  'sam-host-approval-decision row-v1', one single NUL byte and the
  blob. It proves storage consistency only; it is not a signature.
- Idempotency: a replay over the same operation with the identical
  effect/preparation digests, the same decision, the same expires_at
  and the same principal binding returns the FIRST approval_id,
  session_ref and created_at and writes nothing; the deciding session
  is re-resolved for real but never compared to the stored session_ref,
  so a new valid session of the same owner replays against the original
  audit record. Any changed decision, digest, validity or binding
  conflicts and never overwrites. An expired decision stays expired:
  the unique record is kept, no replay revives it, no new expiry
  reopens it and a denial can never be cleared to approve later.
  Concurrent opposite decisions produce exactly one winner; identical
  concurrent decisions return the same row; every failure rolls back
  completely.
- get_approval_decision refuses any existing caller transaction and
  always opens its own fresh short read snapshot; the effective time
  is re-derived only after the first real read has actually
  established the snapshot and absorbed any lock wait, so a wait
  crossing a validity boundary can never pass on the stale entry time.
  The session is authenticated first, then the owner binding, then the
  decision row; a missing operation and one belonging to another
  principal produce the identical unavailable error, so other
  principals' decisions are never exposed. The decision's complete
  prepared binding (operation, both digests, identity, task, run,
  policy and revision fields) is re-verified against the freshly
  re-verified prepared effect and the re-resolved task credential,
  and the full validity chain is re-checked. There is deliberately no
  entry point that enumerates decisions or reads expired history as
  valid.
- ApprovalDecision is plain frozen data: approval_id, operation_id,
  both prepared digests, decision, created_at, expires_at, the
  deep-frozen canonical record and its canonical bytes. to_dict()
  returns a brand-new mutable deep copy. The result carries no session
  or task secret and no credential digest, and has no authorization,
  reservation, consumption or execution methods.

Scope of this slice (explicitly NOT implemented here): approval
consumption, reservations, execution, the operation/attempt state
machine, the Commit Broker, HTTP routes, UI, Git access, default
feature gates and any change to the global app.db schema or
migrations. Error messages never copy caller input or secrets, and
exception chains never retain them: handlers record only the failure
kind or fact and raise fixed exceptions after leaving the handler, so
__cause__ and __context__ stay empty of input-carrying exceptions.

Connection and transaction contract: identical to
app.host_prepared_effects. The constructor takes only a
caller-provided sqlite3.Connection, idle at construction. The write
method owns exactly one BEGIN IMMEDIATE transaction, commits on
success and rolls back on any exception; calling it inside a caller
transaction is refused and leaves that transaction untouched. The
read method refuses any existing caller transaction outright, opens
its own fresh short snapshot and closes it with ROLLBACK; the refused
caller transaction is never closed, committed or rolled back by this
module.

Internal-interface reuse (sanctioned by the F-007 card): boundary
validators (_require_time, _require_label, _LABEL_MAX) come from
app.host_authorization, canonical record serialization and ID rules
from app.facts_tree.git_format, and the prepared module contributes
the secret/canonical/ID/time primitives (_require_secret,
_canonical_bytes, _freeze, _thaw, _store_time, _COLUMNS), its fully
verifying row loader _effect_from_row and its literal scope re-check
_recheck_scope_fit. The identity service's internal
_fetch_installation decides owner eligibility and the credential
service's internal _fetch_credential_by_task/_resolve_core re-verify
the bound task. No dependency module's source is modified and no
secret or credential digest is copied into this module's table.
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
from app.host_authorization import (
    HostAuthorizationService,
    _require_time,
)
from app.host_identity import HostIdentityService
from app.host_prepared_effects import (
    HostPreparedEffectService,
    _COLUMNS as _PREPARED_COLUMNS,
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
CREATE TABLE IF NOT EXISTS host_approval_decisions (
    approval_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    effect_digest TEXT NOT NULL,
    preparation_digest TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    record_blob BLOB NOT NULL,
    row_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS host_approval_decisions_no_update
BEFORE UPDATE ON host_approval_decisions BEGIN
    SELECT RAISE(ABORT, 'host approval decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_approval_decisions_no_delete
BEFORE DELETE ON host_approval_decisions BEGIN
    SELECT RAISE(ABORT, 'host approval decisions are append-only');
END;
-- INSERT OR REPLACE would implicitly delete the stored row before
-- inserting, and that delete does not fire the BEFORE DELETE trigger
-- while recursive_triggers is off (the SQLite default). BEFORE INSERT
-- runs before any conflict resolution while the old row is still
-- present, so these two guards make both REPLACE classes - by
-- approval_id and by the operation_id unique key - unable to overwrite
-- stored decision history.
CREATE TRIGGER IF NOT EXISTS host_approval_decisions_no_replace_approval
BEFORE INSERT ON host_approval_decisions
WHEN EXISTS (
    SELECT 1 FROM host_approval_decisions
    WHERE approval_id = NEW.approval_id
) BEGIN
    SELECT RAISE(ABORT, 'host approval decisions are append-only: INSERT OR REPLACE cannot overwrite an existing decision');
END;
CREATE TRIGGER IF NOT EXISTS host_approval_decisions_no_replace_operation
BEFORE INSERT ON host_approval_decisions
WHEN EXISTS (
    SELECT 1 FROM host_approval_decisions
    WHERE operation_id = NEW.operation_id
) BEGIN
    SELECT RAISE(ABORT, 'host approval decisions are append-only: INSERT OR REPLACE cannot overwrite an existing operation decision');
END;
"""

_COLUMNS = ("approval_id, operation_id, task_id, run_id, effect_digest, "
            "preparation_digest, decision, created_at, expires_at, "
            "record_blob, row_sha256")

# Domain-separated digest prefix: the exact UTF-8 domain text plus one
# single NUL byte, per the approved design.
_ROW_DOMAIN = b"sam-host-approval-decision row-v1\x00"

_SCHEMA_VERSION = 1
_DECISIONS = frozenset({"approved", "denied"})

_RECORD_KEYS = frozenset({
    "schema_version", "approval_id", "operation_id", "effect_digest",
    "preparation_digest", "installation_id", "company_id",
    "principal_id", "member_revision", "task_id", "run_id",
    "policy_id", "policy_revision", "session_ref", "decision",
    "created_at", "expires_at",
})

_UNAVAILABLE_MESSAGE = ("no approval decision is available for this "
                        "operation reference")


class HostApprovalDecisionError(Exception):
    """Base class for every approval-decision failure; fails closed."""


class ApprovalOwnerRequired(HostApprovalDecisionError):
    """Only the principal bound as the installation owner may record or
    read an approval decision; the owner role label is never used."""


class ApprovalPreparationMismatch(HostApprovalDecisionError):
    """The expected effect/preparation digests do not equal the fully
    verified prepared effect; an operation_id alone never decides."""


class ApprovalValidityDenied(HostApprovalDecisionError):
    """The requested decision validity exceeds the prepared effect
    validity; a decision never outlives its preparation."""


class ApprovalDecisionConflict(HostApprovalDecisionError):
    """The operation already carries a different decision; a stored
    decision is never overwritten, reversed or re-expired."""


class ApprovalDecisionUnavailable(HostApprovalDecisionError):
    """No approval decision is available for this operation reference:
    it does not exist or it does not belong to the current principal.
    The same error covers both cases so other principals' decisions are
    never exposed."""


class ApprovalDecisionExpired(HostApprovalDecisionError):
    """The requested or stored decision validity period has passed;
    replays never restore an expired decision."""


@dataclass(frozen=True)
class ApprovalDecision:
    """One immutable approval/denial decision as persisted by the host.

    Plain frozen data: no secret material, no credential digest and
    deliberately no methods that grant consumption, reservation,
    execution or any other authority. The record mapping and everything
    nested in it are deep-frozen (mapping proxies and tuples);
    record_bytes are the exact canonical record bytes. to_dict()
    returns a brand-new mutable deep copy, so a caller can never mutate
    the stored shape.
    """

    approval_id: str
    operation_id: str
    effect_digest: str
    preparation_digest: str
    decision: str
    created_at: datetime
    expires_at: datetime
    record: Mapping
    record_bytes: bytes

    def to_dict(self):
        """Return a brand-new mutable deep copy of the canonical
        record; mutating the copy never affects this ApprovalDecision."""
        return _thaw(self.record)


def _effective_now(base, origin):
    """In-transaction effective time, identical in shape to the
    prepared module's: the trusted base advanced by the monotonic
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
            "stored approval decision %s is not a non-empty string; "
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
            "stored approval decision %s failed to parse; refusing to "
            "trust possibly tampered data" % name)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "stored approval decision %s is naive; refusing to trust "
            "possibly tampered data" % name)
    parsed = parsed.astimezone(timezone.utc)
    if _store_time(parsed) != value:
        raise ValueError(
            "stored approval decision %s is not in the canonical UTC "
            "six-digit-microsecond format; refusing to trust possibly "
            "tampered data" % name)
    return parsed


def _digest_row(blob):
    return hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()


def _require_digest(name, value):
    if not isinstance(value, str):
        raise TypeError("%s must be the str SHA-256 hex digest of the "
                        "prepared effect being decided" % name)
    if len(value) != 64 \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError("%s must be exactly 64 lowercase hex "
                         "characters" % name)
    return value


def _require_decision(value):
    if not isinstance(value, str):
        raise TypeError("decision must be the str 'approved' or 'denied'")
    if value not in _DECISIONS:
        raise ValueError("decision must be exactly 'approved' or "
                         "'denied'")
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
            "stored approval decision %s failed re-validation; refusing "
            "to trust possibly tampered data" % name)
    return value


def _recheck_revision(name, value):
    # Only the built-in int type itself; a stored bool never masquerades
    # as a revision.
    if type(value) is not int or value < 1:
        raise ValueError(
            "stored approval decision %s is not a positive built-in "
            "int; refusing to trust possibly tampered data" % name)
    return value


def _recheck_digest(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError(
            "stored approval decision digest is malformed; refusing to "
            "trust possibly tampered data")
    return value


def _check_record(payload):
    """Re-verify one parsed canonical decision record, field by field."""
    if not isinstance(payload, dict) \
            or frozenset(payload) != _RECORD_KEYS:
        raise ValueError(
            "stored approval decision record has an unexpected shape; "
            "refusing to trust possibly tampered data")
    if type(payload["schema_version"]) is not int \
            or payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(
            "stored approval decision schema_version is not the "
            "built-in int 1; refusing to trust possibly tampered data")
    for name in ("approval_id", "operation_id", "installation_id",
                 "company_id", "principal_id", "task_id", "run_id",
                 "policy_id", "session_ref"):
        _recheck_id(name, payload[name])
    _recheck_revision("member_revision", payload["member_revision"])
    _recheck_revision("policy_revision", payload["policy_revision"])
    _recheck_digest(payload["effect_digest"])
    _recheck_digest(payload["preparation_digest"])
    if payload["decision"] not in _DECISIONS:
        raise ValueError(
            "stored approval decision decision is not 'approved' or "
            "'denied'; refusing to trust possibly tampered data")
    created = _load_time("created_at", payload["created_at"])
    expires = _load_time("expires_at", payload["expires_at"])
    if not created < expires:
        raise ValueError(
            "stored approval decision created_at is not before "
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
            "stored approval decision blob is not bytes; refusing to "
            "trust possibly tampered data")
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
            "stored approval decision blob failed to parse; refusing "
            "to trust possibly tampered data")
    serialized = True
    try:
        canonical = git_format.dumps_record(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        serialized = False
    if not serialized:
        raise ValueError(
            "stored approval decision blob failed re-serialization; "
            "refusing to trust possibly tampered data")
    if canonical != blob:
        raise ValueError(
            "stored approval decision blob is not in canonical record "
            "form; refusing to trust possibly tampered data")
    return _check_record(payload)


class HostApprovalDecisionService:
    """One-time owner approval/denial decisions on one shared connection.

    See the module docstring for the full scope list and the
    connection, transaction, identity, digest and idempotency
    contracts.
    """

    def __init__(self, db, identity, authorization, task_credentials,
                 prepared_effects):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host approval decisions require a caller-provided "
                "sqlite3.Connection; the module never opens or chooses "
                "a database path itself")
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host approval decisions require the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        if not isinstance(authorization, HostAuthorizationService):
            raise TypeError(
                "host approval decisions require the bound "
                "HostAuthorizationService; caller-reported policy "
                "state is never accepted")
        if not isinstance(task_credentials, HostTaskCredentialService):
            raise TypeError(
                "host approval decisions require the bound "
                "HostTaskCredentialService; caller-reported task "
                "context is never accepted")
        if not isinstance(prepared_effects, HostPreparedEffectService):
            raise TypeError(
                "host approval decisions require the bound "
                "HostPreparedEffectService; caller-reported "
                "preparation state is never accepted")
        # Same-object checks, before any DDL: session resolution, owner
        # verification, prepared-effect re-verification and the decision
        # write must share one connection so they share one SQLite
        # transaction. Any second connection - even to the same file -
        # could read a different database or a stale snapshot, so it is
        # refused.
        if identity.db is not db or authorization.db is not db \
                or task_credentials.db is not db \
                or prepared_effects.db is not db:
            raise ValueError(
                "host approval decisions require every bound service's "
                "own connection object; combining separate connections "
                "breaks the single-transaction verification guarantee")
        if authorization.identity is not identity \
                or task_credentials.identity is not identity \
                or task_credentials.authorization is not authorization \
                or prepared_effects.identity is not identity \
                or prepared_effects.authorization is not authorization \
                or prepared_effects.task_credentials is not task_credentials:
            raise ValueError(
                "host approval decisions require the ledger, the "
                "credential service and the prepared-effect service to "
                "be bound to the very same identity, authorization and "
                "credential instances; combining divergent instances "
                "breaks the single-transaction verification guarantee")
        # Checked before any DDL: a caller transaction is left
        # untouched, never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host approval decisions require an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.identity = identity
        self.authorization = authorization
        self.task_credentials = task_credentials
        self.prepared_effects = prepared_effects
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
                "host approval decision writes own their transaction; "
                "the connection must be idle at call time")
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
                "host approval decision reads open their own fresh "
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

    def _resolve_owner_session(self, session_secret, tx_now):
        """Really resolve the session and require the installation owner.

        Owner eligibility comes from the installation record's
        owner_principal_id only; the owner_role label is never
        interpreted, so a custom role text never grants or withholds the
        owner binding.
        """
        context = self.identity.resolve_session(session_secret,
                                                now=tx_now)
        installation = self.identity._fetch_installation()
        if installation is None \
                or installation["installation_id"] \
                != context.installation_id \
                or installation["company_id"] != context.company_id:
            raise ValueError(
                "stored installation binding does not match the "
                "resolved session; refusing to trust possibly tampered "
                "data")
        if context.principal_id != installation["owner_principal_id"]:
            raise ApprovalOwnerRequired(
                "only the principal bound as the installation owner "
                "may record or read an approval decision")
        return context

    def _load_prepared(self, operation_id):
        row = self.db.execute(
            "SELECT " + _PREPARED_COLUMNS
            + " FROM host_prepared_effects WHERE operation_id = ?",
            (operation_id,)).fetchone()
        if row is None:
            return None
        # Full re-verification through the prepared module's own row
        # loader: canonical bytes, row digest, both envelope digests,
        # field types and blob/index agreement.
        return self.prepared_effects._effect_from_row(row)

    def _resolve_prepared_task(self, effect, tx_now):
        """Re-resolve the task credential behind a verified effect.

        The stored credential digest is read only for this internal
        resolution; it is never copied into this module's table. The
        resolved context must equal the envelope binding exactly.
        """
        envelope = effect.envelope
        stored = self.task_credentials._fetch_credential_by_task(
            envelope["task_id"])
        if stored is None:
            raise ValueError(
                "stored prepared effect references a missing task "
                "credential; refusing to trust possibly tampered data")
        ctx = self.task_credentials._resolve_core(
            stored["credential_sha256"], tx_now)
        if envelope["run_id"] != ctx.run_id \
                or envelope["installation_id"] != ctx.installation_id \
                or envelope["company_id"] != ctx.company_id \
                or envelope["principal_id"] != ctx.principal_id \
                or envelope["member_revision"] != ctx.member_revision \
                or envelope["policy_id"] != ctx.policy_id \
                or envelope["policy_revision"] != ctx.policy_revision:
            raise ValueError(
                "stored prepared effect binding does not match the "
                "resolved task credential; refusing to trust possibly "
                "tampered data")
        self.prepared_effects._recheck_scope_fit(ctx, envelope)
        if effect.expires_at > ctx.scope.not_after:
            raise ValueError(
                "stored prepared effect validity exceeds the resolved "
                "task credential scope validity; refusing to trust "
                "possibly tampered data")
        return ctx

    # -- row loading ------------------------------------------------------------

    def _decision_from_payload(self, payload, blob):
        return ApprovalDecision(
            approval_id=payload["approval_id"],
            operation_id=payload["operation_id"],
            effect_digest=payload["effect_digest"],
            preparation_digest=payload["preparation_digest"],
            decision=payload["decision"],
            created_at=_load_time("created_at", payload["created_at"]),
            expires_at=_load_time("expires_at", payload["expires_at"]),
            record=_freeze(payload),
            record_bytes=blob)

    def _decision_from_row(self, row):
        """Re-verify one stored row end to end: index columns, the row
        digest, the canonical blob and the exact blob/index-column
        agreement. The row digest alone is never treated as a full-row
        check."""
        (approval_id, operation_id, task_id, run_id, effect_digest,
         preparation_digest, decision, created_at, expires_at,
         record_blob, row_sha256) = row
        _recheck_id("approval_id", approval_id)
        _recheck_id("operation_id", operation_id)
        _recheck_id("task_id", task_id)
        _recheck_id("run_id", run_id)
        _recheck_digest(effect_digest)
        _recheck_digest(preparation_digest)
        if decision not in _DECISIONS:
            raise ValueError(
                "stored approval decision index carries an unknown "
                "decision; refusing to trust possibly tampered data")
        _load_time("created_at", created_at)
        _load_time("expires_at", expires_at)
        _recheck_digest(row_sha256)
        payload = _loads_record(record_blob)
        blob = bytes(record_blob)
        if _digest_row(blob) != row_sha256:
            raise ValueError(
                "stored approval decision row digest does not match "
                "its blob; refusing to trust possibly tampered data")
        index = (payload["approval_id"], payload["operation_id"],
                 payload["task_id"], payload["run_id"],
                 payload["effect_digest"], payload["preparation_digest"],
                 payload["decision"], payload["created_at"],
                 payload["expires_at"])
        if index != (approval_id, operation_id, task_id, run_id,
                     effect_digest, preparation_digest, decision,
                     created_at, expires_at):
            raise ValueError(
                "stored approval decision index columns do not match "
                "its record; refusing to trust possibly tampered data")
        return self._decision_from_payload(payload, blob)

    def _fetch_by_operation(self, operation_id):
        row = self.db.execute(
            "SELECT " + _COLUMNS + " FROM host_approval_decisions "
            "WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            return None
        return self._decision_from_row(row)

    def _check_decision_binding(self, stored, effect):
        """The stored decision must bind the very prepared effect that
        was just fully re-verified; any drift fails closed."""
        envelope = effect.envelope
        record = stored.record
        if record["operation_id"] != effect.operation_id \
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
                != envelope["policy_revision"]:
            raise ValueError(
                "stored approval decision binding does not match its "
                "prepared effect; refusing to trust possibly tampered "
                "data")
        if stored.expires_at > effect.expires_at:
            raise ValueError(
                "stored approval decision validity exceeds its "
                "prepared effect validity; refusing to trust possibly "
                "tampered data")

    # -- decision (F-007-API-007 persistence slice) ---------------------------

    def approve_effect(self, session_secret, *, now, operation_id,
                       expected_effect_digest,
                       expected_preparation_digest, decision, expires_at):
        """Record the owner's one-time decision for one prepared effect.

        The identity is resolved from the real session inside the write
        transaction and must be the bound installation owner; a
        caller-supplied context or role label is never accepted. Both
        expected digests must equal the fully verified prepared effect.
        On success exactly one row exists for the operation: an
        identical replay (same digests, decision, expires_at and
        principal binding) returns the original approval_id,
        session_ref and created_at and writes nothing, while any change
        - including the opposite decision or a new expiry - is refused
        with ApprovalDecisionConflict and never overwrites. A revoked
        session, task credential, membership or policy, a non-owner
        session, an expired prepared effect or a decision validity
        beyond the preparation all fail closed with zero writes. A
        missing preparation and one bound to another principal,
        installation or company share the identical
        ApprovalDecisionUnavailable error, while a stale membership
        revision is refused as a binding failure instead. This never
        consumes, reserves or executes the effect.
        """
        session_secret = _require_secret("session_secret",
                                         session_secret)
        now = _require_time("now", now)
        # Captured immediately next to the validated trusted host time,
        # before ANY further validation or verification work: the
        # in-transaction effective time must account for all of that
        # elapsed time, not only for the write-lock wait.
        origin = _monotonic_ns()
        operation_id = self._require_id("operation_id", operation_id)
        expected_effect_digest = _require_digest(
            "expected_effect_digest", expected_effect_digest)
        expected_preparation_digest = _require_digest(
            "expected_preparation_digest", expected_preparation_digest)
        decision = _require_decision(decision)
        expires_at = _require_time("expires_at", expires_at)
        if now >= expires_at:
            raise ApprovalDecisionExpired(
                "the requested decision validity has already passed; "
                "an expired decision is never recorded")

        def core():
            # Everything below runs strictly ordered inside this one
            # transaction on the shared connection: real session
            # resolution and owner verification first, then the fully
            # verified prepared effect, then the real task credential
            # re-resolution, then the idempotency lookup and the write.
            # A pre-lock context is never reused.
            tx_now = _effective_now(now, origin)
            context = self._resolve_owner_session(session_secret,
                                                  tx_now)
            effect = self._load_prepared(operation_id)
            if effect is None \
                    or effect.envelope["principal_id"] \
                    != context.principal_id:
                # Missing and foreign operations share one error, so
                # other principals' preparations are never exposed.
                raise ApprovalDecisionUnavailable(_UNAVAILABLE_MESSAGE)
            envelope = effect.envelope
            if envelope["installation_id"] != context.installation_id \
                    or envelope["company_id"] != context.company_id:
                # A preparation bound to another installation or
                # company is foreign even under the same principal: it
                # is indistinguishable from a missing one.
                raise ApprovalDecisionUnavailable(_UNAVAILABLE_MESSAGE)
            if envelope["member_revision"] != context.member_revision:
                # A stale membership revision is a binding failure, not
                # a foreign operation: it must never be smoothed into
                # the unavailable error above.
                raise ValueError(
                    "stored prepared effect binding does not match the "
                    "resolved session; refusing to trust possibly "
                    "tampered data")
            if expected_effect_digest != effect.effect_digest \
                    or expected_preparation_digest \
                    != effect.preparation_digest:
                raise ApprovalPreparationMismatch(
                    "the expected digests do not equal the verified "
                    "prepared effect; an operation reference alone "
                    "never records a decision")
            self._resolve_prepared_task(effect, tx_now)
            if tx_now >= expires_at:
                raise ApprovalDecisionExpired(
                    "the decision validity period ended before the "
                    "write lock was acquired; an expired decision is "
                    "never recorded")
            if expires_at > effect.expires_at:
                raise ApprovalValidityDenied(
                    "the requested decision validity exceeds the "
                    "prepared effect validity; a decision never "
                    "outlives its preparation")
            if tx_now >= effect.expires_at:
                raise ApprovalDecisionExpired(
                    "the prepared effect validity period has passed; "
                    "an expired effect is never decided")
            stored = self._fetch_by_operation(operation_id)
            if stored is not None:
                self._check_decision_binding(stored, effect)
                identical = (
                    stored.decision == decision
                    and stored.expires_at == expires_at
                    and stored.effect_digest == expected_effect_digest
                    and stored.preparation_digest
                    == expected_preparation_digest)
                if identical:
                    if tx_now >= stored.expires_at:
                        raise ApprovalDecisionExpired(
                            "the stored decision validity period has "
                            "passed; replays never restore an expired "
                            "decision")
                    # Idempotent replay: the first approval_id,
                    # session_ref and created_at stand; the current
                    # session was really resolved but is never compared
                    # to the stored session_ref.
                    return stored
                raise ApprovalDecisionConflict(
                    "the operation already carries a different "
                    "decision; a stored decision is never overwritten, "
                    "reversed or re-expired")
            # Host-generated randomness; the caller can never supply an
            # approval_id. It is the record id, not a token.
            approval_id = "had_" + secrets.token_hex(8)
            record = {
                "schema_version": _SCHEMA_VERSION,
                "approval_id": approval_id,
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
                "session_ref": context.session_ref,
                "decision": decision,
                "created_at": _store_time(tx_now),
                "expires_at": _store_time(expires_at),
            }
            blob = _canonical_bytes(record)
            insert_failed = False
            try:
                self.db.execute(
                    "INSERT INTO host_approval_decisions (" + _COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (approval_id, operation_id, envelope["task_id"],
                     envelope["run_id"], effect.effect_digest,
                     effect.preparation_digest, decision,
                     record["created_at"], record["expires_at"], blob,
                     _digest_row(blob)))
            except sqlite3.IntegrityError:
                # Only the failure fact leaves this handler; the
                # database exception object is never re-raised or
                # chained into a caller-visible error.
                insert_failed = True
            if insert_failed:
                # Backstop for a cross-connection race that committed a
                # decision for the same operation between our lookup
                # and our insert: apply the very same replay-or-conflict
                # rule instead of failing the caller. A bare
                # approval_id collision (no such operation row) becomes
                # a fixed conflict error.
                raced = self._fetch_by_operation(operation_id)
                if raced is None:
                    raise ApprovalDecisionConflict(
                        "the approval reference or operation is already "
                        "bound to another decision; a stored decision "
                        "is never overwritten")
                self._check_decision_binding(raced, effect)
                if raced.decision == decision \
                        and raced.expires_at == expires_at \
                        and raced.effect_digest \
                        == expected_effect_digest \
                        and raced.preparation_digest \
                        == expected_preparation_digest:
                    return raced
                raise ApprovalDecisionConflict(
                    "the operation already carries a different "
                    "decision; a stored decision is never overwritten, "
                    "reversed or re-expired")
            # Return through the same fully verifying parse used by the
            # read path, so the returned object is exactly what a later
            # read-back will return.
            return self._decision_from_payload(_loads_record(blob), blob)
        return self._write_tx(core)

    # -- read-back -----------------------------------------------------------------

    def get_approval_decision(self, session_secret, *, now, operation_id):
        """Return the still-valid decision of the current owner for
        operation_id.

        Any existing caller transaction is refused at entry (a
        caller-held snapshot could predate a revocation committed by
        another connection); this method always opens its own fresh
        short read snapshot and never closes, commits or rolls back the
        refused caller transaction. Establishing that snapshot may wait
        on the database lock, so the effective time is re-derived from
        the entry-time monotonic origin only after the first real read
        returns, and the session is really resolved with that effective
        time inside the same snapshot: a wait crossing the session,
        task, policy, prepared or decision validity boundary can never
        pass on the stale entry time. Authentication comes first; a
        missing operation and one belonging to another principal then
        produce the identical ApprovalDecisionUnavailable error, so
        other principals' decisions are never exposed. The stored row
        is re-verified end to end (canonical bytes, row digest, field
        types, blob/index agreement), its complete prepared binding is
        re-verified against the fully re-verified prepared effect and
        the re-resolved task credential, the session's current
        membership revision must exactly match the revision the
        decision and the preparation were recorded under, and the
        validity chain is re-checked. An expired decision fails with
        ApprovalDecisionExpired; there is no history entry point that
        reads it back as valid. This read grants no consumption,
        reservation, execution or enumeration authority.
        """
        session_secret = _require_secret("session_secret",
                                         session_secret)
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
                "SELECT " + _COLUMNS + " FROM host_approval_decisions "
                "WHERE operation_id = ?", (operation_id,)).fetchone()
            tx_now = _effective_now(now, origin)
            context = self._resolve_owner_session(session_secret,
                                                  tx_now)
            if row is None:
                raise ApprovalDecisionUnavailable(_UNAVAILABLE_MESSAGE)
            decision = self._decision_from_row(row)
            record = decision.record
            if record["principal_id"] != context.principal_id \
                    or record["installation_id"] \
                    != context.installation_id \
                    or record["company_id"] != context.company_id:
                # A foreign operation is indistinguishable from a
                # missing one.
                raise ApprovalDecisionUnavailable(_UNAVAILABLE_MESSAGE)
            effect = self._load_prepared(operation_id)
            if effect is None:
                raise ValueError(
                    "stored approval decision references a missing "
                    "prepared effect; refusing to trust possibly "
                    "tampered data")
            self._check_decision_binding(decision, effect)
            if record["member_revision"] != context.member_revision \
                    or effect.envelope["member_revision"] \
                    != context.member_revision:
                # The real session's current membership revision must
                # exactly match the revision the decision and the
                # preparation were recorded under. The task re-
                # resolution alone cannot catch a revision that advanced
                # while the member stayed active: it reports the
                # issuance-time stored revision, not the session's.
                raise ValueError(
                    "stored approval decision does not match the "
                    "current membership revision; refusing to trust "
                    "possibly tampered or stale data")
            self._resolve_prepared_task(effect, tx_now)
            if tx_now >= decision.expires_at:
                raise ApprovalDecisionExpired(
                    "the decision validity period has passed; replays "
                    "never restore an expired decision")
            return decision
        return self._read(fetch)
