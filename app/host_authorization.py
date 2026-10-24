"""Versioned authorization-policy ledger for the SAM host (F-007 step 2).

Approved design baseline 4ba389ebe3ecc39ba10716ce107e4d25e4fb63a6,
F-007 P01 sections 3.1-3.3 (authoritative policy record, revisions are
never rewritten in place, revoked or expired authorization is never
restored), section 4 (the F-007-API-010 pause contract: authorized
operator, policy_id plus expected revision, current state returned,
policy_stale on mismatch) and section 7 implementation step 2, limited
to this slice: ledger persistence and versioned policy state.

This module implements persistence and versioned state only:

- create_policy atomically appends an immutable policy record and its
  revision 1. The caller never supplies actor, installation or
  company: every call re-resolves the session through the bound
  HostIdentityService and takes them from the current authenticated
  OwnerContext. There is no cache, so a revoked session or membership
  fails closed on the very next call.
- The policy scope (target and plan version, trigger and stop
  conditions, actions/objects/fields/read window, egress/recipients,
  validity period, budget, and delegation/concurrency/retry limits) is
  fixed at creation. It is never edited in place; a changed scope can
  only become a new policy. Status changes append a new revision
  carrying the unchanged scope bytes.
- pause/resume/revoke require expected_revision and refuse a mismatch
  with PolicyStale. revoked is terminal. Resume of an expired policy
  is refused: retries, checkpoints and restarts never restore expired
  or revoked authorization. Each effective status change increments
  the ledger-wide monotonic revocation sequence by exactly one,
  enforced by a database trigger.
- Policy, revision and status history rows are append-only: triggers
  fail closed on any UPDATE or DELETE, and a BEFORE INSERT guard
  refuses any insert over an existing primary key, because with the
  default recursive_triggers=0 the implicit delete inside an INSERT OR
  REPLACE would otherwise bypass the DELETE triggers. The monotonic
  revocation sequence row can therefore never be reset by REPLACE
  either, and its idempotent initialization inserts only when the row
  does not exist yet.

Write-time contract: a write call validates its caller-provided
trusted host timestamp, captures a monotonic nanosecond reading
alongside it and only then contends for the SQLite write lock.
Inside the BEGIN IMMEDIATE transaction the effective time is
re-derived as the base timestamp advanced by the monotonic elapsed
time in between: the non-negative nanosecond delta is floored to
whole microseconds, the resolution of datetime, so the effective
time never runs ahead of the observed monotonic elapsed time and
never precedes the trusted base. Session expiry resolution, the
policy not_after boundary check and every stored timestamp use that
in-transaction effective time, so a lock wait that crossed an
expiry boundary - even a sub-second wait crossing it by a single
microsecond - can never create or restore authorization on a stale
pre-lock time; the pre-transaction expiry check in create_policy is
only a fast rejection and is always repeated inside the transaction.
The monotonic clock is not caller data, so a client can never move
the current time. Residual bound: an elapsed increment shorter than
one microsecond is floored away, so the effective time can trail
the true elapsed host time by less than a microsecond and can never
lead it.

Scope of this slice (explicitly NOT implemented here): policy
decisions or enforcement (pausing persists the state; refusing new
triggers at business entry points is a later slice), task credentials,
delegation execution, one-time approvals, the operation ledger, the
Commit Broker, queues, HTTP routes, login adapters, feature gates and
any change to the global app.db schema or migrations. Nothing here
claims the ledger is wired into a business entry point.

Connection contract: identical to app.host_identity. The constructor
takes only a caller-provided sqlite3.Connection, idle at construction;
it never opens a database, never chooses a data path, never reads
environment variables and never touches the default app.db. Write
methods own exactly one BEGIN IMMEDIATE transaction, commit it on
success and roll it back on any exception; calling one inside a caller
transaction is refused. Read methods open their own short snapshot
when the connection is idle and close it with ROLLBACK; inside a
caller transaction they read through it and never close, commit or
roll it back.

Identity contract: the constructor takes the bound
HostIdentityService, and it must be bound to the very same
sqlite3.Connection object as this ledger. Two different connections -
including two connections to the same database file - are refused
before any DDL runs, because identity resolution and the policy write
must live in one SQLite transaction on one connection. Every public
method re-resolves the session through it on every call - inside the
same transaction for writes, so the resolution and the ledger write
are one atomic unit - and the resolved installation and company are
re-checked against the stored policy rows. In the local-owner mode a
resolvable session is the bound owner's; this module adds no role
policy of its own because the approved design freezes none for this
slice.

Undefined business semantics (for example the read window) receive
strict structural validation and lossless storage only; they are
neither interpreted nor executed here.
"""
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from app.facts_tree import git_format
from app.host_identity import HostIdentityService

# Monotonic time source used to re-derive the effective time inside a
# write transaction: integer nanoseconds, so that even a sub-second
# write-lock wait moves the effective time at datetime's microsecond
# resolution. A plain module attribute so deterministic tests can
# simulate a contended write-lock wait without real sleeping;
# production always uses time.monotonic_ns.
_monotonic_ns = time.monotonic_ns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_authorization_policies (
    policy_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_authorization_policy_revisions (
    policy_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'paused', 'revoked')),
    revocation_seq INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    actor_principal_id TEXT NOT NULL,
    actor_member_revision INTEGER NOT NULL,
    session_ref TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, revision)
);
CREATE TABLE IF NOT EXISTS host_authorization_ledger_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revocation_seq INTEGER NOT NULL
);
-- Idempotent singleton initialization: inserts only while the row does
-- not exist, so re-running this schema on an existing database never
-- trips the no-replace guard below and never touches the stored
-- sequence value.
INSERT INTO host_authorization_ledger_state (singleton, revocation_seq)
    SELECT 1, 0 WHERE NOT EXISTS (
        SELECT 1 FROM host_authorization_ledger_state);
CREATE TRIGGER IF NOT EXISTS host_authorization_policies_no_update
BEFORE UPDATE ON host_authorization_policies BEGIN
    SELECT RAISE(ABORT, 'host authorization policies are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_policies_no_delete
BEFORE DELETE ON host_authorization_policies BEGIN
    SELECT RAISE(ABORT, 'host authorization policies are append-only');
END;
-- INSERT OR REPLACE on a conflicting primary key would make SQLite
-- delete the stored row before inserting the replacement, and that
-- implicit delete does not fire the BEFORE DELETE trigger while
-- recursive_triggers is off (the SQLite default). BEFORE INSERT runs
-- before any conflict resolution while the old row is still present,
-- so refusing an insert over an existing key makes REPLACE unable to
-- overwrite or delete stored history.
CREATE TRIGGER IF NOT EXISTS host_authorization_policies_no_replace
BEFORE INSERT ON host_authorization_policies
WHEN EXISTS (
    SELECT 1 FROM host_authorization_policies
    WHERE policy_id = NEW.policy_id
) BEGIN
    SELECT RAISE(ABORT, 'host authorization policies are append-only: INSERT OR REPLACE cannot overwrite an existing policy');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_revisions_no_update
BEFORE UPDATE ON host_authorization_policy_revisions BEGIN
    SELECT RAISE(ABORT, 'host authorization revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_revisions_no_delete
BEFORE DELETE ON host_authorization_policy_revisions BEGIN
    SELECT RAISE(ABORT, 'host authorization revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_revisions_no_replace
BEFORE INSERT ON host_authorization_policy_revisions
WHEN EXISTS (
    SELECT 1 FROM host_authorization_policy_revisions
    WHERE policy_id = NEW.policy_id AND revision = NEW.revision
) BEGIN
    SELECT RAISE(ABORT, 'host authorization revisions are append-only: INSERT OR REPLACE cannot overwrite an existing revision');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_seq_increment_only
BEFORE UPDATE ON host_authorization_ledger_state
WHEN NEW.revocation_seq != OLD.revocation_seq + 1 BEGIN
    SELECT RAISE(ABORT,
        'revocation sequence must increase by exactly one');
END;
CREATE TRIGGER IF NOT EXISTS host_authorization_seq_no_delete
BEFORE DELETE ON host_authorization_ledger_state BEGIN
    SELECT RAISE(ABORT, 'revocation sequence row cannot be deleted');
END;
-- Same REPLACE bypass as above: without this guard, INSERT OR REPLACE
-- could silently delete the singleton row and reinsert it with a reset
-- or forged sequence value. The only legal write to the sequence is the
-- increment-by-one UPDATE inside a transition.
CREATE TRIGGER IF NOT EXISTS host_authorization_seq_no_replace
BEFORE INSERT ON host_authorization_ledger_state
WHEN EXISTS (
    SELECT 1 FROM host_authorization_ledger_state
    WHERE singleton = NEW.singleton
) BEGIN
    SELECT RAISE(ABORT, 'revocation sequence cannot be reset: INSERT OR REPLACE is refused once the row exists');
END;
"""

_POLICY_COLUMNS = ("policy_id, installation_id, company_id, "
                   "granted_by, created_at")
_REVISION_COLUMNS = ("policy_id, revision, status, revocation_seq, "
                     "scope_json, installation_id, company_id, "
                     "actor_principal_id, actor_member_revision, "
                     "session_ref, changed_at")

_STATUSES = frozenset({"active", "paused", "revoked"})

_LABEL_MAX = 200

_SCOPE_PAYLOAD_KEYS = frozenset({
    "target", "plan_version", "trigger_conditions", "stop_conditions",
    "actions", "objects", "fields", "read_window", "egress",
    "recipients", "not_before", "not_after", "budget",
    "max_delegation_depth", "max_concurrency", "max_retries",
})


class HostAuthorizationError(Exception):
    """Base class for every authorization-ledger failure; fails closed."""


class PolicyNotFound(HostAuthorizationError):
    """The policy (or requested revision) does not exist in this ledger."""


class PolicyStale(HostAuthorizationError):
    """expected_revision does not match the current policy revision."""


class PolicyStateConflict(HostAuthorizationError):
    """The requested status transition is not legal from the current one."""


class PolicyAlreadyRevoked(PolicyStateConflict):
    """revoked is terminal; no further status transition is possible."""


class PolicyExpired(HostAuthorizationError):
    """The policy validity period has passed; it cannot take effect."""


@dataclass(frozen=True)
class PolicyScope:
    """Immutable scope of one authorization policy.

    Plain frozen data. Business semantics that the approved design has
    not frozen (for example the read window) are stored losslessly and
    never interpreted by this module. Budget is held as a sorted tuple
    of (key, limit) pairs so the record is genuinely immutable; the
    mapping is restored losslessly on storage and on load.
    """

    target: str
    plan_version: str
    trigger_conditions: tuple
    stop_conditions: tuple
    actions: tuple
    objects: tuple
    fields: tuple
    read_window: str
    egress: object  # None (no egress) or a non-empty str
    recipients: tuple
    not_before: datetime
    not_after: datetime
    budget: tuple
    max_delegation_depth: int
    max_concurrency: int
    max_retries: int


@dataclass(frozen=True)
class PolicyRecord:
    """One policy revision as read from the ledger.

    Plain frozen data: no secret material and no authority methods.
    granted_by/created_at come from the immutable policy row; the rest
    describes this revision. The scope is identical on every revision
    of a policy.
    """

    policy_id: str
    revision: int
    status: str
    revocation_seq: int
    installation_id: str
    company_id: str
    granted_by: str
    actor_principal_id: str
    actor_member_revision: int
    session_ref: str
    created_at: datetime
    changed_at: datetime
    scope: PolicyScope


def _effective_now(base, origin):
    """In-transaction effective time for one write.

    ``base`` is the trusted host timestamp validated at call entry and
    ``origin`` the integer monotonic-nanosecond reading captured
    alongside it, before the call contends for the SQLite write lock.
    Read after BEGIN IMMEDIATE, the monotonic delta covers any lock
    wait down to microsecond granularity, so session expiry, the
    policy not_after boundary and the stored timestamps can never be
    judged on a stale pre-lock time - not even when the wait is
    shorter than a second. The non-negative nanosecond delta is
    floored to whole microseconds, the resolution of datetime: the
    effective time never runs ahead of the observed monotonic elapsed
    time and never precedes the trusted base. Only the monotonic
    clock - never caller data - moves the time forward.
    """
    elapsed_ns = _monotonic_ns() - origin
    if elapsed_ns < 0:
        elapsed_ns = 0
    return base + timedelta(microseconds=elapsed_ns // 1000)


def _store_time(value):
    return value.astimezone(timezone.utc).isoformat()


def _load_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError(
            "stored authorization timestamp is not a non-empty string; "
            "refusing to trust possibly tampered data")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "stored authorization timestamp failed to parse; refusing "
            "to trust possibly tampered data") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "stored authorization timestamp is naive; refusing to "
            "trust possibly tampered data")
    return parsed.astimezone(timezone.utc)


def _require_label(name, value):
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError("%s must be a host-provided str" % name)
    if not value:
        raise ValueError("%s must not be empty" % name)
    if len(value) > _LABEL_MAX:
        raise ValueError("%s exceeds the host label length bound" % name)
    return value


def _require_labels(name, value, allow_empty=False):
    if isinstance(value, bool) or not isinstance(value, (list, tuple)):
        raise TypeError("%s must be a list or tuple of str" % name)
    if not value and not allow_empty:
        raise ValueError("%s must name at least one entry" % name)
    return tuple(_require_label("%s entry" % name, item)
                 for item in value)


def _require_time(name, value):
    if not isinstance(value, datetime):
        raise TypeError(
            "%s must be a caller-provided timezone-aware datetime"
            % name)
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "%s must be timezone-aware; naive datetimes are ambiguous "
            "and rejected" % name)
    return value.astimezone(timezone.utc)


def _require_budget(value):
    if isinstance(value, bool) or not isinstance(value, dict):
        raise TypeError("budget must be a dict of str to positive int")
    if not value:
        raise ValueError("budget must name at least one limit")
    items = []
    for key, limit in value.items():
        _require_label("budget key", key)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(
                "budget limit for %r must be an int; bool is never a "
                "valid limit" % key)
        if limit < 1:
            raise ValueError(
                "budget limit for %r must be positive" % key)
        items.append((key, limit))
    # Sorted once here so the stored form and the in-memory form are
    # both canonical and the round trip through JSON is lossless.
    return tuple(sorted(items))


def _require_limit(name, value):
    # bool is a subclass of int, so the bool check must run first:
    # True can never be a limit at this boundary.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            "%s must be an int; bool is never a valid limit" % name)
    if value < 0:
        raise ValueError("%s must not be negative" % name)
    return value


def _require_revision(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            "%s must be an int revision; bool is never valid" % name)
    if value < 1:
        raise ValueError("%s must be a positive revision" % name)
    return value


def _build_scope(*, target, plan_version, trigger_conditions,
                 stop_conditions, actions, objects, fields, read_window,
                 egress, recipients, not_before, not_after, budget,
                 max_delegation_depth, max_concurrency, max_retries):
    not_before = _require_time("not_before", not_before)
    not_after = _require_time("not_after", not_after)
    if not_after <= not_before:
        raise ValueError("not_after must be later than not_before")
    return PolicyScope(
        target=_require_label("target", target),
        plan_version=_require_label("plan_version", plan_version),
        trigger_conditions=_require_labels("trigger_conditions",
                                           trigger_conditions),
        stop_conditions=_require_labels("stop_conditions",
                                        stop_conditions),
        actions=_require_labels("actions", actions),
        objects=_require_labels("objects", objects),
        fields=_require_labels("fields", fields),
        read_window=_require_label("read_window", read_window),
        egress=None if egress is None
        else _require_label("egress", egress),
        recipients=_require_labels("recipients", recipients,
                                   allow_empty=True),
        not_before=not_before,
        not_after=not_after,
        budget=_require_budget(budget),
        max_delegation_depth=_require_limit("max_delegation_depth",
                                            max_delegation_depth),
        max_concurrency=_require_limit("max_concurrency",
                                       max_concurrency),
        max_retries=_require_limit("max_retries", max_retries),
    )


def _scope_payload(scope):
    return {
        "target": scope.target,
        "plan_version": scope.plan_version,
        "trigger_conditions": list(scope.trigger_conditions),
        "stop_conditions": list(scope.stop_conditions),
        "actions": list(scope.actions),
        "objects": list(scope.objects),
        "fields": list(scope.fields),
        "read_window": scope.read_window,
        "egress": scope.egress,
        "recipients": list(scope.recipients),
        "not_before": _store_time(scope.not_before),
        "not_after": _store_time(scope.not_after),
        "budget": dict(scope.budget),
        "max_delegation_depth": scope.max_delegation_depth,
        "max_concurrency": scope.max_concurrency,
        "max_retries": scope.max_retries,
    }


def _dumps_scope(scope):
    return json.dumps(_scope_payload(scope), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _loads_scope(text):
    if not isinstance(text, str) or not text:
        raise ValueError(
            "stored policy scope is not a non-empty string; refusing "
            "to trust possibly tampered data")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(
            "stored policy scope failed to parse; refusing to trust "
            "possibly tampered data") from exc
    if not isinstance(payload, dict) \
            or frozenset(payload) != _SCOPE_PAYLOAD_KEYS:
        raise ValueError(
            "stored policy scope has an unexpected shape; refusing to "
            "trust possibly tampered data")
    try:
        return _build_scope(
            target=payload["target"],
            plan_version=payload["plan_version"],
            trigger_conditions=payload["trigger_conditions"],
            stop_conditions=payload["stop_conditions"],
            actions=payload["actions"],
            objects=payload["objects"],
            fields=payload["fields"],
            read_window=payload["read_window"],
            egress=payload["egress"],
            recipients=payload["recipients"],
            not_before=_load_time(payload["not_before"]),
            not_after=_load_time(payload["not_after"]),
            budget=payload["budget"],
            max_delegation_depth=payload["max_delegation_depth"],
            max_concurrency=payload["max_concurrency"],
            max_retries=payload["max_retries"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "stored policy scope failed re-validation; refusing to "
            "trust possibly tampered data") from exc


class HostAuthorizationService:
    """Append-only versioned ledger of authorization policies.

    See the module docstring for the full scope list and the
    connection, transaction and identity contracts.
    """

    def __init__(self, db, identity):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host authorization requires a caller-provided "
                "sqlite3.Connection; it never opens or chooses a "
                "database path itself")
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host authorization requires the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        # Same-object check, before any DDL: identity resolution and
        # the ledger write must share one connection so they share one
        # SQLite transaction. A second connection - even to the same
        # file - would let the identity re-check silently read a
        # different database or a stale snapshot, so it is refused.
        if identity.db is not db:
            raise ValueError(
                "host authorization requires the identity service's "
                "own connection object; combining two separate "
                "connections breaks the single-transaction identity "
                "guarantee")
        # Checked before any DDL: a caller transaction is left
        # untouched, never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host authorization requires an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.identity = identity
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- boundary validation ------------------------------------------------

    @staticmethod
    def _require_policy_id(value):
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "policy_id is not a valid host-issued id: %s"
                % exc) from exc
        return value

    # -- stored-row re-validation --------------------------------------------

    @staticmethod
    def _recheck_id(name, value):
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored authorization %s failed re-validation; "
                "refusing to trust possibly tampered data"
                % name) from exc
        return value

    @staticmethod
    def _recheck_int(name, value, minimum):
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < minimum:
            raise ValueError(
                "stored authorization %s is not a valid int; refusing "
                "to trust possibly tampered data" % name)
        return value

    # -- transaction helpers --------------------------------------------------

    def _write_tx(self, core):
        if self.db.in_transaction:
            raise ValueError(
                "host authorization writes own their transaction; the "
                "connection must be idle at call time")
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

    def _fetch_policy_row(self, policy_id):
        row = self.db.execute(
            "SELECT " + _POLICY_COLUMNS
            + " FROM host_authorization_policies WHERE policy_id = ?",
            (policy_id,)).fetchone()
        if row is None:
            return None
        (policy, installation_id, company_id, granted_by,
         created_at) = row
        return {
            "policy_id": self._recheck_id("policy_id", policy),
            "installation_id": self._recheck_id("installation_id",
                                                installation_id),
            "company_id": self._recheck_id("company_id", company_id),
            "granted_by": self._recheck_id("granted_by", granted_by),
            "created_at": _load_time(created_at),
        }

    def _revision_from_row(self, row):
        (policy_id, revision, status, revocation_seq, scope_json,
         installation_id, company_id, actor_principal_id,
         actor_member_revision, session_ref, changed_at) = row
        if status not in _STATUSES:
            raise ValueError(
                "stored policy revision has an unknown status; "
                "refusing to trust possibly tampered data")
        return {
            "policy_id": self._recheck_id("policy_id", policy_id),
            "revision": self._recheck_int("revision", revision, 1),
            "status": status,
            "revocation_seq": self._recheck_int("revocation_seq",
                                                revocation_seq, 0),
            "scope_json": scope_json,
            "scope": _loads_scope(scope_json),
            "installation_id": self._recheck_id("installation_id",
                                                installation_id),
            "company_id": self._recheck_id("company_id", company_id),
            "actor_principal_id": self._recheck_id(
                "actor_principal_id", actor_principal_id),
            "actor_member_revision": self._recheck_int(
                "actor_member_revision", actor_member_revision, 1),
            "session_ref": self._recheck_id("session_ref", session_ref),
            "changed_at": _load_time(changed_at),
        }

    def _fetch_revision_row(self, policy_id, revision):
        row = self.db.execute(
            "SELECT " + _REVISION_COLUMNS
            + " FROM host_authorization_policy_revisions "
            "WHERE policy_id = ? AND revision = ?",
            (policy_id, revision)).fetchone()
        if row is None:
            return None
        return self._revision_from_row(row)

    def _fetch_latest_revision(self, policy_id):
        row = self.db.execute(
            "SELECT " + _REVISION_COLUMNS
            + " FROM host_authorization_policy_revisions "
            "WHERE policy_id = ? ORDER BY revision DESC LIMIT 1",
            (policy_id,)).fetchone()
        if row is None:
            return None
        return self._revision_from_row(row)

    @staticmethod
    def _check_consistency(policy, revision):
        # One ledger serves one bound installation; a drift between the
        # policy row, the revision row or the resolved context means
        # tampered or corrupted data, so fail closed.
        if revision["installation_id"] != policy["installation_id"] \
                or revision["company_id"] != policy["company_id"]:
            raise ValueError(
                "stored policy and revision disagree on installation "
                "or company; refusing to trust possibly tampered data")

    @staticmethod
    def _check_binding(context, policy):
        if policy["installation_id"] != context.installation_id \
                or policy["company_id"] != context.company_id:
            raise ValueError(
                "stored policy does not match the currently bound "
                "installation; refusing to trust possibly tampered "
                "data")

    @staticmethod
    def _record(policy, revision):
        return PolicyRecord(
            policy_id=policy["policy_id"],
            revision=revision["revision"],
            status=revision["status"],
            revocation_seq=revision["revocation_seq"],
            installation_id=policy["installation_id"],
            company_id=policy["company_id"],
            granted_by=policy["granted_by"],
            actor_principal_id=revision["actor_principal_id"],
            actor_member_revision=revision["actor_member_revision"],
            session_ref=revision["session_ref"],
            created_at=policy["created_at"],
            changed_at=revision["changed_at"],
            scope=revision["scope"],
        )

    def _load_current(self, context, policy_id):
        policy = self._fetch_policy_row(policy_id)
        if policy is None:
            raise PolicyNotFound("unknown policy_id")
        current = self._fetch_latest_revision(policy_id)
        if current is None:
            raise ValueError(
                "stored policy has no revisions; refusing to trust "
                "possibly tampered data")
        self._check_consistency(policy, current)
        self._check_binding(context, policy)
        return policy, current

    # -- creation ---------------------------------------------------------------

    def create_policy(self, session_secret, *, now, target, plan_version,
                      trigger_conditions, stop_conditions, actions,
                      objects, fields, read_window, egress, recipients,
                      not_before, not_after, budget,
                      max_delegation_depth, max_concurrency,
                      max_retries):
        """Create one bounded policy as the current authenticated owner.

        All scope fields are required and strictly validated; the
        approved design lists each of them as a must-define field of
        the policy record. Actor, installation and company are never
        accepted from the caller: they are resolved from the current
        OwnerContext inside the write transaction. The scope is frozen
        forever on the returned policy_id; changing scope requires a
        new policy. An already-expired validity period is refused
        twice: once here as a fast rejection before contending for the
        write lock, and again inside the transaction on the effective
        in-transaction time, so a lock wait that crosses the not_after
        boundary can never create an expired policy.
        """
        now = _require_time("now", now)
        # Captured next to the validated trusted host time, before the
        # write-lock contention; the core re-derives the effective time
        # from it after BEGIN IMMEDIATE.
        origin = _monotonic_ns()
        scope = _build_scope(
            target=target, plan_version=plan_version,
            trigger_conditions=trigger_conditions,
            stop_conditions=stop_conditions, actions=actions,
            objects=objects, fields=fields, read_window=read_window,
            egress=egress, recipients=recipients, not_before=not_before,
            not_after=not_after, budget=budget,
            max_delegation_depth=max_delegation_depth,
            max_concurrency=max_concurrency, max_retries=max_retries)
        if now >= scope.not_after:
            raise PolicyExpired(
                "the policy validity period has already passed; an "
                "expired policy is never created")

        def core():
            # Re-derived inside the transaction: the entry-time check
            # above may have gone stale while waiting for the lock.
            tx_now = _effective_now(now, origin)
            context = self.identity.resolve_session(session_secret,
                                                    now=tx_now)
            if tx_now >= scope.not_after:
                raise PolicyExpired(
                    "the policy validity period ended before the write "
                    "lock was acquired; an expired policy is never "
                    "created")
            policy_id = "hap_" + secrets.token_hex(8)
            self.db.execute(
                "INSERT INTO host_authorization_policies "
                "(policy_id, installation_id, company_id, granted_by, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (policy_id, context.installation_id, context.company_id,
                 context.principal_id, _store_time(tx_now)))
            self.db.execute(
                "INSERT INTO host_authorization_policy_revisions "
                "(policy_id, revision, status, revocation_seq, "
                "scope_json, installation_id, company_id, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, 1, 'active', 0, ?, ?, ?, ?, ?, ?, ?)",
                (policy_id, _dumps_scope(scope), context.installation_id,
                 context.company_id, context.principal_id,
                 context.member_revision, context.session_ref,
                 _store_time(tx_now)))
            policy = {"policy_id": policy_id,
                      "installation_id": context.installation_id,
                      "company_id": context.company_id,
                      "granted_by": context.principal_id,
                      "created_at": tx_now}
            revision = {"revision": 1, "status": "active",
                        "revocation_seq": 0,
                        "actor_principal_id": context.principal_id,
                        "actor_member_revision": context.member_revision,
                        "session_ref": context.session_ref,
                        "changed_at": tx_now, "scope": scope}
            return self._record(policy, revision)
        return self._write_tx(core)

    # -- reads --------------------------------------------------------------------

    def get_policy(self, session_secret, *, now, policy_id):
        """Return the current revision of one policy.

        Re-resolves the session on every call; a revoked session or
        membership fails closed immediately.
        """
        now = _require_time("now", now)
        policy_id = self._require_policy_id(policy_id)

        def fetch():
            context = self.identity.resolve_session(session_secret,
                                                    now=now)
            policy, current = self._load_current(context, policy_id)
            return self._record(policy, current)
        return self._read(fetch)

    def get_policy_revision(self, session_secret, *, now, policy_id,
                            revision):
        """Return one fixed historical revision of one policy.

        Historical revisions are immutable; this read never derives or
        rewrites past state.
        """
        now = _require_time("now", now)
        policy_id = self._require_policy_id(policy_id)
        revision = _require_revision("revision", revision)

        def fetch():
            context = self.identity.resolve_session(session_secret,
                                                    now=now)
            policy = self._fetch_policy_row(policy_id)
            if policy is None:
                raise PolicyNotFound("unknown policy_id")
            row = self._fetch_revision_row(policy_id, revision)
            if row is None:
                raise PolicyNotFound("unknown policy revision")
            self._check_consistency(policy, row)
            self._check_binding(context, policy)
            return self._record(policy, row)
        return self._read(fetch)

    # -- status transitions --------------------------------------------------------

    def _transition(self, session_secret, *, now, policy_id,
                    expected_revision, target_status, allowed_from,
                    require_unexpired):
        now = _require_time("now", now)
        policy_id = self._require_policy_id(policy_id)
        expected_revision = _require_revision("expected_revision",
                                              expected_revision)
        # Captured next to the validated trusted host time, before the
        # write-lock contention; the core re-derives the effective time
        # from it after BEGIN IMMEDIATE.
        origin = _monotonic_ns()

        def core():
            # Re-derived inside the transaction, so the session expiry,
            # the not_after boundary and the stored changed_at can
            # never be judged on a stale pre-lock time.
            tx_now = _effective_now(now, origin)
            context = self.identity.resolve_session(session_secret,
                                                    now=tx_now)
            policy, current = self._load_current(context, policy_id)
            if current["revision"] != expected_revision:
                raise PolicyStale(
                    "expected_revision %d does not match the current "
                    "policy revision %d; re-read and re-verify instead "
                    "of silently applying a newer version"
                    % (expected_revision, current["revision"]))
            if current["status"] == "revoked":
                raise PolicyAlreadyRevoked(
                    "revoked is terminal; no further status transition "
                    "is possible")
            if current["status"] not in allowed_from:
                raise PolicyStateConflict(
                    "cannot move a %s policy to %s"
                    % (current["status"], target_status))
            if require_unexpired \
                    and tx_now >= current["scope"].not_after:
                raise PolicyExpired(
                    "the policy validity period has passed; an expired "
                    "policy is never restored")
            cursor = self.db.execute(
                "UPDATE host_authorization_ledger_state "
                "SET revocation_seq = revocation_seq + 1 "
                "WHERE singleton = 1")
            if cursor.rowcount != 1:
                raise ValueError(
                    "revocation sequence row is missing; refusing to "
                    "trust possibly tampered data")
            revocation_seq = self.db.execute(
                "SELECT revocation_seq "
                "FROM host_authorization_ledger_state "
                "WHERE singleton = 1").fetchone()[0]
            revision = current["revision"] + 1
            # The new revision carries the unchanged scope bytes: status
            # changes never edit scope in place.
            self.db.execute(
                "INSERT INTO host_authorization_policy_revisions "
                "(policy_id, revision, status, revocation_seq, "
                "scope_json, installation_id, company_id, "
                "actor_principal_id, actor_member_revision, "
                "session_ref, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (policy_id, revision, target_status, revocation_seq,
                 current["scope_json"], context.installation_id,
                 context.company_id, context.principal_id,
                 context.member_revision, context.session_ref,
                 _store_time(tx_now)))
            new_row = {"revision": revision, "status": target_status,
                       "revocation_seq": revocation_seq,
                       "actor_principal_id": context.principal_id,
                       "actor_member_revision": context.member_revision,
                       "session_ref": context.session_ref,
                       "changed_at": tx_now, "scope": current["scope"]}
            return self._record(policy, new_row)
        return self._write_tx(core)

    def pause_policy(self, session_secret, *, now, policy_id,
                     expected_revision):
        """Pause one active policy (F-007-API-010 persistence slice).

        Persists the paused state as a new revision only; refusing new
        triggers at business entry points is a later, separate slice
        and is not claimed here. Already-recorded state is never
        erased.
        """
        return self._transition(
            session_secret, now=now, policy_id=policy_id,
            expected_revision=expected_revision,
            target_status="paused", allowed_from=frozenset({"active"}),
            require_unexpired=False)

    def resume_policy(self, session_secret, *, now, policy_id,
                      expected_revision):
        """Resume one paused policy; an expired policy never resumes."""
        return self._transition(
            session_secret, now=now, policy_id=policy_id,
            expected_revision=expected_revision,
            target_status="active", allowed_from=frozenset({"paused"}),
            require_unexpired=True)

    def revoke_policy(self, session_secret, *, now, policy_id,
                      expected_revision):
        """Revoke one active or paused policy; revoked is terminal."""
        return self._transition(
            session_secret, now=now, policy_id=policy_id,
            expected_revision=expected_revision,
            target_status="revoked",
            allowed_from=frozenset({"active", "paused"}),
            require_unexpired=False)
