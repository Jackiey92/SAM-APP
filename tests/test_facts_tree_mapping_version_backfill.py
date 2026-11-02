"""R2 slice 2 offline backfill reconciler contracts.

Real parse_staging history rows (synthetic, tmp_path only), the real
VersionedMappingObjectStore cabinet, the real render_mapping function
and a real MappingVersionLedger throughout — no success-path or hash
mocks.  Fault injection only models evidence uncertainty (a renderer
that is non-deterministic or raises), a missing cabinet object and a
mid-batch crash.

Coverage of freeze appendix R2 §7 items 5–6 and V4/V5/V9/V10:
  5 three evidence grades (inline strong / deterministic replay /
    none), read-only source byte identity, triple-run idempotency,
    all-or-nothing batches under an injected mid-batch crash
  6 uncertain evidence stays a gap and is never registered; orphan
    objects are reported only and never deleted
plus: dry-run planning, side-channel JSON reported but never
registered, backfill coordinates on every row, sealed sockets, and
the V7 no-live-import scan for both new modules.
"""
import ast
import hashlib
import json
import socket
import sqlite3
from pathlib import Path

import pytest

from app.facts_tree.mapping_version_backfill import (
    BackfillReport,
    EVIDENCE_DETERMINISTIC_RENDER,
    EVIDENCE_INLINE_BYTES,
    GAP_FILE_HASH_MISMATCH,
    GAP_MALFORMED_PAYLOAD,
    GAP_OBJECT_MISSING,
    GAP_RENDER_FAILED,
    GAP_RENDER_NONDETERMINISTIC,
    GAP_ROW_NOT_PARSED,
    MappingVersionBackfiller,
)
from app.facts_tree.mapping_version_index import (
    MappingVersionLedger,
    SOURCE_KIND_BACKFILL,
)
from app.storage.mapping_store import render_mapping
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

INSTALL = "inst-backfill"
COMPANY = "company-backfill"


# -- synthetic real-shape history/cabinet/ledger fixtures ---------------------

def _history_ddl(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(
        """CREATE TABLE parse_staging (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT NOT NULL,
            format TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""")
    conn.commit()


def _add_row(conn: sqlite3.Connection, file_hash: str, manifest, *,
             status: str = "parsed", raw_payload: str | None = None) -> int:
    payload = raw_payload if raw_payload is not None \
        else json.dumps(manifest, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO parse_staging(file_hash,format,payload,status,"
        "created_at) VALUES (?,?,?,?,?)",
        (file_hash, "md", payload, status, "2026-10-01T00:00:00+00:00"))
    conn.commit()
    return cur.lastrowid


def _inline_manifest(file_hash: str, markdown: str, **document_extra):
    document = {"markdown": markdown}
    document.update(document_extra)
    return {"file_hash": file_hash, "format": "md",
            "parse_summary": {"status": "parsed"}, "document": document}


def _render_manifest(file_hash: str, text: str):
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


@pytest.fixture
def world(tmp_path):
    cabinet = VersionedMappingObjectStore(tmp_path / "objects")
    vault = tmp_path / "vault"
    vault.mkdir()
    ledger = MappingVersionLedger(
        vault / "mapping-version-index",
        installation_id=INSTALL, company_id=COMPANY,
        object_store=cabinet)
    history_path = tmp_path / "history.db"
    writer = sqlite3.connect(history_path)
    try:
        _history_ddl(writer)
    finally:
        writer.close()

    def backfiller(**kwargs):
        return MappingVersionBackfiller(
            ledger=ledger, object_store=cabinet,
            history_db=history_path, **kwargs)

    def writer_conn():
        conn = sqlite3.connect(history_path)
        conn.row_factory = sqlite3.Row
        return conn

    yield {
        "tmp": tmp_path,
        "cabinet": cabinet,
        "ledger": ledger,
        "history_path": history_path,
        "backfiller": backfiller,
        "writer_conn": writer_conn,
    }
    ledger.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gaps_by_reason(report: BackfillReport) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for gap in report.gaps:
        grouped.setdefault(gap.reason, []).append(gap)
    return grouped


def _ledger_rows(world) -> list[sqlite3.Row]:
    raw = sqlite3.connect(world["ledger"].database_path)
    raw.row_factory = sqlite3.Row
    try:
        return list(raw.execute(
            "SELECT * FROM fact_mapping_version_registrations "
            "ORDER BY logical_source_id, version_no"))
    finally:
        raw.close()


# -- §7.5 evidence grades -----------------------------------------------------

def test_three_evidence_grades_strong_replay_none(world):
    # ① strong: inline document.markdown, exact bytes present in cabinet.
    strong_md = "# strong source\n\ninline historical bytes\n"
    # ② replay: no markdown, deterministic render whose bytes are in cabinet.
    replay_manifest = _render_manifest("b" * 64, "replayed body text")
    replay_bytes = render_mapping(replay_manifest).encode("utf-8")
    # ③ no evidence: inline bytes that were never content-addressed.
    ghost_md = "# ghost source\n\nbytes nowhere in cabinet\n"
    failed_md = "# failed parse row\n"
    malformed_md = '{"file_hash":'

    cabinet = world["cabinet"]
    strong_digest = cabinet.put(strong_md.encode("utf-8"))
    replay_digest = cabinet.put(replay_bytes)
    orphan_digest = cabinet.put(b"nobody references me")

    with world["writer_conn"]() as writer:
        id_strong = _add_row(writer, "a" * 64,
                             _inline_manifest("a" * 64, strong_md))
        id_replay = _add_row(writer, "b" * 64, replay_manifest)
        id_ghost = _add_row(writer, "c" * 64,
                            _inline_manifest("c" * 64, ghost_md))
        id_failed = _add_row(
            writer, "d" * 64,
            _inline_manifest("d" * 64, failed_md), status="parse_failed")
        id_malformed = _add_row(
            writer, "e" * 64, None, raw_payload=malformed_md)
        id_mismatch = _add_row(
            writer, "f" * 64,
            _inline_manifest("0" * 64, "mismatch"))

    report = world["backfiller"]().run()

    # ① and ② registered, each as its source's version 1, evidence labeled.
    by_digest = {r.digest: r for r in report.registered}
    assert set(by_digest) == {strong_digest, replay_digest}
    strong = by_digest[strong_digest]
    replay = by_digest[replay_digest]
    assert strong.evidence_kind == EVIDENCE_INLINE_BYTES
    assert replay.evidence_kind == EVIDENCE_DETERMINISTIC_RENDER
    assert strong.version_no == replay.version_no == 1
    assert strong.source_coordinate == f"parse_staging#id={id_strong}"
    assert replay.source_coordinate == f"parse_staging#id={id_replay}"
    assert report.replayed == ()
    assert report.scanned_rows == 6

    # ③ all four no-evidence shapes are explicit gaps, none registered.
    gaps = _gaps_by_reason(report)
    assert set(gaps) == {
        GAP_OBJECT_MISSING, GAP_ROW_NOT_PARSED,
        GAP_MALFORMED_PAYLOAD, GAP_FILE_HASH_MISMATCH}
    ghost_gap = gaps[GAP_OBJECT_MISSING][0]
    assert ghost_gap.source_coordinate == f"parse_staging#id={id_ghost}"
    assert ghost_gap.logical_source_id == "c" * 64
    assert ghost_gap.digest == _sha256(ghost_md.encode("utf-8"))
    assert ghost_gap.size_bytes == len(ghost_md.encode("utf-8"))
    assert ghost_gap.evidence_kind == EVIDENCE_INLINE_BYTES
    assert gaps[GAP_ROW_NOT_PARSED][0].source_coordinate == \
        f"parse_staging#id={id_failed}"
    assert gaps[GAP_MALFORMED_PAYLOAD][0].source_coordinate == \
        f"parse_staging#id={id_malformed}"
    assert gaps[GAP_FILE_HASH_MISMATCH][0].source_coordinate == \
        f"parse_staging#id={id_mismatch}"

    # Only the unrelated cabinet object is an orphan.
    assert [o.digest for o in report.orphans] == [orphan_digest]

    # Report is plain JSON-serializable data.
    json.loads(json.dumps(report.as_dict(), ensure_ascii=False))


# -- §7.5 source is read-only -------------------------------------------------

def test_history_database_is_read_only_and_byte_identical(world):
    history_path = world["history_path"]
    md = "# inline\n\nsource bytes unchanged\n"
    world["cabinet"].put(md.encode("utf-8"))
    with world["writer_conn"]() as writer:
        _add_row(writer, "a" * 64, _inline_manifest("a" * 64, md))
    before = history_path.read_bytes()
    before_sha = _sha256(before)

    report = world["backfiller"]().run()
    assert len(report.registered) == 1

    after_sha = _sha256(history_path.read_bytes())
    assert after_sha == before_sha

    # The tool's own mode=ro contract: no write can succeed on it.
    ro = sqlite3.connect(Path(history_path).as_uri() + "?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO parse_staging(file_hash,format,payload,status,"
                "created_at) VALUES ('z','md','{}','parsed','t')")
    finally:
        ro.close()


# -- §7.5 V5 idempotent repeat runs -------------------------------------------

def test_three_runs_converge_identically_with_stable_numbers(world):
    cabinet = world["cabinet"]
    source_x = "x" * 64
    source_y = "y" * 64
    md_x1, md_x2, md_x1_again = "X version one\n", "X version two\n", \
        "X version one\n"  # identical re-parse of x1 bytes
    y_manifest_1 = _render_manifest(source_y, "Y body first parse")
    y_manifest_2 = _render_manifest(source_y, "Y body second parse")
    y1 = render_mapping(y_manifest_1).encode("utf-8")
    y2 = render_mapping(y_manifest_2).encode("utf-8")
    x1_d = cabinet.put(md_x1.encode())
    x2_d = cabinet.put(md_x2.encode())
    y1_d = cabinet.put(y1)
    y2_d = cabinet.put(y2)

    # Interleave sources by staging id; x's third parse repeats x1 bytes.
    with world["writer_conn"]() as writer:
        coords = [
            _add_row(writer, source_x,
                     _inline_manifest(source_x, md_x1)),
            _add_row(writer, source_y, y_manifest_1),
            _add_row(writer, source_x,
                     _inline_manifest(source_x, md_x2)),
            _add_row(writer, source_y, y_manifest_2),
            _add_row(writer, source_x,
                     _inline_manifest(source_x, md_x1_again)),
        ]

    def snapshot():
        return [(r["logical_source_id"], r["version_no"], r["digest"],
                 r["source_coordinate"], r["source_kind"])
                for r in _ledger_rows(world)]

    run1 = world["backfiller"]().run()
    assert {r.digest for r in run1.registered} == {x1_d, x2_d, y1_d, y2_d}
    numbers1 = snapshot()
    shape1 = world["ledger"].ledger_shape(verify=False)
    digest1 = world["ledger"].ledger_digest()

    # Per-source numbering follows historical staging-id order; the
    # repeated x1 parse is a replay of version 1, not a third version.
    x_rows = [r for r in numbers1 if r[0] == source_x]
    y_rows = [r for r in numbers1 if r[0] == source_y]
    assert [(r[1], r[2]) for r in x_rows] == [(1, x1_d), (2, x2_d)]
    assert [(r[1], r[2]) for r in y_rows] == [(1, y1_d), (2, y2_d)]

    run2 = world["backfiller"]().run()
    run3 = world["backfiller"]().run()
    for report in (run2, run3):
        assert report.registered == ()
        assert {r.digest for r in report.replayed} == \
            {x1_d, x2_d, y1_d, y2_d}
        # The repeated coordinate replays version 1 (idempotent coordinate
        # bookkeeping does not renumber anything).
        repeated = [r for r in report.replayed
                    if r.source_coordinate == f"parse_staging#id={coords[4]}"]
        assert len(repeated) == 1 and repeated[0].version_no == 1
        assert report.gaps == run1.gaps
        assert report.orphans == run1.orphans
    assert snapshot() == numbers1
    assert world["ledger"].ledger_shape(verify=False) == shape1
    assert world["ledger"].ledger_digest() == digest1
    assert len(_ledger_rows(world)) == 4  # never a duplicate row


# -- §7.5 V9 all-or-nothing batches -------------------------------------------

def test_mid_batch_crash_leaves_no_half_batch_and_rerun_converges(world):
    cabinet = world["cabinet"]
    digests = []
    for i in range(4):
        md = f"batch body number {i}\n"
        digests.append(cabinet.put(md.encode("utf-8")))
        with world["writer_conn"]() as writer:
            _add_row(writer, f"{i:064d}",
                     _inline_manifest(f"{i:064d}", md))

    calls = {"n": 0}

    def crash_on_second_batch(candidate):
        calls["n"] += 1
        if calls["n"] == 3:  # first candidate of the second batch
            raise RuntimeError("simulated crash mid batch")

    with pytest.raises(RuntimeError, match="simulated crash"):
        world["backfiller"](batch_size=2).run(
            on_candidate=crash_on_second_batch)

    rows = _ledger_rows(world)
    # First batch fully committed; second batch left nothing behind.
    assert {r["digest"] for r in rows} == set(digests[:2])
    assert len(rows) == 2
    assert all(r["version_no"] == 1 for r in rows)

    # Repeat without the fault: earlier batch replays, rest registers;
    # final set is complete with no duplicate or gap.
    report = world["backfiller"](batch_size=2).run()
    assert {r.digest for r in report.registered} == set(digests[2:])
    assert {r.digest for r in report.replayed} == set(digests[:2])
    final = _ledger_rows(world)
    assert len(final) == 4
    assert {r["digest"] for r in final} == set(digests)
    assert {(r["logical_source_id"], r["version_no"]) for r in final} == \
        {(f"{i:064d}", 1) for i in range(4)}


# -- §7.6 uncertainty is a gap, never a guess ---------------------------------

def test_nondeterministic_renderer_is_gap_not_a_guess(world):
    manifest = _render_manifest("c" * 64, "uncertain text")
    with world["writer_conn"]() as writer:
        _add_row(writer, "c" * 64, manifest)

    counter = {"n": 0}

    def flaky(_manifest):
        counter["n"] += 1
        return f"render attempt {counter['n']} — clock-dependent\n"

    report = world["backfiller"](renderer=flaky).run()
    reasons = _gaps_by_reason(report)
    assert set(reasons) == {GAP_RENDER_NONDETERMINISTIC}
    gap = reasons[GAP_RENDER_NONDETERMINISTIC][0]
    assert gap.logical_source_id == "c" * 64
    assert gap.evidence_kind == EVIDENCE_DETERMINISTIC_RENDER
    assert report.registered == report.replayed == ()
    assert _ledger_rows(world) == []


def test_renderer_that_raises_is_render_failed_gap(world):
    manifest = _render_manifest("d" * 64, "boom text")
    with world["writer_conn"]() as writer:
        _add_row(writer, "d" * 64, manifest)

    def broken(_manifest):
        raise RuntimeError("renderer exploded")

    report = world["backfiller"](renderer=broken).run()
    reasons = _gaps_by_reason(report)
    assert set(reasons) == {GAP_RENDER_FAILED}
    assert "renderer exploded" in reasons[GAP_RENDER_FAILED][0].detail
    assert _ledger_rows(world) == []


def test_real_renderer_is_deterministic_twice_on_same_manifest():
    # The production render itself: no wall-clock/random/environment input,
    # and identical bytes on repeated calls with the same manifest.
    manifest = _render_manifest("e" * 64, "deterministic body")
    first = render_mapping(manifest)
    second = render_mapping(manifest)
    assert first == second
    assert isinstance(first, str) and first.endswith("\n")


# -- §7.6 orphans are report-only ----------------------------------------------

def test_orphan_reported_twice_and_never_deleted(world):
    cabinet = world["cabinet"]
    orphan_digest = cabinet.put(b"orphan bytes live forever")
    md = "# inline\n\nregistered bytes\n"
    registered_digest = cabinet.put(md.encode())
    with world["writer_conn"]() as writer:
        _add_row(writer, "a" * 64, _inline_manifest("a" * 64, md))

    r1 = world["backfiller"]().run()
    r2 = world["backfiller"]().run()
    for report in (r1, r2):
        assert [o.digest for o in report.orphans] == [orphan_digest]
        assert report.orphans[0].size_bytes == len(b"orphan bytes live forever")
    # Still physically present and cabinet-readable after two runs.
    assert cabinet.get(orphan_digest) == b"orphan bytes live forever"

    # Once a historical row evidences the bytes, it leaves the orphan list
    # and becomes a normal registration.
    with world["writer_conn"]() as writer:
        _add_row(writer, "b" * 64,
                 _inline_manifest("b" * 64,
                                  "orphan bytes live forever"))
    r3 = world["backfiller"]().run()
    assert r3.orphans == ()
    hit = [r for r in r3.registered if r.digest == orphan_digest]
    assert len(hit) == 1 and hit[0].logical_source_id == "b" * 64
    assert registered_digest  # the other object stays registered too


# -- dry run -------------------------------------------------------------------

def test_dry_run_plans_everything_but_writes_nothing(world):
    cabinet = world["cabinet"]
    md_present = "# dry\n\npresent\n"
    md_missing = "# dry\n\nmissing from cabinet\n"
    cabinet.put(md_present.encode())
    with world["writer_conn"]() as writer:
        _add_row(writer, "a" * 64,
                 _inline_manifest("a" * 64, md_present))
        _add_row(writer, "b" * 64,
                 _inline_manifest("b" * 64, md_missing))

    dry = world["backfiller"]().run(dry_run=True)
    assert dry.dry_run is True
    assert dry.registered == dry.replayed == ()
    assert len(dry.would_register) == 1
    planned = dry.would_register[0]
    assert planned.logical_source_id == "a" * 64
    assert planned.evidence_kind == EVIDENCE_INLINE_BYTES
    assert planned.digest == _sha256(md_present.encode())
    assert world["ledger"].ledger_shape(verify=False)["total_versions"] == 0
    assert {g.reason for g in dry.gaps} == {GAP_OBJECT_MISSING}

    # A real run after the dry run registers exactly the planned object.
    real = world["backfiller"]().run()
    assert [r.digest for r in real.registered] == [planned.digest]
    assert real.gaps == dry.gaps
    assert real.orphans == dry.orphans


# -- side channels: reported, never registered ---------------------------------

def test_side_channel_json_reported_but_never_registered(world):
    cabinet = world["cabinet"]
    md = "# map with side channels\n"
    side_bytes = json.dumps({"nodes": [1, 2, 3]}).encode()
    cabinet.put(md.encode())
    side_digest = cabinet.put(side_bytes)  # unreferenced by any row
    manifest = _inline_manifest(
        "a" * 64, md,
        document_map={"nodes": []}, assets=[{"name": "x"}],
        hyperlinks=["http://example.invalid"], self_check={"ok": True})
    manifest["document_quality"] = {"score": 1.0}
    manifest["engine_document"] = {"engine": "test"}
    with world["writer_conn"]() as writer:
        _add_row(writer, "a" * 64, manifest)

    report = world["backfiller"]().run()
    assert len(report.side_channels) == 1
    note = report.side_channels[0]
    assert note.keys == (
        "document_map", "assets", "hyperlinks", "self_check",
        "document_quality", "engine_document")

    # Only the mapping markdown is registered; side-channel bytes in the
    # cabinet are orphans, never mapping-version registrations.
    registered_digests = {r.digest for r in report.registered}
    assert registered_digests == {_sha256(md.encode())}
    assert side_digest in {o.digest for o in report.orphans}
    rows = _ledger_rows(world)
    assert all(r["object_type"] == "mapping_version" for r in rows)
    assert all(r["storage_class"] == "mapping_objects" for r in rows)


# -- the ledger is the only write entry, coordinates recorded ------------------

def test_every_row_is_backfill_kind_with_history_coordinate(world):
    for i, md in enumerate((f"body {i}\n" for i in range(3))):
        world["cabinet"].put(md.encode())
        with world["writer_conn"]() as writer:
            coordinate_id = _add_row(
                writer, f"{i:064d}",
                _inline_manifest(f"{i:064d}", md))
    report = world["backfiller"]().run()
    assert len(report.registered) == 3
    rows = _ledger_rows(world)
    assert len(rows) == 3
    for row in rows:
        assert row["source_kind"] == SOURCE_KIND_BACKFILL
        assert row["source_coordinate"].startswith("parse_staging#id=")
        assert row["h_commit_oid"] is None
        assert row["installation_id"] == INSTALL
        assert row["company_id"] == COMPANY
    assert {r.source_coordinate for r in report.registered} == \
        {r["source_coordinate"] for r in rows}
    # The coordinate points at a real historical row id.
    with world["writer_conn"]() as writer:
        for row in rows:
            row_id = int(row["source_coordinate"].rsplit("=", 1)[1])
            assert writer.execute(
                "SELECT 1 FROM parse_staging WHERE id=?",
                (row_id,)).fetchone() is not None


# -- §7.10 network sealed ------------------------------------------------------

def test_backfill_full_flow_succeeds_with_sockets_sealed(world, monkeypatch):
    class SealedSocket(socket.socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("backfiller must not open connections")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("backfiller must not open connections")

        def sendto(self, *args, **kwargs):
            raise AssertionError("backfiller must not send packets")

    monkeypatch.setattr(socket, "socket", SealedSocket)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: pytest.fail("no connections"))

    md = "# offline\n\nall local evidence\n"
    replay_manifest = _render_manifest("b" * 64, "offline replay text")
    world["cabinet"].put(md.encode())
    world["cabinet"].put(render_mapping(replay_manifest).encode())
    with world["writer_conn"]() as writer:
        _add_row(writer, "a" * 64, _inline_manifest("a" * 64, md))
        _add_row(writer, "b" * 64, replay_manifest)

    report = world["backfiller"]().run()
    assert len(report.registered) == 2
    assert report.gaps == ()


# -- §7.8 V7: no live path imports either new module ---------------------------

def test_v7_no_live_path_imports_ledger_or_backfiller():
    repo_root = Path(__file__).resolve().parents[1]
    app_root = repo_root / "app"
    # The facts-tree offline/library siblings may import each other:
    # the ledger, the R2 backfiller, the A1 backup assembler (read-only
    # offline consumer mandated by the A1 freeze brief), the R3
    # online registration seam, and the G1 read-only retention planner.
    # Everything else (webapp, harness, employees, broker, storage, …)
    # may not.
    offline_siblings = {
        "mapping_version_index.py",
        "mapping_version_backfill.py",
        "mapping_version_online.py",
        "backup_assembly.py",
        "object_retention_plan.py",
    }
    watched = ("app.facts_tree.mapping_version_index",
               "app.facts_tree.mapping_version_backfill")

    def resolved_imports(path, node):
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        module = node.module or ""
        if node.level:
            package = ["app"] + list(
                path.parent.relative_to(app_root).parts)
            base = package[:len(package) - node.level + 1]
            return [".".join(base + ([module] if module else []))]
        return [module] if module else []

    offenders = []
    for path in app_root.rglob("*.py"):
        if path.name in offline_siblings:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for name in resolved_imports(path, node):
                if any(name == target or name.startswith(target + ".")
                       for target in watched):
                    offenders.append(f"{path}: {name}")
    assert offenders == []
