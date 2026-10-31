"""Read-only, generation-pinned canonical metadata queries (appendix D4)."""
from dataclasses import dataclass
from hashlib import sha256
import sqlite3
from typing import Optional

from app.facts_tree.projection_cursor import (
    CursorError, decode_cursor, encode_cursor, require_cursor_key, set_cursor_key,
)

DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 1000


@dataclass(frozen=True)
class RecordRow:
    path: str
    family: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class RecordPage:
    records: tuple[RecordRow, ...]
    next_cursor: Optional[str]
    generation_id: str
    projected_commit: str
    record_count: int


class ProjectionQueryError(ValueError):
    """Invalid input or an unavailable query generation."""


class InvalidPageCursor(ProjectionQueryError):
    """Untrusted cursor or changed request context."""


class ProjectionAdvanced(ProjectionQueryError):
    """The pinned generation has a different projected commit."""


class SnapshotExpired(ProjectionQueryError):
    """The connection no longer represents the pinned generation."""


def _validate(connection, authz_digest):
    if not isinstance(connection, sqlite3.Connection):
        raise ProjectionQueryError("connection must be sqlite3.Connection")
    _text(authz_digest, "authz_digest")
    try:
        require_cursor_key()
    except CursorError:
        raise ProjectionQueryError("cursor key not configured") from None


def _text(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProjectionQueryError(name + " must be nonempty str without NUL")


def _meta(connection):
    values = []
    for key in ("generation_id", "projected_commit", "record_count", "status"):
        row = connection.execute(
            "SELECT value FROM generation_meta WHERE key=?", (key,)).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise ProjectionQueryError("invalid generation meta: " + key)
        values.append(row[0])
    generation, commit, count, status = values
    if status != "ready" or not count.isascii() or not count.isdecimal():
        raise ProjectionQueryError("generation not ready or invalid record_count")
    return generation, commit, int(count)


def _decode(token):
    try:
        require_cursor_key()
        data = decode_cursor(token, expected_keys=frozenset({
            "v", "generation_id", "projected_commit", "query_digest",
            "authz_digest", "last_path", "limit"}))
        if (type(data["v"]) is not int or data["v"] != 1
                or type(data["limit"]) is not int or not 1 <= data["limit"] <= MAX_PAGE_LIMIT):
            raise ValueError("shape")
        for key in ("generation_id", "projected_commit", "query_digest", "authz_digest", "last_path"):
            _text(data[key], key)
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidPageCursor("invalid page cursor") from None


def list_records(connection: sqlite3.Connection, *, authz_digest: str,
                 family: Optional[str] = None, path_prefix: Optional[str] = None,
                 include_canonical: bool = False, limit: int = DEFAULT_PAGE_LIMIT,
                 cursor: Optional[str] = None) -> RecordPage:
    """Return path-ordered metadata, bound to a generation and host authorization."""
    _validate(connection, authz_digest)
    for name, value in (("family", family), ("path_prefix", path_prefix)):
        if value is not None:
            _text(value, name)
    if type(include_canonical) is not bool or include_canonical:
        raise ProjectionQueryError("include_canonical is unsupported")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ProjectionQueryError("limit must be int in 1..1000")
    data = _decode(cursor) if cursor is not None else None
    generation, commit, count = _meta(connection)
    digest = sha256("\n".join([repr(family), repr(path_prefix),
                               str(include_canonical), str(limit)]).encode()).hexdigest()
    if data is not None:
        if data["generation_id"] != generation:
            raise SnapshotExpired("SNAPSHOT_EXPIRED")
        if data["projected_commit"] != commit:
            raise ProjectionAdvanced("PROJECTION_ADVANCED")
        if data["query_digest"] != digest or data["limit"] != limit:
            raise InvalidPageCursor("invalid page cursor")
        if data["authz_digest"] != authz_digest:
            error = InvalidPageCursor("authz_changed")
            error.reason = "authz_changed"
            raise error
    clauses, params = [], []
    if family is not None:
        clauses.append("family = ?")
        params.append(family)
    if path_prefix is not None:
        clauses.append("path LIKE ? ESCAPE '\\'")
        params.append(path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        # SQLite LIKE folds ASCII case; enforce the literal prefix as well.
        clauses.append("substr(path, 1, ?) = ?")
        params.extend((len(path_prefix), path_prefix))
    if data is not None:
        clauses.append("path > ?")
        params.append(data["last_path"])
    sql = "SELECT path, family, byte_length, sha256 FROM record_canonical"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = connection.execute(sql + " ORDER BY path ASC LIMIT ?", (*params, limit + 1)).fetchall()
    records = tuple(RecordRow(*row) for row in rows[:limit])
    token = None
    if len(rows) > limit:
        token = encode_cursor(dict(v=1, generation_id=generation, projected_commit=commit,
                             query_digest=digest, authz_digest=authz_digest,
                             last_path=records[-1].path, limit=limit))
    return RecordPage(records, token, generation, commit, count)


def get_record_canonical(connection: sqlite3.Connection, path: str, *,
                         authz_digest: str) -> tuple[bytes, str, str, int]:
    """Fetch canonical bytes verbatim, leaving interpretation to the caller."""
    _validate(connection, authz_digest)
    _text(path, "path")
    _meta(connection)
    row = connection.execute(
        "SELECT canonical_bytes, family, sha256, byte_length FROM record_canonical WHERE path=?",
        (path,)).fetchone()
    if row is None:
        raise ProjectionQueryError("record_not_found")
    return tuple(row)
