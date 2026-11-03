"""Host approval admission facts (E1); no execution or permission API.

Only a trusted host execution boundary calls record_admission at actual
admission. Workers, decision/reservation services and ID-return paths must
never call it to manufacture usage evidence. A real current task credential,
the full reservation chain and both expected digests are required. There is
no production wiring in this slice. A fact proves admission use, never an
execution result, successful side effect, token or permission to execute.

The host explicitly provisions this table with initialize=True on its own
connection, alongside the F-007 tables in a WorkDatabaseSpec database. The
existing whole-database W checkpoint includes these rows and their guards;
no extra file or packaging path is needed. Ordinary construction is read
only and never repairs missing tables/guards. All access checks schema and
guards before trusting absence. read_consumptions is a trusted host history
surface (not a user history endpoint or proof of current non-consumption).
It verifies every row and its prepared -> decision -> reservation chain,
including expired/revoked history. get_current_status separately rechecks
current credentials and validity, and may fail while history remains intact.

record_admission owns a fresh write transaction. _append_admission is an
internal, no-commit composition seam for the future trusted admission/intent
owner: it requires an already owned fresh write transaction on this same
connection. E2 must jointly persist intent and this fact and prove atomicity;
E1 does not implement intent, Broker, CAS or operation state transitions.
There is no public API accepting a transaction or committing a caller's one.

SHA/canonical verification detects corruption; it is not a signature, an
independent trust anchor or protection from a privileged whole-DB rewrite.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from app import host_approval_decisions as decisions_module
from app import host_approval_reservations as reservations_module
from app import host_prepared_effects as prepared_module
from app.facts_tree import git_format
from app.host_approval_reservations import (
    ApprovalReservationExpired,
    ApprovalReservation,
    ApprovalReservationUnavailable,
    HostApprovalReservationService,
    _check_record,
    _effective_now,
    _load_time,
    _monotonic_ns,
    _require_digest,
)
from app.host_authorization import _require_time
from app.host_prepared_effects import _freeze, _require_secret, _store_time, _thaw
from app.host_task_credentials import (
    HostTaskCredentialError,
    TaskCredentialExpired,
    TaskPolicyExpired,
)

_TABLE = "host_approval_consumptions"
_DOMAIN = b"sam-host-approval-consumption row-v1\x00"
_FIELDS = (
    "reservation_id", "approval_id", "operation_id", "task_id", "run_id",
    "effect_digest", "preparation_digest", "created_at", "expires_at",
)
_COLUMNS = ", ".join(_FIELDS + ("record_blob", "row_sha256"))
_KEYS = (reservations_module._RECORD_KEYS - {"reservation_state"}
         | {"consumption_state", "reservation_created_at"})

_SCHEMA = """
CREATE TABLE host_approval_consumptions (
    reservation_id TEXT PRIMARY KEY NOT NULL,
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
CREATE TRIGGER host_approval_consumptions_no_update
BEFORE UPDATE ON host_approval_consumptions BEGIN
    SELECT RAISE(ABORT, 'approval_consumption_append_only');
END;
CREATE TRIGGER host_approval_consumptions_no_delete
BEFORE DELETE ON host_approval_consumptions BEGIN
    SELECT RAISE(ABORT, 'approval_consumption_append_only');
END;
CREATE TRIGGER host_approval_consumptions_no_replace
BEFORE INSERT ON host_approval_consumptions
WHEN EXISTS (
    SELECT 1 FROM host_approval_consumptions
    WHERE reservation_id = NEW.reservation_id
       OR approval_id = NEW.approval_id
       OR operation_id = NEW.operation_id
) BEGIN
    SELECT RAISE(ABORT, 'approval_consumption_append_only');
END;
"""


class ApprovalConsumptionError(Exception):
    """Fixed code only; no input, SQL, secret or dependency exception chain."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApprovalConsumption:
    """Immutable historical admission fact; no execution methods."""

    reservation_id: str
    approval_id: str
    operation_id: str
    effect_digest: str
    preparation_digest: str
    created_at: datetime
    expires_at: datetime
    record: object
    record_bytes: bytes

    def to_dict(self):
        return _thaw(self.record)


@dataclass(frozen=True)
class ApprovalAdmissionStatus:
    """Current verified reservation plus its independent historical fact.

    consumption=None means only that this current ledger has no matching
    fact. Neither branch authorizes execution or proves successful execution.
    """

    reservation: ApprovalReservation
    consumption: ApprovalConsumption | None


def _safe(call):
    """Discard dependency exceptions outside their handlers, including chains."""
    code = None
    try:
        return call()
    except ApprovalConsumptionError as exc:
        code = exc.code
    except (ApprovalReservationExpired, TaskCredentialExpired, TaskPolicyExpired):
        code = "approval_consumption_expired"
    except ApprovalReservationUnavailable:
        code = "approval_consumption_unavailable"
    except HostTaskCredentialError:
        code = "approval_consumption_authorization_invalid"
    except sqlite3.IntegrityError:
        code = "approval_consumption_conflict"
    except sqlite3.Error:
        code = "approval_consumption_storage_invalid"
    except (ValueError, TypeError, KeyError, OverflowError):
        code = "approval_consumption_invalid"
    raise ApprovalConsumptionError(code)


def _normalize_sql(sql):
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"\bIF NOT EXISTS\b", "", sql, flags=re.IGNORECASE)
    return " ".join(sql.strip().rstrip(";").split())


def _definitions(script):
    """Parse only our known static DDL, without opening a scratch database."""
    statement = ""
    result = {}
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = _normalize_sql(statement)
            match = re.match(r"CREATE (TABLE|TRIGGER) (\w+)", sql)
            if not match:
                raise ApprovalConsumptionError("approval_consumption_storage_invalid")
            result[match[2]] = (match[1].lower(), sql)
            statement = ""
    return result


_DEFINITIONS = {}
for _ddl in (_SCHEMA, reservations_module._SCHEMA,
             decisions_module._SCHEMA, prepared_module._SCHEMA):
    _DEFINITIONS.update(_definitions(_ddl))


def _check_storage(db, *, include_consumptions=True):
    expected = {name: value for name, value in _DEFINITIONS.items()
                if include_consumptions or not name.startswith(_TABLE)}
    tables = {name for name, (kind, _) in expected.items() if kind == "table"}
    rows = db.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE tbl_name IN ("
        + ",".join("?" for _ in tables) + ") AND sql IS NOT NULL",
        tuple(sorted(tables))).fetchall()
    actual = {name: (kind, _normalize_sql(sql)) for name, kind, sql in rows}
    if actual != expected:
        raise ApprovalConsumptionError("approval_consumption_storage_invalid")
    # The DDL text alone is insufficient if sqlite_master was damaged:
    # require the actual unique index structures as well.
    unique_keys = {
        "host_approval_consumptions": {("reservation_id",), ("approval_id",), ("operation_id",)},
        "host_approval_reservations": {("reservation_id",), ("approval_id",), ("operation_id",)},
        "host_approval_decisions": {("approval_id",), ("operation_id",)},
        "host_prepared_effects": {("operation_id",), ("task_id", "run_id", "idempotency_key")},
    }
    for table in tables:
        observed = set()
        for index in db.execute("PRAGMA index_list(" + table + ")"):
            if index[2] and not index[4]:
                # Index names originate in sqlite_master, so quote as an SQL
                # identifier even though our own generated names are simple.
                name = '"' + index[1].replace('"', '""') + '"'
                observed.add(tuple(item[2] for item in db.execute(
                    "PRAGMA index_info(" + name + ")")))
        if observed != unique_keys[table]:
            raise ApprovalConsumptionError("approval_consumption_storage_invalid")


def verify_consumption_row(row, *, installation_id, company_id):
    """Pure row verification for an authenticated host/W reader; no DDL/IO.

    Row order is _COLUMNS. Scope comes from the trusted surrounding identity,
    never from the row being checked. Chain verification is a separate step
    performed by the service history reader; a hash alone is insufficient.
    """
    def verify():
        HostApprovalReservationService._require_id("installation_id", installation_id)
        HostApprovalReservationService._require_id("company_id", company_id)
        if not isinstance(row, (tuple, list, sqlite3.Row)) or len(row) != 11:
            raise ValueError("row shape")
        blob, sha = row[-2:]
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise ValueError("blob type")
        blob = bytes(blob)
        payload = git_format.loads_record(blob)
        if git_format.dumps_record(payload) != blob or set(payload) != _KEYS:
            raise ValueError("canonical shape")
        if payload["consumption_state"] != "consumed":
            raise ValueError("fact type")
        reservation = dict(payload)
        del reservation["consumption_state"]
        reservation_created = reservation.pop("reservation_created_at")
        reservation["reservation_state"] = "reserved"
        _check_record(reservation)
        if not (_load_time("reservation_created_at", reservation_created)
                <= _load_time("created_at", payload["created_at"])):
            raise ValueError("time order")
        _require_digest("row_sha256", sha)
        if hashlib.sha256(_DOMAIN + blob).hexdigest() != sha:
            raise ValueError("row digest")
        if tuple(payload[key] for key in _FIELDS) != tuple(row[:-2]):
            raise ValueError("index agreement")
        if (payload["installation_id"] != installation_id
                or payload["company_id"] != company_id):
            raise ValueError("scope")
        return ApprovalConsumption(
            *(payload[key] for key in _FIELDS[:3]),
            payload["effect_digest"], payload["preparation_digest"],
            _load_time("created_at", payload["created_at"]),
            _load_time("expires_at", payload["expires_at"]),
            _freeze(payload), blob)
    return _safe(verify)


class HostApprovalConsumptionService:
    """Connection-bound trusted host storage; never instantiated in production."""

    def __init__(self, db, reservations, *, initialize=False):
        def construct():
            if (not isinstance(db, sqlite3.Connection)
                    or not isinstance(reservations, HostApprovalReservationService)
                    or reservations.db is not db or db.in_transaction
                    or type(initialize) is not bool):
                raise ValueError("connection binding")
            # Validate the entire dependency graph before any DDL. Do not
            # reconstruct services here: their constructors install guards.
            services = (reservations.identity, reservations.authorization,
                        reservations.task_credentials, reservations.prepared_effects,
                        reservations.approval_decisions)
            if any(service.db is not db for service in services):
                raise ValueError("connection binding")
            i, a, t, p, d = services
            if (a.identity is not i or t.identity is not i or t.authorization is not a
                    or p.identity is not i or p.authorization is not a
                    or p.task_credentials is not t or d.identity is not i
                    or d.authorization is not a or d.task_credentials is not t
                    or d.prepared_effects is not p):
                raise ValueError("service binding")
            self.db = db
            self.reservations = reservations
            self.lock = RLock()
            # Explicit first provisioning only, atomically; existing partial
            # schemas are rejected even when initialize=True.
            db.execute("BEGIN IMMEDIATE" if initialize else "BEGIN")
            try:
                _check_storage(db, include_consumptions=False)
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?", (_TABLE,)).fetchone()
                if initialize and not exists:
                    for _, sql in _definitions(_SCHEMA).values():
                        db.execute(sql)
                _check_storage(db)
                if initialize:
                    db.commit()
            finally:
                if db.in_transaction:
                    db.rollback()
        _safe(construct)

    def _transaction(self, write, call):
        def run():
            with self.lock:
                if self.db.in_transaction:
                    raise ApprovalConsumptionError("approval_consumption_transaction_active")
                self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    _check_storage(self.db)
                    result = call()
                    if write:
                        self.db.commit()
                    return result
                finally:
                    if self.db.in_transaction:
                        self.db.rollback()
        return _safe(run)

    def _identity_scope(self):
        installation = self.reservations.identity._fetch_installation()
        if installation is None:
            raise ValueError("missing host identity")
        return {key: installation[key] for key in ("installation_id", "company_id")}

    def _verify_chain(self, fact):
        r = self.reservations
        reservation = r._fetch_by_operation(fact.operation_id)
        effect, decision = r._load_verified_pair(fact.operation_id)
        if reservation is None or effect is None or decision is None:
            raise ValueError("incomplete chain")
        if decision.decision != "approved":
            raise ApprovalConsumptionError("approval_consumption_denied")
        r._check_reservation_binding(reservation, decision, effect)
        record = fact.to_dict()
        record.pop("consumption_state")
        record["reservation_state"] = "reserved"
        record["created_at"] = record.pop("reservation_created_at")
        if record != reservation.to_dict():
            raise ValueError("reservation binding")
        if not (effect.created_at <= decision.created_at <= reservation.created_at
                <= fact.created_at < fact.expires_at <= effect.expires_at):
            raise ValueError("historical time chain")
        return fact

    def _fetch(self, operation_id):
        rows = self.db.execute(
            "SELECT " + _COLUMNS + " FROM " + _TABLE + " WHERE operation_id = ?",
            (operation_id,)).fetchall()
        if len(rows) > 1:
            raise ValueError("duplicate fact")
        if not rows:
            return None
        fact = verify_consumption_row(rows[0], **self._identity_scope())
        return self._verify_chain(fact)

    def _current(self, task_secret, now, origin, operation_id):
        # Authenticate before distinguishing denial from absence. A denied
        # decision is explicit even though it can never have a reservation.
        r = self.reservations
        ctx = r._resolve_task(task_secret, _effective_now(now, origin))
        effect, decision = r._load_verified_pair(operation_id)
        if (effect is not None and decision is not None
                and effect.envelope["task_id"] == ctx.task_id
                and effect.envelope["run_id"] == ctx.run_id
                and decision.decision == "denied"):
            raise ApprovalConsumptionError("approval_consumption_denied")
        return r._get_current_in_transaction(task_secret, now, origin, operation_id)

    def _append_admission(self, task_secret, now, origin, operation_id,
                          reservation_id, approval_id, effect_digest,
                          preparation_digest):
        """Internal trusted write-owner seam; never commits/rolls back.

        The owner must already hold BEGIN IMMEDIATE and recheck actual
        effect/base/dependencies at the execution boundary. This component
        checks the host chain, not real Git or external resources.
        """
        if not self.db.in_transaction:
            raise ApprovalConsumptionError("approval_consumption_transaction_required")
        _check_storage(self.db)
        reservation = self._current(task_secret, now, origin, operation_id)
        if (reservation.reservation_id != reservation_id
                or reservation.approval_id != approval_id
                or reservation.effect_digest != effect_digest
                or reservation.preparation_digest != preparation_digest):
            raise ApprovalConsumptionError("approval_consumption_binding_mismatch")
        stored = self._fetch(operation_id)
        if stored is not None:
            return stored
        record = reservation.to_dict()
        del record["reservation_state"]
        record["consumption_state"] = "consumed"
        record["reservation_created_at"] = record["created_at"]
        created = _effective_now(now, origin)
        if created >= reservation.expires_at:
            raise ApprovalConsumptionError("approval_consumption_expired")
        record["created_at"] = _store_time(created)
        blob = git_format.dumps_record(record)
        row = tuple(record[key] for key in _FIELDS) + (
            blob, hashlib.sha256(_DOMAIN + blob).hexdigest())
        fact = verify_consumption_row(row, **self._identity_scope())
        self._verify_chain(fact)
        self.db.execute("INSERT INTO " + _TABLE + " (" + _COLUMNS + ") VALUES ("
                        + ",".join("?" for _ in row) + ")", row)
        return fact

    def record_admission(self, task_secret, *, now, operation_id, reservation_id,
                         approval_id, expected_effect_digest,
                         expected_preparation_digest):
        """Trusted execution-boundary admission only; never execute a side effect.

        Current validity is required even on replay. Expired/revoked facts
        remain readable through the separate trusted history surface.
        """
        def record():
            secret = _require_secret("task_secret", task_secret)
            base = _require_time("now", now)
            origin = _monotonic_ns()
            for name, value in (("operation_id", operation_id),
                                ("reservation_id", reservation_id),
                                ("approval_id", approval_id)):
                HostApprovalReservationService._require_id(name, value)
            _require_digest("effect_digest", expected_effect_digest)
            _require_digest("preparation_digest", expected_preparation_digest)
            return self._transaction(True, lambda: self._append_admission(
                secret, base, origin, operation_id, reservation_id, approval_id,
                expected_effect_digest, expected_preparation_digest))
        return _safe(record)

    def get_current_status(self, task_secret, *, now, operation_id):
        """Fresh current validation; reservation existence never means consumed."""
        def read():
            secret = _require_secret("task_secret", task_secret)
            base = _require_time("now", now)
            origin = _monotonic_ns()
            HostApprovalReservationService._require_id("operation_id", operation_id)
            def fetch():
                reservation = self._current(secret, base, origin, operation_id)
                return ApprovalAdmissionStatus(reservation, self._fetch(operation_id))
            return self._transaction(False, fetch)
        return _safe(read)

    def read_consumptions(self, *, installation_id, company_id):
        """Trusted host history enumeration, including expired/revoked facts.

        No mutation, authorization claims or missing-row-as-unconsumed inference.
        All rows are verified before returning any results; foreign scope,
        missing chain or malformed rows fail the entire read closed.
        """
        def fetch():
            scope = {"installation_id": installation_id, "company_id": company_id}
            if scope != self._identity_scope():
                raise ApprovalConsumptionError("approval_consumption_scope_mismatch")
            rows = self.db.execute("SELECT " + _COLUMNS + " FROM " + _TABLE
                                   + " ORDER BY reservation_id").fetchall()
            return tuple(self._verify_chain(verify_consumption_row(row, **scope))
                         for row in rows)
        return self._transaction(False, fetch)
