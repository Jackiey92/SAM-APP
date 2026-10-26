"""Immutable prepared-effect envelopes for the SAM host (F-007 API-006).

Approved design baseline 4672fc524dd9946e909f66e3a41ace3f20648eeb,
F-007 P02 sections 2-5: the immutable PREPARED record of an exactly
described effect. Once an accurate effect plan exists, this service
persists its preparation envelope so a FUTURE approval can bind to it.
This slice never approves, never consumes, never reserves and never
executes anything; preparation_state 'prepared' is an immutable
preparation fact, not an execution state, and there is no operation
state machine here.

This module implements persistence and read-back only:

- HostPreparedEffectService(db, identity, authorization,
  task_credentials) binds actual service instances that all share the
  very same sqlite3.Connection object and the very same identity /
  authorization instances. Every binding is checked BEFORE any DDL
  runs, and the connection must be idle at construction: preparation,
  identity resolution and policy re-verification must live in one
  SQLite transaction on one connection. The module never opens a
  database, never chooses a data path, never reads environment
  variables and never touches the default app.db; it creates only its
  own table.
- prepare_effect(task_secret, ...) captures the monotonic origin
  immediately at entry, right next to the validated trusted caller
  time and before any further validation, canonical encoding or digest
  work, then re-derives the effective time inside the BEGIN IMMEDIATE
  write transaction (trusted caller base advanced by the observed
  monotonic elapsed time, exactly as in app.host_authorization and
  app.host_task_credentials) and only then re-resolves the task
  credential through the real
  HostTaskCredentialService.resolve_task_credential inside that
  transaction: credential event history, current installation binding,
  current membership, the bound policy's current revision, status and
  validity period, and the stored issuance-time cap against the current
  policy scope are all re-verified on every call. A pre-lock context
  is never reused. The effect must satisfy tx_now < expires_at <=
  scope.not_after; created_at is tx_now. Encoding work or a lock wait
  crossing the expires boundary can never write on a stale pre-lock
  time.
- Scope checks are literal only: action must be an exact member of
  ctx.scope.actions, target must equal ctx.scope.target, and every
  requested object must be an exact member of ctx.scope.objects. No
  wildcard, URI-prefix, trimming or case folding is interpreted. This
  is a literal scope-membership check only; it does not claim field,
  read-window, egress, budget or actual resource authorization.
- The effect binds the exact action, target, string-sorted objects,
  effect_manifest, object_format/base_commit and dependency_versions.
  object_format is 'sha1' or 'sha256' and base_commit is respectively
  40 or 64 lowercase hex characters; Git is never accessed and the
  commit's existence is never verified. effect_manifest is a
  host-provided dict validated purely by git_format's canonical record
  rules (types, depth, byte budget); the service never reads real
  material and sensitive content or credentials must not be placed in
  this metadata. dependency_versions is a (possibly empty) dict of
  non-empty str keys to non-empty str values, each at most 200
  characters, kept literally; null, nested containers, lists, numbers
  and bools are refused both at write time and on read-back. This
  binds declared dependencies only; it does not prove they are still
  valid.
- Digests are domain-separated SHA-256 over the canonical git_format
  record bytes, each domain terminated by a single NUL byte:
  effect_digest over 'sam-host-prepared-effect effect-v1' and the
  effect payload (action, target, sorted objects, effect_manifest,
  object_format, base_commit, dependency_versions); preparation_digest
  over 'sam-host-prepared-effect preparation-v1' and the preparation
  payload (schema_version, installation/company/principal binding,
  member_revision, task_id, run_id, policy_id, policy_revision,
  idempotency_key, fixed-format expires_at, effect_digest) - the random
  operation_id and created_at are deliberately excluded so idempotent
  replays are stable; row_sha256 over 'sam-host-prepared-effect row-v1'
  and the complete canonical envelope blob, so the row digest also
  covers operation_id and created_at. All digests are 64 lowercase hex
  characters with no prefix. They prove storage consistency only; they
  are not signatures and not external tamper-proofing.
- The stored row is immutable: UPDATE, DELETE and INSERT OR REPLACE are
  refused by triggers. Because an implicit REPLACE delete does not fire
  BEFORE DELETE triggers with the default recursive_triggers=off, a
  BEFORE INSERT guard refuses inserts over an existing operation_id and
  over an existing (task_id, run_id, idempotency_key) triple, so
  neither REPLACE class can overwrite history. The unique key is
  (task_id, run_id, idempotency_key). A replay with the identical
  preparation returns the original operation_id and created_at and adds
  no row; the same key with any changed effect, base, dependency,
  target, object, identity/policy binding or validity period is refused
  and never overwrites. Cross-connection races on one key create at
  most one row. Expired or revoked authorization can never be revived
  by replay, because every replay re-resolves the credential and
  re-checks the validity period first.
- operation_id is host-generated randomness ('hop_' + 64 bits from
  secrets.token_hex(8)); the caller can never supply it.
- get_prepared_effect refuses any existing caller transaction and
  always opens its own fresh short read snapshot (a caller-held
  snapshot could predate a revocation committed by another
  connection); the monotonic origin is captured at entry next to the
  trusted caller time, and the effective time is re-derived only after
  the first real read has actually established the snapshot and
  absorbed any lock wait, so a wait crossing the task, policy or
  prepared validity boundary can never pass on the stale entry time.
  The task credential is resolved with that effective time inside that
  very same snapshot, and the envelope is returned only if it belongs
  to the same current root task and is still valid. A missing operation and
  an operation belonging to another task produce the identical
  unavailable error; other tasks' contents are never exposed. Every
  read re-verifies the canonical bytes, the row digest, both envelope
  digests, field types, and the consistency between the blob and the
  index columns, and re-checks the binding, scope fit, the created_at
  < expires_at relation and the validity period against the freshly
  resolved context (including expires_at <= the current scope
  not_after). Reopening a connection never resets history. There is
  deliberately no entry point that enumerates prepared effects by
  login alone.
- PreparedEffect is plain frozen data: operation_id, effect_digest,
  preparation_digest, created_at, expires_at, the deep-frozen canonical
  envelope and its canonical bytes. to_dict() returns a brand-new
  mutable deep copy. The result carries no session or task secret and
  no credential digest, and has no authorization or execution methods.

Scope of this slice (explicitly NOT implemented here): one-time
approvals and approval consumption, reservations, execution, the
operation/attempt state machine, the Commit Broker, HTTP routes, UI,
Git access, source reading, default feature gates and any change to
the global app.db schema or migrations. Error messages never copy
caller input or secrets, and exception chains never retain them
either: handlers record only the failure kind or fact and raise fixed
exceptions after leaving the handler, so __cause__ and __context__
stay empty of input-carrying exceptions.

Connection and transaction contract: identical to app.host_identity,
app.host_authorization and app.host_task_credentials, with one
deliberate tightening on reads. The constructor takes only a
caller-provided sqlite3.Connection, idle at construction. Write
methods own exactly one BEGIN IMMEDIATE transaction, commit on
success and roll back on any exception; calling one inside a caller
transaction is refused and leaves that transaction untouched. Read
methods refuse any existing caller transaction outright (a
caller-held snapshot could predate a revocation committed by another
connection) and always open their own fresh short snapshot, closed
with ROLLBACK; the refused caller transaction is never closed,
committed or rolled back by this module.

Internal-interface reuse (sanctioned by the F-007 card): boundary
validators (_require_time, _require_label, _LABEL_MAX) are imported
from app.host_authorization, canonical record serialization and ID
rules come from app.facts_tree.git_format, and task resolution is the
credential service's real resolve_task_credential. No dependency
module's source is modified and no credential digest is copied into
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
from types import MappingProxyType

from app.facts_tree import git_format
from app.host_authorization import (
    HostAuthorizationService,
    _LABEL_MAX,
    _require_label,
    _require_time,
)
from app.host_identity import HostIdentityService
from app.host_task_credentials import HostTaskCredentialService

# Monotonic time source for the in-transaction effective time; a plain
# module attribute so deterministic tests can simulate a contended
# write-lock wait, exactly as in app.host_authorization and
# app.host_task_credentials.
_monotonic_ns = time.monotonic_ns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_prepared_effects (
    operation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    effect_digest TEXT NOT NULL,
    preparation_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    record_blob BLOB NOT NULL,
    row_sha256 TEXT NOT NULL,
    UNIQUE (task_id, run_id, idempotency_key)
);
CREATE TRIGGER IF NOT EXISTS host_prepared_effects_no_update
BEFORE UPDATE ON host_prepared_effects BEGIN
    SELECT RAISE(ABORT, 'host prepared effects are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_prepared_effects_no_delete
BEFORE DELETE ON host_prepared_effects BEGIN
    SELECT RAISE(ABORT, 'host prepared effects are append-only');
END;
-- INSERT OR REPLACE would implicitly delete the stored row before
-- inserting, and that delete does not fire the BEFORE DELETE trigger
-- while recursive_triggers is off (the SQLite default). BEFORE INSERT
-- runs before any conflict resolution while the old row is still
-- present, so these two guards make both REPLACE classes - by
-- operation_id and by the (task_id, run_id, idempotency_key) unique
-- key - unable to overwrite stored preparation history.
CREATE TRIGGER IF NOT EXISTS host_prepared_effects_no_replace_operation
BEFORE INSERT ON host_prepared_effects
WHEN EXISTS (
    SELECT 1 FROM host_prepared_effects
    WHERE operation_id = NEW.operation_id
) BEGIN
    SELECT RAISE(ABORT, 'host prepared effects are append-only: INSERT OR REPLACE cannot overwrite an existing operation');
END;
CREATE TRIGGER IF NOT EXISTS host_prepared_effects_no_replace_idempotency
BEFORE INSERT ON host_prepared_effects
WHEN EXISTS (
    SELECT 1 FROM host_prepared_effects
    WHERE task_id = NEW.task_id AND run_id = NEW.run_id
      AND idempotency_key = NEW.idempotency_key
) BEGIN
    SELECT RAISE(ABORT, 'host prepared effects are append-only: INSERT OR REPLACE cannot overwrite an existing idempotency key');
END;
"""

_COLUMNS = ("operation_id, task_id, run_id, idempotency_key, "
            "effect_digest, preparation_digest, created_at, expires_at, "
            "record_blob, row_sha256")

# Domain-separated digest prefixes: the exact UTF-8 domain text plus
# one single NUL byte, per the approved design.
_EFFECT_DOMAIN = b"sam-host-prepared-effect effect-v1\x00"
_PREPARATION_DOMAIN = b"sam-host-prepared-effect preparation-v1\x00"
_ROW_DOMAIN = b"sam-host-prepared-effect row-v1\x00"

_SCHEMA_VERSION = 1
_PREPARATION_STATE = "prepared"

_OBJECT_FORMATS = frozenset({"sha1", "sha256"})
_OID_LENGTH = {"sha1": 40, "sha256": 64}
_LOWER_HEX = frozenset("0123456789abcdef")

_ENVELOPE_KEYS = frozenset({
    "schema_version", "preparation_state", "operation_id",
    "installation_id", "company_id", "principal_id", "member_revision",
    "task_id", "run_id", "policy_id", "policy_revision",
    "idempotency_key", "action", "target", "objects", "effect_manifest",
    "object_format", "base_commit", "dependency_versions",
    "effect_digest", "preparation_digest", "created_at", "expires_at",
})

_EFFECT_PAYLOAD_KEYS = (
    "action", "target", "objects", "effect_manifest", "object_format",
    "base_commit", "dependency_versions",
)

_PREPARATION_PAYLOAD_KEYS = (
    "schema_version", "installation_id", "company_id", "principal_id",
    "member_revision", "task_id", "run_id", "policy_id",
    "policy_revision", "idempotency_key", "expires_at", "effect_digest",
)

_UNAVAILABLE_MESSAGE = ("no prepared effect is available for this "
                        "operation reference")


class HostPreparedEffectError(Exception):
    """Base class for every prepared-effect failure; fails closed."""


class PreparedScopeDenied(HostPreparedEffectError):
    """The requested effect is wider than the resolved task scope."""


class PreparedEffectConflict(HostPreparedEffectError):
    """The idempotency key is already bound to a different preparation."""


class PreparedEffectUnavailable(HostPreparedEffectError):
    """No prepared effect is available for this operation reference:
    it does not exist or it does not belong to the current task. The
    same error covers both cases so other tasks' content is never
    exposed."""


class PreparedEffectExpired(HostPreparedEffectError):
    """The requested or stored effect validity period has passed;
    replays never restore an expired effect."""


@dataclass(frozen=True)
class PreparedEffect:
    """One immutable prepared-effect envelope as persisted by the host.

    Plain frozen data: no secret material, no credential digest and
    deliberately no methods that grant approval, Git-write or execution
    authority. The envelope mapping and everything nested in it are
    deep-frozen (mapping proxies and tuples); envelope_bytes are the
    exact canonical record bytes. to_dict() returns a brand-new mutable
    deep copy, so a caller can never mutate the stored shape.
    """

    operation_id: str
    effect_digest: str
    preparation_digest: str
    created_at: datetime
    expires_at: datetime
    envelope: Mapping
    envelope_bytes: bytes

    def to_dict(self):
        """Return a brand-new mutable deep copy of the canonical
        envelope; mutating the copy never affects this PreparedEffect."""
        return _thaw(self.envelope)


def _effective_now(base, origin):
    """In-transaction effective time, identical in shape to the
    authorization ledger's: the trusted base advanced by the monotonic
    elapsed time since call entry, floored to whole microseconds, so a
    write-lock wait crossing an expiry boundary can never prepare on a
    stale pre-lock time and the effective time can never lead the
    observed monotonic elapsed time."""
    elapsed_ns = _monotonic_ns() - origin
    if elapsed_ns < 0:
        elapsed_ns = 0
    return base + timedelta(microseconds=elapsed_ns // 1000)


def _store_time(value):
    # Fixed six-digit microseconds, always UTC: the canonical stored
    # timestamp format for this module.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _load_time(name, value):
    if not isinstance(value, str) or not value:
        raise ValueError(
            "stored prepared effect %s is not a non-empty string; "
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
            "stored prepared effect %s failed to parse; refusing to "
            "trust possibly tampered data" % name)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "stored prepared effect %s is naive; refusing to trust "
            "possibly tampered data" % name)
    parsed = parsed.astimezone(timezone.utc)
    if _store_time(parsed) != value:
        raise ValueError(
            "stored prepared effect %s is not in the canonical UTC "
            "six-digit-microsecond format; refusing to trust possibly "
            "tampered data" % name)
    return parsed


def _require_secret(name, value):
    if not isinstance(value, str):
        raise TypeError("%s must be a str credential" % name)
    if not value:
        raise ValueError("%s must not be empty" % name)
    # A secret containing lone surrogates would surface as a raw
    # UnicodeEncodeError carrying the whole secret deep inside the task
    # credential resolver's digest step; refuse it here first with a
    # fixed error whose chain is empty. This is an encodability check
    # only: any valid UTF-8 text remains acceptable.
    encodable = True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        encodable = False
    if not encodable:
        raise ValueError("%s must be UTF-8 encodable" % name)
    return value


def _freeze(value):
    """Deep-freeze nested record content: dicts become read-only
    mapping proxies and lists become tuples."""
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    """Build a brand-new mutable deep copy of frozen record content."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _canonical_bytes(payload):
    """Serialize through git_format's canonical record rules.

    git_format's own errors carry no caller input; a UnicodeEncodeError
    from encoding lone-surrogate text WOULD carry the offending input,
    so only the failure fact is recorded inside the handler and the
    fixed exception is raised after leaving it, with an empty chain.
    """
    encodable = True
    try:
        blob = git_format.dumps_record(payload)
    except UnicodeEncodeError:
        encodable = False
    if not encodable:
        raise ValueError(
            "a record payload contains text that is not UTF-8 "
            "encodable")
    return blob


def _digest_effect(effect_payload):
    return hashlib.sha256(
        _EFFECT_DOMAIN + _canonical_bytes(effect_payload)).hexdigest()


def _digest_preparation(preparation_payload):
    return hashlib.sha256(
        _PREPARATION_DOMAIN
        + _canonical_bytes(preparation_payload)).hexdigest()


def _digest_row(blob):
    return hashlib.sha256(_ROW_DOMAIN + blob).hexdigest()


def _check_object_format(value):
    if not isinstance(value, str):
        raise TypeError("object_format must be the str 'sha1' or 'sha256'")
    if value not in _OBJECT_FORMATS:
        raise ValueError(
            "object_format must be 'sha1' or 'sha256'; this slice "
            "prepares only envelopes based on an existing Git commit "
            "and never verifies the commit's existence")
    return value


def _check_base_commit(object_format, value):
    if not isinstance(value, str):
        raise TypeError("base_commit must be a lowercase hex str")
    if len(value) != _OID_LENGTH[object_format] \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError(
            "base_commit must be exactly 40 lowercase hex characters "
            "for sha1 or 64 for sha256")
    return value


def _check_dependency_versions(value):
    if isinstance(value, bool) or not isinstance(value, dict):
        raise TypeError(
            "dependency_versions must be a (possibly empty) dict of "
            "str to str; null is not accepted")
    for key, item in value.items():
        # _require_label enforces str (never bool), non-empty and the
        # host label length bound; nested containers, lists, numbers,
        # bools and nulls all fail the str check.
        _require_label("dependency_versions key", key)
        _require_label("dependency_versions value", item)
    return value


def _check_objects_shape(objects):
    if isinstance(objects, bool) or not isinstance(objects, (list, tuple)):
        raise TypeError("objects must be a non-empty list or tuple of str")
    if not objects:
        raise ValueError("objects must name at least one entry")
    entries = tuple(_require_label("objects entry", entry)
                    for entry in objects)
    if len(set(entries)) != len(entries):
        raise ValueError("objects must not contain duplicate entries")
    return entries


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
            "stored prepared effect %s failed re-validation; refusing "
            "to trust possibly tampered data" % name)
    return value


def _recheck_label(name, value):
    if not isinstance(value, str) or not value or len(value) > _LABEL_MAX:
        raise ValueError(
            "stored prepared effect %s is not a valid label; refusing "
            "to trust possibly tampered data" % name)
    return value


def _recheck_revision(name, value):
    # Only the built-in int type itself; a stored bool never masquerades
    # as a revision.
    if type(value) is not int or value < 1:
        raise ValueError(
            "stored prepared effect %s is not a positive built-in int; "
            "refusing to trust possibly tampered data" % name)
    return value


def _recheck_digest(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError(
            "stored prepared effect digest is malformed; refusing to "
            "trust possibly tampered data")
    return value


def _recheck_base_commit(object_format, value):
    length = _OID_LENGTH.get(object_format)
    if length is None or not isinstance(value, str) \
            or len(value) != length \
            or any(char not in _LOWER_HEX for char in value):
        raise ValueError(
            "stored prepared effect base_commit is malformed; refusing "
            "to trust possibly tampered data")
    return value


def _recheck_dependency_versions(value):
    if not isinstance(value, dict):
        raise ValueError(
            "stored prepared effect dependency_versions is not an "
            "object; refusing to trust possibly tampered data")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) \
                or not key or not item \
                or len(key) > _LABEL_MAX or len(item) > _LABEL_MAX:
            raise ValueError(
                "stored prepared effect dependency_versions failed "
                "re-validation; refusing to trust possibly tampered "
                "data")
    return value


def _check_envelope(payload):
    """Re-verify one parsed canonical envelope, field by field, and
    recompute both envelope digests from its payload."""
    if not isinstance(payload, dict) \
            or frozenset(payload) != _ENVELOPE_KEYS:
        raise ValueError(
            "stored prepared effect envelope has an unexpected shape; "
            "refusing to trust possibly tampered data")
    if type(payload["schema_version"]) is not int \
            or payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(
            "stored prepared effect schema_version is not the built-in "
            "int 1; refusing to trust possibly tampered data")
    if payload["preparation_state"] != _PREPARATION_STATE:
        raise ValueError(
            "stored prepared effect preparation_state is not "
            "'prepared'; refusing to trust possibly tampered data")
    for name in ("operation_id", "installation_id", "company_id",
                 "principal_id", "task_id", "run_id", "policy_id",
                 "idempotency_key"):
        _recheck_id(name, payload[name])
    _recheck_revision("member_revision", payload["member_revision"])
    _recheck_revision("policy_revision", payload["policy_revision"])
    _recheck_label("action", payload["action"])
    _recheck_label("target", payload["target"])
    objects = payload["objects"]
    if not isinstance(objects, list) or not objects:
        raise ValueError(
            "stored prepared effect objects are not a non-empty list; "
            "refusing to trust possibly tampered data")
    for entry in objects:
        _recheck_label("objects entry", entry)
    if len(set(objects)) != len(objects):
        raise ValueError(
            "stored prepared effect objects contain duplicates; "
            "refusing to trust possibly tampered data")
    if objects != sorted(objects):
        raise ValueError(
            "stored prepared effect objects are not string-sorted; "
            "refusing to trust possibly tampered data")
    if not isinstance(payload["effect_manifest"], dict):
        raise ValueError(
            "stored prepared effect manifest is not an object; "
            "refusing to trust possibly tampered data")
    if payload["object_format"] not in _OBJECT_FORMATS:
        raise ValueError(
            "stored prepared effect object_format is unknown; refusing "
            "to trust possibly tampered data")
    _recheck_base_commit(payload["object_format"],
                         payload["base_commit"])
    _recheck_dependency_versions(payload["dependency_versions"])
    _recheck_digest(payload["effect_digest"])
    _recheck_digest(payload["preparation_digest"])
    created = _load_time("created_at", payload["created_at"])
    expires = _load_time("expires_at", payload["expires_at"])
    if not created < expires:
        raise ValueError(
            "stored prepared effect created_at is not before "
            "expires_at; refusing to trust possibly tampered data")
    effect_payload = {key: payload[key] for key in _EFFECT_PAYLOAD_KEYS}
    if _digest_effect(effect_payload) != payload["effect_digest"]:
        raise ValueError(
            "stored prepared effect digest does not match its payload; "
            "refusing to trust possibly tampered data")
    preparation_payload = {key: payload[key]
                           for key in _PREPARATION_PAYLOAD_KEYS}
    if _digest_preparation(preparation_payload) \
            != payload["preparation_digest"]:
        raise ValueError(
            "stored prepared effect preparation digest does not match "
            "its payload; refusing to trust possibly tampered data")
    return payload


def _loads_envelope(blob):
    """Parse stored envelope bytes and fully re-verify them.

    The stored bytes must BE the canonical serialization of their
    payload: the parsed content is re-serialized and compared byte for
    byte, so a non-canonical (even if self-consistently hashed) blob
    fails closed.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError(
            "stored prepared effect blob is not bytes; refusing to "
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
            "stored prepared effect blob failed to parse; refusing to "
            "trust possibly tampered data")
    serialized = True
    try:
        canonical = git_format.dumps_record(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        serialized = False
    if not serialized:
        raise ValueError(
            "stored prepared effect blob failed re-serialization; "
            "refusing to trust possibly tampered data")
    if canonical != blob:
        raise ValueError(
            "stored prepared effect blob is not in canonical record "
            "form; refusing to trust possibly tampered data")
    return _check_envelope(payload)


class HostPreparedEffectService:
    """Immutable prepared-effect envelopes on one shared host connection.

    See the module docstring for the full scope list and the
    connection, transaction, identity, digest and idempotency
    contracts.
    """

    def __init__(self, db, identity, authorization, task_credentials):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host prepared effects require a caller-provided "
                "sqlite3.Connection; the module never opens or chooses "
                "a database path itself")
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host prepared effects require the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        if not isinstance(authorization, HostAuthorizationService):
            raise TypeError(
                "host prepared effects require the bound "
                "HostAuthorizationService; caller-reported policy "
                "state is never accepted")
        if not isinstance(task_credentials, HostTaskCredentialService):
            raise TypeError(
                "host prepared effects require the bound "
                "HostTaskCredentialService; caller-reported task "
                "context is never accepted")
        # Same-object checks, before any DDL: credential resolution,
        # policy re-verification and the envelope write must share one
        # connection so they share one SQLite transaction. Any second
        # connection - even to the same file - could read a different
        # database or a stale snapshot, so it is refused.
        if identity.db is not db or authorization.db is not db \
                or task_credentials.db is not db:
            raise ValueError(
                "host prepared effects require every bound service's "
                "own connection object; combining separate connections "
                "breaks the single-transaction verification guarantee")
        if authorization.identity is not identity \
                or task_credentials.identity is not identity \
                or task_credentials.authorization is not authorization:
            raise ValueError(
                "host prepared effects require the authorization "
                "ledger and the credential service to be bound to the "
                "very same identity and authorization instances; "
                "combining divergent instances breaks the "
                "single-transaction verification guarantee")
        # Checked before any DDL: a caller transaction is left
        # untouched, never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host prepared effects require an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.identity = identity
        self.authorization = authorization
        self.task_credentials = task_credentials
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- boundary validation ------------------------------------------------

    @staticmethod
    def _require_id(name, value):
        # git_format.encode_id enforces the host-issued ID bounds: str,
        # non-empty, length-capped. Pure boundary validation; the
        # encoded form is not needed for storage.
        # The library error may carry the offending text, so only the
        # failure kind leaves the handler; the raised error is fixed
        # and never chains the original exception.
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
                "host prepared effect writes own their transaction; "
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
                "host prepared effect reads open their own fresh "
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

    # -- scope ------------------------------------------------------------------

    @staticmethod
    def _check_scope_membership(ctx, action, target, objects):
        """Literal scope-membership check only: no wildcard, URI-prefix,
        trimming or case-folding interpretation, and no claim of field,
        read-window, egress, budget or resource authorization."""
        if action not in ctx.scope.actions:
            raise PreparedScopeDenied(
                "the requested action is not literally inside the "
                "task credential scope actions")
        if target != ctx.scope.target:
            raise PreparedScopeDenied(
                "the requested target does not literally equal the "
                "task credential scope target")
        allowed = set(ctx.scope.objects)
        for entry in objects:
            if entry not in allowed:
                raise PreparedScopeDenied(
                    "a requested object is not literally inside the "
                    "task credential scope objects")

    @staticmethod
    def _recheck_scope_fit(ctx, envelope):
        """A legitimately stored envelope always fits its credential's
        issuance-time cap (the cap is revalidated against the current
        policy by resolution itself), so a misfit means forged data."""
        allowed = set(ctx.scope.objects)
        if envelope["action"] not in ctx.scope.actions \
                or envelope["target"] != ctx.scope.target \
                or any(entry not in allowed
                       for entry in envelope["objects"]):
            raise ValueError(
                "stored prepared effect no longer fits the task "
                "credential scope; refusing to trust possibly tampered "
                "data")

    # -- row loading ------------------------------------------------------------

    def _effect_from_payload(self, payload, blob):
        return PreparedEffect(
            operation_id=payload["operation_id"],
            effect_digest=payload["effect_digest"],
            preparation_digest=payload["preparation_digest"],
            created_at=_load_time("created_at", payload["created_at"]),
            expires_at=_load_time("expires_at", payload["expires_at"]),
            envelope=_freeze(payload),
            envelope_bytes=blob)

    def _effect_from_row(self, row):
        """Re-verify one stored row end to end: index columns, the row
        digest, the canonical blob, both envelope digests and the exact
        blob/index-column agreement. The preparation digest alone is
        never treated as a full-row check: the row digest covers the
        complete canonical envelope including operation_id and
        created_at."""
        (operation_id, task_id, run_id, idempotency_key, effect_digest,
         preparation_digest, created_at, expires_at, record_blob,
         row_sha256) = row
        _recheck_id("operation_id", operation_id)
        _recheck_id("task_id", task_id)
        _recheck_id("run_id", run_id)
        _recheck_id("idempotency_key", idempotency_key)
        _recheck_digest(effect_digest)
        _recheck_digest(preparation_digest)
        _load_time("created_at", created_at)
        _load_time("expires_at", expires_at)
        _recheck_digest(row_sha256)
        payload = _loads_envelope(record_blob)
        blob = bytes(record_blob)
        if _digest_row(blob) != row_sha256:
            raise ValueError(
                "stored prepared effect row digest does not match its "
                "blob; refusing to trust possibly tampered data")
        index = (payload["operation_id"], payload["task_id"],
                 payload["run_id"], payload["idempotency_key"],
                 payload["effect_digest"], payload["preparation_digest"],
                 payload["created_at"], payload["expires_at"])
        if index != (operation_id, task_id, run_id, idempotency_key,
                     effect_digest, preparation_digest, created_at,
                     expires_at):
            raise ValueError(
                "stored prepared effect index columns do not match "
                "its envelope; refusing to trust possibly tampered "
                "data")
        return self._effect_from_payload(payload, blob)

    def _fetch_by_idempotency(self, task_id, run_id, idempotency_key):
        row = self.db.execute(
            "SELECT " + _COLUMNS + " FROM host_prepared_effects "
            "WHERE task_id = ? AND run_id = ? AND idempotency_key = ?",
            (task_id, run_id, idempotency_key)).fetchone()
        if row is None:
            return None
        return self._effect_from_row(row)

    def _replay(self, task_id, run_id, idempotency_key,
                preparation_digest):
        """Return the stored preparation when it is identical, or refuse.

        The preparation digest deterministically covers the complete
        request apart from the host-generated operation_id and
        created_at, so digest equality IS the complete-request
        comparison: the effect, base, dependencies, target, objects,
        identity/policy binding, idempotency key and validity period
        all match. The stored row is fully re-verified before anything
        is returned."""
        stored = self._fetch_by_idempotency(task_id, run_id,
                                            idempotency_key)
        if stored is None:
            return None
        if stored.preparation_digest != preparation_digest:
            raise PreparedEffectConflict(
                "the idempotency key is already bound to a different "
                "preparation; changing the effect, base, dependencies, "
                "target, objects, identity or policy binding, or the "
                "validity period requires a new idempotency key")
        return stored

    # -- preparation (F-007-API-006 persistence slice) ---------------------------

    def prepare_effect(self, task_secret, *, now, idempotency_key,
                       action, target, objects, effect_manifest,
                       object_format, base_commit, dependency_versions,
                       expires_at):
        """Persist one immutable prepared-effect envelope.

        The identity is resolved from the real task credential inside
        the write transaction; a caller-supplied context or scope is
        never accepted. On success exactly one row exists for the
        (task_id, run_id, idempotency_key) triple: a replay with the
        identical preparation returns the original operation_id and
        created_at and writes nothing, while the same key with any
        change is refused with PreparedEffectConflict and never
        overwrites. A revoked credential, lost membership, paused or
        re-versioned policy, expired credential or a request wider than
        the resolved scope all fail closed with zero writes. This never
        approves, reserves, consumes or executes the effect.
        """
        task_secret = _require_secret("task_secret", task_secret)
        now = _require_time("now", now)
        # Captured immediately next to the validated trusted host time,
        # before ANY further validation, canonical encoding or digest
        # work: the in-transaction effective time must account for all
        # of that elapsed time, not only for the write-lock wait.
        origin = _monotonic_ns()
        expires_at = _require_time("expires_at", expires_at)
        idempotency_key = self._require_id("idempotency_key",
                                           idempotency_key)
        action = _require_label("action", action)
        target = _require_label("target", target)
        objects = _check_objects_shape(objects)
        if isinstance(effect_manifest, bool) \
                or not isinstance(effect_manifest, dict):
            raise TypeError("effect_manifest must be a host-provided "
                            "canonically encodable dict")
        object_format = _check_object_format(object_format)
        base_commit = _check_base_commit(object_format, base_commit)
        dependency_versions = _check_dependency_versions(
            dependency_versions)
        sorted_objects = tuple(sorted(objects))
        effect_payload = {
            "action": action,
            "target": target,
            "objects": list(sorted_objects),
            "effect_manifest": effect_manifest,
            "object_format": object_format,
            "base_commit": base_commit,
            "dependency_versions": dependency_versions,
        }
        # Serializing here also validates the manifest against every
        # git_format type, depth and byte-budget rule, before the write
        # lock is ever contended for.
        effect_digest = _digest_effect(effect_payload)
        if now >= expires_at:
            raise PreparedEffectExpired(
                "the requested effect validity has already passed; an "
                "expired effect is never prepared")

        def core():
            # Everything below runs strictly ordered inside this one
            # transaction on the shared connection: real credential
            # resolution first (event history, installation, membership,
            # policy revision/status/validity and the stored cap against
            # the current policy scope), then the scope and validity
            # checks, then the idempotency lookup and the write. The
            # pre-lock context is never reused.
            tx_now = _effective_now(now, origin)
            ctx = self.task_credentials.resolve_task_credential(
                task_secret, now=tx_now)
            if tx_now >= expires_at:
                raise PreparedEffectExpired(
                    "the effect validity period ended before the write "
                    "lock was acquired; an expired effect is never "
                    "prepared")
            if expires_at > ctx.scope.not_after:
                raise PreparedScopeDenied(
                    "the requested effect validity exceeds the task "
                    "credential scope validity; a prepared effect "
                    "never outlives its task")
            self._check_scope_membership(ctx, action, target,
                                         sorted_objects)
            created_text = _store_time(tx_now)
            expires_text = _store_time(expires_at)
            preparation_payload = {
                "schema_version": _SCHEMA_VERSION,
                "installation_id": ctx.installation_id,
                "company_id": ctx.company_id,
                "principal_id": ctx.principal_id,
                "member_revision": ctx.member_revision,
                "task_id": ctx.task_id,
                "run_id": ctx.run_id,
                "policy_id": ctx.policy_id,
                "policy_revision": ctx.policy_revision,
                "idempotency_key": idempotency_key,
                "expires_at": expires_text,
                "effect_digest": effect_digest,
            }
            preparation_digest = _digest_preparation(preparation_payload)
            stored = self._replay(ctx.task_id, ctx.run_id,
                                  idempotency_key, preparation_digest)
            if stored is not None:
                return stored
            # Host-generated randomness; the caller can never supply an
            # operation_id.
            operation_id = "hop_" + secrets.token_hex(8)
            envelope = {
                "schema_version": _SCHEMA_VERSION,
                "preparation_state": _PREPARATION_STATE,
                "operation_id": operation_id,
                "installation_id": ctx.installation_id,
                "company_id": ctx.company_id,
                "principal_id": ctx.principal_id,
                "member_revision": ctx.member_revision,
                "task_id": ctx.task_id,
                "run_id": ctx.run_id,
                "policy_id": ctx.policy_id,
                "policy_revision": ctx.policy_revision,
                "idempotency_key": idempotency_key,
                "action": action,
                "target": target,
                "objects": list(sorted_objects),
                "effect_manifest": effect_manifest,
                "object_format": object_format,
                "base_commit": base_commit,
                "dependency_versions": dependency_versions,
                "effect_digest": effect_digest,
                "preparation_digest": preparation_digest,
                "created_at": created_text,
                "expires_at": expires_text,
            }
            blob = _canonical_bytes(envelope)
            insert_failed = False
            try:
                self.db.execute(
                    "INSERT INTO host_prepared_effects (" + _COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (operation_id, ctx.task_id, ctx.run_id,
                     idempotency_key, effect_digest, preparation_digest,
                     created_text, expires_text, blob,
                     _digest_row(blob)))
            except sqlite3.IntegrityError:
                # Only the failure fact leaves this handler; the
                # database exception object is never re-raised or
                # chained into a caller-visible error.
                insert_failed = True
            if insert_failed:
                # Backstop for a cross-connection race that committed
                # the same idempotency key between our lookup and our
                # insert: apply the very same replay-or-conflict rule
                # instead of failing the caller. A bare operation_id
                # collision (no such key row) becomes a fixed conflict
                # error.
                raced = self._replay(ctx.task_id, ctx.run_id,
                                     idempotency_key, preparation_digest)
                if raced is None:
                    raise PreparedEffectConflict(
                        "the idempotency key or operation reference is "
                        "already bound to another prepared effect; a "
                        "prepared effect is never overwritten")
                return raced
            # Return through the same fully verifying parse used by the
            # read path, so the returned object is exactly what a later
            # read-back will return.
            return self._effect_from_payload(_loads_envelope(blob), blob)
        return self._write_tx(core)

    # -- read-back -----------------------------------------------------------------

    def get_prepared_effect(self, task_secret, *, now, operation_id):
        """Return the still-valid prepared effect of the current root
        task for operation_id.

        Any existing caller transaction is refused at entry (a
        caller-held snapshot could predate a revocation committed by
        another connection); this method always opens its own fresh
        short read snapshot and never closes, commits or rolls back the
        refused caller transaction. Establishing that snapshot may wait
        on the database lock, so the effective time is re-derived from
        the entry-time monotonic origin only after the first real read
        returns, and the task credential is re-resolved for real with
        that effective time inside the same snapshot: a wait crossing
        the task, policy or prepared validity boundary can never pass
        on the stale entry time. The stored row is then re-verified end
        to end (canonical bytes, row digest, both envelope digests,
        field types, blob/index agreement), its
        task/run/installation/company/principal/member-revision/policy
        binding must equal the freshly resolved context, and its scope
        fit and validity period are re-checked (including expires_at <=
        the current scope not_after). A missing operation and one
        belonging to another task produce the identical
        PreparedEffectUnavailable error, so other tasks' content is
        never exposed. An expired effect fails with
        PreparedEffectExpired; replays never restore it. This read
        grants no approval, execution or enumeration authority.
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
                "SELECT " + _COLUMNS + " FROM host_prepared_effects "
                "WHERE operation_id = ?", (operation_id,)).fetchone()
            tx_now = _effective_now(now, origin)
            ctx = self.task_credentials.resolve_task_credential(
                task_secret, now=tx_now)
            if row is None or row[1] != ctx.task_id \
                    or row[2] != ctx.run_id:
                raise PreparedEffectUnavailable(_UNAVAILABLE_MESSAGE)
            effect = self._effect_from_row(row)
            envelope = effect.envelope
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
            self._recheck_scope_fit(ctx, envelope)
            if effect.expires_at > ctx.scope.not_after:
                raise ValueError(
                    "stored prepared effect validity exceeds the "
                    "resolved task credential scope validity; refusing "
                    "to trust possibly tampered data")
            if tx_now >= effect.expires_at:
                raise PreparedEffectExpired(
                    "the prepared effect validity period has passed; "
                    "replays never restore an expired effect")
            return effect
        return self._read(fetch)
