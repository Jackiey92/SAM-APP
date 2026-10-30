"""Immutable, same-directory full wide projections (appendix D8 v1).

The pure assembly step consumes D6-decoded payloads. The IO shell pins one
read-only standard snapshot and publishes only a fully verified companion.
"""
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from app.facts_tree import git_format
from app.facts_tree.projection_decode import decode_rows, UNSCHEMA_FAMILIES
from app.facts_tree.record_schema import SCHEMA_COLUMNS


_SOURCE_TABLES = frozenset({"generation_meta", "record_canonical", "family_counts",
                            "fact_current_view", "placement_current_view"})
_OWNERSHIP = ("trunk_code", "leaf_code", "pack_version", "template_pack", "binding_revision")
_SYSTEM = (("path", "TEXT", False, 1), ("payload_json", "TEXT", True, 0),
           ("canonical_sha256", "TEXT", True, 0), ("byte_length", "INTEGER", True, 0))


class WideBuildError(ValueError):
    """Invalid source, out-of-scope path, or failed wide generation validation."""


@dataclass(frozen=True)
class WideGeneration:
    db_path: str
    generation_id: str
    projected_commit: str
    render_sha256: str
    record_count: int
    ready: bool


def _compact_json(value) -> str:
    """Sorted compact JSON, preserving Decimal as an exact bare number."""
    out: list[str] = []

    def emit(v) -> None:
        if v is None:
            out.append("null")
        elif v is True:
            out.append("true")
        elif v is False:
            out.append("false")
        elif isinstance(v, Decimal):
            if not v.is_finite():
                raise ValueError("non-finite decimal is not allowed")
            out.append(format(v, "f"))
        elif isinstance(v, int):
            out.append(str(v))
        elif isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError("non-finite float is not allowed")
            out.append(repr(v))
        elif isinstance(v, str):
            out.append(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, dict):
            out.append("{")
            for i, (key, item) in enumerate(sorted(v.items(), key=lambda kv: kv[0])):
                if i:
                    out.append(",")
                if not isinstance(key, str):
                    raise TypeError("json object key must be str")
                out.append(json.dumps(key, ensure_ascii=False))
                out.append(":")
                emit(item)
            out.append("}")
        elif isinstance(v, (list, tuple)):
            out.append("[")
            for i, item in enumerate(v):
                if i:
                    out.append(",")
                emit(item)
            out.append("]")
        else:
            raise TypeError("unsupported payload value type: %s" % type(v).__name__)

    emit(value)
    return "".join(out)


def _table_names(connection):
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _healthy(connection):
    connection.execute("PRAGMA foreign_keys=ON")
    if (connection.execute("PRAGMA foreign_keys").fetchone() != (1,)
            or connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchall()):
        raise WideBuildError("database integrity check failed")


def _source(connection, expected_repo_id, expected_company_id):
    if _table_names(connection) != _SOURCE_TABLES:
        raise WideBuildError("standard generation table set mismatch")
    _healthy(connection)
    meta = dict(connection.execute("SELECT key, value FROM generation_meta"))
    for key in ("generation_id", "projected_commit", "render_sha256", "repo_id",
                "company_id", "status"):
        if type(meta.get(key)) is not str or not meta[key] or "\x00" in meta[key]:
            raise WideBuildError("invalid standard metadata: " + key)
    if (meta["status"] != "ready" or meta["repo_id"] != expected_repo_id
            or meta["company_id"] != expected_company_id):
        raise WideBuildError("standard generation not ready or identity mismatch")
    if re.fullmatch(r"[0-9a-f]{32}", meta["generation_id"]) is None:
        raise WideBuildError("invalid generation_id")
    rows = connection.execute(
        "SELECT path, family, canonical_bytes, byte_length, sha256 "
        "FROM record_canonical ORDER BY path").fetchall()
    # D6 checks every canonical checksum, path, schema and source-owned scope.
    decoded = decode_rows(connection, [(p, f, n, h) for p, f, _, n, h in rows],
                          authz_digest="D8-local-build")
    counts = Counter(row[1] for row in rows)
    if (dict(connection.execute("SELECT family, record_count FROM family_counts")) != counts
            or str(len(rows)) != meta.get("record_count")
            or sum(decoded.skipped_families.values()) != sum(
                counts[family] for family in UNSCHEMA_FAMILIES)):
        raise WideBuildError("standard family/record counts mismatch")
    render = b"\x1e".join(f.encode() + b"\x1f" + p.encode() + b"\x1f" + raw
                            for p, f, raw, _, _ in rows)
    if sha256(render).hexdigest() != meta["render_sha256"]:
        raise WideBuildError("standard render digest mismatch")
    return meta, rows, decoded.records


def _layout(family):
    columns = [(c.name, c.sql_type, c.required, 0) for c in SCHEMA_COLUMNS[family]]
    if family == "fact_current":
        columns += [(name, "INTEGER" if name in {"pack_version", "binding_revision"}
                     else "TEXT", False, 0) for name in _OWNERSHIP]
    return columns + list(_SYSTEM)


def _assemble(records, source_rows):
    """Pure scalar projection and exact current-pointer ownership resolution."""
    payloads = {record.path: dict(record.payload) for record in records}
    pointers, placements = {}, {}
    for record in records:
        payload = payloads[record.path]
        if record.family == "placement_current":
            uid = payload["fact_uid"]
            if uid in pointers:
                raise WideBuildError("duplicate placement_current")
            pointers[uid] = payload["revision"]
        elif record.family == "placement_revision":
            key = (payload["fact_uid"], payload["revision"])
            if key in placements:
                raise WideBuildError("duplicate placement_revision")
            placements[key] = payload
    metadata = {p: (raw, n, h) for p, _, raw, n, h in source_rows}
    result = {family: [] for family in sorted(SCHEMA_COLUMNS)}
    for record in records:
        payload = payloads[record.path]
        values = []
        for column in SCHEMA_COLUMNS[record.family]:
            value = payload.get(column.name)
            valid = (value is None and not column.required
                     or column.sql_type == "TEXT" and type(value) is str
                     or column.sql_type == "INTEGER" and type(value) in (int, bool)
                     or column.sql_type == "REAL" and type(value) is float and math.isfinite(value))
            if not valid or (type(value) is int and not -(2**63) <= value < 2**63):
                raise WideBuildError("invalid scalar column: " + record.path + ":" + column.name)
            values.append(value)
        raw, length, digest = metadata[record.path]
        payload_json = _compact_json(payload)
        if git_format.dumps_record(git_format.loads_record(payload_json)) != raw:
            raise WideBuildError("payload JSON canonical roundtrip mismatch")
        if record.family == "fact_current":
            uid = payload["fact_uid"]
            placement = placements.get((uid, pointers.get(uid)))
            expected_ref = {"record_type": "fact_revision", "stable_id": uid,
                            "revision": payload["revision"]}
            if placement is None or placement["fact_ref"] != expected_ref:
                raise WideBuildError("missing or mismatched unique current placement: " + uid)
            values.extend(placement[name] for name in _OWNERSHIP)
        values.extend((record.path, payload_json, digest, length))
        result[record.family].append(tuple(values))
    return result


def _wide_meta(meta):
    return {**{key: meta[key] for key in (
        "generation_id", "projected_commit", "render_sha256", "repo_id", "company_id")},
        "wide_schema_version": "1", "status": "ready",
        "source_table_count": str(len(_SOURCE_TABLES)),
        "wide_table_count": str(len(SCHEMA_COLUMNS))}


def _write(path, expected, meta):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE wide_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for family, rows in expected.items():
                declarations = [name + " " + sql_type + (" NOT NULL" if required else "")
                                + (" PRIMARY KEY" if pk else "")
                                for name, sql_type, required, pk in _layout(family)]
                connection.execute("CREATE TABLE dw_" + family + " (" + ",".join(declarations) + ")")
                connection.executemany("INSERT INTO dw_" + family + " VALUES ("
                                       + ",".join("?" for _ in declarations) + ")", rows)
            connection.executemany("INSERT INTO wide_meta VALUES (?, ?)", sorted(meta.items()))


def _verify(path, expected, meta, source_rows):
    """Fresh read-only verification, including every scalar and ownership value."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        _healthy(connection)
        connection.execute("BEGIN")
        if _table_names(connection) != {"wide_meta", *("dw_" + f for f in SCHEMA_COLUMNS)}:
            raise WideBuildError("wide table set mismatch")
        if connection.execute("SELECT * FROM wide_meta ORDER BY key").fetchall() != sorted(meta.items()):
            raise WideBuildError("wide metadata mismatch")
        total = 0
        source = {p: (f, raw, n, h) for p, f, raw, n, h in source_rows}
        for family, rows in expected.items():
            layout = _layout(family)
            info = [(i, name, typ, int(required), None, pk)
                    for i, (name, typ, required, pk) in enumerate(layout)]
            if connection.execute("PRAGMA table_info(dw_" + family + ")").fetchall() != info:
                raise WideBuildError("wide columns mismatch: " + family)
            stored = connection.execute("SELECT * FROM dw_" + family + " ORDER BY path").fetchall()
            if stored != rows:
                raise WideBuildError("wide row/scalar/ownership mismatch: " + family)
            total += len(stored)
            offset = len(SCHEMA_COLUMNS[family]) + (len(_OWNERSHIP) if family == "fact_current" else 0)
            for row in stored:
                record_path, payload_json, digest, length = row[offset:offset + 4]
                source_family, raw, source_length, source_digest = source[record_path]
                encoded = git_format.dumps_record(git_format.loads_record(payload_json))
                if (source_family != family or encoded != raw or len(encoded) != length
                        or length != source_length or digest != source_digest
                        or sha256(encoded).hexdigest() != digest):
                    raise WideBuildError("wide canonical roundtrip mismatch")
        if total != sum(f in SCHEMA_COLUMNS for _, f, _, _, _ in source_rows):
            raise WideBuildError("wide total record count mismatch")


def _regular_path(value):
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise WideBuildError("symbolic links are not allowed")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise WideBuildError("standard generation must be a regular file")
    return resolved


def _sync_file(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def build_projection_wide(standard_db_path, *, directory, expected_repo_id,
                          expected_company_id) -> WideGeneration:
    """Build or fully verify one immutable companion without writing the source.

    flock on the read-only source inode serializes same-generation builders
    without adding lock files to the generation directory. No destination
    directory entry is created before the source and all payloads validate.
    """
    temp = final = None
    owned_final = False
    directory_fd = None
    lock = None
    try:
        source = _regular_path(standard_db_path)
        parent = Path(directory).resolve(strict=True)
        if not parent.is_dir() or source.parent != parent:
            raise WideBuildError("standard and derived generation must share directory")
        lock = source.open("rb")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            meta, source_rows, records = _source(connection, expected_repo_id, expected_company_id)
            final = parent / ("derived-" + meta["generation_id"] + ".sqlite")
            if final.is_symlink() or final.resolve().parent != parent:
                raise WideBuildError("derived generation is outside directory or a symlink")
            expected = _assemble(records, source_rows)
            wide_meta = _wide_meta(meta)
            if os.path.lexists(final):
                if not final.is_file():
                    raise WideBuildError("existing derived generation is not a regular file")
                _verify(final, expected, wide_meta, source_rows)
            else:
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                fd, name = tempfile.mkstemp(prefix="." + final.name + ".tmp-" + str(os.getpid()) + "-",
                                            dir=parent)
                temp = Path(name)
                os.close(fd)
                staging_meta = {key: value for key, value in wide_meta.items() if key != "status"}
                _write(temp, expected, staging_meta)
                _verify(temp, expected, staging_meta, source_rows)
                with closing(sqlite3.connect(temp)) as writer:
                    writer.execute("PRAGMA foreign_keys=ON")
                    with writer:
                        writer.execute("INSERT INTO wide_meta VALUES ('status', 'ready')")
                _verify(temp, expected, wide_meta, source_rows)
                _sync_file(temp)
                os.fsync(directory_fd)
                os.replace(temp, final)
                temp = None
                owned_final = True
                _verify(final, expected, wide_meta, source_rows)
                _sync_file(final)
                os.fsync(directory_fd)
            result = WideGeneration(str(final), meta["generation_id"], meta["projected_commit"],
                                    meta["render_sha256"], len(records), True)
            owned_final = False
            return result
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, WideBuildError):
            raise
        raise WideBuildError("cannot build/verify wide generation: " + str(exc)) from exc
    finally:
        try:
            for path in (temp, final if owned_final else None):
                if path is not None:
                    for suffix in ("-journal", "-wal", "-shm", ""):
                        Path(str(path) + suffix).unlink(missing_ok=True)
            if directory_fd is not None and (temp is not None or owned_final):
                os.fsync(directory_fd)
        except OSError as exc:
            raise WideBuildError("cannot clean failed wide generation: " + str(exc)) from exc
        finally:
            try:
                if directory_fd is not None:
                    os.close(directory_fd)
            finally:
                if lock is not None:
                    lock.close()
