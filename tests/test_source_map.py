from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.db import connect, init_db
from app.harness.staging import StagingStore
from app.storage import mapping_store
from app.storage.mapping_store import SourceMapService, render_mapping
from app.storage.versioned_mapping_objects import (
    MappingObjectEntry,
    UnsafeObjectPathError,
    VersionedMappingObjectStore,
)
from app.storage import ArchiveFileStore


def _manifest(file_hash: str) -> dict:
    return {
        "source_id": file_hash[:12], "filename": "报告.pdf", "format": "pdf",
        "file_hash": file_hash, "pages": [{"page_no": 2, "text_items": [
            {"text": "原始段落", "source_loc": {"page": 2, "locator": "p2:0-4"}},
            {"text": "无坐标段落", "source_loc": {}},
        ], "tables": [{"headers": ["指标", "值"], "rows": [["营收", "10"]],
                         "source_loc": {"page_no": 2, "locator": "table:p2:r0"}}]}],
        "parse_summary": {"status": "parsed", "raw_bytes_external": False, "full_text_external": False},
    }


def _service(tmp_path: Path):
    db = tmp_path / "app.db"
    objects = tmp_path / "objects"
    init_db(db)
    # The sibling mapping-object root's parent must exist before any mapping
    # is persisted: the object store creates only its own leaf directory and
    # never recursively materializes ancestors.
    (tmp_path / "2a").mkdir(exist_ok=True)
    store = ArchiveFileStore(objects, db)
    data = b"immutable original"
    stored = store.put_bytes(data, original_name="报告.pdf", mime_type="application/pdf")
    staging = StagingStore(db, maps_path=tmp_path / "2a/map")
    staging.save_manifest(_manifest(stored.file_hash))
    # Aligned maps root: the service and the staging store resolve to the
    # same projection files and the same mapping-object root.
    service = SourceMapService(db, objects, maps_path=staging.maps_path)
    return service, stored, data, db


def test_mapping_is_standard_markdown_and_preserves_coordinates(tmp_path: Path):
    service, stored, _, _ = _service(tmp_path)
    mapping = service.read_map(file_hash=stored.file_hash, company_id="acme")
    assert mapping.startswith("---\n")
    assert "document_type: \"2a_source_mapping\"" in mapping
    assert "source: page=2; locator=p2:0-4" in mapping
    assert "source: page=2; locator=missing" in mapping
    assert "不是事实认定" in mapping


def test_replace_requires_confirm_and_is_append_only(tmp_path: Path):
    service, stored, original, db = _service(tmp_path)
    with pytest.raises(PermissionError):
        service.replace(file_hash=stored.file_hash, company_id="acme",
                        target_locator="page=2; locator=p2:0-4", replacement_text="修订")
    row = service.replace(file_hash=stored.file_hash, company_id="acme",
                         target_locator="page=2; locator=p2:0-4", replacement_text="修订", confirm=True)
    assert row["status"] == "active"
    assert "修订" in service.read_map(file_hash=stored.file_hash, company_id="acme")
    assert ArchiveFileStore(service.objects_path, db).get_bytes(stored.file_hash) == original
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_edits").fetchone()[0] == 1


def test_remove_and_second_correction_supersede_only_same_coordinate(tmp_path: Path):
    service, stored, _, db = _service(tmp_path)
    first = service.replace(file_hash=stored.file_hash, company_id="acme",
                            target_locator="page=2; locator=p2:0-4", replacement_text="一", confirm=True)
    second = service.remove(file_hash=stored.file_hash, company_id="acme",
                            target_locator="page=2; locator=p2:0-4", confirm=True)
    mapping = service.read_map(file_hash=stored.file_hash, company_id="acme")
    assert "已移除原文片段" in mapping
    with connect(db) as conn:
        rows = conn.execute("SELECT status,supersedes_id FROM source_edits ORDER BY id").fetchall()
    assert [(row["status"], row["supersedes_id"]) for row in rows] == [("superseded", None), ("active", first["id"])]


def test_table_row_replace_and_remove_are_reflected_in_mapping(tmp_path: Path):
    service, stored, _, _ = _service(tmp_path)
    locator = "page=2; locator=table:p2:r0"
    service.replace(file_hash=stored.file_hash, company_id="acme",
                    target_locator=locator, replacement_text="营收 => 99", confirm=True)
    assert "营收 => 99" in service.read_map(file_hash=stored.file_hash, company_id="acme")
    service.remove(file_hash=stored.file_hash, company_id="acme", target_locator=locator, confirm=True)
    assert "已移除原文行" in service.read_map(file_hash=stored.file_hash, company_id="acme")


def test_company_scope_is_required_by_cli_write_surface(tmp_path: Path):
    service, stored, _, _ = _service(tmp_path)
    with pytest.raises(ValueError):
        service.replace(file_hash=stored.file_hash, company_id="", target_locator="x", replacement_text="y", confirm=True)


def test_same_configuration_derives_same_object_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAM_2A_ROOT", str(tmp_path / "custom-2a"))
    db = tmp_path / "app.db"
    init_db(db)
    staging = StagingStore(db)
    service = SourceMapService(db)
    assert staging.maps_path == service.maps_path == tmp_path / "custom-2a" / "map"
    assert staging.mapping_objects_path == service.mapping_objects_path
    assert staging.mapping_objects_path == tmp_path / "custom-2a" / "mapping-objects"
    # The sibling rule is purely derived from the maps root, so an explicit
    # maps_path lands both writers on the same object root as well.
    explicit = tmp_path / "explicit" / "map"
    staging2 = StagingStore(db, maps_path=explicit)
    service2 = SourceMapService(db, tmp_path / "objects", maps_path=explicit)
    assert staging2.mapping_objects_path == service2.mapping_objects_path
    assert staging2.mapping_objects_path == tmp_path / "explicit" / "mapping-objects"


def test_successive_mappings_preserve_exact_old_bytes(tmp_path: Path):
    service, stored, _, db = _service(tmp_path)
    staging = StagingStore(db, maps_path=service.maps_path)
    assert staging.mapping_objects_path == service.mapping_objects_path
    first_bytes = render_mapping(_manifest(stored.file_hash)).encode("utf-8")

    second_manifest = _manifest(stored.file_hash)
    second_manifest["pages"][0]["text_items"][0]["text"] = "修订后的段落"
    staging.save_manifest(second_manifest)
    second_bytes = render_mapping(second_manifest).encode("utf-8")
    assert first_bytes != second_bytes

    objects = VersionedMappingObjectStore(staging.mapping_objects_path)
    first_digest = hashlib.sha256(first_bytes).hexdigest()
    second_digest = hashlib.sha256(second_bytes).hexdigest()
    # The old exact bytes stay readable and byte-identical after the newer
    # version was written.
    assert objects.get(first_digest) == first_bytes
    assert objects.get(second_digest) == second_bytes
    # The rebuildable latest projection is the new version.
    assert service.map_path(stored.file_hash).read_bytes() == second_bytes
    # Inventory is sorted by digest, carries exact sizes, and every listed
    # entry reads back byte-exact.
    expected = sorted([MappingObjectEntry(first_digest, len(first_bytes)),
                       MappingObjectEntry(second_digest, len(second_bytes))])
    assert objects.inventory() == expected
    for entry in objects.inventory():
        data = objects.get(entry.digest)
        assert data == (first_bytes if entry.digest == first_digest else second_bytes)
        assert entry.size_bytes == len(data)

    # Replaying identical mapping content is content-addressed away: no
    # second object appears.
    staging.save_manifest(second_manifest)
    assert objects.inventory() == expected


def test_object_write_failure_blocks_projection_and_staging_row(tmp_path: Path, monkeypatch):
    service, stored, _, db = _service(tmp_path)
    staging = StagingStore(db, maps_path=service.maps_path)
    map_file = service.map_path(stored.file_hash)
    first_text = map_file.read_text(encoding="utf-8")

    def failing_put(self, data):
        raise OSError("simulated object store failure")

    monkeypatch.setattr(VersionedMappingObjectStore, "put", failing_put)
    changed = _manifest(stored.file_hash)
    changed["pages"][0]["text_items"][0]["text"] = "不会落地的版本"
    with pytest.raises(OSError, match="simulated object store failure"):
        staging.save_manifest(changed)
    # The latest projection was not replaced and the parse staging row was
    # not committed; the failed version left no object behind either.
    assert map_file.read_text(encoding="utf-8") == first_text
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parse_staging").fetchone()[0] == 1
    changed_digest = hashlib.sha256(render_mapping(changed).encode("utf-8")).hexdigest()
    assert VersionedMappingObjectStore(staging.mapping_objects_path).has(changed_digest) is False


def test_correction_fails_closed_when_object_write_fails(tmp_path: Path, monkeypatch):
    service, stored, _, db = _service(tmp_path)
    map_file = service.map_path(stored.file_hash)
    first_text = map_file.read_text(encoding="utf-8")

    def failing_put(self, data):
        raise OSError("simulated object store failure")

    monkeypatch.setattr(VersionedMappingObjectStore, "put", failing_put)
    with pytest.raises(OSError, match="simulated object store failure"):
        service.replace(file_hash=stored.file_hash, company_id="acme",
                        target_locator="page=2; locator=p2:0-4",
                        replacement_text="修订", confirm=True)
    # The edit transaction rolled back and the projection was not replaced.
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_edits").fetchone()[0] == 0
    assert map_file.read_text(encoding="utf-8") == first_text


def _symlink_or_skip(target, link_path):
    """Create ``link_path`` -> ``target`` or skip with the platform boundary.

    Platforms or filesystems that cannot create symlinks cannot exercise
    symlink rejection at all; those cases skip loudly instead of passing
    silently.
    """
    if not hasattr(os, "symlink"):
        pytest.skip("platform has no os.symlink; "
                    "symlink rejection is untestable here")
    try:
        os.symlink(target, link_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot create symlinks on this platform/filesystem "
                    f"({exc}); symlink rejection is untestable here")


def _synthetic_hash() -> str:
    return hashlib.sha256(b"immutable original").hexdigest()


def test_first_save_prepares_derived_2a_root_without_env(tmp_path: Path, monkeypatch):
    # Regression: with an explicit db_path and no SAM_2A_ROOT, the maps root
    # derives to db_path.parent/"2a"/"map".  The "2a" parent is deliberately
    # not pre-created here; the first save must durably prepare that single
    # adjacent directory level and succeed.
    monkeypatch.delenv("SAM_2A_ROOT", raising=False)
    db = tmp_path / "app.db"
    init_db(db)
    file_hash = _synthetic_hash()
    staging = StagingStore(db)
    assert staging.maps_path == tmp_path / "2a" / "map"
    assert staging.mapping_objects_path == tmp_path / "2a" / "mapping-objects"
    assert not (tmp_path / "2a").exists()

    staging.save_manifest(_manifest(file_hash))

    assert (tmp_path / "2a").is_dir()
    # The service on the same configuration resolves to the same projection
    # and the same object root, and both agree with the first saved bytes.
    service = SourceMapService(db, tmp_path / "objects", maps_path=staging.maps_path)
    assert service.maps_path == staging.maps_path
    assert service.mapping_objects_path == staging.mapping_objects_path
    expected = render_mapping(_manifest(file_hash)).encode("utf-8")
    assert service.map_path(file_hash).read_bytes() == expected
    assert service.read_map(file_hash=file_hash, company_id="acme") == expected.decode("utf-8")
    objects = VersionedMappingObjectStore(service.mapping_objects_path)
    digest = hashlib.sha256(expected).hexdigest()
    assert objects.get(digest) == expected
    assert objects.inventory() == [MappingObjectEntry(digest, len(expected))]


def test_first_save_fails_closed_on_symlinked_2a_parent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAM_2A_ROOT", raising=False)
    db = tmp_path / "app.db"
    init_db(db)
    file_hash = _synthetic_hash()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, tmp_path / "2a")
    staging = StagingStore(db)
    with pytest.raises(UnsafeObjectPathError):
        staging.save_manifest(_manifest(file_hash))
    # The link is never followed: nothing lands in its target, no projection
    # tree is created, and the staging row is rolled back.
    assert list(outside.iterdir()) == []
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parse_staging").fetchone()[0] == 0


def test_first_save_fails_closed_on_non_directory_2a_parent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAM_2A_ROOT", raising=False)
    db = tmp_path / "app.db"
    init_db(db)
    file_hash = _synthetic_hash()
    (tmp_path / "2a").write_bytes(b"not a directory")
    staging = StagingStore(db)
    with pytest.raises(UnsafeObjectPathError):
        staging.save_manifest(_manifest(file_hash))
    # The occupant stays byte-identical; nothing is written through it.
    assert (tmp_path / "2a").read_bytes() == b"not a directory"
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parse_staging").fetchone()[0] == 0


def test_persist_creates_only_the_single_adjacent_parent_level(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SAM_2A_ROOT", raising=False)
    db = tmp_path / "app.db"
    init_db(db)
    file_hash = _synthetic_hash()
    # The object-root parent ("absent/deep") is missing and so is its own
    # parent ("absent"): only the immediately adjacent single level may ever
    # be created, so this must fail closed without materializing ancestors.
    staging = StagingStore(db, maps_path=tmp_path / "absent" / "deep" / "map")
    with pytest.raises(FileNotFoundError):
        staging.save_manifest(_manifest(file_hash))
    assert not (tmp_path / "absent").exists()
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parse_staging").fetchone()[0] == 0


def test_persist_keeps_objects_inside_pinned_parent_after_swap(tmp_path: Path, monkeypatch):
    """End-to-end regression for the parent check/use gap, without concurrency.

    Preparation pins the verified "2a" parent; the wrapper below then
    deterministically renames that directory's entry and replaces the
    original path with a symlink to an outside sandbox before the persist
    flow continues.  The mapping object — root creation and object write —
    must still land inside the renamed verified directory through the
    pinned descriptor, never behind the symlink.  Only the rebuildable
    latest projection, which is path-based by contract, follows the swapped
    maps path; the object boundary does not.
    """
    monkeypatch.delenv("SAM_2A_ROOT", raising=False)
    db = tmp_path / "app.db"
    init_db(db)
    file_hash = _synthetic_hash()
    staging = StagingStore(db)
    renamed = tmp_path / "2a-verified"
    outside = tmp_path / "outside"
    real_prepare = mapping_store._prepare_objects_parent

    def prepare_then_swap(objects_root):
        parent_fd = real_prepare(objects_root)
        os.rename(tmp_path / "2a", renamed)
        outside.mkdir()
        _symlink_or_skip(outside, tmp_path / "2a")
        return parent_fd

    monkeypatch.setattr(mapping_store, "_prepare_objects_parent",
                        prepare_then_swap)
    staging.save_manifest(_manifest(file_hash))

    expected = render_mapping(_manifest(file_hash)).encode("utf-8")
    digest = hashlib.sha256(expected).hexdigest()
    objects_dir = renamed / "mapping-objects"
    assert (objects_dir / digest[:2] / digest).read_bytes() == expected
    # The symlink target never received the object store, and the symlink
    # stays an unfollowed symlink.
    assert not (outside / "mapping-objects").exists()
    assert sorted(p.name for p in outside.iterdir()) == ["map"]
    assert os.path.islink(tmp_path / "2a")
    assert os.readlink(tmp_path / "2a") == str(outside)
    # The verified directory holds exactly the object store, and the store
    # reads back through a freshly constructed (path-mode) instance too:
    # the rename kept the real tree self-consistent.
    assert sorted(p.name for p in objects_dir.iterdir()) == [digest[:2]]
    assert VersionedMappingObjectStore(objects_dir).get(digest) == expected


def test_object_write_failure_closes_the_pinned_parent_fd(tmp_path: Path, monkeypatch):
    """fd ownership: the persist call site closes the borrowed descriptor
    even when the object write fails — no leak on the failure path."""
    service, stored, _, db = _service(tmp_path)
    staging = StagingStore(db, maps_path=service.maps_path)
    prepared = []
    real_prepare = mapping_store._prepare_objects_parent

    def recording_prepare(objects_root):
        parent_fd = real_prepare(objects_root)
        prepared.append(parent_fd)
        return parent_fd

    monkeypatch.setattr(mapping_store, "_prepare_objects_parent",
                        recording_prepare)

    def failing_put(self, data):
        raise OSError("simulated object store failure")

    monkeypatch.setattr(VersionedMappingObjectStore, "put", failing_put)
    changed = _manifest(stored.file_hash)
    with pytest.raises(OSError, match="simulated object store failure"):
        staging.save_manifest(changed)
    assert len(prepared) == 1
    # The descriptor was closed by the persist call site: fstat on it must
    # fail with EBADF.
    with pytest.raises(OSError):
        os.fstat(prepared[0])
