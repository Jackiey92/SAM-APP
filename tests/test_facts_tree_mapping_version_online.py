"""R3 live-path online mapping-version registration contracts.

Everything here is real: the production app SQLite database
(``init_db``), the real immutable mapping-object cabinet, a real R2
:class:`MappingVersionLedger`, a real bare Git facts repository as
formal history H, and the real parsing entry points
(``StagingStore.save_manifest``/``update_payload`` and
``SourceMapService.rebuild_map``/``replace``/``remove``).  Success
paths, hashes and version numbers are never mocked.  Faults are
injected only at the boundaries the R3 freeze names: filesystem
permissions, an actual held SQLite lock, cabinet-byte tampering, a
process-boundary ledger wrapper and an H-read provider.

Coverage of freeze appendix R3 §4 / invariants O1–O11:

 1 green path through StagingStore: online row, version 1, real
   current H oid, replay keeps the number, changed bytes issue 2, both
   versions round-trip byte-identical through the real cabinet (§4.1,
   O1/O4/O5/O6)
 2 with-ledger vs no-ledger byte-identical outputs and return values
   (§4.2, O1/O3)
 3 unwritable ledger directory degrades: projection/parse still
   succeed, structured reason+coordinate diagnostic (§4.3, O2)
 4 the degraded object is repaired idempotently by the R2 offline
   backfiller; the final per-source version set is complete, ordered,
   duplicate-free (§4.3, O7)
 5 a really held ledger lock degrades after one bounded wait, then an
   online rebuild registers the missing version (O2, O5)
 6 arbitrary process faults (RuntimeError, SQLITE_FULL) degrade with
   redacted diagnostics — no exception message, absolute path or
   token in the outcome (O2, O10)
 7 cabinet put failure: projection untouched, staging row rolled
   back, zero new ledger rows (§4.4, O4)
 8 object corrupted in the put→register window: ghost refusal, no row
   naming the wrong digest, projection still produced (§4.5, O4)
 9 multi-thread interleaved persists number contiguously with no
   duplicate/gap; repeated rebuilds add nothing (§4.6, O5)
10 H unavailable/malformed/not configured: h_commit_oid is None,
   never fabricated, the provider is read per call not cached (§4.7,
   O6)
11 foreign-scope ledger is refused at the seam and never written
   (§4.8, O11)
12 update_payload/_edit live paths: unchanged bytes replay, changed
   bytes version (O1/O5)
13 sockets sealed end to end, plus the live modules never import the
   ledger (duck-typed seam only) and the seam never imports storage
   (§4.10, O8/O10)
14 explicit sha256 H repository (the env-driven default covers the
   dual-hash rerun under GIT_TEST_DEFAULT_HASH=sha256) (§4.9)
"""
from __future__ import annotations

import ast
import hashlib
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.db import init_db
from app.facts_tree.mapping_version_backfill import MappingVersionBackfiller
from app.facts_tree.mapping_version_index import (
    BYTE_PRESENT,
    MappingVersionLedger,
    SOURCE_KIND_BACKFILL,
    SOURCE_KIND_ONLINE,
)
from app.facts_tree.mapping_version_online import (
    DEGRADED_GHOST_OBJECT,
    DEGRADED_LEDGER_BUSY,
    DEGRADED_LEDGER_FAULT,
    DEGRADED_LEDGER_FULL,
    DEGRADED_LEDGER_READONLY,
    DEGRADED_SCOPE_MISMATCH,
    H_NOT_CONFIGURED,
    H_OK,
    H_UNAVAILABLE,
    OnlineMappingRegistrar,
    STATUS_DEGRADED,
)
from app.harness.staging import StagingStore
from app.storage import ArchiveFileStore
from app.storage.mapping_store import SourceMapService, render_mapping
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

INSTALL = "inst-r3"
COMPANY = "company-r3"
REPO_ID = "repo-r3"
INSTALL_B = "inst-r3-foreign"
COMPANY_B = "company-r3-foreign"

_NON_ROOT = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="permission fault injection only distinguishes itself as a "
           "non-root user")


# -- real-world assembly ------------------------------------------------------

def _source(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _manifest(file_hash: str, text: str) -> dict:
    return {
        "file_hash": file_hash,
        "format": "pdf",
        "filename": f"{file_hash[:8]}.pdf",
        "parse_summary": {"status": "parsed"},
        "pages": [{
            "page_no": 1,
            "text_items": [
                {"text": text, "source_loc": {"page": 1, "locator": "L10"}}],
        }],
    }


def _inline_manifest(file_hash: str, markdown: str) -> dict:
    return {
        "file_hash": file_hash, "format": "md",
        "parse_summary": {"status": "parsed"},
        "document": {
            "markdown": markdown,
            "document_map": {"page": 1},
            "self_check": {"ok": True},
        },
    }


def _object_format() -> str:
    value = os.environ.get("GIT_TEST_DEFAULT_HASH", "sha1")
    return value if value in ("sha1", "sha256") else "sha1"


def assemble_world(root: Path, *, object_format: str | None = None,
                   with_registrar: bool = True,
                   installation_id: str = INSTALL,
                   company_id: str = COMPANY,
                   h_provider="default"):
    """Build one fully real parse/cabinet/ledger/H world under ``root``.

    ``h_provider="default"`` wires the real repository ``head_oid``;
    ``None`` constructs the seam with no provider; any callable is used
    verbatim as the provider.
    """
    root.mkdir(parents=True, exist_ok=True)
    db = root / "app.db"
    init_db(db)
    (root / "2a").mkdir(exist_ok=True)
    maps = root / "2a" / "map"
    cabinet_dir = root / "2a" / "mapping-objects"
    cabinet = VersionedMappingObjectStore(cabinet_dir)

    from app.facts_tree.git_repository import FactsGitRepository
    repository = FactsGitRepository.initialize(
        root / "facts.git",
        object_format=object_format or _object_format(),
        repo_id=REPO_ID, installation_id=installation_id,
        company_id=company_id)
    ledger = None
    online = None
    if with_registrar:
        vault = root / "vault"
        vault.mkdir(exist_ok=True)
        ledger = MappingVersionLedger(
            vault / "mvi",
            installation_id=installation_id, company_id=company_id,
            object_store=cabinet)
        provider = None
        if h_provider == "default":
            provider = repository.head_oid
        elif callable(h_provider):
            provider = h_provider
        online = OnlineMappingRegistrar(
            installation_id=installation_id, company_id=company_id,
            ledger=ledger, h_provider=provider)
    staging = StagingStore(db, maps_path=maps, online_registrar=online)
    service = SourceMapService(db, root / "archive", maps_path=maps,
                               online_registrar=online)
    return {
        "root": root, "db": db, "maps": maps,
        "cabinet_dir": cabinet_dir, "cabinet": cabinet,
        "repository": repository, "ledger": ledger,
        "registrar": online, "staging": staging, "service": service,
    }


@pytest.fixture
def world(tmp_path):
    data = assemble_world(tmp_path / "world")
    yield data
    if data["ledger"] is not None:
        data["ledger"].close()


def _versions(world_, file_hash: str):
    return world_["ledger"].list_versions(
        file_hash, installation_id=INSTALL, company_id=COMPANY)


def _snapshot_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(
            path.read_bytes()).hexdigest()
    return out


def _cabinet_digest_set(world_) -> set[tuple[str, int]]:
    return {(entry.digest, entry.size_bytes)
            for entry in world_["cabinet"].inventory()}


def _staging_row_count(world_) -> int:
    with sqlite3.connect(world_["db"]) as raw:
        return raw.execute(
            "SELECT COUNT(*) FROM parse_staging").fetchone()[0]


def _render_digest(manifest: dict) -> str:
    return hashlib.sha256(
        render_mapping(manifest).encode("utf-8")).hexdigest()


def _admit_source(world_, name: str) -> str:
    """Admit a real original through the archive cabinet (FK parent row).

    ``source_edits.file_hash`` references ``source_files.file_hash``; the
    real production admission path is ``ArchiveFileStore.put_bytes``,
    which hashes the original bytes and inserts the parent row.  Tests
    that exercise replace/remove must admit the original first, exactly
    like tests/test_source_map.py's fixture does.
    """
    data = f"immutable original for {name}\n".encode("utf-8")
    stored = ArchiveFileStore(
        world_["root"] / "archive", world_["db"]).put_bytes(
            data, original_name=f"{name}.pdf",
            mime_type="application/pdf")
    return stored.file_hash


def _harden_and_soften_ledger(world_):
    """Make the open ledger unwritable; return a restore callback.

    The ledger keeps one already-open SQLite connection, so a file
    mode change alone does not stop writes through the open
    descriptor; the containing directory is made non-writable too, so
    the DELETE journal can neither be created nor removed and SQLite
    reports a read-only database on the next write.
    """
    ledger_db = Path(world_["ledger"].database_path)
    ledger_dir = ledger_db.parent
    os.chmod(ledger_db, 0o400)
    os.chmod(ledger_dir, 0o500)

    def restore():
        os.chmod(ledger_dir, 0o700)
        os.chmod(ledger_db, 0o600)

    return restore


# -- §4.1 green path ----------------------------------------------------------

def test_green_path_registers_online_version_with_real_h(world):
    fh = _source("green")
    staging_id = world["staging"].save_manifest(_manifest(fh, "第一版正文"))
    versions = _versions(world, fh)
    assert len(versions) == 1
    v1 = versions[0]
    assert v1.version_no == 1
    assert v1.source_kind == SOURCE_KIND_ONLINE
    assert v1.byte_status == BYTE_PRESENT
    assert v1.source_coordinate == f"parse_staging#id={staging_id}"
    # The H oid is the formal history repository's current tip, really.
    assert v1.h_commit_oid == world["repository"].head_oid()
    assert len(v1.h_commit_oid) == (
        64 if _object_format() == "sha256" else 40)
    outcome = world["registrar"].last_outcome
    assert outcome.status == "registered"
    assert outcome.h_status == H_OK
    assert outcome.h_commit_oid == v1.h_commit_oid


def test_reparse_same_bytes_replays_same_version_and_keeps_coordinate(world):
    fh = _source("replay")
    first_id = world["staging"].save_manifest(_manifest(fh, "重复正文"))
    second_id = world["staging"].save_manifest(_manifest(fh, "重复正文"))
    assert second_id != first_id
    versions = _versions(world, fh)
    assert [v.version_no for v in versions] == [1]
    # Provenance of the first durable registration wins forever.
    assert versions[0].source_coordinate == f"parse_staging#id={first_id}"
    assert world["registrar"].last_outcome.status == "replayed"


def test_changed_bytes_issue_next_version_and_round_trip_through_cabinet(world):
    fh = _source("versions")
    manifest_v1 = _manifest(fh, "v1 body")
    manifest_v2 = _manifest(fh, "v2 body, different")
    world["staging"].save_manifest(manifest_v1)
    v1_projection = world["service"].map_path(fh).read_bytes()
    world["staging"].save_manifest(manifest_v2)
    v2_projection = world["service"].map_path(fh).read_bytes()
    assert v1_projection != v2_projection
    versions = _versions(world, fh)
    assert [v.version_no for v in versions] == [1, 2]
    for version, projection in zip(versions, (v1_projection, v2_projection)):
        record = world["ledger"].get_version(
            fh, version.version_no,
            installation_id=INSTALL, company_id=COMPANY,
            include_bytes=True)
        assert record.byte_status == BYTE_PRESENT
        assert record.content == projection
        assert hashlib.sha256(projection).hexdigest() == record.digest


# -- §4.2 / O1/O3 byte-identical with/without the seam ------------------------

def _parsing_script(world_) -> list:
    """Exercise every live parse entry point with identical fixed inputs."""
    staging, service = world_["staging"], world_["service"]
    # Edits reference source_edits→source_files: admit the real originals
    # through the archive cabinet before replace/remove can insert.
    fh1, fh2 = _admit_source(world_, "src-one"), _admit_source(world_, "src-two")
    returns: list = []
    returns.append(("save", staging.save_manifest(_manifest(fh1, "正文甲"))))
    returns.append(("save", staging.save_manifest(_manifest(fh1, "正文甲"))))
    third = staging.save_manifest(_manifest(fh2, "正文乙"))
    returns.append(("save", third))
    staging.update_payload(third, {"routed_company": "acme"})
    returns.append(("rebuild1", service.rebuild_map(file_hash=fh1)))
    edit = service.replace(
        file_hash=fh1, company_id="acme",
        target_locator="page=1; locator=L10",
        replacement_text="订正后", confirm=True)
    # created_at is wall-clock output and is not part of the parse
    # contract; compare only the deterministic edit-record fields.
    returns.append(("edit1", (edit["operation"], edit["status"],
                              edit["target_locator"], edit["company_id"],
                              edit["supersedes_id"])))
    service.remove(file_hash=fh2, company_id="acme",
                   target_locator="page=1; locator=L10", confirm=True)
    markdown = "# 内联 markdown 投影\n\n正文在这里\n"
    staging.save_manifest(_inline_manifest(fh2, markdown))
    returns.append(("rebuild2", service.rebuild_map(file_hash=fh2)))
    return returns


def test_with_and_without_ledger_outputs_are_byte_identical(tmp_path):
    plain = assemble_world(tmp_path / "plain", with_registrar=False)
    wired = assemble_world(tmp_path / "wired", with_registrar=True)
    returns_plain = _parsing_script(plain)
    returns_wired = _parsing_script(wired)
    # Same staging ids, rendered bytes, and edit-record fields.
    assert returns_plain == returns_wired
    # Latest projections and every sidecar JSON hash the same per relpath.
    assert _snapshot_tree(plain["maps"]) == _snapshot_tree(wired["maps"])
    # The immutable cabinet ends with exactly the same digest/size set:
    # fh1 pdf render, fh2 pdf render and fh2 inline markdown.
    assert _cabinet_digest_set(plain) == _cabinet_digest_set(wired)
    rows = wired["ledger"].canonical_payload()["registrations"]
    assert {(r["sha256_digest"].split(":", 1)[1], r["size_bytes"])
            for r in rows} == _cabinet_digest_set(wired)
    wired["ledger"].close()


def test_default_none_path_creates_no_ledger_and_parses_normally(tmp_path):
    plain = assemble_world(tmp_path / "plain", with_registrar=False)
    fh = _source("x")
    plain["staging"].save_manifest(_manifest(fh, "正文"))
    assert not (plain["root"] / "vault").exists()
    assert plain["service"].map_path(fh).is_file()


# -- §4.3 / O2 degrade: unwritable ledger -------------------------------------

@_NON_ROOT
def test_unwritable_ledger_degrades_but_projection_and_parse_succeed(world):
    fh = _source("readonly")
    world["staging"].save_manifest(_manifest(fh, "registered first"))
    restore = _harden_and_soften_ledger(world)
    manifest = _manifest(fh, "second body while ledger is read-only")
    try:
        rows_before = _staging_row_count(world)
        staging_id = world["staging"].save_manifest(manifest)
        # Main link fully succeeded: row committed, projection replaced.
        assert _staging_row_count(world) == rows_before + 1
        projected = world["service"].map_path(fh).read_text(
            encoding="utf-8")
        assert "second body while ledger is read-only" in projected
        outcome = world["registrar"].last_outcome
        assert outcome.status == STATUS_DEGRADED
        assert outcome.reason == DEGRADED_LEDGER_READONLY
        assert outcome.source_coordinate == f"parse_staging#id={staging_id}"
        assert outcome.digest == _render_digest(manifest)
        assert outcome.version_no is None
        assert any(d.reason == DEGRADED_LEDGER_READONLY
                   for d in world["registrar"].diagnostics)
    finally:
        restore()
    # Only version 1 exists; the second object is cabinet-present but
    # ledger-missing until a repair runs.
    assert [v.version_no for v in _versions(world, fh)] == [1]
    assert world["cabinet"].has(_render_digest(manifest))


# -- §4.3 / O7: R2 backfiller repairs the degraded coordinate -----------------

@_NON_ROOT
def test_r2_backfiller_repairs_degraded_online_coordinate(world):
    fh = _source("repair")
    id1 = world["staging"].save_manifest(_manifest(fh, "first online body"))
    restore = _harden_and_soften_ledger(world)
    try:
        id2 = world["staging"].save_manifest(
            _manifest(fh, "second body degraded"))
        assert world["registrar"].last_outcome.status == STATUS_DEGRADED
    finally:
        restore()
    manifest_v1 = _manifest(fh, "first online body")
    manifest_v2 = _manifest(fh, "second body degraded")
    expected_d1, expected_d2 = (_render_digest(manifest_v1),
                                _render_digest(manifest_v2))

    # The offline R2 reconciler, given the same real cabinet and the
    # same app history database, repairs the in-cabinet/not-in-ledger
    # coordinate without touching the existing online row.
    backfiller = MappingVersionBackfiller(
        ledger=world["ledger"],
        object_store=VersionedMappingObjectStore(world["cabinet_dir"]),
        history_db=world["db"])
    report = backfiller.run()
    registered = {(r.logical_source_id, r.version_no, r.digest,
                   r.source_coordinate) for r in report.registered}
    assert registered == {
        (fh, 2, expected_d2, f"parse_staging#id={id2}")}
    assert {(r.logical_source_id, r.digest) for r in report.replayed} \
        == {(fh, expected_d1)}

    versions = _versions(world, fh)
    assert [v.version_no for v in versions] == [1, 2]
    assert [v.digest for v in versions] == [expected_d1, expected_d2]
    assert [v.source_kind for v in versions] == [
        SOURCE_KIND_ONLINE, SOURCE_KIND_BACKFILL]
    assert versions[0].source_coordinate == f"parse_staging#id={id1}"
    assert versions[1].source_coordinate == f"parse_staging#id={id2}"
    assert all(v.byte_status == BYTE_PRESENT for v in versions)

    # Final-set semantics: repeat runs converge to exactly the same
    # complete, ordered, duplicate-free version set.
    second = backfiller.run()
    assert second.registered == ()
    versions_again = _versions(world, fh)
    assert [(v.version_no, v.digest) for v in versions_again] == \
        [(v.version_no, v.digest) for v in versions]
    assert world["ledger"].ledger_shape(verify=True)["total_gaps"] == 0


# -- O2 lock busy -------------------------------------------------------------

def test_held_ledger_lock_degrades_after_one_bounded_wait_then_recovers(world):
    fh = _source("locked")
    world["staging"].save_manifest(_manifest(fh, "before the lock"))
    holder = sqlite3.connect(world["ledger"].database_path)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        world["staging"].save_manifest(
            _manifest(fh, "during the held lock"))
        elapsed = time.monotonic() - start
        outcome = world["registrar"].last_outcome
        assert outcome.status == STATUS_DEGRADED
        assert outcome.reason == DEGRADED_LEDGER_BUSY
        # One bounded wait (the ledger's busy_timeout=5000), never a retry
        # loop: lower bound well above an immediate refusal, upper bound
        # below one timeout plus a generous scheduling margin.
        assert 1.0 <= elapsed < 20.0
    finally:
        # The exclusive lock (DELETE journal blocks readers too) must be
        # fully released before any ledger read opens a second connection.
        holder.close()
    assert [v.version_no for v in _versions(world, fh)] == [1]
    # Lock released: an ordinary online rebuild of the newest bytes
    # registers the missing version through the same live seam.
    world["service"].rebuild_map(file_hash=fh)
    assert [v.version_no for v in _versions(world, fh)] == [1, 2]


# -- O2/O10 arbitrary process faults, redacted diagnostics --------------------

class _FaultyLedger(MappingVersionLedger):
    fault: Exception

    def register_version(self, **kwargs):
        raise self.fault


def _faulty_world(tmp_path, fault: Exception):
    data = assemble_world(tmp_path / "world")
    faulty = _FaultyLedger(
        tmp_path / "world" / "vault" / "faulty-mvi",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=VersionedMappingObjectStore(data["cabinet_dir"]))
    faulty.fault = fault
    data["ledger"].close()
    registrar = OnlineMappingRegistrar(
        installation_id=INSTALL, company_id=COMPANY, ledger=faulty,
        h_provider=data["repository"].head_oid)
    data["registrar"] = registrar
    data["ledger"] = faulty
    data["staging"] = StagingStore(
        data["db"], maps_path=data["maps"], online_registrar=registrar)
    data["service"] = SourceMapService(
        data["db"], data["root"] / "archive", maps_path=data["maps"],
        online_registrar=registrar)
    return data, faulty


def test_arbitrary_ledger_exception_degrades_with_redacted_diagnostic(
        tmp_path):
    secret_path = str(tmp_path / "SECRET-LEDGER-PATH")
    data, faulty = _faulty_world(
        tmp_path,
        RuntimeError(f"disk full at {secret_path} token=ZZTOPSECRET"))
    fh = _source("runtime-fault")
    data["staging"].save_manifest(_manifest(fh, "body despite fault"))
    outcome = data["registrar"].last_outcome
    assert outcome.status == STATUS_DEGRADED
    assert outcome.reason == DEGRADED_LEDGER_FAULT
    assert outcome.detail == "RuntimeError"
    rendered = str(outcome)
    assert "SECRET" not in rendered and "ZZTOPSECRET" not in rendered
    assert secret_path not in rendered and str(tmp_path) not in rendered
    assert data["service"].map_path(fh).is_file()
    # The faulty ledger wrote nothing.
    assert faulty.ledger_shape()["total_versions"] == 0
    faulty.close()


def test_sqlite_full_classifies_as_full_and_redacts_message(tmp_path):
    fault = sqlite3.OperationalError(
        f"database or disk is full: {tmp_path}/SECRET")
    fault.sqlite_errorcode = 13
    fault.sqlite_errorname = "SQLITE_FULL"
    data, faulty = _faulty_world(tmp_path, fault)
    fh = _source("disk-full")
    data["staging"].save_manifest(_manifest(fh, "body despite full disk"))
    outcome = data["registrar"].last_outcome
    assert outcome.reason == DEGRADED_LEDGER_FULL
    assert outcome.detail == "sqlite3.SQLITE_FULL"
    assert "SECRET" not in str(outcome)
    faulty.close()


# -- §4.4 / O4 cabinet put failure --------------------------------------------

@_NON_ROOT
def test_cabinet_put_failure_registers_nothing_and_keeps_projection(world):
    fh = _source("put-fails")
    world["staging"].save_manifest(_manifest(fh, "first safe body"))
    before = world["service"].map_path(fh).read_bytes()
    rows_before = _staging_row_count(world)
    # 0000 (not r-x): the store cannot even open its root directory, so
    # put fails before touching bytes; a mere 0500 would still allow
    # writes inside the pre-existing 0755 shard directories.
    os.chmod(world["cabinet_dir"], 0o000)
    try:
        with pytest.raises(OSError):
            world["staging"].save_manifest(
                _manifest(fh, "second body that cannot be put"))
    finally:
        os.chmod(world["cabinet_dir"], 0o700)
    # Projection untouched, staging insert rolled back, no version 2.
    assert world["service"].map_path(fh).read_bytes() == before
    assert _staging_row_count(world) == rows_before
    assert [v.version_no for v in _versions(world, fh)] == [1]


# -- §4.5 / O4 ghost in the put→register window -------------------------------

class _GhostInjector:
    """Corrupt the real cabinet object between put and register."""

    def __init__(self, inner, cabinet_dir: Path):
        self._inner = inner
        self._cabinet_dir = cabinet_dir

    def register(self, **kwargs):
        obj = self._cabinet_dir / kwargs["digest"][:2] / kwargs["digest"]
        assert obj.is_file()  # the put really happened
        obj.write_bytes(b"tampered after put, before register")
        return self._inner.register(**kwargs)


def test_ghost_object_after_put_is_refused_and_unindexed(world):
    ghost_registrar = _GhostInjector(world["registrar"], world["cabinet_dir"])
    staging = StagingStore(
        world["db"], maps_path=world["maps"],
        online_registrar=ghost_registrar)
    fh = _source("ghost")
    staging.save_manifest(_manifest(fh, "body whose object gets tampered"))
    outcome = world["registrar"].last_outcome
    assert outcome.status == STATUS_DEGRADED
    assert outcome.reason == DEGRADED_GHOST_OBJECT
    assert outcome.detail == "sha256_mismatch"
    # No row may name the bad digest; the cabinet mismatch is reported
    # as a would-be gap, never substituted by another version.
    assert _versions(world, fh) == ()
    assert world["ledger"].ledger_shape()["total_versions"] == 0
    # Main link still produced the projection from its in-memory bytes.
    assert "body whose object gets tampered" in \
        world["service"].map_path(fh).read_text(encoding="utf-8")


# -- §4.6 / O5 concurrency -----------------------------------------------------

def test_concurrent_persists_number_contiguously_without_gap_or_dup(world):
    fh = _source("concurrent")
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            local = StagingStore(
                world["db"], maps_path=world["maps"],
                online_registrar=world["registrar"])
            manifest = _manifest(fh, f"concurrent body number {i}")
            barrier.wait()
            local.save_manifest(manifest)
            local.save_manifest(manifest)  # immediate replay
        except Exception as exc:  # captured: the main link must not fail
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    versions = _versions(world, fh)
    assert len(versions) == thread_count
    assert sorted(v.version_no for v in versions) == list(
        range(1, thread_count + 1))
    assert len({v.digest for v in versions}) == thread_count
    assert all(v.byte_status == BYTE_PRESENT for v in versions)
    # Repeated rebuilds of the newest bytes add nothing.
    for _ in range(3):
        world["service"].rebuild_map(file_hash=fh)
    assert len(_versions(world, fh)) == thread_count


# -- §4.7 / O6 H association honesty ------------------------------------------

def test_h_unavailable_or_malformed_registers_none_never_fabricated(tmp_path):
    scripted = [
        RuntimeError("cannot read H at /SECRET"),
        "not-an-oid",
    ]

    def provider():
        item = scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    data = assemble_world(tmp_path / "world", h_provider=provider)
    try:
        fh = _source("h-down")
        data["staging"].save_manifest(_manifest(fh, "body one"))
        outcome = data["registrar"].last_outcome
        assert outcome.status == "registered"
        assert outcome.h_status == H_UNAVAILABLE
        assert outcome.h_commit_oid is None
        assert _versions(data, fh)[0].h_commit_oid is None

        data["staging"].save_manifest(_manifest(fh, "body two"))
        assert data["registrar"].last_outcome.h_status == H_UNAVAILABLE
        assert [v.h_commit_oid for v in _versions(data, fh)] == [None, None]
    finally:
        data["ledger"].close()


def test_h_provider_is_called_per_registration_not_cached(tmp_path):
    data = assemble_world(tmp_path / "world", with_registrar=False)
    calls: list[str | None] = []
    scripted = ["a" * 40, None, "b" * 40]

    def provider():
        value = scripted[len(calls)]
        calls.append(value)
        return value

    vault = data["root"] / "vault"
    vault.mkdir(exist_ok=True)
    ledger = MappingVersionLedger(
        vault / "mvi2",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=VersionedMappingObjectStore(data["cabinet_dir"]))
    registrar = OnlineMappingRegistrar(
        installation_id=INSTALL, company_id=COMPANY, ledger=ledger,
        h_provider=provider)
    staging = StagingStore(
        data["db"], maps_path=data["maps"], online_registrar=registrar)
    fh = _source("h-scripted")
    staging.save_manifest(_manifest(fh, "body one"))
    staging.save_manifest(_manifest(fh, "body two"))
    staging.save_manifest(_manifest(fh, "body three"))
    assert calls == ["a" * 40, None, "b" * 40]
    records = ledger.list_versions(
        fh, installation_id=INSTALL, company_id=COMPANY)
    assert [r.h_commit_oid for r in records] == [
        "a" * 40, None, "b" * 40]
    assert registrar.last_outcome.h_status == H_OK
    ledger.close()


def test_registrar_without_h_provider_marks_not_configured(tmp_path):
    data = assemble_world(tmp_path / "world", with_registrar=False)
    vault = data["root"] / "vault"
    vault.mkdir(exist_ok=True)
    ledger = MappingVersionLedger(
        vault / "mvi",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=VersionedMappingObjectStore(data["cabinet_dir"]))
    registrar = OnlineMappingRegistrar(
        installation_id=INSTALL, company_id=COMPANY, ledger=ledger)
    staging = StagingStore(
        data["db"], maps_path=data["maps"], online_registrar=registrar)
    fh = _source("h-none")
    staging.save_manifest(_manifest(fh, "body"))
    assert registrar.last_outcome.h_status == H_NOT_CONFIGURED
    assert registrar.last_outcome.h_commit_oid is None
    ledger.close()


# -- §4.8 / O11 scope ----------------------------------------------------------

def test_foreign_scope_ledger_is_refused_without_cross_write(world):
    foreign = MappingVersionLedger(
        world["root"] / "vault" / "mvi-foreign",
        installation_id=INSTALL_B, company_id=COMPANY_B,
        object_store=VersionedMappingObjectStore(world["cabinet_dir"]))
    # Declared scope (this installation/company) does not match the
    # bound foreign ledger: the seam refuses before touching it.
    cross = OnlineMappingRegistrar(
        installation_id=INSTALL, company_id=COMPANY, ledger=foreign)
    staging = StagingStore(
        world["db"], maps_path=world["maps"], online_registrar=cross)
    fh = _source("cross-scope")
    staging.save_manifest(_manifest(fh, "body that must not cross scopes"))
    outcome = cross.last_outcome
    assert outcome.status == STATUS_DEGRADED
    assert outcome.reason == DEGRADED_SCOPE_MISMATCH
    assert world["service"].map_path(fh).is_file()
    assert foreign.ledger_shape()["total_versions"] == 0
    assert _versions(world, fh) == ()  # the local ledger never saw it
    foreign.close()


def test_seam_construction_rejects_non_ledger_and_bad_scope(world):
    with pytest.raises(TypeError):
        OnlineMappingRegistrar(
            installation_id=INSTALL, company_id=COMPANY, ledger="nope")
    with pytest.raises(ValueError):
        OnlineMappingRegistrar(
            installation_id="", company_id=COMPANY,
            ledger=world["ledger"])


# -- update_payload / _edit live paths (O1/O5) --------------------------------

def test_update_payload_metadata_replays_but_new_bytes_version(world):
    fh = _source("payload")
    staging_id = world["staging"].save_manifest(
        _manifest(fh, "original body"))
    world["staging"].update_payload(staging_id, {"routed_to": "acme"})
    assert world["registrar"].last_outcome.status == "replayed"
    new_markdown = "# replaced inline markdown\n\nnew content\n"
    world["staging"].update_payload(
        staging_id, {"document": {"markdown": new_markdown}})
    outcome = world["registrar"].last_outcome
    assert outcome.status == "registered" and outcome.version_no == 2
    assert world["service"].map_path(fh).read_bytes() == \
        new_markdown.encode("utf-8")
    world["staging"].update_payload(
        staging_id, {"document": {"markdown": new_markdown}})
    assert world["registrar"].last_outcome.status == "replayed"
    assert [v.version_no for v in _versions(world, fh)] == [1, 2]


def test_edits_replay_shared_projection_and_changed_manifest_versions(world):
    fh = _admit_source(world, "edits")
    world["staging"].save_manifest(_manifest(fh, "editable body"))
    world["service"].replace(
        file_hash=fh, company_id="acme",
        target_locator="page=1; locator=L10",
        replacement_text="订正", confirm=True)
    assert world["registrar"].last_outcome.status == "replayed"
    world["service"].remove(
        file_hash=fh, company_id="acme",
        target_locator="page=1; locator=L10", confirm=True)
    assert world["registrar"].last_outcome.status == "replayed"
    world["staging"].save_manifest(_manifest(fh, "genuinely new bytes"))
    assert world["registrar"].last_outcome.status == "registered"
    assert [v.version_no for v in _versions(world, fh)] == [1, 2]


# -- §4.10 / O10 zero network, import graph -----------------------------------

def test_full_parse_and_register_flow_with_sockets_sealed(world,
                                                          monkeypatch):
    class SealedSocket(socket.socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("online registration must not connect")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("online registration must not connect")

        def sendto(self, *args, **kwargs):
            raise AssertionError("online registration must not send")

    monkeypatch.setattr(socket, "socket", SealedSocket)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: pytest.fail("no connections"))
    world["staging"].save_manifest(_manifest(_source("net1"), "禁网正文甲"))
    world["staging"].save_manifest(_manifest(_source("net1"), "禁网正文甲"))
    world["staging"].save_manifest(_manifest(_source("net2"), "禁网正文乙"))
    shape = world["ledger"].ledger_shape()
    assert shape["total_versions"] == 2
    for source in shape["sources"]:
        records = world["ledger"].list_versions(
            source["logical_source_id"],
            installation_id=INSTALL, company_id=COMPANY)
        assert all(r.byte_status == BYTE_PRESENT for r in records)


def test_live_modules_never_import_the_ledger_only_hold_the_seam():
    repo_root = Path(__file__).resolve().parents[1]

    def imported_modules(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        package_parts = list(
            path.parent.relative_to(repo_root / "app").parts)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    base = (["app"]
                            + package_parts[:len(package_parts) - node.level
                                           + 1])
                    if module:
                        names.add(".".join(base + [module]))
                elif module:
                    names.add(module)
        return names

    live = {
        "storage/mapping_store.py":
            repo_root / "app" / "storage" / "mapping_store.py",
        "harness/staging.py":
            repo_root / "app" / "harness" / "staging.py",
    }
    for label, path in live.items():
        names = imported_modules(path)
        assert not any(
            name == "app.facts_tree"
            or name.startswith("app.facts_tree.") for name in names), label
    seam_names = imported_modules(
        repo_root / "app" / "facts_tree" / "mapping_version_online.py")
    assert not any(
        name.startswith("app.storage") or name.startswith("app.harness")
        or name.startswith("app.employees") for name in seam_names)


# -- §4.9 explicit sha256 history repository ----------------------------------

def test_sha256_history_oid_is_registered(tmp_path):
    data = assemble_world(tmp_path / "world256", object_format="sha256")
    try:
        fh = _source("sha256-h")
        data["staging"].save_manifest(_manifest(fh, "sha256 body"))
        version = data["ledger"].get_version(
            fh, 1, installation_id=INSTALL, company_id=COMPANY)
        assert len(version.h_commit_oid) == 64
        assert version.h_commit_oid == data["repository"].head_oid()
    finally:
        data["ledger"].close()
