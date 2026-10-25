"""Canonical 2A source mapping and append-only human corrections.

This module is deliberately below the CLI and above the storage/database
layers.  It never changes the immutable source blob or promotes anything to
2B.  A mapping is a projection of the latest parsed L2 manifest plus active
correction records, rendered as a conservative Markdown document.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import base64
import errno
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..db.database import connect
from .archive_store import ArchiveFileStore
from .archive_paths import evidence_root, hash_relpath
from .versioned_mapping_objects import (
    UnsafeObjectPathError,
    VersionedMappingObjectStore,
)

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# Preparing the single directory level above an object root must verify and
# create it relative to a descriptor pinned to its own parent; without
# dir_fd mkdir/lstat plus O_NOFOLLOW/O_DIRECTORY the no-follow boundary
# cannot be enforced, and the mapping write is refused instead of degraded.
_PREPARE_DIR_FD_PRIMITIVES = (os.mkdir, os.lstat)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coord(loc: Any, *, fallback_page: Any = None) -> str:
    """Render a stable, machine-readable coordinate, including missing data."""
    loc = loc if isinstance(loc, dict) else {}
    page = loc.get("page")
    if page is None:
        page = loc.get("page_no", fallback_page if fallback_page is not None else "?")
    locator = loc.get("locator") or loc.get("char_range")
    return f"page={page if page is not None else '?'}; locator={locator or 'missing'}"


def _frontmatter(values: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in values.items():
        encoded = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {encoded}")
    lines.append("---")
    return "\n".join(lines)


def _text_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in manifest.get("pages", []) if isinstance(manifest, dict) else []:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page_no", page.get("page", "?"))
        for index, item in enumerate(page.get("text_items", []) or []):
            if not isinstance(item, dict):
                continue
            entry = deepcopy(item)
            entry["_page_no"] = page_no
            entry["_index"] = index
            items.append(entry)
    return items


def _table_rows(manifest: dict[str, Any]) -> list[tuple[Any, int, dict[str, Any]]]:
    rows: list[tuple[Any, int, dict[str, Any]]] = []
    for page in manifest.get("pages", []) if isinstance(manifest, dict) else []:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page_no", page.get("page", "?"))
        for table_index, table in enumerate(page.get("tables", []) or []):
            if not isinstance(table, dict):
                continue
            for row_index, row in enumerate(table.get("rows", []) or []):
                values = row if isinstance(row, dict) else {str(i): value for i, value in enumerate(row or [])}
                row_locs = table.get("row_source_locs") or []
                location = (row_locs[row_index] if row_index < len(row_locs)
                            else row.get("source_loc") if isinstance(row, dict) else table.get("source_loc"))
                rows.append((page_no, row_index, {"table_index": table_index, "headers": table.get("headers") or [], "values": values, "loc": location}))
    return rows


def render_mapping(manifest: dict[str, Any], *, edits: list[dict[str, Any]] | None = None) -> str:
    """Render an L2 mapping Markdown without inventing missing coordinates."""
    edits = edits or []
    document = manifest.get("document") or {}
    if isinstance(document.get("markdown"), str) and not any(e.get("status") == "active" for e in edits):
        return document["markdown"]
    active = {str(item["target_locator"]): item for item in edits if item.get("status") == "active"}
    summary = manifest.get("parse_summary") if isinstance(manifest, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    meta = {
        "document_type": "2a_source_mapping",
        "source_id": manifest.get("source_id"),
        "file_hash": manifest.get("file_hash"),
        "original_name": manifest.get("filename") or manifest.get("original_name"),
        "format": manifest.get("format"),
        "parse_status": summary.get("status", "unknown"),
        "coordinate_policy": "source_loc is preserved; missing coordinates are explicit",
    }
    lines = [_frontmatter(meta), "", f"# {meta['original_name'] or '未命名原件'}", "",
             "> 本页是原件的 2A 映射，不是事实认定。未提供坐标处明确标注为 `missing`。", ""]
    lines += ["## 正文", ""]
    items = _text_items(manifest)
    if not items:
        lines.append("暂无可解析正文。")
    for item in items:
        loc = _coord(item.get("source_loc"), fallback_page=item.get("_page_no"))
        edit = active.get(loc)
        if edit and edit.get("operation") == "remove":
            lines.append(f"- ~~已移除原文片段~~ <!-- source: {loc}; correction: {edit['id']} -->")
            continue
        text = item.get("text", "")
        if edit and edit.get("operation") == "replace":
            text = edit.get("replacement_text") or ""
            marker = f"; correction: {edit['id']}"
        else:
            marker = ""
        lines.append(f"- {text} <!-- source: {loc}{marker} -->")

    lines += ["", "## 表格", "", "| 页码 | 行 | 内容 | 坐标 |", "| --- | ---: | --- | --- |"]
    rows = _table_rows(manifest)
    if not rows:
        lines.append("| — | — | — | missing |")
    else:
        for page_no, row_index, row in rows:
            values = " / ".join(str(v) for v in row["values"].values())
            if row["headers"]:
                values = " / ".join(str(v) for v in row["headers"]) + " => " + values
            loc = _coord(row.get("loc"), fallback_page=page_no)
            edit = active.get(loc)
            if edit and edit.get("operation") == "remove":
                values = "~~已移除原文行~~"
            elif edit and edit.get("operation") == "replace":
                values = str(edit.get("replacement_text") or "")
            lines.append(f"| {page_no} | {row_index} | {values.replace('|', '\\|')} | {loc} |")
    return "\n".join(lines) + "\n"


def write_mapping(path: Path, mapping: str) -> None:
    """Atomically replace a rebuildable projection using a unique sibling tmp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=".mapping-", suffix=".tmp",
                                         delete=False) as stream:
            tmp_path = Path(stream.name)
            stream.write(mapping)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _open_pinned_directory(name: str, shown: str, *, dir_fd: int | None,
                           missing_message: str) -> int:
    """Open one directory component with ``O_DIRECTORY``/``O_NOFOLLOW``.

    ``name`` is resolved relative to ``dir_fd`` when one is given, so an
    already-verified ancestor descriptor — not a re-resolvable path —
    anchors the open, and the final component is never followed when it
    is a symlink.  ``shown`` only shapes error text.  A symlink or
    non-directory occupant fails closed as
    :class:`UnsafeObjectPathError`; a missing component fails as
    ``FileNotFoundError(missing_message)``.
    """
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        if dir_fd is None:
            return os.open(name, flags)
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeObjectPathError(
                f"{shown} is a symbolic link; refusing to follow it"
            ) from None
        if exc.errno == errno.ENOTDIR:
            raise UnsafeObjectPathError(
                f"{shown} is not a directory"
            ) from None
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(missing_message) from None
        raise


def _prepare_objects_parent(objects_root: Path | str) -> int:
    """Durably materialize the single directory level above ``objects_root``.

    Application-layer companion to the object store's approved root
    contract: the store itself still creates only its leaf root, requires
    this parent (the maps root's parent, e.g. the ``2a`` directory derived
    from an explicit db_path) to exist, and refuses recursive mkdir.  This
    preparation therefore creates at most the immediately adjacent parent
    directory and fsyncs the grandparent entry naming it, so the first
    mapping write under a fresh explicit db_path succeeds without ever
    materializing deeper ancestors whose own entries would never be
    persisted.

    Returns a descriptor pinned to the verified parent directory, opened
    with ``O_DIRECTORY``/``O_NOFOLLOW`` relative to the still-open
    grandparent descriptor.  Ownership of the pinned descriptor transfers
    to the caller, which must close it and should hand it to
    :class:`VersionedMappingObjectStore` so the root's creation, every
    root open, and the parent fsync all stay relative to the verified
    directory; re-resolving the parent path after preparation would
    reopen the very check-then-use window this pin closes.

    Fail-closed: a symlink or non-directory occupant at the parent, a
    symlinked, non-directory, or missing grandparent, and a platform
    missing the dir_fd ``mkdir``/``lstat`` or ``O_NOFOLLOW``/``O_DIRECTORY``
    primitives all raise instead of following links or silently degrading.
    The parent is verified and created relative to a descriptor pinned to
    the grandparent, so there is no check-then-create window at the parent
    level, and the grandparent is fsynced whether or not this call created
    the parent — an earlier creation's sync cannot be assumed to have
    reached stable storage.  No descriptor leaks: the grandparent
    descriptor is closed on every path, and a failure after the pinned
    parent open closes that descriptor again before the error propagates.
    Path components above the grandparent remain the caller's
    responsibility, matching the object store's boundary.
    """
    if _O_NOFOLLOW == 0 or _O_DIRECTORY == 0 or not all(
        fn in os.supports_dir_fd for fn in _PREPARE_DIR_FD_PRIMITIVES
    ):
        raise RuntimeError(
            "preparing the mapping-object root parent requires POSIX "
            "dir_fd mkdir/lstat, O_NOFOLLOW and O_DIRECTORY support; "
            "this platform cannot verify or create that single directory "
            "level without following symlinks, so the mapping write is "
            "refused instead of silently degrading a missing flag to 0"
        )
    # Freeze the absolute path lexically (not resolve()), so symlink
    # components stay intact for the no-follow checks below and a later
    # chdir can never relocate the preparation, matching the store.
    parent = Path(os.path.abspath(objects_root)).parent
    grandparent = parent.parent
    if parent == grandparent:
        # The filesystem root: it always exists and owns no directory
        # entry of its own to sync; pin it directly for the caller.
        return _open_pinned_directory(
            str(parent),
            f"mapping-object root parent {parent}",
            dir_fd=None,
            missing_message=(
                f"mapping-object root parent {parent} does not exist"
            ))
    gp_fd = _open_pinned_directory(
        str(grandparent),
        f"mapping-object root parent's own parent {grandparent}",
        dir_fd=None,
        missing_message=(
            f"cannot prepare mapping-object root parent {parent}: its "
            f"own parent {grandparent} does not exist; only the single "
            "adjacent directory level is ever created, never deeper "
            "ancestors"))
    try:
        try:
            os.mkdir(parent.name, dir_fd=gp_fd)
        except FileExistsError:
            # Pre-existing or concurrently (re)created; the occupant is
            # verified through lstat below instead of a symlink-following
            # probe, and a genuine directory needs no further action.
            pass
        st = os.lstat(parent.name, dir_fd=gp_fd)
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeObjectPathError(
                f"mapping-object root parent {parent} is a symbolic link; "
                "refusing to follow it"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeObjectPathError(
                f"mapping-object root parent {parent} is not a directory"
            )
        # Pin the verified parent through the still-open grandparent
        # descriptor, so neither this caller nor the object store it hands
        # the descriptor to ever re-resolves the parent path; O_NOFOLLOW
        # turns a replacement between the lstat and this open into an
        # explicit failure instead of a silent redirect.
        parent_fd = _open_pinned_directory(
            parent.name,
            f"mapping-object root parent {parent}",
            dir_fd=gp_fd,
            missing_message=(
                f"mapping-object root parent {parent} vanished during "
                "preparation"))
        try:
            # Persist the parent's entry in the grandparent on every
            # preparation, mirroring the store's sync-on-every-construction
            # rule for its own parent.
            os.fsync(gp_fd)
        except BaseException:
            os.close(parent_fd)
            raise
        return parent_fd
    finally:
        os.close(gp_fd)


def persist_latest_mapping(conn, file_hash: str, path: Path,
                           objects_root: Path | str) -> str:
    """Caller holds the SQLite writer lock while replacing its projection.

    ``objects_root`` is the explicit root of the immutable mapping-object
    store (the sibling ``mapping-objects`` directory of the maps root).
    Immediately before the store is constructed,
    :func:`_prepare_objects_parent` durably materializes
    ``objects_root.parent`` — the maps root's parent — as at most the
    single adjacent directory level and returns a descriptor pinned to
    the verified directory.  The store borrows that descriptor for root
    creation, every root open, and the parent fsync, so the parent path
    is never re-resolved between verification and use; this call site
    owns the descriptor and closes it on success and failure alike.  The
    store itself still creates only its leaf root and refuses recursive
    mkdir.  The exact shared L2 mapping bytes are first preserved there
    content-addressed, and only after that succeeds is the rebuildable
    latest projection atomically replaced; an object-store failure
    therefore aborts before any projection is touched and is never
    reported as a successful mapping write.  The DB transaction and the
    file stores are not one atomic transaction: a later DB rollback may
    leave an unreferenced object behind, which inventory is allowed to
    show.
    """
    row = conn.execute(
        "SELECT payload FROM parse_staging WHERE file_hash=? AND status='parsed' ORDER BY id DESC LIMIT 1",
        (file_hash,),
    ).fetchone()
    if row is None:
        raise KeyError(f"parsed mapping not found: {file_hash}")
    mapping = render_mapping(json.loads(row["payload"]))
    parent_fd = _prepare_objects_parent(objects_root)
    try:
        VersionedMappingObjectStore(objects_root, parent_fd=parent_fd).put(
            mapping.encode("utf-8"))
    finally:
        # The store borrows the pinned parent descriptor for its lifetime
        # and never closes it; preparation transferred ownership here, so
        # this call site closes it on the success path and on every
        # object-store failure path alike — no descriptor may leak.
        os.close(parent_fd)
    write_mapping(path, mapping)
    manifest = json.loads(row["payload"])
    document = manifest.get("document") or {}
    for name in ("document_map", "assets", "hyperlinks", "self_check"):
        if name in document:
            write_mapping(path.with_suffix(f".{name}.json"), json.dumps(document[name], ensure_ascii=False, indent=2) + "\n")
    if "document_quality" in manifest:
        write_mapping(path.with_suffix(".quality.json"), json.dumps(manifest["document_quality"], ensure_ascii=False, indent=2) + "\n")
    if "engine_document" in manifest:
        write_mapping(path.with_suffix(".engine.json"), json.dumps(manifest["engine_document"], ensure_ascii=False) + "\n")
    return mapping


class SourceMapService:
    """Read mappings and append controlled replace/remove corrections."""

    def __init__(self, db_path, objects_path: Path | str | None = None, *, maps_path=None):
        self.db_path = db_path
        self.objects_path = Path(objects_path) if objects_path is not None else evidence_root() / "bin"
        self.maps_path = Path(maps_path) if maps_path is not None else self.objects_path.parent / "map"
        # Immutable mapping objects live in the explicit sibling of the maps
        # root, so the same configuration always resolves to the same store
        # root as StagingStore; nothing here reads environment variables or
        # the current working directory beyond the configured paths.  The
        # object store creates only this leaf; persist_latest_mapping
        # prepares its parent (the maps root's parent) as at most a single
        # durable directory level before any object write.
        self.mapping_objects_path = self.maps_path.parent / "mapping-objects"

    def _manifest(self, file_hash: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM parse_staging WHERE file_hash=? AND status='parsed' ORDER BY id DESC LIMIT 1",
                (file_hash,),
            ).fetchone()
        if row is None:
            raise KeyError(f"parsed mapping not found: {file_hash}")
        return json.loads(row["payload"])

    def list_edits(self, *, file_hash: str, company_id: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM source_edits WHERE file_hash=? AND company_id=? ORDER BY id ASC",
                (file_hash, company_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_manifest(self, *, file_hash: str) -> dict[str, Any]:
        """Read the latest parsed L2; callers must enforce their company catalog."""
        return self._manifest(file_hash)

    def map_path(self, file_hash: str) -> Path:
        return self.maps_path / hash_relpath(file_hash, mapping=True)

    def rebuild_map(self, *, file_hash: str) -> str:
        """Persist the shared source projection, never company-private edits.

        Serialize with staging writers so concurrent parses cannot replace the
        newest projection with an older manifest. Reads never rebuild files.
        """
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return persist_latest_mapping(conn, file_hash, self.map_path(file_hash),
                                          self.mapping_objects_path)

    def read_map(self, *, file_hash: str, company_id: str, manifest=None) -> str:
        # Synchronize the DB snapshot and file read with staging writers. This
        # transaction changes no rows; it only guards a projection replacement.
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM parse_staging WHERE file_hash=? AND status='parsed' ORDER BY id DESC LIMIT 1",
                (file_hash,),
            ).fetchone()
            if row is None:
                raise KeyError(f"parsed mapping not found: {file_hash}")
            local = json.loads(row["payload"])
            edits = [dict(edit) for edit in conn.execute(
                "SELECT * FROM source_edits WHERE file_hash=? AND company_id=? ORDER BY id ASC",
                (file_hash, company_id),
            )]
            selected = local if manifest is None else manifest
            # Preserve remote-manifest preference and company correction scope.
            if selected == local and not any(edit["status"] == "active" for edit in edits):
                try:
                    return self.map_path(file_hash).read_text(encoding="utf-8")
                except (FileNotFoundError, UnicodeDecodeError):
                    pass
            return render_mapping(selected, edits=edits)

    def read_original(self, *, file_hash: str) -> dict[str, Any]:
        stored = ArchiveFileStore(objects_path=self.objects_path, db_path=self.db_path).get(file_hash)
        data = ArchiveFileStore(objects_path=self.objects_path, db_path=self.db_path).get_bytes(file_hash)
        result: dict[str, Any] = {
            "file_hash": stored.file_hash, "original_name": stored.original_name,
            "mime_type": stored.mime_type, "size_bytes": stored.size_bytes,
            "status": stored.status, "sha256_verified": hashlib.sha256(data).hexdigest() == file_hash,
        }
        try:
            result["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            result["base64"] = base64.b64encode(data).decode("ascii")
        return result

    def _edit(self, *, file_hash: str, company_id: str, operation: str,
              target_locator: str, replacement_text: str | None, confirm: bool) -> dict[str, Any]:
        if not company_id or not isinstance(company_id, str):
            raise ValueError("company_id must be injected by the host")
        if not confirm:
            raise PermissionError("write operation requires --confirm")
        if operation not in {"replace", "remove"}:
            raise ValueError("unsupported correction operation")
        self._manifest(file_hash)  # verify a parsed mapping exists before writing
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM source_edits WHERE file_hash=? AND company_id=? AND target_locator=? AND status='active' ORDER BY id DESC LIMIT 1",
                (file_hash, company_id, target_locator),
            ).fetchone()
            if current is not None:
                conn.execute("UPDATE source_edits SET status='superseded' WHERE id=?", (current["id"],))
            cur = conn.execute(
                "INSERT INTO source_edits(file_hash,company_id,operation,target_locator,replacement_text,status,supersedes_id,created_at) VALUES (?,?,?,?,?,'active',?,?)",
                (file_hash, company_id, operation, target_locator, replacement_text,
                 current["id"] if current is not None else None, _now()),
            )
            persist_latest_mapping(conn, file_hash, self.map_path(file_hash),
                                   self.mapping_objects_path)
            conn.commit()
            row = conn.execute("SELECT * FROM source_edits WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def replace(self, *, file_hash: str, company_id: str, target_locator: str,
                replacement_text: str, confirm: bool = False) -> dict[str, Any]:
        return self._edit(file_hash=file_hash, company_id=company_id, operation="replace",
                          target_locator=target_locator, replacement_text=replacement_text, confirm=confirm)

    def remove(self, *, file_hash: str, company_id: str, target_locator: str,
               confirm: bool = False) -> dict[str, Any]:
        return self._edit(file_hash=file_hash, company_id=company_id, operation="remove",
                          target_locator=target_locator, replacement_text=None, confirm=confirm)
