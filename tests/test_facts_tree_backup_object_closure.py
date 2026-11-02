"""R necessary-immutable-object closure contracts (F-001 appendix R1).

Real-machine drills only: a real temporary 2A root with a real ArchiveFileStore
and its SQLite metadata, a real VersionedMappingObjectStore for exact mapping
bytes, real sha256, real symlinks and a real SQLite W checkpoint. Nothing mocks
the stores' core read/write or the pinned component verifier; the only
monkeypatching seals the network or deliberately corrupts/traverses content.

The ten cases mirror appendix R1 section 4:

1. exact historical mapping v1 bytes are closed (not the newest v2); tamper ->
   corrupt and the whole closure is unpublishable.
2. completeness: original, mapping, attachment, candidate envelope, audit and
   send evidence all close with every required field and traceable roots.
3. a deleted referenced object is an incomplete gap with exact roots; a
   same-named wrong-hash decoy is corrupt, never a substitute.
4. a foreign-company reference stops collection before any byte is read and is
   listed as a scope violation, never admitted.
5. ``../evil`` / absolute coordinates and a symlink pointing outside the root
   are refused without reading out-of-root bytes.
6. three runs plus a second temp location give identical normalized lists and
   R digest regardless of ordering, time or temp path.
7. read-only: the whole 2A tree and the SQLite contents are byte/row identical
   before and after; no pin or file appears.
8. with socket connections sealed, closure collection still completes.
9. an attachment that still lives inline in a real SQLite W checkpoint is
   ``referenced_but_not_externalized`` -> incomplete, no crash, no inline bytes
   promoted into R.
10. the R digest is exactly backup_manifest's canonical rule, no home-grown
    algorithm, and the digest payload carries no timestamp or absolute path.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
from pathlib import Path

import pytest

from app.db import init_db
from app.facts_tree import backup_object_closure as boc
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.backup_object_closure import (
    ClosureScope,
    ClosureStores,
    OBJECT_ATTACHMENT,
    OBJECT_AUDIT_EVIDENCE,
    OBJECT_CANDIDATE_ENVELOPE,
    OBJECT_MAPPING_VERSION,
    OBJECT_ORIGINAL,
    OBJECT_SEND_EVIDENCE,
    ObjectClosureError,
    ObjectReference,
    ObjectScope,
    ReferenceRoot,
    SCOPE_COMPANY,
    STATUS_CORRUPT,
    STATUS_MISSING,
    STATUS_NOT_EXTERNALIZED,
    STATUS_PRESENT,
    STATUS_UNSAFE_PATH,
)
from app.storage.archive_store import ArchiveFileStore
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

INSTALLATION = "inst-r1-acc"
COMPANY = "co-r1-acc"
COMMIT = "0" * 39 + "a"  # 40 lowercase hex, like a real pinned git oid
CHECKPOINT = "w-checkpoint-20261101T090000Z"


def h_root(coordinate: str, *, commit: str = COMMIT) -> ReferenceRoot:
    return ReferenceRoot(kind="H", root_id=commit, coordinate=coordinate)


def w_root(coordinate: str, *, checkpoint: str = CHECKPOINT) -> ReferenceRoot:
    return ReferenceRoot(kind="W", root_id=checkpoint, coordinate=coordinate)


def company_scope(company: str = COMPANY,
                  installation: str = INSTALLATION) -> ObjectScope:
    return ObjectScope(kind=SCOPE_COMPANY, installation_id=installation,
                       company_id=company)


@pytest.fixture
def world(tmp_path):
    """A real isolated 2A installation: SQLite + archive + mapping objects."""
    db = tmp_path / "app.db"
    init_db(db)
    # DELETE journaling keeps honest read-only SELECTs from creating -wal/-shm
    # side files during the zero-side-effect drill.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    root = tmp_path / "2a"
    archive = ArchiveFileStore(root / "bin", db)
    mapping_objects = VersionedMappingObjectStore(
        root / "mapping-objects"
    )
    stores = ClosureStores(archive=archive, mapping_objects=mapping_objects)
    scope = ClosureScope(installation_id=INSTALLATION, company_id=COMPANY)

    def put_blob(data: bytes, *, name: str, zone: str = "internal"
                 ) -> tuple[str, int]:
        stored = archive.put_bytes(
            data, original_name=name, origin_zone=zone
        )
        return stored.file_hash, stored.size_bytes

    def collect(references):
        return boc.collect_object_closure(
            commit_oid=COMMIT,
            authorized_scope=scope,
            references=references,
            stores=stores,
        )

    return {
        "tmp": tmp_path,
        "db": db,
        "root": root,
        "archive": archive,
        "mapping_objects": mapping_objects,
        "stores": stores,
        "scope": scope,
        "put_blob": put_blob,
        "collect": collect,
    }


def _entry(result, logical_id: str):
    matches = [entry for entry in result.entries
               if entry.logical_id == logical_id]
    assert len(matches) == 1
    return matches[0]


def _tree_snapshot(root: Path):
    seen = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            seen.append((rel, "symlink", os.readlink(path)))
        elif path.is_dir():
            seen.append((rel, "dir", None))
        else:
            seen.append((rel, "file",
                         (path.stat().st_size, hashlib.sha256(
                             path.read_bytes()).hexdigest())))
    return seen


def _db_snapshot(db: Path):
    rows = {}
    with sqlite3.connect(db) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            rows[name] = sorted(
                tuple(row) for row in
                conn.execute(f"SELECT * FROM {name}")
            )
    return rows


def test_r01_exact_historical_mapping_v1_closed_tamper_is_corrupt(world):
    """Case 1: H cites old mapping v1; closure keeps v1 bytes, tamper fails."""
    original_hash, original_size = world["put_blob"](
        b"acc-source-bytes", name="source.pdf"
    )
    # Same length, different bytes -> distinct content addresses, like a
    # historical mapping and the regenerated newest one.
    v1 = b"# mapping v1 " + b"a" * 30
    v2 = b"# mapping v2 " + b"b" * 30
    assert len(v1) == len(v2)
    v1_digest = world["mapping_objects"].put(v1)
    v2_digest = world["mapping_objects"].put(v2)
    assert v1_digest != v2_digest

    refs = [
        ObjectReference(
            logical_id=f"source:{original_hash}",
            object_type=OBJECT_ORIGINAL,
            version=original_hash,
            referenced_by=[h_root(f"sources/{original_hash}/versions/1.json")],
            scope=company_scope(),
            sha256_digest=f"sha256:{original_hash}",
        ),
        ObjectReference(
            logical_id=f"mapping:{original_hash}:v1",
            object_type=OBJECT_MAPPING_VERSION,
            version="mapping-revision-1",
            referenced_by=[h_root(
                f"sources/{original_hash}/versions/1.json",
            )],
            scope=company_scope(),
            sha256_digest=f"sha256:{v1_digest}",
        ),
    ]

    result = world["collect"](refs)
    assert result.status == "complete"
    assert result.publishable is True
    mapping_entry = _entry(result, f"mapping:{original_hash}:v1")
    assert mapping_entry.status == STATUS_PRESENT
    assert mapping_entry.sha256_digest == f"sha256:{v1_digest}"
    assert mapping_entry.size_bytes == len(v1)
    assert mapping_entry.relative_path == f"{v1_digest[:2]}/{v1_digest}"
    # The newest v2 is not what H cited and must not enter the closure.
    pinned = {obj["sha256_digest"] for obj in
              result.retention_pin_request["objects"]}
    assert f"sha256:{v1_digest}" in pinned
    assert f"sha256:{v2_digest}" not in pinned
    assert result.retention_pin_request["created_pin"] is False

    # Tamper the exact historical bytes (same length -> digest mismatch,
    # not just a size mismatch).
    v1_path = (world["root"] / "mapping-objects"
               / v1_digest[:2] / v1_digest)
    tampered_bytes = b"# TAMPERED" + b"x" * (len(v1) - len(b"# TAMPERED"))
    assert len(tampered_bytes) == len(v1)
    v1_path.write_bytes(tampered_bytes)
    tampered = world["collect"](refs)
    assert tampered.status == "corrupt"
    assert tampered.publishable is False
    bad = _entry(tampered, f"mapping:{original_hash}:v1")
    assert bad.status == STATUS_CORRUPT
    assert bad.issue == "sha256_mismatch"
    assert bad.sha256_digest == f"sha256:{v1_digest}"
    assert bad.referenced_by[0].kind == "H"
    assert bad.referenced_by[0].root_id == COMMIT
    # Restore not needed: each test gets a fresh world.


def test_r02_closure_completeness_fields_and_traceable_roots(world):
    """Case 2: all five object families close with every required field."""
    original_hash, original_size = world["put_blob"](
        b"original-bytes", name="o.pdf"
    )
    attachment_hash, attachment_size = world["put_blob"](
        b"attachment-zip-bytes", name="a.zip"
    )
    envelope_hash, envelope_size = world["put_blob"](
        b"sensitive-candidate-envelope", name="env.json", zone="candidate"
    )
    audit_hash, audit_size = world["put_blob"](
        b"audit-evidence-bytes", name="audit.json", zone="audit"
    )
    send_hash, send_size = world["put_blob"](
        b"send-receipt-bytes", name="send.json", zone="audit"
    )
    mapping = b"# exact mapping bytes for completeness case"
    mapping_digest = world["mapping_objects"].put(mapping)

    h_source = h_root(f"sources/{original_hash}/versions/1.json")
    refs = [
        # The original is cited by BOTH the H record and a W row: roots merge.
        ObjectReference(
            logical_id=f"source:{original_hash}",
            object_type=OBJECT_ORIGINAL,
            version=original_hash,
            referenced_by=[
                h_source,
                w_root("parse_jobs/41"),
            ],
            scope=company_scope(),
            sha256_digest=f"sha256:{original_hash}",
        ),
        ObjectReference(
            logical_id=f"mapping:{original_hash}:v1",
            object_type=OBJECT_MAPPING_VERSION,
            version="mapping-revision-1",
            referenced_by=[h_source],
            scope=company_scope(),
            sha256_digest=f"sha256:{mapping_digest}",
        ),
        ObjectReference(
            logical_id="attachment:att-1",
            object_type=OBJECT_ATTACHMENT,
            version="attachment-rev-1",
            referenced_by=[w_root("candidate_revisions/7/attachments")],
            scope=company_scope(),
            sha256_digest=f"sha256:{attachment_hash}",
        ),
        ObjectReference(
            logical_id="candidate-envelope:env-9",
            object_type=OBJECT_CANDIDATE_ENVELOPE,
            version="candidate-revision-9",
            referenced_by=[w_root("candidate_submissions/9/envelope")],
            scope=company_scope(),
            sha256_digest=f"sha256:{envelope_hash}",
            key_reference="kek:inst-r1-acc:key-2026-01",
        ),
        ObjectReference(
            logical_id="audit-evidence:evt-3",
            object_type=OBJECT_AUDIT_EVIDENCE,
            version="audit-event-3",
            referenced_by=[w_root("audit_log/13")],
            scope=company_scope(),
            sha256_digest=f"sha256:{audit_hash}",
        ),
        ObjectReference(
            logical_id="send-evidence:op-5",
            object_type=OBJECT_SEND_EVIDENCE,
            version="dispatch-op-5",
            referenced_by=[w_root("dispatch_results/op-5")],
            scope=company_scope(),
            sha256_digest=f"sha256:{send_hash}",
        ),
    ]

    result = world["collect"](refs)
    assert result.status == "complete"
    assert result.publishable is True
    assert len(result.entries) == 6

    original = _entry(result, f"source:{original_hash}")
    assert original.object_type == OBJECT_ORIGINAL
    assert original.sha256_digest == f"sha256:{original_hash}"
    assert original.size_bytes == original_size
    assert original.scope.kind == SCOPE_COMPANY
    assert original.scope.installation_id == INSTALLATION
    assert original.scope.company_id == COMPANY
    assert original.version == original_hash
    assert original.key_reference is None
    assert original.storage_class == "archive"
    assert original.relative_path == f"{original_hash[:2]}/{original_hash}"
    roots = {(r.kind, r.root_id, r.coordinate)
             for r in original.referenced_by}
    assert roots == {
        ("H", COMMIT, f"sources/{original_hash}/versions/1.json"),
        ("W", CHECKPOINT, "parse_jobs/41"),
    }

    envelope = _entry(result, "candidate-envelope:env-9")
    assert envelope.key_reference == "kek:inst-r1-acc:key-2026-01"
    assert envelope.size_bytes == envelope_size

    attachment = _entry(result, "attachment:att-1")
    assert attachment.sha256_digest == f"sha256:{attachment_hash}"
    assert attachment.size_bytes == attachment_size

    # Repeating the same citation never duplicates the reference root.
    again = world["collect"](refs + [
        ObjectReference(
            logical_id=f"source:{original_hash}",
            object_type=OBJECT_ORIGINAL,
            version=original_hash,
            referenced_by=[
                h_root(f"sources/{original_hash}/versions/1.json"),
            ],
            scope=company_scope(),
            sha256_digest=f"sha256:{original_hash}",
        ),
    ])
    assert len(again.entries) == 6
    assert len(_entry(again, f"source:{original_hash}").referenced_by) == 2

    # Every present object is in the pin description; nothing is pinned.
    pin = result.retention_pin_request
    assert pin["kind"] == "retention_pin_description"
    assert len(pin["objects"]) == 6
    pinned_digests = {o["sha256_digest"] for o in pin["objects"]}
    assert pinned_digests == {
        f"sha256:{h}" for h in (
            original_hash, mapping_digest, attachment_hash, envelope_hash,
            audit_hash, send_hash,
        )
    }
    assert all(o["size_bytes"] >= 0 for o in pin["objects"])


def test_r03_missing_gap_listed_and_same_name_decoy_is_corrupt(world):
    """Case 3: deletion -> precise gap; decoy bytes -> corrupt, no substitute."""
    keep_hash, _ = world["put_blob"](b"keep-bytes", name="keep.pdf")
    gone_hash, _ = world["put_blob"](b"gone-bytes-that-are-longer",
                                    name="gone.pdf")
    gone_ref = ObjectReference(
        logical_id=f"source:{gone_hash}",
        object_type=OBJECT_ORIGINAL,
        version=gone_hash,
        referenced_by=[h_root(f"sources/{gone_hash}/versions/2.json")],
        scope=company_scope(),
        sha256_digest=f"sha256:{gone_hash}",
    )
    refs = [
        ObjectReference(
            logical_id=f"source:{keep_hash}",
            object_type=OBJECT_ORIGINAL,
            version=keep_hash,
            referenced_by=[h_root("sources/keep/versions/1.json")],
            scope=company_scope(),
            sha256_digest=f"sha256:{keep_hash}",
        ),
        gone_ref,
    ]

    # Really delete the referenced object from the content-addressed tree
    # (its DB metadata row remains): the declared address is absent.
    declared_path = (world["root"] / "bin"
                     / gone_hash[:2] / gone_hash)
    declared_path.unlink()

    # A decoy landing under the decoy's own shard changes nothing: the
    # declared address is still absent, and a different hash can never fill
    # the gap.
    world["archive"].put_bytes(b"decoy-different-content-at-other-address",
                               original_name="gone.pdf")
    assert not declared_path.exists()
    result = world["collect"](refs)
    assert result.status == "incomplete"
    assert result.publishable is False
    gap = _entry(result, f"source:{gone_hash}")
    assert gap.status == STATUS_MISSING
    assert gap.issue == "object_missing"
    assert gap.size_bytes is None
    assert gap.referenced_by[0].coordinate == (
        f"sources/{gone_hash}/versions/2.json"
    )
    assert {e.logical_id for e in result.gap_entries} == {
        f"source:{gone_hash}"
    }

    # Now plant same-length wrong bytes under the exact declared name:
    # hash mismatch -> corrupt, still unpublishable, still the old logical id.
    declared_path = (world["root"] / "bin"
                     / gone_hash[:2] / gone_hash)
    decoy_same_length = b"x" * len(b"gone-bytes-that-are-longer")
    declared_path.parent.mkdir(parents=True, exist_ok=True)
    declared_path.write_bytes(decoy_same_length)
    decoy_result = world["collect"](refs)
    assert decoy_result.status == "corrupt"
    assert decoy_result.publishable is False
    bad = _entry(decoy_result, f"source:{gone_hash}")
    assert bad.status == STATUS_CORRUPT
    assert bad.issue == "sha256_mismatch"
    assert bad.sha256_digest == f"sha256:{gone_hash}"
    decoy_hash = hashlib.sha256(decoy_same_length).hexdigest()
    assert decoy_hash != gone_hash
    assert all(e.sha256_digest != f"sha256:{decoy_hash}"
               for e in decoy_result.entries)


def test_r04_foreign_company_scope_stops_before_any_byte_read(world,
                                                              monkeypatch):
    """Case 4: foreign-company object is refused from references alone."""
    own_hash, _ = world["put_blob"](b"own-company-bytes", name="own.pdf")
    foreign_hash, _ = world["put_blob"](b"OTHER-company-secret",
                                        name="foreign.pdf")

    verified = []
    real_verify = boc.verify_backup_component

    def spying_verify(root_fd, **kwargs):
        verified.append(kwargs["relative_path"])
        return real_verify(root_fd, **kwargs)

    monkeypatch.setattr(boc, "verify_backup_component", spying_verify)

    refs = [
        ObjectReference(
            logical_id=f"source:{own_hash}",
            object_type=OBJECT_ORIGINAL,
            version=own_hash,
            referenced_by=[h_root("sources/own/versions/1.json")],
            scope=company_scope(),
            sha256_digest=f"sha256:{own_hash}",
        ),
        ObjectReference(
            logical_id=f"source:{foreign_hash}",
            object_type=OBJECT_ORIGINAL,
            version=foreign_hash,
            referenced_by=[w_root("foreign_catalog/99")],
            scope=company_scope(company="co-OTHER"),
            sha256_digest=f"sha256:{foreign_hash}",
        ),
        ObjectReference(
            logical_id="installation-doc:inst-other",
            object_type=OBJECT_AUDIT_EVIDENCE,
            version="evt-x",
            referenced_by=[w_root("audit_log/foreign")],
            scope=ObjectScope(
                kind="installation",
                installation_id="inst-OTHER",
            ),
        ),
    ]
    result = world["collect"](refs)
    assert result.status == "scope_violation"
    assert result.publishable is False
    assert result.entries == ()
    assert result.retention_pin_request["objects"] == []
    reasons = {v.logical_id: v.reason for v in result.scope_violations}
    assert reasons[f"source:{foreign_hash}"] == "foreign_company"
    assert reasons["installation-doc:inst-other"] == "foreign_installation"
    # Collection stopped at the reference scan: no store root was opened and
    # not a single component byte was verified, in-scope or foreign.
    assert verified == []


def test_r05_traversal_and_escaping_symlink_refused(world, monkeypatch):
    """Case 5: ../evil, absolute coordinates and out-root symlink refused."""
    target_hash, _ = world["put_blob"](b"real-object-bytes", name="real.pdf")
    evil = world["tmp"] / "evil-outside.bin"
    evil.write_bytes(b"CANARY-BYTES-OUTSIDE-RETENTION-ROOT")

    verified_paths = []
    real_verify = boc.verify_backup_component

    def spying_verify(root_fd, **kwargs):
        verified_paths.append(kwargs["relative_path"])
        return real_verify(root_fd, **kwargs)

    monkeypatch.setattr(boc, "verify_backup_component", spying_verify)

    traversal_ref = ObjectReference(
        logical_id="evidence:traversal",
        object_type=OBJECT_AUDIT_EVIDENCE,
        version="evt-traversal",
        referenced_by=[w_root("audit_log/../evil")],
        scope=company_scope(),
        sha256_digest=f"sha256:{target_hash}",
        relative_path="../evil-outside.bin",
    )
    absolute_ref = ObjectReference(
        logical_id="evidence:absolute",
        object_type=OBJECT_AUDIT_EVIDENCE,
        version="evt-absolute",
        referenced_by=[w_root("audit_log/abs")],
        scope=company_scope(),
        sha256_digest=f"sha256:{target_hash}",
        relative_path=str(evil),
    )
    result = world["collect"]([traversal_ref, absolute_ref])
    assert result.status == "unsafe_paths"
    assert result.publishable is False
    for logical_id in ("evidence:traversal", "evidence:absolute"):
        entry = _entry(result, logical_id)
        assert entry.status == STATUS_UNSAFE_PATH
        assert entry.issue == "path_traversal"
    assert "../evil-outside.bin" not in verified_paths
    assert str(evil) not in verified_paths
    assert all("CANARY" not in p for p in verified_paths)

    # Replace the content-addressed occupant with a symlink leading out of
    # the root: O_NOFOLLOW must refuse it without touching the canary.
    object_path = (world["root"] / "bin"
                   / target_hash[:2] / target_hash)
    object_path.unlink()
    os.symlink(evil, object_path)
    linked = world["collect"]([
        ObjectReference(
            logical_id=f"source:{target_hash}",
            object_type=OBJECT_ORIGINAL,
            version=target_hash,
            referenced_by=[h_root("sources/real/versions/1.json")],
            scope=company_scope(),
            sha256_digest=f"sha256:{target_hash}",
        ),
    ])
    assert linked.status == "unsafe_paths"
    entry = _entry(linked, f"source:{target_hash}")
    assert entry.status == STATUS_UNSAFE_PATH
    assert entry.issue == "symbolic_link"
    assert entry.size_bytes is None
    assert evil.read_bytes() == b"CANARY-BYTES-OUTSIDE-RETENTION-ROOT"
    assert f"{target_hash[:2]}/{target_hash}" in verified_paths


def test_r06_deterministic_across_runs_order_and_temp_path(world, tmp_path):
    """Case 6: three runs and a relocated store copy digest identically."""
    first_hash, _ = world["put_blob"](b"first", name="f.pdf")
    second_hash, _ = world["put_blob"](b"second", name="s.pdf")
    mapping_digest = world["mapping_objects"].put(b"map-bytes-v1")

    def refs():
        return [
            ObjectReference(
                logical_id=f"source:{second_hash}",
                object_type=OBJECT_ORIGINAL,
                version=second_hash,
                referenced_by=[w_root("w/2")],
                scope=company_scope(),
                sha256_digest=f"sha256:{second_hash}",
            ),
            ObjectReference(
                logical_id="mapping:m:v1",
                object_type=OBJECT_MAPPING_VERSION,
                version="mv1",
                referenced_by=[h_root("h/m/1")],
                scope=company_scope(),
                sha256_digest=f"sha256:{mapping_digest}",
            ),
            ObjectReference(
                logical_id=f"source:{first_hash}",
                object_type=OBJECT_ORIGINAL,
                version=first_hash,
                referenced_by=[h_root("h/1"), w_root("w/1")],
                scope=company_scope(),
                sha256_digest=f"sha256:{first_hash}",
            ),
        ]

    ordered = refs()
    runs = [world["collect"](ordered) for _ in range(3)]
    digests = {r.r_list_digest for r in runs}
    assert len(digests) == 1
    payloads = [json.dumps(r.canonical_payload, sort_keys=True,
                           ensure_ascii=False) for r in runs]
    assert len(set(payloads)) == 1
    # Entries keep the deterministic type/id/hash order, not input order.
    expected_order = sorted(
        ["mapping:m:v1", f"source:{first_hash}", f"source:{second_hash}"]
    )
    assert [e.logical_id for e in runs[0].entries] == expected_order

    # Input reordering changes nothing.
    shuffled = [ordered[2], ordered[0], ordered[1]]
    assert world["collect"](shuffled).r_list_digest == runs[0].r_list_digest

    # Relocate the entire installation (db + objects + mapping objects) to
    # another temp path: absolute paths must not enter the digest.
    other_root = tmp_path / "relocated-elsewhere" / "2a"
    shutil.copytree(world["root"], other_root)
    other_db = tmp_path / "relocated-elsewhere" / "app.db"
    shutil.copy2(world["db"], other_db)
    other_archive = ArchiveFileStore(other_root / "bin", other_db)
    other_mappings = VersionedMappingObjectStore(other_root / "mapping-objects")
    other_stores = ClosureStores(
        archive=other_archive, mapping_objects=other_mappings
    )
    relocated = boc.collect_object_closure(
        commit_oid=COMMIT,
        authorized_scope=world["scope"],
        references=ordered,
        stores=other_stores,
    )
    assert relocated.r_list_digest == runs[0].r_list_digest
    assert json.dumps(relocated.canonical_payload, sort_keys=True,
                      ensure_ascii=False) == payloads[0]


def test_r07_read_only_zero_side_effects_on_tree_and_db(world):
    """Case 7: nothing in the object tree or SQLite changes."""
    one_hash, _ = world["put_blob"](b"one", name="one.pdf")
    two_hash, _ = world["put_blob"](b"two-two", name="two.pdf")
    world["mapping_objects"].put(b"mapping-bytes")
    refs = [
        ObjectReference(
            logical_id=f"source:{one_hash}",
            object_type=OBJECT_ORIGINAL,
            version=one_hash,
            referenced_by=[h_root("h/one")],
            scope=company_scope(),
            sha256_digest=f"sha256:{one_hash}",
        ),
        ObjectReference(
            logical_id=f"source:{two_hash}",
            object_type=OBJECT_ORIGINAL,
            version=two_hash,
            referenced_by=[w_root("w/two")],
            scope=company_scope(),
            sha256_digest=f"sha256:{two_hash}",
        ),
    ]

    before_tree = _tree_snapshot(world["root"])
    before_tmp = _tree_snapshot(world["tmp"])
    before_db = _db_snapshot(world["db"])
    db_bytes_before = world["db"].read_bytes()

    result = world["collect"](refs)
    assert result.status == "complete"

    assert _tree_snapshot(world["root"]) == before_tree
    assert _tree_snapshot(world["tmp"]) == before_tmp
    assert _db_snapshot(world["db"]) == before_db
    assert world["db"].read_bytes() == db_bytes_before
    # No pin/lock/checkpoint artifact FILES were created anywhere.
    leftovers = [
        p.relative_to(world["tmp"]).as_posix()
        for p in world["tmp"].rglob("*")
        if p.is_file() and (
            p.name.endswith((".pin", ".lock", ".checkpoint", ".tmp"))
            or p.name.startswith(".object-")
            or p.name.startswith(".original-")
        )
    ]
    assert leftovers == []


def test_r08_completes_with_network_sealed(world, monkeypatch):
    """Case 8: any socket attempt fails hard; collection still finishes."""

    class SealedSocket(socket.socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("closure must not open network connections")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("closure must not open network connections")

        def sendto(self, *args, **kwargs):
            raise AssertionError("closure must not send packets")

    monkeypatch.setattr(socket, "socket", SealedSocket)

    def sealed_create_connection(*args, **kwargs):
        raise AssertionError("closure must not create connections")

    monkeypatch.setattr(socket, "create_connection",
                        sealed_create_connection)

    blob_hash, _ = world["put_blob"](b"offline-bytes", name="off.pdf")
    mapping_digest = world["mapping_objects"].put(b"offline-mapping")
    result = world["collect"]([
        ObjectReference(
            logical_id=f"source:{blob_hash}",
            object_type=OBJECT_ORIGINAL,
            version=blob_hash,
            referenced_by=[h_root("h/off")],
            scope=company_scope(),
            sha256_digest=f"sha256:{blob_hash}",
        ),
        ObjectReference(
            logical_id="mapping:off:v1",
            object_type=OBJECT_MAPPING_VERSION,
            version="mv1",
            referenced_by=[h_root("h/offmap")],
            scope=company_scope(),
            sha256_digest=f"sha256:{mapping_digest}",
        ),
    ])
    assert result.status == "complete"
    assert result.publishable is True


def test_r09_sqlite_inline_attachment_is_not_externalized(world, tmp_path):
    """Case 9: an attachment still only in the real W SQLite DB is a gap."""
    original_hash, _ = world["put_blob"](b"ext-bytes", name="ext.pdf")
    inline_bytes = b"inline-sensitive-attachment-stuck-in-w"

    # A real SQLite W checkpoint database with an inline attachment row:
    # this models what the future W-extraction slice reads; the closure
    # collector itself never opens this file.
    w_db = tmp_path / "w-checkpoint.db"
    with sqlite3.connect(w_db) as conn:
        conn.execute(
            "CREATE TABLE w_attachments("
            "logical_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, "
            "version TEXT NOT NULL, content BLOB NOT NULL, "
            "key_reference TEXT)"
        )
        conn.execute(
            "INSERT INTO w_attachments VALUES (?,?,?,?,?)",
            ("attachment:inline-1", COMPANY, "attachment-rev-1",
             inline_bytes, "kek:inst-r1-acc:key-9"),
        )
        conn.commit()

    # Read-only extraction (as a later assembly slice would do) produces the
    # reference WITHOUT a digest: the bytes are not externalized.
    with sqlite3.connect(f"file:{w_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT logical_id, version, key_reference, length(content) "
            "FROM w_attachments WHERE logical_id=?",
            ("attachment:inline-1",),
        ).fetchone()
    inline_logical_id, inline_version, inline_key, inline_len = row
    assert inline_len == len(inline_bytes)

    refs = [
        ObjectReference(
            logical_id=f"source:{original_hash}",
            object_type=OBJECT_ORIGINAL,
            version=original_hash,
            referenced_by=[h_root("h/ext")],
            scope=company_scope(),
            sha256_digest=f"sha256:{original_hash}",
        ),
        ObjectReference(
            logical_id=inline_logical_id,
            object_type=OBJECT_ATTACHMENT,
            version=inline_version,
            referenced_by=[w_root("w_attachments/attachment:inline-1")],
            scope=company_scope(),
            key_reference=inline_key,
        ),
    ]
    result = world["collect"](refs)
    assert result.status == "incomplete"
    assert result.publishable is False
    inline_entry = _entry(result, "attachment:inline-1")
    assert inline_entry.status == STATUS_NOT_EXTERNALIZED
    assert inline_entry.sha256_digest is None
    assert inline_entry.size_bytes is None
    assert inline_entry.storage_class is None
    assert inline_entry.relative_path is None
    assert inline_entry.key_reference == "kek:inst-r1-acc:key-9"
    assert {e.logical_id for e in result.gap_entries} == {
        "attachment:inline-1"
    }
    # The inline bytes are never promoted: no present entry claims their
    # hash, and the pin description omits them.
    inline_digest = "sha256:" + hashlib.sha256(inline_bytes).hexdigest()
    assert all(e.sha256_digest != inline_digest for e in result.entries)
    assert all(obj["sha256_digest"] != inline_digest
               for obj in result.retention_pin_request["objects"])

    # The gap is digest-bound: dropping the reference changes the R digest.
    without_gap = world["collect"](refs[:1])
    assert without_gap.status == "complete"
    assert without_gap.r_list_digest != result.r_list_digest


def test_r10_r_list_digest_uses_backup_manifest_rule(world):
    """Case 10: R digest == compute_core_digest(payload), no new algorithm."""
    blob_hash, _ = world["put_blob"](b"digest-rule-bytes", name="d.pdf")
    refs = [
        ObjectReference(
            logical_id=f"source:{blob_hash}",
            object_type=OBJECT_ORIGINAL,
            version=blob_hash,
            referenced_by=[h_root("h/d")],
            scope=company_scope(),
            sha256_digest=f"sha256:{blob_hash}",
        ),
    ]
    result = world["collect"](refs)

    # The exact manifest-mouth digest, recomputed independently.
    assert result.r_list_digest == compute_core_digest(
        result.canonical_payload
    )
    assert result.r_list_digest.startswith("sha256:")
    assert len(result.r_list_digest) == 7 + 64

    serialized = json.dumps(result.canonical_payload,
                            ensure_ascii=False, sort_keys=True)
    # No volatile/absolute data enters the digest-bound list.
    assert str(world["tmp"]) not in serialized
    for forbidden in ("created_at", "timestamp", "manifest_digest",
                      "report_digest"):
        assert forbidden not in serialized
    payload = result.canonical_payload
    assert payload["schema"] == "sam.f001.r_object_closure"
    assert payload["version"] == 1
    assert payload["commit_oid"] == COMMIT


def test_rejects_malformed_calls(world):
    """Collector inputs that cannot describe a closure raise, not gap out."""
    good_kwargs = dict(
        logical_id="x:1",
        object_type=OBJECT_ORIGINAL,
        version="v",
        referenced_by=[h_root("h/x")],
        scope=company_scope(),
        sha256_digest="sha256:" + "a" * 64,
    )
    with pytest.raises(ObjectClosureError):
        boc.collect_object_closure(
            commit_oid="not-an-oid",
            authorized_scope=world["scope"],
            references=[ObjectReference(**good_kwargs)],
            stores=world["stores"],
        )
    with pytest.raises(ObjectClosureError):
        ObjectReference(**{**good_kwargs, "sha256_digest": "deadbeef"})
    with pytest.raises(ObjectClosureError):
        # Mapping bytes may not be looked up in the archive store.
        ObjectReference(**{**good_kwargs,
                           "object_type": OBJECT_MAPPING_VERSION,
                           "storage_class": "archive"})
    with pytest.raises(ObjectClosureError):
        ObjectReference(**{**good_kwargs, "referenced_by": []})
    with pytest.raises(ObjectClosureError):
        # Contradictory declarations of one logical id.
        world["collect"]([
            ObjectReference(**good_kwargs),
            ObjectReference(**{**good_kwargs,
                               "sha256_digest": "sha256:" + "b" * 64}),
        ])


@pytest.fixture
def sha256_world(world):
    """Real bare SHA-256 authority plus the real archive/mapping stores."""
    from app.facts_tree.git_repository import FactsGitRepository

    repo = FactsGitRepository.initialize(
        world["tmp"] / "facts.git", repo_id="repo-h1",
        installation_id=INSTALLATION, company_id=COMPANY,
        object_format="sha256",
    )
    assert repo.object_format == "sha256"
    assert len(repo.genesis_oid) == 64
    assert (repo.path / "HEAD").is_file()
    assert not (repo.path / ".git").exists()  # bare, no working checkout
    return {**world, "repo": repo, "commit": repo.genesis_oid}


def _sha256_collect(world, refs):
    return boc.collect_object_closure(
        commit_oid=world["commit"], object_format="sha256",
        authorized_scope=world["scope"], references=refs, stores=world["stores"],
    )


def _sha256_refs(world):
    blob, _ = world["put_blob"](b"h1-original", name="h1.pdf")
    mapping = world["mapping_objects"].put(b"h1-historical-mapping")
    return [
        ObjectReference(
            logical_id=logical_id, object_type=object_type, version=digest,
            referenced_by=[ReferenceRoot(
                "H", world["commit"], coordinate, object_format="sha256",
            ), ReferenceRoot("W", CHECKPOINT, "rows/1", object_format="sha256")],
            scope=company_scope(), sha256_digest=f"sha256:{digest}",
        )
        for logical_id, object_type, digest, coordinate in [
            (f"source:{blob}", OBJECT_ORIGINAL, blob, "sources/1.json"),
            ("mapping:h1:v1", OBJECT_MAPPING_VERSION, mapping, "mapping/1.json"),
        ]
    ]


def test_h1_sha256_present_sorted_deterministic_read_only(sha256_world):
    world = sha256_world
    refs = _sha256_refs(world)
    before = _tree_snapshot(world["tmp"])
    first = _sha256_collect(world, refs)
    second = _sha256_collect(world, list(reversed(refs)))
    assert first.status == "complete"
    assert first.publishable is True
    assert all(entry.status == STATUS_PRESENT for entry in first.entries)
    assert [entry.logical_id for entry in first.entries] == sorted(
        ref.logical_id for ref in refs
    )
    assert first.canonical_payload == second.canonical_payload
    assert first.r_list_digest == second.r_list_digest
    assert first.r_list_digest == compute_core_digest(first.canonical_payload)
    assert first.canonical_payload["commit_oid"] == world["commit"]
    assert refs[0].referenced_by[0].normalized() == {
        "kind": "H", "root_id": world["commit"], "coordinate": "sources/1.json",
    }
    # Includes Git files, both object stores, and the SQLite metadata bytes.
    assert _tree_snapshot(world["tmp"]) == before


@pytest.mark.parametrize("state", ["missing", "corrupt"])
def test_h1_sha256_missing_corrupt(sha256_world, state):
    world = sha256_world
    refs = _sha256_refs(world)
    digest = refs[0].sha256_digest.removeprefix("sha256:")
    path = world["root"] / "bin" / digest[:2] / digest
    if state == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * len(b"h1-original"))
    before = _tree_snapshot(world["tmp"])
    result = _sha256_collect(world, refs)
    assert result.publishable is False
    assert result.status == ("incomplete" if state == "missing" else "corrupt")
    entry = _entry(result, refs[0].logical_id)
    assert entry.status == state
    assert entry.issue == ("object_missing" if state == "missing" else "sha256_mismatch")
    assert _tree_snapshot(world["tmp"]) == before


@pytest.mark.parametrize("foreign", ["company", "installation"])
def test_h1_sha256_cross_scope_rejected_before_read(sha256_world, monkeypatch, foreign):
    world = sha256_world
    refs = _sha256_refs(world)
    ref = refs[0]
    refs[0] = ObjectReference(
        logical_id=ref.logical_id, object_type=ref.object_type, version=ref.version,
        referenced_by=ref.referenced_by, sha256_digest=ref.sha256_digest,
        scope=company_scope(**{foreign: "foreign"}),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("cross-scope closure read object bytes")

    monkeypatch.setattr(boc, "verify_backup_component", forbidden)
    before = _tree_snapshot(world["tmp"])
    result = _sha256_collect(world, refs)
    assert result.status == "scope_violation"
    assert result.publishable is False
    assert result.entries == ()
    assert result.scope_violations[0].reason == f"foreign_{foreign}"
    assert _tree_snapshot(world["tmp"]) == before


@pytest.mark.parametrize("fmt,length", [("sha1", 40), ("sha256", 64)])
@pytest.mark.parametrize("oid", ["", *["a" * n for n in (39, 40, 41, 63, 64, 65)],
                                  "g" * 40, "g" * 64, "A" * 40, "A" * 64])
def test_h1_oid_boundaries_both_entrypoints(world, fmt, length, oid):
    def root():
        return ReferenceRoot("H", oid, "records/1", object_format=fmt)

    def collect():
        return boc.collect_object_closure(
            commit_oid=oid, object_format=fmt, authorized_scope=world["scope"],
            references=[], stores=world["stores"],
        )

    if oid == "a" * length:
        assert root().root_id == oid
        assert collect().status == "complete"
    else:
        for call in (root, collect):
            with pytest.raises(ObjectClosureError) as error:
                call()
            if oid:
                assert f"{length}-hex fixed commit oid" in str(error.value)


@pytest.mark.parametrize("fmt", ["md5", None, "SHA256", "", 64, [], {}])
def test_h1_invalid_object_format(world, fmt):
    for kind in ("H", "W"):
        with pytest.raises(ObjectClosureError, match="object_format"):
            ReferenceRoot(kind, COMMIT, "records/1", object_format=fmt)
    with pytest.raises(ObjectClosureError, match="object_format"):
        boc.collect_object_closure(
            commit_oid=COMMIT, object_format=fmt, authorized_scope=world["scope"],
            references=[], stores=world["stores"],
        )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("checkpoint", ["checkpoint:rows/1", "z", "a" * 39,
                                        "a" * 40, "a" * 41, "a" * 63,
                                        "a" * 64, "a" * 65])
def test_h1_w_roots_do_not_validate_oid_length(fmt, checkpoint):
    assert ReferenceRoot("W", checkpoint, "rows/1", object_format=fmt).normalized() == {
        "kind": "W", "root_id": checkpoint, "coordinate": "rows/1",
    }


def test_h1_keyword_only_and_sha1_error_compatibility(world):
    with pytest.raises(TypeError):
        ReferenceRoot("H", COMMIT, "records/1", "sha1")
    with pytest.raises(TypeError):
        boc.collect_object_closure(
            COMMIT, world["scope"], [], "sha1", stores=world["stores"],
        )
    with pytest.raises(ObjectClosureError) as error:
        ReferenceRoot("H", "a" * 64, "records/1")
    assert str(error.value) == "H reference root id must be the 40-hex fixed commit oid"
    with pytest.raises(ObjectClosureError) as error:
        boc.collect_object_closure(
            commit_oid="a" * 64, authorized_scope=world["scope"],
            references=[], stores=world["stores"],
        )
    assert str(error.value) == "commit_oid must be the 40-hex fixed commit oid"
