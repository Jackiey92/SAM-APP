"""Live-path online registration seam for F-001 R3.

The R2 :class:`MappingVersionLedger` is deliberately fail-closed: a
missing object, a scope mismatch, any storage fault raises and writes
nothing.  The employee-parsing main link
(``StagingStore.save_manifest``/``update_payload`` and
``SourceMapService.rebuild_map``/``_edit`` via
``persist_latest_mapping``) must obey the *opposite* policy fixed by the
R3 freeze (appendix R3 §6): once the immutable mapping bytes are safely
in the byte cabinet, no ledger fault may ever fail, roll back or leave
a half-finished parse — the latest projection and the sidecar JSON
files are written as usual, the cabinet object stays
"in-cabinet, not-yet-indexed", and the R2 offline backfiller repairs it
later.

This module is the single seam that keeps the two policies apart.  It
wraps one already-constructed, scope-bound ledger and is the *only*
object the parsing path holds: the storage/harness layer never imports
the ledger (the R2 V7 import guard still holds for it) and calls this
seam duck-typed.  Every method on this seam degrades into a structured
:class:`OnlineRegistrationOutcome` — it never raises into the parsing
path, never retries in a loop and never waits longer than the ledger's
own single bounded lock attempt.  The A1 backup assembly does not use
this seam: it opens the ledger directly and stays fail-closed.

The seam also binds the honest H association (O6): the injected
``h_provider`` is called once per registration and must return the
formal history H's *current* commit oid (40/64 lowercase hex).  When it
is absent, returns a non-oid or raises, the registration proceeds with
``h_commit_oid=None`` and the outcome flags ``h_status`` — a stale or
fabricated oid is never cached and presented as current H.

Diagnostics carry reason codes plus stable coordinates only
(``parse_staging#id=<row>``); exception *messages* are deliberately
excluded because SQLite errors embed absolute database paths, so the
bounded ``detail`` keeps the exception class name and the SQLite error
code name at most.  No network or model is touched.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .mapping_version_index import (
    GhostObjectError,
    LedgerIntegrityError,
    MappingVersionIndexError,
    MappingVersionLedger,
    MappingVersionScopeError,
    SOURCE_KIND_ONLINE,
    UnsafeLedgerRootError,
)

# Outcome statuses.
STATUS_DEGRADED = "degraded"

# Degrade reason codes. One fixed vocabulary; the parsing path never
# sees a free-form string from the underlying exception.
DEGRADED_SCOPE_MISMATCH = "degraded_scope_mismatch"
DEGRADED_GHOST_OBJECT = "degraded_ghost_object"
DEGRADED_LEDGER_BUSY = "degraded_ledger_busy"
DEGRADED_LEDGER_READONLY = "degraded_ledger_readonly"
DEGRADED_LEDGER_FULL = "degraded_ledger_full"
DEGRADED_LEDGER_IO = "degraded_ledger_io"
DEGRADED_LEDGER_INTEGRITY = "degraded_ledger_integrity"
DEGRADED_LEDGER_ROOT = "degraded_ledger_root"
DEGRADED_LEDGER_INVALID = "degraded_ledger_invalid"
DEGRADED_LEDGER_FAULT = "degraded_ledger_fault"

# H association statuses (O6).
H_OK = "ok"
H_UNAVAILABLE = "unavailable"
H_NOT_CONFIGURED = "not_configured"

# sha1 (40) or sha256 (64) git commit oids, structural shape only.
_COMMIT_OID_RE = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")
_MAX_ID_CHARS = 256

# Bounded memory of structured degrade events per seam instance; this is
# observability state, never a retry queue (the offline backfiller is the
# repair path).
_MAX_DIAGNOSTICS = 256

_SQLITE_CODE_REASONS = {
    5: DEGRADED_LEDGER_BUSY,       # SQLITE_BUSY
    6: DEGRADED_LEDGER_BUSY,       # SQLITE_LOCKED
    8: DEGRADED_LEDGER_READONLY,   # SQLITE_READONLY
    11: DEGRADED_LEDGER_INTEGRITY,  # SQLITE_CORRUPT
    13: DEGRADED_LEDGER_FULL,      # SQLITE_FULL
    18: DEGRADED_LEDGER_FULL,      # SQLITE_TOOBIG
    10: DEGRADED_LEDGER_IO,        # SQLITE_IOERR
    14: DEGRADED_LEDGER_IO,        # SQLITE_CANTOPEN
    17: DEGRADED_LEDGER_INVALID,   # SQLITE_SCHEMA
    26: DEGRADED_LEDGER_INTEGRITY,  # SQLITE_NOTADB
}

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OnlineRegistrationOutcome:
    """Structured result of one online-registration seam call.

    ``status`` is ``registered``/``replayed`` on success or
    :data:`STATUS_DEGRADED` when the parsing main link had to continue
    without a ledger row.  A degraded outcome never carries a
    ``version_no``; ``reason`` classifies it, ``detail`` stays a bounded
    class/code label (never an exception message, never a path), and the
    cabinet digest/coordinate are always recorded so the R2 offline
    backfiller can repair the same coordinate.
    """

    status: str
    reason: str | None
    installation_id: str
    company_id: str
    logical_source_id: str
    digest: str
    size_bytes: int
    version_no: int | None
    source_coordinate: str | None
    h_commit_oid: str | None
    h_status: str
    detail: str | None = None


class OnlineMappingRegistrar:
    """Injectable degrade-never-block seam for the parsing main link.

    Construction only wires an already-open, scope-bound
    :class:`MappingVersionLedger`, the declared installation/company
    scope, an optional zero-argument ``h_provider`` returning the formal
    history H's current commit oid, and an optional diagnostic sink.
    Nothing here creates global state, opens production paths or
    performs I/O; production wiring/enablement is a later slice.  The
    seam is safe to share between parsing threads (the ledger itself
    serializes numbering).
    """

    def __init__(self, *,
                 installation_id: str,
                 company_id: str,
                 ledger: MappingVersionLedger,
                 h_provider: Callable[[], str | None] | None = None,
                 on_diagnostic: Callable[[OnlineRegistrationOutcome],
                                         None] | None = None) -> None:
        if not isinstance(ledger, MappingVersionLedger):
            # Construction failure must be a loud host-wiring error, not
            # a silent degrade: without a real ledger there is no seam.
            raise TypeError(
                "ledger must be a real MappingVersionLedger")
        for value, label in ((installation_id, "installation_id"),
                            (company_id, "company_id")):
            if not isinstance(value, str) or not value \
                    or len(value) > _MAX_ID_CHARS or "\x00" in value:
                raise ValueError(
                    f"{label} must be a non-empty str of at most "
                    f"{_MAX_ID_CHARS} chars without NUL")
        if h_provider is not None and not callable(h_provider):
            raise TypeError("h_provider must be callable or None")
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise TypeError("on_diagnostic must be callable or None")
        self.installation_id = installation_id
        self.company_id = company_id
        self._ledger = ledger
        self._h_provider = h_provider
        self._on_diagnostic = on_diagnostic
        self._lock = threading.Lock()
        self._diagnostics: deque[OnlineRegistrationOutcome] = deque(
            maxlen=_MAX_DIAGNOSTICS)
        self._last_outcome: OnlineRegistrationOutcome | None = None

    # -- the single seam entry the parsing path is allowed to call -----------

    def register(self, *,
                 logical_source_id: str,
                 digest: str,
                 size_bytes: int,
                 source_coordinate: str | None = None
                 ) -> OnlineRegistrationOutcome:
        """Register one cabinet-backed mapping version; never raises.

        Success returns the ledger's issued/replayed outcome.  Every
        fault — scope mismatch, ghost cabinet bytes, an unwritable,
        busy, full or malformed ledger, or any unexpected exception —
        returns a degraded outcome with a stable reason code, records
        the structured diagnostic and continues.  There is no retry
        loop and no extra lock wait beyond the ledger's own bounded
        attempt.
        """
        try:
            return self._register(
                logical_source_id=logical_source_id, digest=digest,
                size_bytes=size_bytes, source_coordinate=source_coordinate)
        except Exception as exc:  # parsing-link safety net, see module doc
            return self._degrade(
                DEGRADED_LEDGER_FAULT, logical_source_id, digest,
                size_bytes, source_coordinate,
                detail=type(exc).__qualname__)

    # -- internals ------------------------------------------------------------

    def _register(self, *, logical_source_id, digest, size_bytes,
                  source_coordinate) -> OnlineRegistrationOutcome:
        # The declared online scope is checked against the bound ledger
        # before any work (even reading H): a foreign-scope seam must
        # never reach the ledger's own scope refusal (O11 double
        # enforcement).
        if (self.installation_id != self._ledger.installation_id
                or self.company_id != self._ledger.company_id):
            return self._degrade(
                DEGRADED_SCOPE_MISMATCH, logical_source_id, digest,
                size_bytes, source_coordinate,
                detail="declared scope does not match the bound ledger")

        h_oid, h_status = self._resolve_h()

        def degrade(reason: str, *, detail: str) -> OnlineRegistrationOutcome:
            return self._degrade(
                reason, logical_source_id, digest, size_bytes,
                source_coordinate, detail=detail,
                h_commit_oid=h_oid, h_status=h_status)

        try:
            result = self._ledger.register_version(
                logical_source_id=logical_source_id,
                digest=digest,
                size_bytes=size_bytes,
                installation_id=self.installation_id,
                company_id=self.company_id,
                source_kind=SOURCE_KIND_ONLINE,
                source_coordinate=source_coordinate,
                h_commit_oid=h_oid)
        except GhostObjectError as exc:
            # V3 refusal: the cabinet get-recheck failed after put, so no
            # row may name these bytes.
            return degrade(DEGRADED_GHOST_OBJECT, detail=exc.reason)
        except MappingVersionScopeError:
            return degrade(
                DEGRADED_SCOPE_MISMATCH,
                detail="ledger refused the declared scope")
        except UnsafeLedgerRootError:
            return degrade(
                DEGRADED_LEDGER_ROOT, detail="unsafe ledger root")
        except LedgerIntegrityError:
            return degrade(
                DEGRADED_LEDGER_INTEGRITY,
                detail="ledger integrity check failed")
        except MappingVersionIndexError:
            return degrade(
                DEGRADED_LEDGER_INVALID,
                detail="ledger refused the registration")
        except sqlite3.DatabaseError as exc:
            return degrade(self._sqlite_reason(exc),
                           detail=self._sqlite_detail(exc))
        except Exception as exc:  # arbitrary fault: disk-full wrappers etc.
            return degrade(DEGRADED_LEDGER_FAULT,
                           detail=type(exc).__qualname__)

        outcome = OnlineRegistrationOutcome(
            status=result.status,
            reason=None,
            installation_id=self.installation_id,
            company_id=self.company_id,
            logical_source_id=logical_source_id,
            digest=digest,
            size_bytes=size_bytes,
            version_no=result.version_no,
            source_coordinate=source_coordinate,
            h_commit_oid=h_oid,
            h_status=h_status)
        with self._lock:
            self._last_outcome = outcome
        return outcome

    def _resolve_h(self) -> tuple[str | None, str]:
        """Return (current-H oid, h_status); never fabricate, never raise."""
        if self._h_provider is None:
            return None, H_NOT_CONFIGURED
        try:
            oid = self._h_provider()
        except Exception:
            return None, H_UNAVAILABLE
        if isinstance(oid, str) and _COMMIT_OID_RE.fullmatch(oid):
            return oid, H_OK
        return None, H_UNAVAILABLE

    @staticmethod
    def _sqlite_reason(exc: sqlite3.DatabaseError) -> str:
        # Classify on the primary result code: Python exposes the extended
        # code (e.g. SQLITE_READONLY_DIRECTORY = 8|(2<<8) = 520), whose low
        # byte still carries the stable vocabulary the diagnostics report.
        code = getattr(exc, "sqlite_errorcode", None)
        reason = _SQLITE_CODE_REASONS.get((code or 0) & 0xFF)
        return reason or DEGRADED_LEDGER_IO

    @staticmethod
    def _sqlite_detail(exc: sqlite3.DatabaseError) -> str:
        # The symbolic code name carries no path; the exception message
        # does, and therefore never enters the diagnostic.
        name = getattr(exc, "sqlite_errorname", None)
        return f"sqlite3.{name}" if isinstance(name, str) and name \
            else type(exc).__qualname__

    def _degrade(self, reason: str, logical_source_id, digest, size_bytes,
                 source_coordinate, *, detail: str,
                 h_commit_oid: str | None = None,
                 h_status: str = H_UNAVAILABLE
                 ) -> OnlineRegistrationOutcome:
        outcome = OnlineRegistrationOutcome(
            status=STATUS_DEGRADED,
            reason=reason,
            installation_id=self.installation_id,
            company_id=self.company_id,
            logical_source_id=(logical_source_id
                               if isinstance(logical_source_id, str)
                               else "<invalid>"),
            digest=digest if isinstance(digest, str) else "<invalid>",
            size_bytes=size_bytes if isinstance(size_bytes, int)
            and not isinstance(size_bytes, bool) and size_bytes >= 0 else -1,
            version_no=None,
            source_coordinate=(source_coordinate
                               if isinstance(source_coordinate, str)
                               else None),
            h_commit_oid=h_commit_oid,
            h_status=h_status,
            detail=detail)
        with self._lock:
            self._diagnostics.append(outcome)
            self._last_outcome = outcome
        # Structured, secret-free, path-free fields only.
        _LOGGER.warning(
            "online mapping registration degraded: reason=%s source=%s "
            "coordinate=%s digest=%s detail=%s",
            reason, outcome.logical_source_id,
            source_coordinate if isinstance(source_coordinate, str) else "-",
            outcome.digest, detail)
        if self._on_diagnostic is not None:
            try:
                self._on_diagnostic(outcome)
            except Exception as exc:
                # A broken diagnostic sink must not reach the parsing
                # path either; the logging line above is the fallback.
                _LOGGER.warning(
                    "online registration diagnostic sink raised %s",
                    type(exc).__qualname__)
        return outcome

    # -- read-only observability surface --------------------------------------

    @property
    def diagnostics(self) -> tuple[OnlineRegistrationOutcome, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    @property
    def last_outcome(self) -> OnlineRegistrationOutcome | None:
        with self._lock:
            return self._last_outcome
