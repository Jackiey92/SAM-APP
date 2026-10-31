"""Pinned, authorization-bound wide and FTS keyset queries on paired stores."""
from dataclasses import dataclass
from hashlib import sha256

from app.facts_tree.projection_bundle import ProjectionBundle
from app.facts_tree import projection_fts as fts
from app.facts_tree.record_schema import SCHEMA_COLUMNS
from app.facts_tree.projection_cursor import (
    CursorError, decode_cursor, encode_cursor, require_cursor_key,
)
from app.facts_tree.projection_query import (
    DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, ProjectionQueryError, InvalidPageCursor,
    ProjectionAdvanced, SnapshotExpired,
)


@dataclass(frozen=True)
class WideRow:
    path: str
    family: str
    payload_json: str
    canonical_sha256: str
    byte_length: int


@dataclass(frozen=True)
class WidePage:
    rows: tuple[WideRow, ...]
    next_cursor: str | None
    generation_id: str
    projected_commit: str
    family: str


@dataclass(frozen=True)
class SearchHitRow:
    path: str
    family: str
    score: str
    fact_uid: str | None
    revision: int | None
    status: str | None


@dataclass(frozen=True)
class SearchPage:
    hits: tuple[SearchHitRow, ...]
    next_cursor: str | None
    generation_id: str
    projected_commit: str
    record_count: int


def _text(value, name):
    if type(value) is not str or not value or "\x00" in value:
        raise ProjectionQueryError(name + " must be nonempty str without NUL")


def _validate(bundle, authz_digest, limit):
    if not isinstance(bundle, ProjectionBundle):
        raise ProjectionQueryError("bundle must be ProjectionBundle")
    _text(authz_digest, "authz_digest")
    if type(limit) is not int or limit <= 0:
        raise ProjectionQueryError("limit must be a positive integer")
    if limit > MAX_PAGE_LIMIT:
        raise ProjectionQueryError("limit must be int in 1..1000")
    try:
        require_cursor_key()
    except CursorError:
        raise ProjectionQueryError("cursor key not configured") from None


def _decode(token, kind, digest_key):
    keys = frozenset({"v", "kind", "generation_id", "projected_commit", digest_key,
                      "authz_digest", "last_path", "limit"})
    if kind == "fts-v1":
        keys |= {"last_score"}
    try:
        data = decode_cursor(token, expected_keys=keys)
        if (type(data["v"]) is not int or data["v"] != 1 or data["kind"] != kind
                or type(data["limit"]) is not int or not 1 <= data["limit"] <= MAX_PAGE_LIMIT):
            raise ValueError("shape")
        for key in keys - {"v", "limit"}:
            _text(data[key], key)
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidPageCursor("invalid page cursor") from None


def _pin(data, serving, digest_key, digest, authz_digest, limit):
    if data is None:
        return
    if data["generation_id"] != serving.generation_id:
        raise SnapshotExpired("SNAPSHOT_EXPIRED")
    if data["projected_commit"] != serving.projected_commit:
        raise ProjectionAdvanced("PROJECTION_ADVANCED")
    if data[digest_key] != digest or data["limit"] != limit:
        raise InvalidPageCursor("invalid page cursor")
    if data["authz_digest"] != authz_digest:
        error = InvalidPageCursor("authz_changed")
        error.reason = "authz_changed"
        raise error


def _encode(payload):
    try:
        return encode_cursor(payload)
    except CursorError as exc:
        raise ProjectionQueryError(str(exc)) from None


def list_family_rows(bundle, family: str, *, authz_digest: str,
                     limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None) -> WidePage:
    """Return verbatim system columns in path order for one schema family."""
    _validate(bundle, authz_digest, limit)
    if type(family) is not str or family not in SCHEMA_COLUMNS:
        raise ProjectionQueryError("unknown family")
    digest = sha256(family.encode()).hexdigest()
    data = _decode(cursor, "wide-v1", "family_digest") if cursor is not None else None
    serving = bundle.serving
    _pin(data, serving, "family_digest", digest, authz_digest, limit)
    sql = "SELECT path, payload_json, canonical_sha256, byte_length FROM dw_" + family
    params = []
    if data is not None:
        sql += " WHERE path > ?"
        params.append(data["last_path"])
    raw = bundle.derived.execute(sql + " ORDER BY path ASC LIMIT ?", (*params, limit + 1)).fetchall()
    rows = tuple(WideRow(path, family, payload, checksum, size)
                 for path, payload, checksum, size in raw[:limit])
    token = None
    if len(raw) > limit:
        token = _encode(dict(v=1, kind="wide-v1", generation_id=serving.generation_id,
                             projected_commit=serving.projected_commit, family_digest=digest,
                             authz_digest=authz_digest, last_path=rows[-1].path, limit=limit))
    return WidePage(rows, token, serving.generation_id, serving.projected_commit, family)


def _search_view(family, status, leaf_code, current_only):
    if family is not None and (type(family) is not str or family not in SCHEMA_COLUMNS):
        raise ProjectionQueryError("unknown family")
    if family == "fact_current":
        raise ProjectionQueryError("fact_current has no searchable text")
    if current_only is not None and type(current_only) is not bool:
        raise ProjectionQueryError("current_only must be bool or None")
    if current_only is True and family != "fact_revision":
        raise ProjectionQueryError("current_only requires fact_revision")
    if leaf_code is not None and family != "fact_revision":
        raise ProjectionQueryError("leaf_code requires a fact family")
    if status is not None and (family is None or (family != "fact_revision" and not any(
            c.name == "status" for c in SCHEMA_COLUMNS[family]))):
        raise ProjectionQueryError("status requires a family with a status column")
    filtered_fact = family == "fact_revision" and (status is not None or leaf_code is not None)
    if filtered_fact and current_only is False:
        raise ProjectionQueryError("historical fact view does not support status or leaf_code")
    return family == "fact_revision" and (current_only is True or filtered_fact)


def _record_count(connection):
    row = connection.execute("SELECT value FROM fts_meta WHERE key='record_count'").fetchone()
    if (row is None or type(row[0]) is not str or not row[0]
            or not row[0].isascii() or not row[0].isdecimal()):
        raise ProjectionQueryError("invalid fts record_count")
    try:
        return int(row[0])
    except ValueError:
        raise ProjectionQueryError("invalid fts record_count") from None


def search(bundle, query: str, *, authz_digest: str, family: str | None = None,
           status: str | None = None, leaf_code: str | None = None,
           current_only: bool | None = None, limit: int = DEFAULT_PAGE_LIMIT,
           cursor: str | None = None) -> SearchPage:
    """Search a paired generation in the frozen textual-score/path total order."""
    _validate(bundle, authz_digest, limit)
    if type(query) is not str or "\x00" in query:
        raise ProjectionQueryError("invalid query")
    try:
        expression = fts.build_match_expression(query)
    except fts.FTSBuildError as exc:
        message = "empty query" if str(exc) == "empty query" else "invalid query"
        raise ProjectionQueryError(message) from None
    current = _search_view(family, status, leaf_code, current_only)
    digest = sha256("\n".join([repr(query), repr(family), repr(status), repr(leaf_code),
                               repr(bool(current_only)), str(limit)]).encode()).hexdigest()
    data = _decode(cursor, "fts-v1", "search_digest") if cursor is not None else None
    serving = bundle.serving
    _pin(data, serving, "search_digest", digest, authz_digest, limit)
    count = _record_count(bundle.derived)
    joins, clauses, params = [], ["fts_records MATCH ?"], [expression]
    if family is not None:
        joins.append("JOIN dw_" + family + " d ON d.path=f.path")
        clauses.append("f.family=?")
        params.append(family)
    if current:
        joins.append("JOIN dw_fact_current w ON w.fact_uid=d.fact_uid AND w.revision=d.revision")
    if status is not None:
        clauses.append(("w" if current else "d") + ".status=?")
        params.append(status)
    if leaf_code is not None:
        clauses.append("w.leaf_code=?")
        params.append(leaf_code)
    score = "CAST(bm25(fts_records) AS TEXT)"
    if data is not None:
        clauses.append("(" + score + " > ? OR (" + score + " = ? AND f.path > ?))")
        params.extend((data["last_score"], data["last_score"], data["last_path"]))
    fields = "f.path,f.family," + score
    fields += (",d.fact_uid,d.revision," + ("w.status" if current else "NULL")
               if family == "fact_revision" else ",NULL,NULL,NULL")
    sql = ("SELECT " + fields + " FROM fts_records f " + " ".join(joins)
           + " WHERE " + " AND ".join(clauses) + " ORDER BY " + score + ",f.path LIMIT ?")
    raw = bundle.derived.execute(sql, (*params, limit + 1)).fetchall()
    hits = tuple(SearchHitRow(*row) for row in raw[:limit])
    token = None
    if len(raw) > limit:
        token = _encode(dict(v=1, kind="fts-v1", generation_id=serving.generation_id,
                             projected_commit=serving.projected_commit, search_digest=digest,
                             authz_digest=authz_digest, last_score=hits[-1].score,
                             last_path=hits[-1].path, limit=limit))
    return SearchPage(hits, token, serving.generation_id, serving.projected_commit, count)
