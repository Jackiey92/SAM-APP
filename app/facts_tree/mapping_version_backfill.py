"""Offline read-only backfill reconciler for F-001 R2 slice 2.

This is a one-shot / repeatably-runnable *offline* tool, never a service:
it reconciles historical "mapping version → byte" evidence from the
append-only ``parse_staging`` history into the slice-1
:class:`MappingVersionLedger`, without touching any live write path.

Evidence grading (read-only reconnaissance, 2026-11-02)
------------------------------------------------------

``parse_staging`` rows are insert-only in practice (``StagingStore.
save_manifest`` always INSERTs a new row per parse; no application code
DELETEs them; ``update_payload`` can merge metadata later), ordered by
``id``; only ``status='parsed'`` rows ever produced mapping bytes.  A row
never stores a digest of the persisted mapping bytes, so there is no
independent fingerprint column: the evidence has exactly two grades.

1. :data:`EVIDENCE_INLINE_BYTES` (strong): the row payload is a JSON
   object whose ``document.markdown`` is a string.  ``persist_latest_mapping``
   renders with no corrections and, on that exact condition, returns that
   string verbatim and puts ``markdown.encode("utf-8")`` into the cabinet.
   The historical row therefore carries the exact mapping bytes inline;
   the cabinet's content address is their SHA-256.  Certainty comes from
   the bytes themselves, not from a guessed association.
2. :data:`EVIDENCE_DETERMINISTIC_RENDER` (replay): no inline markdown, so
   the shared projection is ``render_mapping(manifest)`` — the same pure
   call the write path used.  Determinism is established two ways: code
   inspection (inputs are only the parsed manifest structures; no clock,
   random source, environment, locale-sensitive formatting beyond fixed
   JSON/str conversions of JSON scalars, network or file read exists in
   the render path), and a runtime double-render guard that runs the
   renderer twice and refuses the row if the two byte sequences differ.
   Even deterministic bytes are only registered when their digest is
   present in, and was hash-verified by, the real cabinet inventory:
   replay never manufactures cabinet content.

Everything else is an explicit gap, never a guess (V4): non-``parsed``
rows, malformed payloads, file_hash disagreement between row and payload,
a renderer that raises or is observed to be non-deterministic, or
historical bytes whose content address is absent from the cabinet.  Gap
reasons classify each rejected coordinate exactly once.

Side channels (``document_map``/``assets``/``hyperlinks``/``self_check``
inside ``document``, and top-level ``document_quality``/
``engine_document``) were written by the live path as separate ``.json``
projection files and were *never* put in the mapping-object cabinet, so
they cannot be registered as mapping-version objects: this slice only
reports their presence per coordinate.  If such bytes happen to be in the
cabinet they are unreferenced and show up in the orphan report like any
other unreferenced object; they are never deleted, moved or renamed
(V10).

The history database is opened read-only (``mode=ro`` URI) and only
``SELECT`` is issued against it; the cabinet is only read
(``inventory()``); the *only* write is the ledger's
``register_version(..., source_kind="backfill", source_coordinate=...)``,
so V1/V2/V3 enforcement and idempotent replay are inherited unchanged.
Registrations are grouped into all-or-nothing batches via the ledger's
additive ``registration_batch()`` seam (V9).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.storage.mapping_store import render_mapping
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

from .mapping_version_index import (
    MappingVersionIndexError,
    MappingVersionLedger,
    SOURCE_KIND_BACKFILL,
    STATUS_REGISTERED,
    STATUS_REPLAYED,
)

# Evidence grades.
EVIDENCE_INLINE_BYTES = "inline_historical_bytes"
EVIDENCE_DETERMINISTIC_RENDER = "deterministic_render"

# Gap reasons; one per rejected history coordinate.
GAP_ROW_NOT_PARSED = "row_not_parsed"
GAP_MALFORMED_PAYLOAD = "malformed_payload"
GAP_FILE_HASH_MISMATCH = "file_hash_mismatch"
GAP_RENDER_FAILED = "render_failed"
GAP_RENDER_NONDETERMINISTIC = "render_nondeterministic"
GAP_OBJECT_MISSING = "object_missing"
GAP_OBJECT_CORRUPT = "object_corrupt"

# Structural keys the live path projected as separate sidecar .json files
# (never cabinet mapping objects). Reported only, never registered.
_DOCUMENT_SIDE_CHANNELS = ("document_map", "assets", "hyperlinks",
                           "self_check")
_MANIFEST_SIDE_CHANNELS = ("document_quality", "engine_document")

_PARSED_STATUS = "parsed"
_DEFAULT_BATCH_SIZE = 100
_MAX_DETAIL_CHARS = 300


class MappingBackfillError(RuntimeError):
    """Fatal setup or reconciliation failure; the run wrote nothing more."""


@dataclass(frozen=True)
class BackfillRegistration:
    logical_source_id: str
    version_no: int
    digest: str
    size_bytes: int
    evidence_kind: str
    source_coordinate: str


@dataclass(frozen=True)
class BackfillCandidate:
    """A certain-evidence history coordinate seen during a dry run."""

    logical_source_id: str
    digest: str
    size_bytes: int
    evidence_kind: str
    source_coordinate: str


@dataclass(frozen=True)
class BackfillGap:
    logical_source_id: str | None
    source_coordinate: str
    reason: str
    detail: str
    evidence_kind: str | None = None
    digest: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class BackfillOrphan:
    digest: str
    size_bytes: int


@dataclass(frozen=True)
class SideChannelNote:
    logical_source_id: str
    source_coordinate: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class BackfillReport:
    installation_id: str
    company_id: str
    dry_run: bool
    scanned_rows: int
    registered: tuple[BackfillRegistration, ...]
    replayed: tuple[BackfillRegistration, ...]
    would_register: tuple[BackfillCandidate, ...]
    gaps: tuple[BackfillGap, ...]
    orphans: tuple[BackfillOrphan, ...]
    side_channels: tuple[SideChannelNote, ...]

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True)
class _PlannedRegistration:
    logical_source_id: str
    digest: str
    size_bytes: int
    evidence_kind: str
    source_coordinate: str


def _coordinate(row_id: int) -> str:
    return f"parse_staging#id={row_id}"


def _detail(message: str) -> str:
    message = str(message)
    return message if len(message) <= _MAX_DETAIL_CHARS \
        else message[:_MAX_DETAIL_CHARS - 3] + "..."


class MappingVersionBackfiller:
    """Reconcile parse_staging history into a ready ledger, offline.

    Construction wires together three already-existing things: a ready
    :class:`MappingVersionLedger` (defines the single authorized scope),
    the real :class:`VersionedMappingObjectStore` (read-only), and the
    path of a history database containing ``parse_staging`` (opened
    read-only by this tool).  ``renderer`` defaults to the production
    render call and is injectable only for fault/uncertainty tests;
    ``batch_size`` bounds one all-or-nothing registration transaction.
    """

    def __init__(self, *,
                 ledger: MappingVersionLedger,
                 object_store: VersionedMappingObjectStore,
                 history_db: Path | str,
                 batch_size: int = _DEFAULT_BATCH_SIZE,
                 renderer: Callable[[dict[str, Any]], str] | None = None
                 ) -> None:
        if not isinstance(ledger, MappingVersionLedger):
            raise MappingBackfillError(
                "ledger must be a real MappingVersionLedger")
        if not isinstance(object_store, VersionedMappingObjectStore):
            raise MappingBackfillError(
                "object_store must be a real VersionedMappingObjectStore")
        if not isinstance(history_db, (str, Path)) or not str(history_db):
            raise MappingBackfillError(
                "history_db must be an explicit read-only database path")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) \
                or batch_size < 1:
            raise MappingBackfillError("batch_size must be a positive int")
        if renderer is None:
            renderer = render_mapping
        if not callable(renderer):
            raise MappingBackfillError("renderer must be callable")
        self._ledger = ledger
        self._store = object_store
        self._history_db = os.path.abspath(os.fspath(history_db))
        self._batch_size = batch_size
        self._renderer = renderer
        # Scope comes from the ledger binding; the run is refused if the
        # caller's declared scope ever differs.
        self.installation_id = ledger.installation_id
        self.company_id = ledger.company_id

    # -- public entry ---------------------------------------------------------

    def run(self, *, dry_run: bool = False,
            on_candidate: Callable[[_PlannedRegistration], None] | None = None
            ) -> BackfillReport:
        """Reconcile one history snapshot; repeat runs converge identically.

        ``dry_run`` performs the whole read-only planning and produces the
        same gap/orphan/side-channel report plus a ``would_register`` list,
        but never opens a ledger write transaction.  ``on_candidate`` is
        called with each planned registration inside its batch immediately
        before the ledger call; raising aborts and rolls that batch back.
        """
        if on_candidate is not None and not callable(on_candidate):
            raise MappingBackfillError("on_candidate must be callable")

        candidates, gaps, side_channels, referenced, inventory = self._plan()
        orphans = self._orphans(referenced, inventory)

        registered: list[BackfillRegistration] = []
        replayed: list[BackfillRegistration] = []
        would: list[BackfillCandidate] = []
        if dry_run:
            would = [BackfillCandidate(
                logical_source_id=c.logical_source_id,
                digest=c.digest, size_bytes=c.size_bytes,
                evidence_kind=c.evidence_kind,
                source_coordinate=c.source_coordinate)
                for c in candidates]
        else:
            registered, replayed = self._commit_batches(
                candidates, on_candidate)

        return BackfillReport(
            installation_id=self.installation_id,
            company_id=self.company_id,
            dry_run=bool(dry_run),
            scanned_rows=self._scanned_rows,
            registered=tuple(registered),
            replayed=tuple(replayed),
            would_register=tuple(would),
            gaps=tuple(gaps),
            orphans=tuple(orphans),
            side_channels=tuple(side_channels))

    # -- read-only planning ---------------------------------------------------

    def _open_history(self) -> sqlite3.Connection:
        """Open parse_staging history read-only via a mode=ro file URI."""
        path = Path(self._history_db)
        if not path.is_file():
            raise MappingBackfillError(
                f"history database does not exist: {self._history_db}")
        conn = sqlite3.connect(
            Path(self._history_db).as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _plan(self) -> tuple[list[_PlannedRegistration],
                             list[BackfillGap], list[SideChannelNote],
                             set[str], dict[str, int]]:
        candidates: list[_PlannedRegistration] = []
        gaps: list[BackfillGap] = []
        side_channels: list[SideChannelNote] = []
        referenced: set[str] = set()
        # One cabinet enumeration for planning and orphan accounting:
        # inventory itself re-reads and hash-verifies every occupant, so
        # membership means present+verified.
        inventory = {entry.digest: entry.size_bytes
                     for entry in self._store.inventory()}

        conn = self._open_history()
        try:
            # A deferred read transaction pins one consistent snapshot for
            # the whole scan; it issues no writes.
            conn.execute("BEGIN")
            try:
                rows = conn.execute(
                    "SELECT id, file_hash, format, payload, status "
                    "FROM parse_staging ORDER BY id ASC").fetchall()
            except sqlite3.OperationalError as exc:
                raise MappingBackfillError(
                    f"cannot read parse_staging history: {exc}") from exc
            conn.execute("COMMIT")
        finally:
            conn.close()
        self._scanned_rows = len(rows)

        for row in rows:
            coordinate = _coordinate(row["id"])
            source = row["file_hash"]
            valid_source = isinstance(source, str) and bool(source)

            status = row["status"]
            if status != _PARSED_STATUS:
                gaps.append(BackfillGap(
                    logical_source_id=source if valid_source else None,
                    source_coordinate=coordinate,
                    reason=GAP_ROW_NOT_PARSED,
                    detail=_detail(f"row status is {status!r}, not "
                                   f"{_PARSED_STATUS!r}; no mapping bytes "
                                   "were ever produced for it")))
                continue

            try:
                manifest = json.loads(row["payload"])
            except (TypeError, ValueError) as exc:
                gaps.append(BackfillGap(
                    logical_source_id=source if valid_source else None,
                    source_coordinate=coordinate,
                    reason=GAP_MALFORMED_PAYLOAD,
                    detail=_detail(f"payload is not decodable JSON: {exc}")))
                continue
            if not isinstance(manifest, dict):
                gaps.append(BackfillGap(
                    logical_source_id=source if valid_source else None,
                    source_coordinate=coordinate,
                    reason=GAP_MALFORMED_PAYLOAD,
                    detail="payload JSON is not a JSON object"))
                continue
            if not valid_source \
                    or manifest.get("file_hash") != source:
                gaps.append(BackfillGap(
                    logical_source_id=source if valid_source else None,
                    source_coordinate=coordinate,
                    reason=GAP_FILE_HASH_MISMATCH,
                    detail=_detail(
                        f"row file_hash {source!r} does not match payload "
                        f"file_hash {manifest.get('file_hash')!r}")))
                continue

            note = self._side_channel_note(source, coordinate, manifest)
            if note is not None:
                side_channels.append(note)

            data, evidence, grade_error = self._historical_bytes(
                manifest, coordinate)
            if grade_error is not None:
                gaps.append(BackfillGap(
                    logical_source_id=source,
                    source_coordinate=coordinate,
                    reason=grade_error[0],
                    detail=grade_error[1],
                    evidence_kind=grade_error[2]))
                continue

            digest = self._store.digest_of(data)
            size = len(data)
            # Any computable historical content address is "referenced"
            # for orphan accounting, even when the cabinet lost the bytes.
            referenced.add(digest)

            inventory_size = inventory.get(digest)
            if inventory_size is None:
                gaps.append(BackfillGap(
                    logical_source_id=source,
                    source_coordinate=coordinate,
                    reason=GAP_OBJECT_MISSING,
                    detail="historical bytes are determined but their "
                           "content address is absent from the cabinet; "
                           "the backfiller never inserts cabinet objects",
                    evidence_kind=evidence,
                    digest=digest, size_bytes=size))
                continue
            if inventory_size != size:
                gaps.append(BackfillGap(
                    logical_source_id=source,
                    source_coordinate=coordinate,
                    reason=GAP_OBJECT_CORRUPT,
                    detail=f"cabinet entry for {digest} reports size "
                           f"{inventory_size}, but the historical bytes are "
                           f"{size}",
                    evidence_kind=evidence,
                    digest=digest, size_bytes=size))
                continue
            candidates.append(_PlannedRegistration(
                logical_source_id=source,
                digest=digest, size_bytes=size,
                evidence_kind=evidence,
                source_coordinate=coordinate))
        return candidates, gaps, side_channels, referenced, inventory

    def _historical_bytes(self, manifest: dict[str, Any],
                          coordinate: str
                          ) -> tuple[bytes | None, str | None,
                                     tuple[str, str, str] | None]:
        """Return (bytes, evidence_kind) or a gap tuple (reason, detail, kind)."""
        document = manifest.get("document")
        if isinstance(document, dict) \
                and isinstance(document.get("markdown"), str):
            try:
                return document["markdown"].encode("utf-8"), \
                    EVIDENCE_INLINE_BYTES, None
            except UnicodeEncodeError as exc:
                return None, None, (
                    GAP_MALFORMED_PAYLOAD,
                    _detail("inline document.markdown is not valid UTF-8: "
                            f"{exc}"),
                    EVIDENCE_INLINE_BYTES)

        # No inline bytes: the shared projection is the pure render. Prove
        # determinism at runtime by rendering twice; a renderer that uses
        # wall-clock/random state is caught here before anything registers.
        try:
            first = self._renderer(manifest)
            second = self._renderer(manifest)
        except Exception as exc:  # renderer failure is evidence failure
            return None, None, (
                GAP_RENDER_FAILED,
                _detail(f"renderer raised {type(exc).__name__}: {exc}"),
                EVIDENCE_DETERMINISTIC_RENDER)
        if not isinstance(first, str) or not isinstance(second, str):
            return None, None, (
                GAP_RENDER_FAILED,
                _detail("renderer must return str; got "
                        f"{type(first).__name__}/{type(second).__name__}"),
                EVIDENCE_DETERMINISTIC_RENDER)
        if first != second:
            return None, None, (
                GAP_RENDER_NONDETERMINISTIC,
                "rendering the same manifest twice produced different "
                "bytes; the historical mapping cannot be reconstructed "
                "with certainty",
                EVIDENCE_DETERMINISTIC_RENDER)
        try:
            return first.encode("utf-8"), \
                EVIDENCE_DETERMINISTIC_RENDER, None
        except UnicodeEncodeError as exc:
            return None, None, (
                GAP_RENDER_FAILED,
                _detail(f"rendered mapping is not valid UTF-8: {exc}"),
                EVIDENCE_DETERMINISTIC_RENDER)

    @staticmethod
    def _side_channel_note(source: str, coordinate: str,
                           manifest: dict[str, Any]) -> SideChannelNote | None:
        keys: list[str] = []
        document = manifest.get("document")
        if isinstance(document, dict):
            keys.extend(key for key in _DOCUMENT_SIDE_CHANNELS
                        if key in document)
        keys.extend(key for key in _MANIFEST_SIDE_CHANNELS if key in manifest)
        if not keys:
            return None
        return SideChannelNote(
            logical_source_id=source,
            source_coordinate=coordinate,
            keys=tuple(keys))

    # -- write phase: the ledger stays the only write entry -------------------

    def _commit_batches(self, candidates: list[_PlannedRegistration],
                        on_candidate: Callable[[_PlannedRegistration],
                                               None] | None
                        ) -> tuple[list[BackfillRegistration],
                                   list[BackfillRegistration]]:
        registered: list[BackfillRegistration] = []
        replayed: list[BackfillRegistration] = []
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start:start + self._batch_size]
            # All-or-nothing batch (V9): any raised error rolls back every
            # registration in this batch, and the next repeat run replays
            # earlier batches instead of duplicating them.
            with self._ledger.registration_batch():
                for candidate in batch:
                    if on_candidate is not None:
                        on_candidate(candidate)
                    try:
                        result = self._ledger.register_version(
                            logical_source_id=candidate.logical_source_id,
                            digest=candidate.digest,
                            size_bytes=candidate.size_bytes,
                            installation_id=self.installation_id,
                            company_id=self.company_id,
                            source_kind=SOURCE_KIND_BACKFILL,
                            source_coordinate=candidate.source_coordinate,
                            h_commit_oid=None)
                    except MappingVersionIndexError as exc:
                        # Planning already cabinet-verified every candidate,
                        # so reaching this means the world changed under the
                        # run; fail the batch closed instead of guessing.
                        raise MappingBackfillError(
                            f"registration at {candidate.source_coordinate} "
                            f"was refused after planning: {exc}") from exc
                    entry = BackfillRegistration(
                        logical_source_id=candidate.logical_source_id,
                        version_no=result.version_no,
                        digest=candidate.digest,
                        size_bytes=candidate.size_bytes,
                        evidence_kind=candidate.evidence_kind,
                        source_coordinate=candidate.source_coordinate)
                    if result.status == STATUS_REGISTERED:
                        registered.append(entry)
                    elif result.status == STATUS_REPLAYED:
                        replayed.append(entry)
                    else:  # pragma: no cover - ledger vocabulary is pinned
                        raise MappingBackfillError(
                            f"unknown registration status {result.status!r}")
        return registered, replayed

    # -- orphan accounting (report only) --------------------------------------

    def _orphans(self, referenced_digests: set[str],
                 inventory: dict[str, int]) -> list[BackfillOrphan]:
        """Cabinet objects referenced by no parse_staging row and no ledger."""
        ledger_digests = {
            reg["sha256_digest"].split(":", 1)[1]
            for reg in self._ledger.canonical_payload()["registrations"]
        }
        return [
            BackfillOrphan(digest=digest, size_bytes=size)
            for digest, size in sorted(inventory.items())
            if digest not in referenced_digests and digest not in ledger_digests
        ]
