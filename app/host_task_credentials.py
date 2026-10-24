"""Restricted root-task credential lifecycle for the SAM host (F-007).

Approved design baseline 4ba389ebe3ecc39ba10716ce107e4d25e4fb63a6,
F-007 sections 2, 3.1-3.3 (task credential record, permission
computation, revisions are never rewritten in place), the
F-007-API-002 authorize_task contract limited to credential issuance,
section 5 ordering requirements and section 7 implementation step 2,
limited to this slice: issuing, resolving and revoking credentials for
authorized ROOT tasks (tasks without a parent).

This module implements the credential lifecycle only:

- issue_task_credential re-resolves the caller's user session through
  the bound HostIdentityService inside the write transaction and binds
  the credential to the host-confirmed installation, company and
  principal, to the policy_id and its exact current revision, to
  host-generated task_id and run_id, and to an issuance-time scope cap.
  The caller can only NARROW the approved policy scope: every requested
  trigger condition, action, object, field or recipient must already be
  inside the policy scope, stop conditions must match it exactly, the
  read window, target and plan version are taken from or must equal the
  policy, budget limits and delegation/concurrency/retry limits must be
  at most the policy's, and the credential validity period must sit
  inside the policy validity period. Anything wider is refused with
  TaskScopeDenied. A paused, revoked, expired or not-yet-valid policy,
  or an expected_revision that does not match the current revision,
  refuses issuance (policy_stale semantics); no new trigger is allowed
  against a paused policy.
- The raw credential is a high-entropy 256-bit random secret returned
  exactly once on the TaskCredentialGrant and excluded from its repr.
  Only its SHA-256 digest (domain-separated from identity digests) is
  stored; the raw secret never reaches the database, logs, exception
  messages or reprs. The user's session secret is never copied into the
  task credential: the credential secret is generated independently.
- resolve_task_credential re-verifies on EVERY call, inside one read
  snapshot on the same connection: the credential's existence and
  non-revoked event history, the current installation binding, the
  issuing principal's current membership status, the bound policy's
  current revision, status and validity period, AND the stored
  issuance-time cap against the current policy scope through the same
  narrowing predicates issuance used. Policy-revision equality alone is
  never treated as proof: a stored scope wider or inconsistent in any
  single dimension fails closed even when it names the exact bound
  revision, and a cap that passes is returned unchanged, never widened.
  Revocation, expiry, pause, membership loss, any policy revision
  change or a stored scope that no longer fits inside its policy fail
  closed. There is no cache. The returned TaskRunContext is plain
  frozen data with no secret and no authority methods.
- revoke_task_credential is an authenticated user-session call. It
  appends an immutable revocation event carrying the actor, session
  reference, timestamp and a monotonic ledger-wide revocation sequence
  number. It revokes exactly one root-task credential; it does not
  claim to stop a running worker and there is no subtask propagation,
  because this slice has no subtasks. Repeat revocation returns False
  and changes nothing.

Ordering and race safety: policy lookup, identity verification and the
credential write happen strictly ordered inside one BEGIN IMMEDIATE
transaction on one shared sqlite3.Connection, so a "verify first,
revoke later" race cannot interleave on this connection; cross-
connection writers are serialized by SQLite's write lock. As in
app.host_authorization, a write call validates the caller-provided
trusted timestamp, captures a monotonic nanosecond reading before
contending for the lock and re-derives the effective time inside the
transaction, so a lock wait crossing an expiry boundary can never issue
or verify on a stale pre-lock time.

Persistence shape: the issuance row is immutable (UPDATE, DELETE and
INSERT OR REPLACE are refused by triggers; parent_task_id carries a
CHECK enforcing NULL, i.e. root tasks only). State lives in an
append-only event history (issued, revoked) whose shape is also
trigger-guarded: exactly one issued event first, at most one revoked
event, no rewrite, no delete, no replace. The revocation sequence
singleton mirrors the authorization ledger's guards: increment by
exactly one, never deleted, never replaced.

Scope of this slice (explicitly NOT implemented here): delegation and
subtasks, queues, worker injection, business tools, egress, one-time
approvals, the operation ledger, the Commit Broker, HTTP routes, login
adapters, feature gates, budget consumption accounting and any change
to the global app.db schema or migrations. Revoking a credential does
not stop an already-running worker; it makes the next host-side
verification fail closed.

Connection contract: identical to app.host_identity and
app.host_authorization. The constructor takes only a caller-provided
sqlite3.Connection, idle at construction; it never opens a database,
never chooses a data path, never reads environment variables and never
touches the default app.db. Write methods own exactly one BEGIN
IMMEDIATE transaction, commit on success and roll back on any
exception; calling one inside a caller transaction is refused. Read
methods open their own short snapshot when idle and close it with
ROLLBACK; inside a caller transaction they read through it.

Identity and ledger contract: the constructor takes the bound
HostIdentityService and HostAuthorizationService, and both must be
bound to the very same connection object AND to each other. Any other
combination is refused before DDL, because identity resolution, policy
re-verification and the credential write must live in one SQLite
transaction on one connection.

Internal-interface reuse (sanctioned by the F-007 card): current policy
state is read through HostAuthorizationService's internal row loaders
and consistency checks (_fetch_policy_row, _fetch_latest_revision,
_check_consistency, _check_binding) so the credential module can never
observe a policy through a second, divergent code path; scope
validation and storage reuse the ledger's _build_scope/_dumps_scope/
_loads_scope so a credential cap is exactly a PolicyScope, losslessly
re-validated on every load. Current membership and installation state
are read through HostIdentityService's internal loaders
(_fetch_member, _fetch_installation) inside this module's transaction.
No file outside this module is modified.
"""
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock

from app.facts_tree import git_format
from app.host_authorization import (
    HostAuthorizationService,
    PolicyNotFound,
    PolicyScope,
    _build_scope,
    _dumps_scope,
    _load_time,
    _loads_scope,
    _require_budget,
    _require_label,
    _require_labels,
    _require_limit,
    _require_revision,
    _require_time,
    _store_time,
)
from app.host_identity import HostIdentityService

# Monotonic time source for the in-transaction effective time; a plain
# module attribute so deterministic tests can simulate a contended
# write-lock wait, exactly as in app.host_authorization.
_monotonic_ns = time.monotonic_ns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_task_credentials (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    credential_sha256 TEXT NOT NULL UNIQUE,
    parent_task_id TEXT CHECK (parent_task_id IS NULL),
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    member_revision INTEGER NOT NULL,
    issued_by_session_ref TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_task_credential_events (
    task_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event TEXT NOT NULL CHECK (event IN ('issued', 'revoked')),
    revocation_seq INTEGER NOT NULL CHECK (revocation_seq >= 0),
    actor_principal_id TEXT NOT NULL,
    actor_member_revision INTEGER NOT NULL,
    session_ref TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY (task_id, event_seq)
);
CREATE TABLE IF NOT EXISTS host_task_credentials_ledger_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revocation_seq INTEGER NOT NULL
);
-- Idempotent singleton initialization: inserts only while the row does
-- not exist, so re-running this schema never trips the no-replace guard
-- and never touches the stored sequence value.
INSERT INTO host_task_credentials_ledger_state (singleton, revocation_seq)
    SELECT 1, 0 WHERE NOT EXISTS (
        SELECT 1 FROM host_task_credentials_ledger_state);
CREATE TRIGGER IF NOT EXISTS host_task_credentials_no_update
BEFORE UPDATE ON host_task_credentials BEGIN
    SELECT RAISE(ABORT, 'host task credentials are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credentials_no_delete
BEFORE DELETE ON host_task_credentials BEGIN
    SELECT RAISE(ABORT, 'host task credentials are append-only');
END;
-- INSERT OR REPLACE would implicitly delete the stored row before
-- inserting, and that delete does not fire the BEFORE DELETE trigger
-- while recursive_triggers is off (the SQLite default). The BEFORE
-- INSERT guard makes REPLACE unable to overwrite stored issuance data.
CREATE TRIGGER IF NOT EXISTS host_task_credentials_no_replace
BEFORE INSERT ON host_task_credentials
WHEN EXISTS (
    SELECT 1 FROM host_task_credentials
    WHERE task_id = NEW.task_id
) BEGIN
    SELECT RAISE(ABORT, 'host task credentials are append-only: INSERT OR REPLACE cannot overwrite an existing credential');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_no_update
BEFORE UPDATE ON host_task_credential_events BEGIN
    SELECT RAISE(ABORT, 'host task credential events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_no_delete
BEFORE DELETE ON host_task_credential_events BEGIN
    SELECT RAISE(ABORT, 'host task credential events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_no_replace
BEFORE INSERT ON host_task_credential_events
WHEN EXISTS (
    SELECT 1 FROM host_task_credential_events
    WHERE task_id = NEW.task_id AND event_seq = NEW.event_seq
) BEGIN
    SELECT RAISE(ABORT, 'host task credential events are append-only: INSERT OR REPLACE cannot overwrite an existing event');
END;
-- Event-shape guards: an issuance event must be the first event of its
-- task, and a revocation must reference an issued credential and exist
-- at most once, so the derived current status can never be forged or
-- silently rewritten through direct SQL.
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_issued_once
BEFORE INSERT ON host_task_credential_events
WHEN NEW.event = 'issued' AND EXISTS (
    SELECT 1 FROM host_task_credential_events
    WHERE task_id = NEW.task_id
) BEGIN
    SELECT RAISE(ABORT, 'a task credential has exactly one issuance event');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_revoked_needs_issued
BEFORE INSERT ON host_task_credential_events
WHEN NEW.event = 'revoked' AND NOT EXISTS (
    SELECT 1 FROM host_task_credential_events
    WHERE task_id = NEW.task_id AND event = 'issued'
) BEGIN
    SELECT RAISE(ABORT, 'a revocation event requires an issued credential');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credential_events_revoked_once
BEFORE INSERT ON host_task_credential_events
WHEN NEW.event = 'revoked' AND EXISTS (
    SELECT 1 FROM host_task_credential_events
    WHERE task_id = NEW.task_id AND event = 'revoked'
) BEGIN
    SELECT RAISE(ABORT, 'a task credential is revoked at most once');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credentials_seq_increment_only
BEFORE UPDATE ON host_task_credentials_ledger_state
WHEN NEW.revocation_seq != OLD.revocation_seq + 1 BEGIN
    SELECT RAISE(ABORT,
        'revocation sequence must increase by exactly one');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credentials_seq_no_delete
BEFORE DELETE ON host_task_credentials_ledger_state BEGIN
    SELECT RAISE(ABORT, 'revocation sequence row cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS host_task_credentials_seq_no_replace
BEFORE INSERT ON host_task_credentials_ledger_state
WHEN EXISTS (
    SELECT 1 FROM host_task_credentials_ledger_state
    WHERE singleton = NEW.singleton
) BEGIN
    SELECT RAISE(ABORT, 'revocation sequence cannot be reset: INSERT OR REPLACE is refused once the row exists');
END;
"""

_CREDENTIAL_COLUMNS = ("task_id, run_id, credential_sha256, "
                       "parent_task_id, installation_id, company_id, "
                       "principal_id, member_revision, "
                       "issued_by_session_ref, policy_id, "
                       "policy_revision, scope_json, issued_at")
_EVENT_COLUMNS = ("task_id, event_seq, event, revocation_seq, "
                  "actor_principal_id, actor_member_revision, "
                  "session_ref, changed_at")

_EVENT_NAMES = frozenset({"issued", "revoked"})

# Sentinel separating "egress not requested" (inherit the policy cap)
# from an explicit egress=None request (narrow to no egress at all).
_UNSET = object()


class HostTaskCredentialError(Exception):
    """Base class for every task-credential failure; fails closed."""


class TaskScopeDenied(HostTaskCredentialError):
    """The requested cap is wider than the approved policy scope, or a
    stored cap no longer fits inside the bound policy's current scope."""


class TaskPolicyStale(HostTaskCredentialError):
    """The policy revision does not match the bound/expected revision."""


class TaskPolicyInactive(HostTaskCredentialError):
    """The bound policy is paused or revoked; it authorizes nothing."""


class TaskPolicyExpired(HostTaskCredentialError):
    """The bound policy validity period has passed."""


class TaskPolicyNotYetValid(HostTaskCredentialError):
    """The bound policy validity period has not started."""


class TaskCredentialInvalid(HostTaskCredentialError):
    """The task credential secret is not recognized."""


class TaskCredentialRevoked(HostTaskCredentialError):
    """The task credential has been revoked; replays keep failing."""


class TaskCredentialExpired(HostTaskCredentialError):
    """The task credential is past its issuance-time not_after."""


class TaskCredentialNotYetValid(HostTaskCredentialError):
    """The task credential validity period has not started."""


class TaskMembershipRevoked(HostTaskCredentialError):
    """The issuing principal's membership is missing or revoked."""


class TaskBindingMismatch(HostTaskCredentialError):
    """Stored credential state does not match the bound installation."""


class UnknownTaskCredential(HostTaskCredentialError):
    """revoke_task_credential target task_id does not exist."""


@dataclass(frozen=True)
class TaskRunContext:
    """Trusted run context resolved from current server-side state.

    Plain frozen data: no secret material and deliberately no methods
    that grant policy, Git-write or approval authority. The scope is
    the issuance-time cap (a PolicyScope, never wider than the approved
    policy); every binding field is re-read from the database at
    resolution time.
    """

    task_id: str
    run_id: str
    installation_id: str
    company_id: str
    principal_id: str
    member_revision: int
    policy_id: str
    policy_revision: int
    scope: PolicyScope
    issued_at: datetime
    resolved_at: datetime


@dataclass(frozen=True)
class TaskCredentialGrant:
    """Root-task credential as returned at issuance, exactly once.

    The raw secret exists only on this returned object; it is excluded
    from repr and is never stored, logged or included in exceptions.
    The context carries no secret.
    """

    task_id: str
    run_id: str
    expires_at: datetime
    context: TaskRunContext
    secret: str = field(repr=False)


def _credential_digest(secret):
    # Domain-separated from every identity/authorization digest: a
    # stored digest can never be replayed across credential classes.
    return hashlib.sha256(
        b"sam-host-task-credentials credential-v1\x00"
        + secret.encode("utf-8")).hexdigest()


def _effective_now(base, origin):
    """In-transaction effective time, identical in shape to the
    authorization ledger's: the trusted base advanced by the monotonic
    elapsed time since call entry, floored to whole microseconds, so a
    write-lock wait crossing an expiry boundary can never issue on a
    stale pre-lock time and the effective time can never lead the
    observed monotonic elapsed time."""
    elapsed_ns = _monotonic_ns() - origin
    if elapsed_ns < 0:
        elapsed_ns = 0
    return base + timedelta(microseconds=elapsed_ns // 1000)


def _require_secret(name, value):
    if not isinstance(value, str):
        raise TypeError("%s must be a str credential" % name)
    if not value:
        raise ValueError("%s must not be empty" % name)
    return value


def _narrow_labels(name, requested, allowed, allow_empty=False):
    """Validate a requested label list as a subset of the policy's.

    Entries outside the approved list are a widening attempt and are
    refused; the stored form is canonicalized to the policy order and
    de-duplicated, so equal requests store byte-identical scope JSON.
    """
    if requested is None:
        return tuple(allowed)
    entries = _require_labels(name, requested, allow_empty=True)
    allowed_set = set(allowed)
    for entry in entries:
        if entry not in allowed_set:
            raise TaskScopeDenied(
                "requested %s are wider than the approved policy "
                "scope; a task credential can only narrow it" % name)
    requested_set = set(entries)
    narrowed = tuple(entry for entry in allowed
                     if entry in requested_set)
    if not narrowed and not allow_empty:
        raise ValueError("%s must name at least one entry" % name)
    return narrowed


def _exact_labels(name, requested, allowed):
    """Stop conditions can be neither narrowed nor widened: dropping one
    would remove a safety stop, adding one would invent host semantics
    this module does not interpret."""
    if requested is None:
        return tuple(allowed)
    entries = _require_labels(name, requested, allow_empty=True)
    if set(entries) != set(allowed):
        raise TaskScopeDenied(
            "%s must match the approved policy scope exactly; they "
            "can be neither narrowed nor widened" % name)
    return tuple(allowed)


def _narrow_egress(requested, allowed):
    if requested is _UNSET:
        return allowed
    if requested is None:
        # No egress at all is always at most as wide as the policy.
        return None
    value = _require_label("egress", requested)
    if allowed is None or value != allowed:
        raise TaskScopeDenied(
            "requested egress is wider than the approved policy scope; "
            "a task credential can only narrow it")
    return value


def _narrow_budget(requested, allowed):
    if requested is None:
        return tuple(allowed)
    narrowed = _require_budget(requested)
    allowed_map = dict(allowed)
    for key, limit in narrowed:
        if key not in allowed_map:
            raise TaskScopeDenied(
                "requested budget key %r is not inside the approved "
                "policy budget" % key)
        if limit > allowed_map[key]:
            raise TaskScopeDenied(
                "requested budget limit for %r exceeds the approved "
                "policy budget" % key)
    return narrowed


def _narrow_limit(name, requested, allowed):
    if requested is None:
        return allowed
    value = _require_limit(name, requested)
    if value > allowed:
        raise TaskScopeDenied(
            "%s exceeds the approved policy limit; a task credential "
            "can only narrow it" % name)
    return value


def _narrow_scope(policy_scope, tx_now, *, trigger_conditions,
                  stop_conditions, actions, objects, fields, read_window,
                  egress, recipients, not_before, not_after, budget,
                  max_delegation_depth, max_concurrency, max_retries):
    """Build the issuance-time cap: every dimension is either inherited
    from the policy scope or a verified narrowing of it. Target and
    plan version always come from the policy; the caller never restates
    them."""
    if read_window is None:
        narrowed_read_window = policy_scope.read_window
    else:
        narrowed_read_window = _require_label("read_window", read_window)
        if narrowed_read_window != policy_scope.read_window:
            raise TaskScopeDenied(
                "read_window must match the approved policy scope; no "
                "narrowing semantics are defined for it")
    if not_before is None:
        narrowed_not_before = policy_scope.not_before
    else:
        narrowed_not_before = _require_time("not_before", not_before)
        if narrowed_not_before < policy_scope.not_before:
            raise TaskScopeDenied(
                "credential not_before precedes the approved policy "
                "validity period")
    if not_after is None:
        narrowed_not_after = policy_scope.not_after
    else:
        narrowed_not_after = _require_time("not_after", not_after)
        if narrowed_not_after > policy_scope.not_after:
            raise TaskScopeDenied(
                "credential not_after exceeds the approved policy "
                "validity period; a credential never outlives its "
                "policy")
    if tx_now >= narrowed_not_after:
        raise TaskScopeDenied(
            "the requested credential validity period has already "
            "passed; an expired credential is never issued")
    return _build_scope(
        target=policy_scope.target,
        plan_version=policy_scope.plan_version,
        trigger_conditions=_narrow_labels(
            "trigger_conditions", trigger_conditions,
            policy_scope.trigger_conditions),
        stop_conditions=_exact_labels(
            "stop_conditions", stop_conditions,
            policy_scope.stop_conditions),
        actions=_narrow_labels("actions", actions, policy_scope.actions),
        objects=_narrow_labels("objects", objects, policy_scope.objects),
        fields=_narrow_labels("fields", fields, policy_scope.fields),
        read_window=narrowed_read_window,
        egress=_narrow_egress(egress, policy_scope.egress),
        recipients=_narrow_labels("recipients", recipients,
                                  policy_scope.recipients,
                                  allow_empty=True),
        not_before=narrowed_not_before,
        not_after=narrowed_not_after,
        budget=dict(_narrow_budget(budget, policy_scope.budget)),
        max_delegation_depth=_narrow_limit(
            "max_delegation_depth", max_delegation_depth,
            policy_scope.max_delegation_depth),
        max_concurrency=_narrow_limit(
            "max_concurrency", max_concurrency,
            policy_scope.max_concurrency),
        max_retries=_narrow_limit("max_retries", max_retries,
                                  policy_scope.max_retries),
    )


def _check_stored_scope(stored_scope, policy_scope):
    """Revalidate a stored issuance-time cap against the CURRENT policy
    scope, dimension by dimension, through the very same narrowing
    predicates issuance used.

    Policy-revision equality alone is not proof that a stored cap is
    legitimate: a row can be forged or corrupted with a syntactically
    valid scope wider than its policy while still naming an existing
    policy_id and its exact current revision. A legitimate cap is
    always a narrowing of the policy: target, plan version and read
    window match exactly, stop conditions match as a set, trigger
    conditions, actions, objects, fields and recipients are subsets,
    egress is absent or identical, the validity window sits inside the
    policy's, and every budget key and delegation/concurrency/retry
    limit is at most the policy's. Any dimension wider or inconsistent
    fails closed with TaskScopeDenied. This check never rewrites and
    never widens the stored cap; when it passes, the cap is used
    unchanged.
    """
    if stored_scope.target != policy_scope.target:
        raise TaskScopeDenied(
            "stored task credential target does not match the bound "
            "policy scope")
    if stored_scope.plan_version != policy_scope.plan_version:
        raise TaskScopeDenied(
            "stored task credential plan version does not match the "
            "bound policy scope")
    if stored_scope.read_window != policy_scope.read_window:
        raise TaskScopeDenied(
            "stored task credential read_window does not match the "
            "bound policy scope")
    for name in ("trigger_conditions", "actions", "objects", "fields"):
        _narrow_labels(name, getattr(stored_scope, name),
                       getattr(policy_scope, name))
    _exact_labels("stop_conditions", stored_scope.stop_conditions,
                  policy_scope.stop_conditions)
    _narrow_labels("recipients", stored_scope.recipients,
                   policy_scope.recipients, allow_empty=True)
    _narrow_egress(stored_scope.egress, policy_scope.egress)
    if stored_scope.not_before < policy_scope.not_before:
        raise TaskScopeDenied(
            "stored task credential not_before precedes the bound "
            "policy validity period")
    if stored_scope.not_after > policy_scope.not_after:
        raise TaskScopeDenied(
            "stored task credential not_after exceeds the bound policy "
            "validity period")
    _narrow_budget(dict(stored_scope.budget), policy_scope.budget)
    for name in ("max_delegation_depth", "max_concurrency",
                 "max_retries"):
        _narrow_limit(name, getattr(stored_scope, name),
                      getattr(policy_scope, name))


class HostTaskCredentialService:
    """Root-task credential lifecycle on one shared host connection.

    See the module docstring for the full scope list and the
    connection, transaction, identity and ledger contracts.
    """

    def __init__(self, db, identity, authorization):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host task credentials require a caller-provided "
                "sqlite3.Connection; the module never opens or chooses "
                "a database path itself")
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host task credentials require the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        if not isinstance(authorization, HostAuthorizationService):
            raise TypeError(
                "host task credentials require the bound "
                "HostAuthorizationService; caller-reported policy state "
                "is never accepted")
        # Same-object checks, before any DDL: identity resolution,
        # policy re-verification and the credential write must share
        # one connection so they share one SQLite transaction and are
        # strictly ordered against revocations. Any second connection -
        # even to the same file - could read a different database or a
        # stale snapshot, so it is refused.
        if identity.db is not db or authorization.db is not db:
            raise ValueError(
                "host task credentials require the identity service's "
                "and the authorization ledger's own connection object; "
                "combining separate connections breaks the "
                "single-transaction verification guarantee")
        if authorization.identity is not identity:
            raise ValueError(
                "host task credentials require the authorization "
                "ledger to be bound to the very same identity service; "
                "combining divergent identity instances breaks the "
                "single-transaction verification guarantee")
        # Checked before any DDL: a caller transaction is left
        # untouched, never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host task credentials require an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.identity = identity
        self.authorization = authorization
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- boundary validation ------------------------------------------------

    @staticmethod
    def _require_id(name, value):
        # git_format.encode_id enforces the host-issued ID bounds: str,
        # non-empty, length-capped. Pure boundary validation; the
        # encoded form is not needed for storage.
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s is not a valid host-issued id: %s"
                             % (name, exc)) from exc
        return value

    # -- stored-row re-validation --------------------------------------------

    @staticmethod
    def _recheck_id(name, value):
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored task credential %s failed re-validation; "
                "refusing to trust possibly tampered data"
                % name) from exc
        return value

    @staticmethod
    def _recheck_int(name, value, minimum):
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < minimum:
            raise ValueError(
                "stored task credential %s is not a valid int; "
                "refusing to trust possibly tampered data" % name)
        return value

    @staticmethod
    def _recheck_digest(value):
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(
                "stored task credential digest is malformed; refusing "
                "to trust possibly tampered data")
        return value

    # -- transaction helpers --------------------------------------------------

    def _write_tx(self, core):
        if self.db.in_transaction:
            raise ValueError(
                "host task credential writes own their transaction; "
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
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                return fetch()
            finally:
                if owns_snapshot:
                    # Read-only snapshot owned by this service: close it
                    # with ROLLBACK on success and failure alike. A
                    # caller-owned transaction is never touched.
                    self.db.rollback()

    # -- row loading ------------------------------------------------------------

    def _credential_from_row(self, row):
        (task_id, run_id, credential_sha256, parent_task_id,
         installation_id, company_id, principal_id, member_revision,
         issued_by_session_ref, policy_id, policy_revision, scope_json,
         issued_at) = row
        if parent_task_id is not None:
            raise ValueError(
                "stored task credential carries a parent task; this "
                "slice supports root tasks only; refusing to trust "
                "possibly tampered data")
        return {
            "task_id": self._recheck_id("task_id", task_id),
            "run_id": self._recheck_id("run_id", run_id),
            "credential_sha256": self._recheck_digest(credential_sha256),
            "installation_id": self._recheck_id("installation_id",
                                                installation_id),
            "company_id": self._recheck_id("company_id", company_id),
            "principal_id": self._recheck_id("principal_id",
                                             principal_id),
            "member_revision": self._recheck_int("member_revision",
                                                 member_revision, 1),
            "issued_by_session_ref": self._recheck_id(
                "issued_by_session_ref", issued_by_session_ref),
            "policy_id": self._recheck_id("policy_id", policy_id),
            "policy_revision": self._recheck_int("policy_revision",
                                                 policy_revision, 1),
            "scope": _loads_scope(scope_json),
            "issued_at": _load_time(issued_at),
        }

    def _fetch_credential_by_digest(self, digest):
        row = self.db.execute(
            "SELECT " + _CREDENTIAL_COLUMNS
            + " FROM host_task_credentials WHERE credential_sha256 = ?",
            (digest,)).fetchone()
        if row is None:
            return None
        return self._credential_from_row(row)

    def _fetch_credential_by_task(self, task_id):
        row = self.db.execute(
            "SELECT " + _CREDENTIAL_COLUMNS
            + " FROM host_task_credentials WHERE task_id = ?",
            (task_id,)).fetchone()
        if row is None:
            return None
        return self._credential_from_row(row)

    def _event_from_row(self, row):
        (task_id, event_seq, event, revocation_seq, actor_principal_id,
         actor_member_revision, session_ref, changed_at) = row
        if event not in _EVENT_NAMES:
            raise ValueError(
                "stored task credential event has an unknown name; "
                "refusing to trust possibly tampered data")
        return {
            "task_id": self._recheck_id("task_id", task_id),
            "event_seq": self._recheck_int("event_seq", event_seq, 1),
            "event": event,
            "revocation_seq": self._recheck_int("revocation_seq",
                                                revocation_seq, 0),
            "actor_principal_id": self._recheck_id(
                "actor_principal_id", actor_principal_id),
            "actor_member_revision": self._recheck_int(
                "actor_member_revision", actor_member_revision, 1),
            "session_ref": self._recheck_id("session_ref", session_ref),
            "changed_at": _load_time(changed_at),
        }

    def _fetch_events(self, task_id):
        rows = self.db.execute(
            "SELECT " + _EVENT_COLUMNS
            + " FROM host_task_credential_events "
            "WHERE task_id = ? ORDER BY event_seq", (task_id,)).fetchall()
        events = [self._event_from_row(row) for row in rows]
        # Re-verify the append-only shape even though triggers enforce
        # it on this schema: a history that does not start with exactly
        # one issuance and end with at most one revocation means
        # tampered or corrupted data, so fail closed.
        if not events:
            raise ValueError(
                "stored task credential has no issuance event; "
                "refusing to trust possibly tampered data")
        for expected_seq, event in enumerate(events, start=1):
            if event["event_seq"] != expected_seq:
                raise ValueError(
                    "stored task credential event history is not "
                    "contiguous from 1; refusing to trust possibly "
                    "tampered data")
        if events[0]["event"] != "issued":
            raise ValueError(
                "stored task credential event history does not start "
                "with its issuance; refusing to trust possibly "
                "tampered data")
        revocations = [e for e in events if e["event"] == "revoked"]
        if len(revocations) > 1 or (revocations
                                    and events[-1]["event"] != "revoked"):
            raise ValueError(
                "stored task credential event history has an "
                "unexpected revocation shape; refusing to trust "
                "possibly tampered data")
        return events

    @staticmethod
    def _is_revoked(events):
        return any(event["event"] == "revoked" for event in events)

    def _load_bound_policy(self, stored):
        """Re-read the bound policy's CURRENT state and verify the
        credential binding against it. Raises on any drift; never
        rewrites anything."""
        policy = self.authorization._fetch_policy_row(stored["policy_id"])
        if policy is None:
            raise ValueError(
                "stored task credential references a missing policy; "
                "refusing to trust possibly tampered data")
        current = self.authorization._fetch_latest_revision(
            stored["policy_id"])
        if current is None:
            raise ValueError(
                "stored policy has no revisions; refusing to trust "
                "possibly tampered data")
        self.authorization._check_consistency(policy, current)
        if policy["installation_id"] != stored["installation_id"] \
                or policy["company_id"] != stored["company_id"]:
            raise TaskBindingMismatch(
                "task credential and its bound policy disagree on "
                "installation or company")
        return policy, current

    def _check_current_policy(self, stored, current, now):
        if current["revision"] != stored["policy_revision"]:
            raise TaskPolicyStale(
                "the policy moved to revision %d after this credential "
                "was bound to revision %d; a revision change never "
                "flows into an existing credential"
                % (current["revision"], stored["policy_revision"]))
        if current["status"] != "active":
            raise TaskPolicyInactive(
                "the bound policy is %s; the credential no longer "
                "verifies" % current["status"])
        if now >= current["scope"].not_after:
            raise TaskPolicyExpired(
                "the bound policy validity period has passed")
        if now < current["scope"].not_before:
            raise TaskPolicyNotYetValid(
                "the bound policy validity period has not started")

    def _context(self, stored, now):
        return TaskRunContext(
            task_id=stored["task_id"],
            run_id=stored["run_id"],
            installation_id=stored["installation_id"],
            company_id=stored["company_id"],
            principal_id=stored["principal_id"],
            member_revision=stored["member_revision"],
            policy_id=stored["policy_id"],
            policy_revision=stored["policy_revision"],
            scope=stored["scope"],
            issued_at=stored["issued_at"],
            resolved_at=now,
        )

    # -- issuance (F-007-API-002, credential slice) ------------------------------

    def issue_task_credential(self, session_secret, *, now, policy_id,
                              expected_revision, trigger_conditions=None,
                              stop_conditions=None, actions=None,
                              objects=None, fields=None, read_window=None,
                              egress=_UNSET, recipients=None,
                              not_before=None, not_after=None,
                              budget=None, max_delegation_depth=None,
                              max_concurrency=None, max_retries=None):
        """Issue one restricted credential for one root task.

        Requires the session secret of a currently valid, authenticated
        user session and the policy_id plus the exact revision the
        caller relies on. Identity, installation, company, task_id,
        run_id and the policy binding are all host-generated or
        host-resolved inside one write transaction; the caller cannot
        submit any of them. Every scope parameter is optional and
        defaults to the policy's value; anything wider than the current
        active policy scope is refused with TaskScopeDenied, a revision
        mismatch with TaskPolicyStale and an inactive or expired policy
        with its own error. The raw secret is returned exactly once on
        the grant; only its digest is stored.
        """
        session_secret = _require_secret("session_secret", session_secret)
        now = _require_time("now", now)
        policy_id = self._require_id("policy_id", policy_id)
        expected_revision = _require_revision("expected_revision",
                                              expected_revision)
        # Captured next to the validated trusted host time, before the
        # write-lock contention; the core re-derives the effective time
        # from it after BEGIN IMMEDIATE.
        origin = _monotonic_ns()

        def core():
            # Everything below runs strictly ordered inside this one
            # transaction on the shared connection: identity first,
            # then the current policy state, then the credential
            # write. A revocation landing in between is impossible on
            # this connection and serialized by the write lock across
            # connections, so verify-then-issue cannot straddle a
            # revocation.
            tx_now = _effective_now(now, origin)
            context = self.identity.resolve_session(session_secret,
                                                    now=tx_now)
            policy = self.authorization._fetch_policy_row(policy_id)
            if policy is None:
                raise PolicyNotFound("unknown policy_id")
            current = self.authorization._fetch_latest_revision(
                policy_id)
            if current is None:
                raise ValueError(
                    "stored policy has no revisions; refusing to "
                    "trust possibly tampered data")
            self.authorization._check_consistency(policy, current)
            self.authorization._check_binding(context, policy)
            if current["revision"] != expected_revision:
                raise TaskPolicyStale(
                    "expected_revision %d does not match the current "
                    "policy revision %d; re-read and re-verify instead "
                    "of silently issuing against a newer version"
                    % (expected_revision, current["revision"]))
            if current["status"] != "active":
                raise TaskPolicyInactive(
                    "a %s policy authorizes no new task credential"
                    % current["status"])
            if tx_now >= current["scope"].not_after:
                raise TaskPolicyExpired(
                    "the policy validity period has passed; no task "
                    "credential is issued against an expired policy")
            if tx_now < current["scope"].not_before:
                raise TaskPolicyNotYetValid(
                    "the policy validity period has not started; no "
                    "task credential is issued against it yet")
            scope = _narrow_scope(
                current["scope"], tx_now,
                trigger_conditions=trigger_conditions,
                stop_conditions=stop_conditions, actions=actions,
                objects=objects, fields=fields, read_window=read_window,
                egress=egress, recipients=recipients,
                not_before=not_before, not_after=not_after,
                budget=budget,
                max_delegation_depth=max_delegation_depth,
                max_concurrency=max_concurrency,
                max_retries=max_retries)
            task_id = "htk_" + secrets.token_hex(8)
            run_id = "htr_" + secrets.token_hex(8)
            # Independent high-entropy secret: never derived from, and
            # never equal to, the user's session secret.
            secret = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO host_task_credentials "
                "(task_id, run_id, credential_sha256, parent_task_id, "
                "installation_id, company_id, principal_id, "
                "member_revision, issued_by_session_ref, policy_id, "
                "policy_revision, scope_json, issued_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, _credential_digest(secret),
                 context.installation_id, context.company_id,
                 context.principal_id, context.member_revision,
                 context.session_ref, policy_id, current["revision"],
                 _dumps_scope(scope), _store_time(tx_now)))
            self.db.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, 1, 'issued', 0, ?, ?, ?, ?)",
                (task_id, context.principal_id, context.member_revision,
                 context.session_ref, _store_time(tx_now)))
            stored = {"task_id": task_id, "run_id": run_id,
                      "installation_id": context.installation_id,
                      "company_id": context.company_id,
                      "principal_id": context.principal_id,
                      "member_revision": context.member_revision,
                      "policy_id": policy_id,
                      "policy_revision": current["revision"],
                      "scope": scope, "issued_at": tx_now}
            run_context = self._context(stored, tx_now)
            return TaskCredentialGrant(task_id=task_id, run_id=run_id,
                                       expires_at=scope.not_after,
                                       context=run_context,
                                       secret=secret)
        return self._write_tx(core)

    # -- resolution --------------------------------------------------------------

    def resolve_task_credential(self, secret, *, now):
        """Resolve a task credential secret to a trusted TaskRunContext.

        Re-verifies the credential's event history, the current
        installation binding, the issuing principal's current
        membership, the bound policy's current revision, status and
        validity period, and the stored issuance-time cap against the
        current policy scope on every call, inside one read snapshot on
        the shared connection; there is no cache, so a revocation,
        pause, policy revision change or membership loss takes effect
        on the very next resolution. Unknown secrets, revoked, expired
        or not-yet-valid credentials, and stored scopes wider or
        inconsistent with the bound policy in any dimension, all fail
        closed (TaskScopeDenied); a cap that passes is returned
        unchanged, never widened. The returned context carries no
        secret and no authority methods.
        """
        secret = _require_secret("secret", secret)
        now = _require_time("now", now)
        return self._read(
            lambda: self._resolve_core(_credential_digest(secret), now))

    def _resolve_core(self, digest, now):
        stored = self._fetch_credential_by_digest(digest)
        if stored is None:
            raise TaskCredentialInvalid(
                "task credential is not recognized")
        # Deterministic failure order: the credential's own state
        # first, then the host bindings it depends on. A revoked
        # credential stays revoked no matter what else changed.
        events = self._fetch_events(stored["task_id"])
        if self._is_revoked(events):
            raise TaskCredentialRevoked(
                "task credential has been revoked")
        installation = self.identity._fetch_installation()
        if installation is None \
                or installation["installation_id"] \
                != stored["installation_id"] \
                or installation["company_id"] != stored["company_id"]:
            raise TaskBindingMismatch(
                "task credential does not match the currently bound "
                "installation")
        member = self.identity._fetch_member(stored["principal_id"])
        if member is None or member["status"] != "active":
            raise TaskMembershipRevoked(
                "the issuing principal's membership is not active")
        if member["installation_id"] != stored["installation_id"] \
                or member["company_id"] != stored["company_id"]:
            raise TaskBindingMismatch(
                "membership does not match the credential binding")
        _policy, current = self._load_bound_policy(stored)
        self._check_current_policy(stored, current, now)
        # Revision equality alone never proves the stored cap is
        # legitimate: revalidate it against the current policy scope
        # before any of its values are used. A cap that passes is
        # carried into the context unchanged, never widened.
        _check_stored_scope(stored["scope"], current["scope"])
        if now >= stored["scope"].not_after:
            raise TaskCredentialExpired("task credential has expired")
        if now < stored["scope"].not_before:
            raise TaskCredentialNotYetValid(
                "task credential validity period has not started")
        return self._context(stored, now)

    # -- revocation ---------------------------------------------------------------

    def revoke_task_credential(self, session_secret, *, now, task_id):
        """Revoke one root-task credential; idempotent.

        Authenticated by the operator's current user session, resolved
        inside the same write transaction as the revocation event.
        Returns True when this call appended the revocation event
        (consuming the next ledger-wide revocation sequence number),
        False when the credential was already revoked. This does not
        claim to stop a running worker; every subsequent host-side
        resolution of the credential fails closed immediately.
        """
        session_secret = _require_secret("session_secret", session_secret)
        task_id = self._require_id("task_id", task_id)
        now = _require_time("now", now)
        origin = _monotonic_ns()

        def core():
            tx_now = _effective_now(now, origin)
            context = self.identity.resolve_session(session_secret,
                                                    now=tx_now)
            stored = self._fetch_credential_by_task(task_id)
            if stored is None:
                raise UnknownTaskCredential(
                    "unknown task credential")
            if stored["installation_id"] != context.installation_id \
                    or stored["company_id"] != context.company_id:
                raise TaskBindingMismatch(
                    "task credential does not belong to the operator's "
                    "bound installation or company")
            events = self._fetch_events(task_id)
            if self._is_revoked(events):
                # Repeat revocation returns the current state and
                # consumes no sequence number.
                return False
            cursor = self.db.execute(
                "UPDATE host_task_credentials_ledger_state "
                "SET revocation_seq = revocation_seq + 1 "
                "WHERE singleton = 1")
            if cursor.rowcount != 1:
                raise ValueError(
                    "revocation sequence row is missing; refusing to "
                    "trust possibly tampered data")
            revocation_seq = self.db.execute(
                "SELECT revocation_seq "
                "FROM host_task_credentials_ledger_state "
                "WHERE singleton = 1").fetchone()[0]
            self._recheck_int("revocation_seq", revocation_seq, 1)
            self.db.execute(
                "INSERT INTO host_task_credential_events "
                "(task_id, event_seq, event, revocation_seq, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, ?, 'revoked', ?, ?, ?, ?, ?)",
                (task_id, len(events) + 1, revocation_seq,
                 context.principal_id, context.member_revision,
                 context.session_ref, _store_time(tx_now)))
            return True
        return self._write_tx(core)
