"""Trusted identity entrance for the SAM host (F-007 P01 step 1).

Approved design baseline 4ba389ebe3ecc39ba10716ce107e4d25e4fb63a6,
F-007 P01 sections 2-3 (identity/trust boundary and authoritative
records), section 6 (TC-001) and section 7 implementation step 1: the
trusted identity entrance. This module implements the independent
authentication core only:

- Controlled installation initialization binds the local owner and
  company to one installation exactly once. It is a host-managed call;
  there is no HTTP route and no client-reported actor/company/role can
  establish identity here or anywhere else in this module.
- The host issues pairing credentials that are high-entropy (256-bit
  random secrets), time-limited and one-time. Only the SHA-256 digest
  of a credential is stored; the raw secret is returned once at
  issuance and never written to the database, logs, exceptions or
  reprs.
- A successful pairing atomically consumes the credential and issues a
  server-side session in the same transaction. Only the session
  secret's SHA-256 digest is stored; the raw session secret is
  returned once in the SessionGrant.
- resolve_session re-reads the current installation, membership and
  session state from the database on every call. There is no cache:
  revoking the session or the member makes the very next resolution
  fail closed.
- The returned OwnerContext is a plain frozen data object. It carries
  no secret and grants nothing: no policy, task, Git-write or approval
  authority exists on it, and no such method exists on this service.

Scope of this slice (explicitly NOT implemented here): authorization
policies and revisions, task credentials, delegation, one-time
approvals, the Commit Broker, queues, web routes, login adapters,
feature gates, multi-company tenancy and any change to the global
app.db schema or migrations.

Connection contract: the constructor takes only a caller-provided
sqlite3.Connection. It never opens a database, never chooses a data
path, never reads environment variables and never touches the default
app.db. The connection must be idle at construction (no active caller
transaction); construction then creates only this module's own tables
with CREATE TABLE IF NOT EXISTS, which is idempotent across reconnects.

Transaction contract: every write operation (initialize, issue, pair,
revoke) owns exactly one BEGIN IMMEDIATE transaction on the provided
connection, commits it on success and rolls it back on any exception.
Calling a write method while the caller holds a transaction is
refused, because this module must be able to commit or roll back its
own atomic unit. Read methods (resolve_session, get_installation) open
their own short read snapshot when the connection is idle and always
close it with ROLLBACK; inside a caller transaction they read through
it and never close, commit or roll it back.

Atomicity and concurrency: the owner binding is enforced by a
singleton primary key, so concurrent initialization binds at most
once. Pairing consumption is a guarded UPDATE ... WHERE
status='pending' whose rowcount is verified, and the session insert
runs in the same transaction, so concurrent pairing with the same
credential issues at most one session and a failed pairing consumes
nothing and leaves no session behind. Cross-connection concurrency is
serialized by SQLite's BEGIN IMMEDIATE write lock.

Lifetimes: pairing-credential and session lifetimes are explicit
caller-provided configuration (positive int seconds). No default
lifetime is fixed here, because the approved design does not freeze a
product security policy for them. All timestamps are caller-provided
timezone-aware datetimes, normalized to UTC; naive datetimes are
rejected.

Failure semantics: errors fail closed and never include raw secrets,
digests usable as secrets, or unauthorized identity material. Unknown,
expired and consumed credentials/sessions are distinct exception
subclasses so operators and tests can assert precise behaviour, while
every path simply refuses to establish identity.
"""
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock

from app.facts_tree import git_format

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_identity_installation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    owner_principal_id TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    bound_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_identity_members (
    principal_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS host_identity_pairing_credentials (
    credential_id TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL UNIQUE,
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'consumed')),
    consumed_at TEXT,
    consumed_session_ref TEXT
);
CREATE TABLE IF NOT EXISTS host_identity_sessions (
    session_ref TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL UNIQUE,
    installation_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    auth_method TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    revoked_at TEXT
);
"""

_INSTALLATION_COLUMNS = ("singleton, installation_id, company_id, "
                         "owner_principal_id, owner_role, bound_at")
_MEMBER_COLUMNS = ("principal_id, installation_id, company_id, role, "
                   "status, revision, created_at, revoked_at")
_CREDENTIAL_COLUMNS = ("credential_id, secret_sha256, installation_id, "
                       "company_id, principal_id, issued_at, expires_at, "
                       "status, consumed_at, consumed_session_ref")
_SESSION_COLUMNS = ("session_ref, secret_sha256, installation_id, "
                    "company_id, principal_id, auth_method, issued_at, "
                    "expires_at, status, revoked_at")

_AUTH_METHOD = "local_owner_pairing"

_MEMBER_STATUSES = frozenset({"active", "revoked"})
_CREDENTIAL_STATUSES = frozenset({"pending", "consumed"})
_SESSION_STATUSES = frozenset({"active", "revoked"})


class HostIdentityError(Exception):
    """Base class for every host-identity failure; always fails closed."""


class InstallationRequired(HostIdentityError):
    """The installation has not been bound yet; no identity can exist."""


class InstallationAlreadyBound(HostIdentityError):
    """The one-time owner/company binding already exists."""


class PairingFailed(HostIdentityError):
    """Base class for refused pairings; no session was issued."""


class PairingCredentialInvalid(PairingFailed):
    """The pairing credential is not recognized."""


class PairingCredentialExpired(PairingFailed):
    """The pairing credential is past its explicit expiry."""


class PairingCredentialConsumed(PairingFailed):
    """The pairing credential was already consumed by an earlier pairing."""


class SessionResolutionFailed(HostIdentityError):
    """Base class for refused session resolutions (F-007-API-001)."""


class Unauthenticated(SessionResolutionFailed):
    """The session credential is not recognized."""


class SessionExpired(SessionResolutionFailed):
    """The session is past its explicit expiry."""


class SessionRevoked(SessionResolutionFailed):
    """The session has been revoked."""


class InstallationMismatch(SessionResolutionFailed):
    """Stored session/member state does not match the bound installation."""


class MembershipRevoked(SessionResolutionFailed):
    """The principal's membership is missing or revoked."""


class UnknownSession(HostIdentityError):
    """revoke_session target reference does not exist."""


class UnknownMember(HostIdentityError):
    """revoke_member target principal does not exist."""


@dataclass(frozen=True)
class OwnerContext:
    """Trusted actor context resolved from current server-side state.

    Plain frozen data: no secret material, and deliberately no methods
    that grant policy, task, Git-write or approval authority. Every
    field is re-read from the database at resolution time.
    """

    installation_id: str
    company_id: str
    principal_id: str
    role: str
    member_revision: int
    session_ref: str
    auth_method: str
    session_issued_at: datetime
    session_expires_at: datetime
    resolved_at: datetime


@dataclass(frozen=True)
class PairingTicket:
    """One-time pairing credential as returned at issuance.

    The raw secret exists only on this returned object; it is excluded
    from repr and is never stored, logged or included in exceptions.
    """

    credential_id: str
    expires_at: datetime
    secret: str = field(repr=False)


@dataclass(frozen=True)
class SessionGrant:
    """Server-side session as returned at issuance.

    The raw secret exists only on this returned object; it is excluded
    from repr and is never stored, logged or included in exceptions.
    The context carries no secret.
    """

    session_ref: str
    expires_at: datetime
    context: OwnerContext
    secret: str = field(repr=False)


def _pairing_digest(secret):
    return hashlib.sha256(
        b"sam-host-identity pairing-v1\x00"
        + secret.encode("utf-8")).hexdigest()


def _session_digest(secret):
    return hashlib.sha256(
        b"sam-host-identity session-v1\x00"
        + secret.encode("utf-8")).hexdigest()


def _store_time(value):
    return value.astimezone(timezone.utc).isoformat()


def _load_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError(
            "stored host identity timestamp is not a non-empty string; "
            "refusing to trust possibly tampered data")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "stored host identity timestamp failed to parse; refusing "
            "to trust possibly tampered data") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "stored host identity timestamp is naive; refusing to trust "
            "possibly tampered data")
    return parsed.astimezone(timezone.utc)


class HostIdentityService:
    """Independent authentication core for one bound local installation.

    See the module docstring for the full scope list, the connection
    and transaction contracts, and the secrets-handling rules.
    """

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "host identity requires a caller-provided "
                "sqlite3.Connection; it never opens or chooses a "
                "database path itself")
        # Checked before any DDL: a caller transaction is left untouched,
        # never joined and never committed by construction.
        if db.in_transaction:
            raise ValueError(
                "host identity requires an idle connection at "
                "construction; refusing to join or commit a caller "
                "transaction")
        self.db = db
        self.lock = RLock()
        self.db.executescript(_SCHEMA)

    # -- boundary validation ------------------------------------------------

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
    def _require_label(name, value):
        if not isinstance(value, str):
            raise TypeError("%s must be a host-provided str" % name)
        if not value:
            raise ValueError("%s must not be empty" % name)
        if len(value) > 200:
            raise ValueError("%s exceeds the host label length bound" % name)
        return value

    @staticmethod
    def _require_secret(name, value):
        if not isinstance(value, str):
            raise TypeError("%s must be a str credential" % name)
        if not value:
            raise ValueError("%s must not be empty" % name)
        # A plain str can carry lone surrogates; encoding it for the
        # digest would raise UnicodeEncodeError with the raw secret
        # embedded in its object/args/repr. Refuse it at the boundary
        # instead. Only a boolean leaves the except block, so the fixed
        # error below carries no cause, context or other trace of the
        # original encode failure.
        unencodable = False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            unencodable = True
        if unencodable:
            raise ValueError("%s is not a UTF-8 encodable credential"
                             % name)
        return value

    @staticmethod
    def _require_now(value):
        if not isinstance(value, datetime):
            raise TypeError(
                "now must be a caller-provided timezone-aware datetime")
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "now must be timezone-aware; naive datetimes are "
                "ambiguous and rejected")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _require_lifetime(name, value):
        # bool is a subclass of int, so an explicit isinstance check for
        # bool must run first: True can never be a lifetime at this
        # boundary.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                "%s must be an int number of seconds; bool is never a "
                "valid lifetime" % name)
        if value < 1:
            raise ValueError("%s must be a positive number of seconds"
                             % name)
        return value

    # -- stored-row re-validation -------------------------------------------

    @staticmethod
    def _recheck_id(name, value):
        try:
            git_format.encode_id(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stored host identity %s failed re-validation; refusing "
                "to trust possibly tampered data" % name) from exc
        return value

    @staticmethod
    def _recheck_status(name, value, allowed):
        if value not in allowed:
            raise ValueError(
                "stored host identity %s has an unknown state; refusing "
                "to trust possibly tampered data" % name)
        return value

    @staticmethod
    def _recheck_digest(value):
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(
                "stored host identity digest is malformed; refusing to "
                "trust possibly tampered data")
        return value

    @staticmethod
    def _recheck_revision(value):
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < 1:
            raise ValueError(
                "stored member revision is not a positive int; refusing "
                "to trust possibly tampered data")
        return value

    # -- transaction helpers --------------------------------------------------

    def _write_tx(self, core):
        if self.db.in_transaction:
            raise ValueError(
                "host identity writes own their transaction; the "
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

    def _fetch_installation(self):
        row = self.db.execute(
            "SELECT " + _INSTALLATION_COLUMNS
            + " FROM host_identity_installation WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        (_singleton, installation_id, company_id, owner_principal_id,
         owner_role, bound_at) = row
        return {
            "installation_id": self._recheck_id("installation_id",
                                                installation_id),
            "company_id": self._recheck_id("company_id", company_id),
            "owner_principal_id": self._recheck_id("owner_principal_id",
                                                   owner_principal_id),
            "owner_role": self._require_label("owner_role", owner_role),
            "bound_at": _load_time(bound_at),
        }

    def _fetch_member(self, principal_id):
        row = self.db.execute(
            "SELECT " + _MEMBER_COLUMNS
            + " FROM host_identity_members WHERE principal_id = ?",
            (principal_id,)).fetchone()
        if row is None:
            return None
        (principal, installation_id, company_id, role, status, revision,
         created_at, revoked_at) = row
        if revoked_at is not None:
            _load_time(revoked_at)
        _load_time(created_at)
        return {
            "principal_id": self._recheck_id("principal_id", principal),
            "installation_id": self._recheck_id("installation_id",
                                                installation_id),
            "company_id": self._recheck_id("company_id", company_id),
            "role": self._require_label("role", role),
            "status": self._recheck_status("member status", status,
                                           _MEMBER_STATUSES),
            "revision": self._recheck_revision(revision),
        }

    # -- controlled installation initialization --------------------------------

    def initialize_installation(self, *, installation_id, company_id,
                                owner_principal_id, owner_role, now):
        """Bind the local owner and company to this installation, once.

        Host-managed controlled-installation call; there is no HTTP
        route for it. The first call atomically records the singleton
        installation binding and the active owner membership. Every
        later call raises InstallationAlreadyBound; the singleton
        primary key enforces this even under concurrent callers.
        """
        installation_id = self._require_id("installation_id",
                                           installation_id)
        company_id = self._require_id("company_id", company_id)
        owner_principal_id = self._require_id("owner_principal_id",
                                              owner_principal_id)
        owner_role = self._require_label("owner_role", owner_role)
        now = self._require_now(now)

        def core():
            try:
                self.db.execute(
                    "INSERT INTO host_identity_installation "
                    "(singleton, installation_id, company_id, "
                    "owner_principal_id, owner_role, bound_at) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (installation_id, company_id, owner_principal_id,
                     owner_role, _store_time(now)))
                self.db.execute(
                    "INSERT INTO host_identity_members "
                    "(principal_id, installation_id, company_id, role, "
                    "status, revision, created_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, 'active', 1, ?, NULL)",
                    (owner_principal_id, installation_id, company_id,
                     owner_role, _store_time(now)))
            except sqlite3.IntegrityError as exc:
                raise InstallationAlreadyBound(
                    "this installation is already bound; the one-time "
                    "owner/company binding cannot be replaced") from exc
            return {
                "installation_id": installation_id,
                "company_id": company_id,
                "owner_principal_id": owner_principal_id,
                "owner_role": owner_role,
                "bound_at": now,
            }
        return self._write_tx(core)

    def get_installation(self):
        """Return the bound installation record, or None when unbound."""
        return self._read(self._fetch_installation)

    # -- pairing credentials ----------------------------------------------------

    def issue_pairing_credential(self, *, now, lifetime_seconds):
        """Issue a high-entropy, time-limited, one-time pairing credential.

        Host-managed call. The credential pairs as the already-bound
        owner only; it carries no client-chosen actor, company or role.
        Only its SHA-256 digest is stored. The raw secret exists only
        on the returned PairingTicket. Each credential is independently
        valid until its explicit expiry or its one-time consumption.
        """
        now = self._require_now(now)
        lifetime_seconds = self._require_lifetime("lifetime_seconds",
                                                  lifetime_seconds)

        def core():
            installation = self._fetch_installation()
            if installation is None:
                raise InstallationRequired(
                    "no pairing credential can be issued before the "
                    "controlled installation binding exists")
            credential_id = "hpc_" + secrets.token_hex(8)
            secret = secrets.token_urlsafe(32)
            expires_at = now + timedelta(seconds=lifetime_seconds)
            self.db.execute(
                "INSERT INTO host_identity_pairing_credentials "
                "(credential_id, secret_sha256, installation_id, "
                "company_id, principal_id, issued_at, expires_at, "
                "status, consumed_at, consumed_session_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)",
                (credential_id, _pairing_digest(secret),
                 installation["installation_id"],
                 installation["company_id"],
                 installation["owner_principal_id"],
                 _store_time(now), _store_time(expires_at)))
            return PairingTicket(credential_id=credential_id,
                                 expires_at=expires_at, secret=secret)
        return self._write_tx(core)

    def pair(self, secret, *, now, session_lifetime_seconds):
        """Consume a pairing credential and atomically issue a session.

        Takes no actor, company or role input: identity is resolved
        entirely from the server-side binding recorded at issuance. On
        success the credential is consumed and the session inserted in
        one transaction; concurrent pairing with the same credential
        issues at most one session. On any failure nothing is consumed
        and no session remains. Only the session secret's digest is
        stored; the raw session secret exists only on the returned
        SessionGrant.
        """
        secret = self._require_secret("secret", secret)
        now = self._require_now(now)
        session_lifetime_seconds = self._require_lifetime(
            "session_lifetime_seconds", session_lifetime_seconds)

        def core():
            row = self.db.execute(
                "SELECT " + _CREDENTIAL_COLUMNS
                + " FROM host_identity_pairing_credentials "
                "WHERE secret_sha256 = ?",
                (_pairing_digest(secret),)).fetchone()
            if row is None:
                raise PairingCredentialInvalid(
                    "pairing credential is not recognized")
            (credential_id, stored_digest, installation_id, company_id,
             principal_id, issued_at, expires_at, status, _consumed_at,
             _consumed_ref) = row
            self._recheck_id("credential_id", credential_id)
            self._recheck_digest(stored_digest)
            self._recheck_id("installation_id", installation_id)
            self._recheck_id("company_id", company_id)
            self._recheck_id("principal_id", principal_id)
            _load_time(issued_at)
            expires_at = _load_time(expires_at)
            self._recheck_status("credential status", status,
                                 _CREDENTIAL_STATUSES)
            if status != "pending":
                raise PairingCredentialConsumed(
                    "pairing credential has already been consumed")
            if now >= expires_at:
                raise PairingCredentialExpired(
                    "pairing credential has expired")
            installation = self._fetch_installation()
            if installation is None \
                    or installation["installation_id"] != installation_id \
                    or installation["company_id"] != company_id:
                raise PairingFailed(
                    "pairing credential does not match the currently "
                    "bound installation")
            member = self._fetch_member(principal_id)
            if member is None or member["status"] != "active":
                raise MembershipRevoked(
                    "the membership bound to this pairing credential "
                    "is not active")
            session_secret = secrets.token_urlsafe(32)
            session_ref = "hss_" + secrets.token_hex(8)
            session_digest = _session_digest(session_secret)
            session_expires_at = now + timedelta(
                seconds=session_lifetime_seconds)
            # The guarded UPDATE is the one-time consumption point: only
            # a row still pending can be claimed, so a concurrent pairing
            # of the same credential cannot claim it again.
            cursor = self.db.execute(
                "UPDATE host_identity_pairing_credentials "
                "SET status = 'consumed', consumed_at = ?, "
                "consumed_session_ref = ? "
                "WHERE credential_id = ? AND status = 'pending'",
                (_store_time(now), session_ref, credential_id))
            if cursor.rowcount != 1:
                raise PairingCredentialConsumed(
                    "pairing credential has already been consumed")
            self.db.execute(
                "INSERT INTO host_identity_sessions "
                "(session_ref, secret_sha256, installation_id, "
                "company_id, principal_id, auth_method, issued_at, "
                "expires_at, status, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)",
                (session_ref, session_digest, installation_id,
                 company_id, principal_id, _AUTH_METHOD,
                 _store_time(now), _store_time(session_expires_at)))
            # Build the returned context through the same re-read path
            # used by resolve_session, inside this transaction.
            context = self._resolve_core(session_digest, now)
            return SessionGrant(session_ref=session_ref,
                                expires_at=session_expires_at,
                                context=context, secret=session_secret)
        return self._write_tx(core)

    # -- session resolution ------------------------------------------------------

    def resolve_session(self, secret, *, now):
        """Resolve a session credential to a trusted OwnerContext.

        F-007-API-001 for the local-owner mode. Re-reads the current
        session, installation and membership state on every call; there
        is no cache, so a revocation takes effect on the very next
        resolution. Unknown secrets, expired or revoked sessions,
        installation drift and revoked memberships all fail closed. The
        returned context carries no secret and no authority methods.
        """
        secret = self._require_secret("secret", secret)
        now = self._require_now(now)
        return self._read(
            lambda: self._resolve_core(_session_digest(secret), now))

    def _resolve_core(self, session_digest, now):
        row = self.db.execute(
            "SELECT " + _SESSION_COLUMNS
            + " FROM host_identity_sessions WHERE secret_sha256 = ?",
            (session_digest,)).fetchone()
        if row is None:
            raise Unauthenticated("session credential is not recognized")
        (session_ref, stored_digest, installation_id, company_id,
         principal_id, auth_method, issued_at, expires_at, status,
         revoked_at) = row
        self._recheck_id("session_ref", session_ref)
        self._recheck_digest(stored_digest)
        self._recheck_id("installation_id", installation_id)
        self._recheck_id("company_id", company_id)
        self._recheck_id("principal_id", principal_id)
        self._require_label("auth_method", auth_method)
        issued_at = _load_time(issued_at)
        expires_at = _load_time(expires_at)
        if revoked_at is not None:
            _load_time(revoked_at)
        self._recheck_status("session status", status, _SESSION_STATUSES)
        if status != "active":
            raise SessionRevoked("session has been revoked")
        if now >= expires_at:
            raise SessionExpired("session has expired")
        installation = self._fetch_installation()
        if installation is None \
                or installation["installation_id"] != installation_id \
                or installation["company_id"] != company_id:
            raise InstallationMismatch(
                "session does not match the currently bound "
                "installation")
        member = self._fetch_member(principal_id)
        if member is None or member["status"] != "active":
            raise MembershipRevoked(
                "the principal's membership is not active")
        if member["installation_id"] != installation_id \
                or member["company_id"] != company_id:
            raise InstallationMismatch(
                "membership does not match the currently bound "
                "installation")
        return OwnerContext(
            installation_id=installation_id,
            company_id=company_id,
            principal_id=principal_id,
            role=member["role"],
            member_revision=member["revision"],
            session_ref=session_ref,
            auth_method=auth_method,
            session_issued_at=issued_at,
            session_expires_at=expires_at,
            resolved_at=now,
        )

    # -- revocation ---------------------------------------------------------------

    def revoke_session(self, session_ref, *, now):
        """Revoke one session by its public reference; idempotent.

        Returns True when this call transitioned the session to
        revoked, False when it was already revoked. Subsequent
        resolutions of that session fail closed immediately.
        """
        session_ref = self._require_id("session_ref", session_ref)
        now = self._require_now(now)

        def core():
            row = self.db.execute(
                "SELECT status FROM host_identity_sessions "
                "WHERE session_ref = ?", (session_ref,)).fetchone()
            if row is None:
                raise UnknownSession("unknown session reference")
            self._recheck_status("session status", row[0],
                                 _SESSION_STATUSES)
            if row[0] == "revoked":
                return False
            self.db.execute(
                "UPDATE host_identity_sessions "
                "SET status = 'revoked', revoked_at = ? "
                "WHERE session_ref = ? AND status = 'active'",
                (_store_time(now), session_ref))
            return True
        return self._write_tx(core)

    def revoke_member(self, principal_id, *, now):
        """Revoke one membership; idempotent.

        Returns True when this call transitioned the member to revoked,
        False when it was already revoked. Existing sessions are left
        untouched in storage, but every subsequent resolution re-reads
        the membership state and fails closed immediately.
        """
        principal_id = self._require_id("principal_id", principal_id)
        now = self._require_now(now)

        def core():
            row = self.db.execute(
                "SELECT status FROM host_identity_members "
                "WHERE principal_id = ?", (principal_id,)).fetchone()
            if row is None:
                raise UnknownMember("unknown member principal")
            self._recheck_status("member status", row[0],
                                 _MEMBER_STATUSES)
            if row[0] == "revoked":
                return False
            self.db.execute(
                "UPDATE host_identity_members "
                "SET status = 'revoked', revoked_at = ?, "
                "revision = revision + 1 "
                "WHERE principal_id = ? AND status = 'active'",
                (_store_time(now), principal_id))
            return True
        return self._write_tx(core)
