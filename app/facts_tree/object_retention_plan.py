"""Read-only retention planner for the external mapping-object cabinet (G1).

F-001 design 98 §4: external-object GC must protect the accepted history,
live W state, in-flight/preparing operations and in-flight backup roots;
reflogs, dangling objects and default grace periods do not count.  This
module is the read-only foundation a later deletion executor (a separate
slice) will consume; it never deletes, moves, unlinks, writes, creates
network connections or runs subprocesses.

Inputs are injected: a constructed
:class:`app.storage.versioned_mapping_objects.VersionedMappingObjectStore`
and, optionally, a scope-bound
:class:`app.facts_tree.mapping_version_index.MappingVersionLedger`, plus
caller-supplied in-flight and explicit protection digests.  Physical
objects come exclusively from ``store.inventory()`` (which re-reads and
SHA-256 revalidates every object it enumerates); the planner never walks
paths itself.  The protection set is the union of every digest registered
in the ledger across all logical sources and all versions (old versions
and gap-adjacent versions included), the caller's in-flight digests, and
explicit extras.

Conservative rules — when in doubt, keep:

- No ledger (``None``) or an unreadable/corrupt/wrong-root ledger means
  "cannot prove unreferenced": every physical object is classified
  ``protected`` with reason ``ledger_unavailable`` and zero reclaimable
  candidates are emitted, with a warning.  A *foreign-scope* ledger is
  different: it is refused with ``MappingVersionScopeError`` before any
  classification, never silently treated as empty.
- A ledger digest is protection even when the cabinet bytes are
  currently missing or corrupt (a registered gap): old versions remain
  reachable through backup closures by their own digest, and the ledger
  is the authority on acceptance history.
- Inventory enumeration is fail-closed in the cabinet itself: one
  corrupted or unsafe occupant aborts the whole listing.  The planner
  turns that into a ``corrupt`` entry plus an ``inventory_incomplete``
  warning and emits **no** protected/reclaimable classification at all
  for that run — a single bad object poisons the plan, so nothing can
  ride a partial enumeration into the reclaimable set.  The digest in
  the exception text is best-effort audit annotation only; when it
  cannot be extracted it stays ``None`` and the run still yields zero
  reclaimable candidates.
- Inventory is not an atomic snapshot (see ``VersionedMappingObjectStore
  .inventory``): an object written after the relevant listdir is simply
  absent, matching the cabinet's documented semantics.  A later
  deletion slice must re-verify behind a maintenance barrier; this
  planner only labels candidates, it deletes nothing.

The returned :class:`RetentionPlan` is a frozen, digest-sorted,
deterministic record carrying scope, per-set counts and byte totals,
warnings and a ``plan_digest`` computed over its canonical body with
:func:`app.facts_tree.backup_manifest.compute_core_digest`.  The planner
reads no wall clock; ``planned_at`` is caller-injected or omitted, so the
same inputs replay to the same digest.

Import surface is restricted to read-only primitives; ``sqlite3`` is
imported solely to classify ledger read failures in ``except`` clauses —
the module opens no database connection and issues no SQL.  An AST guard
test pins both rules.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .backup_manifest import compute_core_digest
from .mapping_version_index import (
    LedgerIntegrityError,
    MappingVersionLedger,
    MappingVersionScopeError,
    UnsafeLedgerRootError,
)
from ..storage.versioned_mapping_objects import (
    InvalidDigestError,
    ObjectMissingError,
    ObjectMismatchError,
    UnexpectedEntryError,
    UnsafeObjectPathError,
    VersionedMappingObjectStore,
)

SCHEMA = "sam.f001.object_retention_plan"
SCHEMA_VERSION = 1
SCOPE_KIND_COMPANY = "company"

# Protection-hit reason codes.  Multiple hits are all reported, sorted.
REASON_IN_LEDGER = "in_ledger"
REASON_IN_FLIGHT = "in_flight"
REASON_EXPLICIT = "explicit"
REASON_UNREFERENCED = "unreferenced"
# The ledger could not prove the object unreferenced, so it is kept.
REASON_LEDGER_UNAVAILABLE = "ledger_unavailable"

# Corrupt-entry reason codes (a corrupted/unsafe object is never itself
# protected-as-referenced nor reclaimable; it needs manual reconciliation).
CORRUPT_REASON_SHA256_MISMATCH = "sha256_mismatch"
CORRUPT_REASON_UNSAFE_PATH = "unsafe_path"

# Stable, path-free audit detail strings (absolute paths must stay out of
# the plan body so plan_digest stays location-independent).
CORRUPT_DETAIL_SHA256_MISMATCH = (
    "stored bytes failed read-time sha256 revalidation")
CORRUPT_DETAIL_UNSAFE_PATH = (
    "symlink, non-regular occupant, or unsafe cabinet path")

# Warning codes.
WARNING_LEDGER_UNAVAILABLE = "ledger_unavailable"
WARNING_INVENTORY_INCOMPLETE = "inventory_incomplete"
WARNING_CORRUPT_OBJECT_PREFIX = "inventory_corrupt_object:"
WARNING_UNSAFE_PREFIX = "inventory_unsafe:"

# Ledger-read failure kinds suffixing ``ledger_unavailable:``.
LEDGER_FAILURE_INTEGRITY = "integrity"
LEDGER_FAILURE_UNSAFE_ROOT = "unsafe_root"
LEDGER_FAILURE_DATABASE = "database"
LEDGER_FAILURE_OSERROR = "oserror"

# Inventory-abort kinds suffixing ``inventory_unsafe:``.
INVENTORY_UNSAFE_PATH = "unsafe_path"
INVENTORY_UNEXPECTED_ENTRY = "unexpected_entry"
INVENTORY_OBJECT_MISSING = "object_missing"
INVENTORY_OSERROR = "oserror"

# Suffix used when a corrupt object's digest cannot be recovered from the
# exception text; it is an explicit "unknown" marker, never a guess.
UNKNOWN_DIGEST = "unknown"

_DIGEST_HEX_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DIGEST_IN_TEXT_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")

# Ledger read failures that turn the ledger into "unavailable" (fail
# closed: all physical objects protected, zero reclaimable).  Scope
# refusal is deliberately absent — it propagates to the caller.
_LEDGER_FAILURE_KINDS = (
    (LedgerIntegrityError, LEDGER_FAILURE_INTEGRITY),
    (UnsafeLedgerRootError, LEDGER_FAILURE_UNSAFE_ROOT),
    (sqlite3.DatabaseError, LEDGER_FAILURE_DATABASE),
    (OSError, LEDGER_FAILURE_OSERROR),
)
_LEDGER_FAILURE_TYPES = tuple(
    exc_type for exc_type, _label in _LEDGER_FAILURE_KINDS)


@dataclass(frozen=True)
class RetentionItem:
    """One inventory-verified object with its protection-hit reasons."""

    digest: str
    size_bytes: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CorruptObject:
    """An enumeration abort tied to one digest-best-effort object.

    ``digest`` is ``None`` when the cabinet exception text carried no
    64-hex digest; ``size_bytes`` is always ``None`` because a corrupt
    object's bytes are never trusted enough to measure for the plan.
    """

    digest: str | None
    size_bytes: int | None
    reasons: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class RetentionPlan:
    """Deterministic read-only classification of one cabinet/scope cut."""

    installation_id: str
    company_id: str
    protected: tuple[RetentionItem, ...]
    reclaimable_candidates: tuple[RetentionItem, ...]
    corrupt: tuple[CorruptObject, ...]
    warnings: tuple[str, ...]
    ledger_digest: str | None
    planned_at: str | None
    plan_digest: str = field(init=False)

    def __post_init__(self) -> None:
        # Recomputed on every construction so a plan can never disagree
        # with its own canonical body.
        object.__setattr__(
            self, "plan_digest",
            compute_core_digest(self.canonical_payload()))

    @property
    def protected_count(self) -> int:
        return len(self.protected)

    @property
    def reclaimable_count(self) -> int:
        return len(self.reclaimable_candidates)

    @property
    def corrupt_count(self) -> int:
        return len(self.corrupt)

    @property
    def protected_bytes(self) -> int:
        return sum(item.size_bytes for item in self.protected)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.size_bytes for item in self.reclaimable_candidates)

    @property
    def total_bytes(self) -> int:
        # Corrupt objects contribute no trusted byte count.
        return self.protected_bytes + self.reclaimable_bytes

    @property
    def all_protected(self) -> bool:
        """True in every conservative case, starting with zero reclaimable."""
        return not self.reclaimable_candidates

    def canonical_payload(self) -> dict:
        """Stable digest body: sorted lists, no paths, no wall clock beyond
        the caller-injected ``planned_at``."""
        return {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "scope": {
                "kind": SCOPE_KIND_COMPANY,
                "installation_id": self.installation_id,
                "company_id": self.company_id,
            },
            "protected": [_item_payload(item) for item in self.protected],
            "reclaimable_candidates": [
                _item_payload(item) for item in self.reclaimable_candidates],
            "corrupt": [
                {
                    "digest": entry.digest,
                    "size_bytes": entry.size_bytes,
                    "reasons": list(entry.reasons),
                    "detail": entry.detail,
                }
                for entry in self.corrupt
            ],
            "warnings": list(self.warnings),
            "ledger_digest": self.ledger_digest,
            "planned_at": self.planned_at,
        }


def _item_payload(item: RetentionItem) -> dict:
    return {
        "digest": item.digest,
        "size_bytes": item.size_bytes,
        "reasons": list(item.reasons),
    }


def _require_scope_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty str")
    return value


def _digest_set(values: object, label: str) -> set[str]:
    """Validate a caller protection set as an iterable of 64-hex digests.

    A bare ``str``/``bytes`` is rejected outright so it can never be
    iterated character by character into nonsense "digests".
    """
    if isinstance(values, (str, bytes)):
        raise InvalidDigestError(
            f"{label} must be an iterable of lowercase 64-hex sha256 digests")
    try:
        iterator = iter(values)
    except TypeError:
        raise InvalidDigestError(
            f"{label} must be an iterable of lowercase 64-hex sha256 "
            "digests") from None
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _DIGEST_HEX_RE.fullmatch(value):
            raise InvalidDigestError(
                f"{label} must contain only lowercase 64-hex sha256 "
                f"digests; got {value!r}")
        result.add(value)
    return result


def _extract_digest(text: object) -> str | None:
    """Best-effort pull of one 64-hex digest out of an exception message.

    Audit annotation only: a message without an exact 64-hex token yields
    ``None``; the caller must never guess, truncate or re-enumerate.
    """
    if not isinstance(text, str):
        return None
    match = _DIGEST_IN_TEXT_RE.search(text)
    return match.group(0) if match is not None else None


def _ledger_protection(
    ledger: MappingVersionLedger | None,
    installation_id: str,
    company_id: str,
    warnings: list[str],
) -> tuple[set[str], str | None, bool]:
    """Return ``(registered digests, ledger digest, unavailable flag)``.

    Sources come from the public ``ledger_shape(verify=True)`` read and
    rows from public ``list_versions`` calls — never a private SQL query.
    Any integrity/root/database/IO failure downgrades to "unavailable":
    the partial set is discarded, no digest is trusted as complete, and
    every physical object will be kept by the caller.
    """
    if ledger is None:
        warnings.append(WARNING_LEDGER_UNAVAILABLE)
        return set(), None, True

    if (ledger.installation_id != installation_id
            or ledger.company_id != company_id):
        raise MappingVersionScopeError(
            "the supplied ledger is bound to installation "
            f"{ledger.installation_id!r}/company {ledger.company_id!r}; "
            f"planning as {installation_id!r}/{company_id!r} is refused")

    try:
        shape = ledger.ledger_shape(verify=True)
        registered: set[str] = set()
        for source in shape["sources"]:
            records = ledger.list_versions(
                source["logical_source_id"],
                installation_id=installation_id,
                company_id=company_id)
            registered.update(record.digest for record in records)
        ledger_digest_value = ledger.ledger_digest()
    except MappingVersionScopeError:
        # A foreign scope is a refusal, never an "empty/unavailable"
        # ledger; propagate it unchanged.
        raise
    except _LEDGER_FAILURE_TYPES as exc:
        kind = next(
            label for exc_type, label in _LEDGER_FAILURE_KINDS
            if isinstance(exc, exc_type))
        warnings.append(f"{WARNING_LEDGER_UNAVAILABLE}:{kind}")
        return set(), None, True
    return registered, ledger_digest_value, False


def plan_mapping_retention(
    *,
    store: VersionedMappingObjectStore,
    ledger: MappingVersionLedger | None = None,
    in_flight_digests: object = (),
    extra_protected: object = (),
    installation_id: str,
    company_id: str,
    planned_at: str | None = None,
) -> RetentionPlan:
    """Classify every cabinet object as protected / reclaimable / corrupt.

    Read-only: the store is only inventoried and the ledger is only read.
    ``in_flight_digests`` protects objects referenced by preparing/in-flight
    operations and backup roots; ``extra_protected`` is an explicit reserve
    (e.g. future self-contained package references).  Both are shape-checked
    as 64-hex digests; malformed input raises ``InvalidDigestError`` instead
    of being silently dropped.  ``planned_at`` is an optional caller-owned
    timestamp string; the planner never reads the clock itself.
    """
    if not isinstance(store, VersionedMappingObjectStore):
        raise TypeError(
            "store must be a constructed VersionedMappingObjectStore")
    if ledger is not None and not isinstance(ledger, MappingVersionLedger):
        raise TypeError(
            "ledger must be a constructed scope-bound MappingVersionLedger "
            "or None")
    installation = _require_scope_id(installation_id, "installation_id")
    company = _require_scope_id(company_id, "company_id")
    if planned_at is not None and (not isinstance(planned_at, str)
                                   or not planned_at):
        raise ValueError("planned_at must be a non-empty str or None")
    in_flight = _digest_set(in_flight_digests, "in_flight_digests")
    explicit = _digest_set(extra_protected, "extra_protected")

    warnings: list[str] = []
    registered, ledger_digest_value, ledger_unavailable = (
        _ledger_protection(ledger, installation, company, warnings))

    protected: list[RetentionItem] = []
    reclaimable: list[RetentionItem] = []
    corrupt: list[CorruptObject] = []
    try:
        entries = store.inventory()
    except ObjectMismatchError as exc:
        # One bad digest aborts the whole cabinet listing; classify just
        # this audit marker and keep every other object by emitting no
        # classifications this run.
        digest = _extract_digest(str(exc))
        corrupt.append(CorruptObject(
            digest=digest,
            size_bytes=None,
            reasons=(CORRUPT_REASON_SHA256_MISMATCH,),
            detail=CORRUPT_DETAIL_SHA256_MISMATCH))
        warnings.append(WARNING_INVENTORY_INCOMPLETE)
        warnings.append(
            WARNING_CORRUPT_OBJECT_PREFIX
            + (digest if digest is not None else UNKNOWN_DIGEST))
        entries = None
    except UnsafeObjectPathError as exc:
        digest = _extract_digest(str(exc))
        corrupt.append(CorruptObject(
            digest=digest,
            size_bytes=None,
            reasons=(CORRUPT_REASON_UNSAFE_PATH,),
            detail=CORRUPT_DETAIL_UNSAFE_PATH))
        warnings.append(WARNING_INVENTORY_INCOMPLETE)
        warnings.append(WARNING_UNSAFE_PREFIX + INVENTORY_UNSAFE_PATH)
        entries = None
    except UnexpectedEntryError:
        # Wrong shard/object names or an in-flight temporary visible at
        # listdir time: no content-addressed object to name, nothing may
        # be classified from a tainted listing.
        warnings.append(WARNING_INVENTORY_INCOMPLETE)
        warnings.append(WARNING_UNSAFE_PREFIX + INVENTORY_UNEXPECTED_ENTRY)
        entries = None
    except ObjectMissingError:
        # An enumerated entry vanished before its verified read.
        warnings.append(WARNING_INVENTORY_INCOMPLETE)
        warnings.append(WARNING_UNSAFE_PREFIX + INVENTORY_OBJECT_MISSING)
        entries = None
    except OSError:
        # Any other enumeration/Read uncertainty is fail-closed as well.
        warnings.append(WARNING_INVENTORY_INCOMPLETE)
        warnings.append(WARNING_UNSAFE_PREFIX + INVENTORY_OSERROR)
        entries = None

    if entries is not None:
        # inventory() already returns digest-sorted entries; keep the
        # explicit ordering contract regardless.
        for entry in sorted(entries, key=lambda item: item.digest):
            hits = []
            if ledger_unavailable:
                hits.append(REASON_LEDGER_UNAVAILABLE)
            if entry.digest in registered:
                hits.append(REASON_IN_LEDGER)
            if entry.digest in in_flight:
                hits.append(REASON_IN_FLIGHT)
            if entry.digest in explicit:
                hits.append(REASON_EXPLICIT)
            if hits:
                protected.append(RetentionItem(
                    digest=entry.digest,
                    size_bytes=entry.size_bytes,
                    reasons=tuple(sorted(set(hits)))))
            else:
                reclaimable.append(RetentionItem(
                    digest=entry.digest,
                    size_bytes=entry.size_bytes,
                    reasons=(REASON_UNREFERENCED,)))

    return RetentionPlan(
        installation_id=installation,
        company_id=company,
        protected=tuple(protected),
        reclaimable_candidates=tuple(reclaimable),
        corrupt=tuple(corrupt),
        warnings=tuple(sorted(set(warnings))),
        ledger_digest=ledger_digest_value,
        planned_at=planned_at,
    )
