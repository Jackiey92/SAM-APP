"""Read-only, schema-validated canonical generation decoding (appendix D6)."""
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
import sqlite3
from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional, Tuple

from app.facts_tree import git_format
from app.facts_tree.record_schema import loads_schema_record

UNSCHEMA_FAMILIES = frozenset({
    "repository_meta", "taxonomy_manifest", "taxonomy_leaf", "bindings_current"})
_SCHEMA_FAMILIES = frozenset({
    "consumed_candidate", "entity_current", "entity_revision", "event", "evidence",
    "fact_current", "fact_revision", "input_candidate", "issue_event", "link_revision",
    "operation", "placement_current", "placement_revision", "proposal_event",
    "source_lifecycle", "source_version"})


@dataclass(frozen=True)
class DecodedRecord:
    path: str
    family: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class DecodeResult:
    records: Tuple[DecodedRecord, ...]
    decoded_families: Mapping[str, int]
    skipped_families: Mapping[str, int]
    generation_id: str
    projected_commit: str


class ProjectionDecodeError(ValueError):
    """Invalid request, generation metadata, or canonical byte integrity."""


def _text(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProjectionDecodeError(name + " must be nonempty str without NUL")


def _context(connection, authz_digest):
    if not isinstance(connection, sqlite3.Connection):
        raise ProjectionDecodeError("connection must be sqlite3.Connection")
    _text(authz_digest, "authz_digest")
    meta = {}
    for key in ("repo_id", "company_id", "object_format", "generation_id",
                "projected_commit", "record_count", "status"):
        row = connection.execute("SELECT value FROM generation_meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ProjectionDecodeError("invalid generation meta: " + key)
        _text(row[0], "generation meta: " + key)
        meta[key] = row[0]
    count = meta["record_count"]
    if meta["status"] != "ready" or not count.isascii() or not count.isdecimal():
        raise ProjectionDecodeError("generation not ready or invalid record_count")
    if meta["object_format"] not in ("sha1", "sha256"):
        raise ProjectionDecodeError("invalid generation meta: object_format")
    return meta


def _paths(paths):
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence) or not paths:
        raise ProjectionDecodeError("paths must be a nonempty sequence")
    for path in paths:
        _text(path, "path")
    if len(set(paths)) != len(paths):
        raise ProjectionDecodeError("duplicate path")
    return sorted(paths)


def _fetch(connection, paths):
    rows = []
    for start in range(0, len(paths), 500):
        batch = paths[start:start + 500]
        rows.extend(connection.execute(
            "SELECT path, family, canonical_bytes, byte_length, sha256 FROM record_canonical "
            "WHERE path IN (" + ",".join("?" for _ in batch) + ")", batch))
    if len(rows) != len(paths):
        raise ProjectionDecodeError("record_not_found")
    return sorted(rows, key=lambda row: row[0])


def _decode(rows, meta, families=None, limit=None, expected=None):
    records, decoded, skipped = [], Counter(), Counter()
    for path, family, raw, length, digest in rows:
        if families is not None and family not in families:
            continue
        if (type(raw) is not bytes or type(length) is not int or len(raw) != length
                or sha256(raw).hexdigest() != digest):
            raise ProjectionDecodeError("canonical_bytes_mismatch: " + path)
        if expected is not None and expected[path] != (family, length, digest):
            raise ProjectionDecodeError("row_metadata_mismatch: " + path)
        if git_format.canonical_record_type(path) != family:
            raise ProjectionDecodeError("record_family_mismatch: " + path)
        if family in UNSCHEMA_FAMILIES:
            skipped[family] += 1
            continue
        if family not in _SCHEMA_FAMILIES:
            raise ProjectionDecodeError("unknown family: " + family)
        payload = loads_schema_record(raw, path=path, repo_id=meta["repo_id"],
                                      company_id=meta["company_id"], object_format=meta["object_format"])
        records.append(DecodedRecord(path, family, MappingProxyType(payload)))
        decoded[family] += 1
        if limit is not None and len(records) == limit:
            break
    return DecodeResult(tuple(records), MappingProxyType(dict(sorted(decoded.items()))),
                        MappingProxyType(dict(sorted(skipped.items()))),
                        meta["generation_id"], meta["projected_commit"])


def decode_generation_records(connection, *, authz_digest: str,
                              families: Optional[FrozenSet[str]] = None,
                              paths: Optional[Sequence[str]] = None,
                              limit: Optional[int] = None) -> DecodeResult:
    """Decode a deterministic path prefix, using only generation-owned scope."""
    meta = _context(connection, authz_digest)
    if families is not None:
        if not isinstance(families, (frozenset, set, tuple, list)) or not families:
            raise ProjectionDecodeError("families must be a nonempty container")
        for family in families:
            _text(family, "family")
            if family not in _SCHEMA_FAMILIES:
                raise ProjectionDecodeError("unknown family: " + family)
        families = frozenset(families)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ProjectionDecodeError("limit must be int >= 1")
    if paths is not None:
        rows = _fetch(connection, _paths(paths))
    else:
        sql = "SELECT path, family, canonical_bytes, byte_length, sha256 FROM record_canonical"
        params = sorted(families) if families is not None else []
        if families is not None:
            sql += " WHERE family IN (" + ",".join("?" for _ in params) + ")"
        rows = connection.execute(sql + " ORDER BY path", params)
    return _decode(rows, meta, families, limit)


def decode_rows(connection, rows, *, authz_digest: str) -> DecodeResult:
    """Decode D4 metadata rows, checking them against the pinned generation."""
    meta = _context(connection, authz_digest)
    expected = {}
    try:
        for row in rows:
            if hasattr(row, "path"):
                path, family, length, digest = row.path, row.family, row.byte_length, row.sha256
            else:
                path, family, length, digest = row
            _text(path, "path")
            _text(family, "family")
            _text(digest, "sha256")
            if type(length) is not int or length < 0 or path in expected:
                raise ProjectionDecodeError("invalid or duplicate row")
            expected[path] = (family, length, digest)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ProjectionDecodeError):
            raise
        raise ProjectionDecodeError("rows must contain canonical metadata rows") from exc
    return _decode(_fetch(connection, sorted(expected)), meta, expected=expected)
