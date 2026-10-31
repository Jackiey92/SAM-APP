"""Read-only, same-generation standard and derived projection handles."""
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePath
import sqlite3

from app.facts_tree import projection_router as router
from app.facts_tree.record_schema import SCHEMA_COLUMNS


class BundleUnavailable(router.ProjectionRouterError):
    """The current generation's derived companion is unavailable or untrusted."""


@dataclass(frozen=True)
class ProjectionBundle:
    serving: router.ServingGeneration
    standard: sqlite3.Connection
    derived: sqlite3.Connection
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        try:
            self.standard.close()
        finally:
            self.derived.close()

    def __enter__(self) -> "ProjectionBundle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def derived_path(serving: router.ServingGeneration) -> PurePath:
    """Derive the companion name without reading or resolving any path."""
    path = PurePath(serving.db_path)
    if not path.name.startswith("gen-") or not path.name.endswith(".sqlite"):
        raise BundleUnavailable("standard generation filename must be gen-*.sqlite")
    return path.parent / ("derived-" + path.name[len("gen-"):])


def _verify_meta(connection, table, expected):
    info = connection.execute("PRAGMA table_info(" + table + ")").fetchall()
    if (len(info) != 2 or info[0][1:3] != ("key", "TEXT") or info[0][5] != 1
            or info[1][1:4] != ("value", "TEXT", 1) or info[1][5] != 0):
        raise BundleUnavailable(table + " schema mismatch")
    rows = connection.execute("SELECT key, value FROM " + table).fetchall()
    for key, value in rows:
        if type(key) is not str or not key:
            raise BundleUnavailable(table + " must contain nonempty TEXT keys")
        if type(value) is not str or not value:
            name = key if key in expected else "value"
            raise BundleUnavailable(table + " invalid metadata value: " + name)
    meta = dict(rows)
    if len(meta) != len(rows):
        raise BundleUnavailable(table + " duplicate metadata key")
    for key, value in expected.items():
        if meta.get(key) != value:
            raise BundleUnavailable(table + " metadata mismatch: " + key)


def _verify_derived(connection, path, serving, repo_id, company_id):
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise BundleUnavailable("derived integrity_check failed")
    connection.execute("PRAGMA query_only=ON")
    # Use SQLite's shadow classification, matching the publication gate, rather
    # than treating every similarly prefixed ordinary table as an FTS shadow.
    tables = dict(connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT IN (SELECT name FROM pragma_table_list "
        "WHERE schema='main' AND type='shadow')"))
    fts_sql = tables.get("fts_records")
    if (type(fts_sql) is not str
            or not fts_sql.startswith("CREATE VIRTUAL TABLE fts_records USING fts5(")):
        raise BundleUnavailable("fts_records must be an fts5 virtual table")
    public_tables = {name for name in tables if not name.startswith("sqlite_")}
    expected_tables = {"wide_meta", "fts_meta", "fts_records", *("dw_" + f for f in SCHEMA_COLUMNS)}
    if public_tables != expected_tables:
        raise BundleUnavailable("derived table set mismatch")
    expected = dict(generation_id=serving.generation_id, projected_commit=serving.projected_commit,
                    render_sha256=serving.render_sha256, repo_id=repo_id,
                    company_id=company_id, status="ready")
    for table in ("wide_meta", "fts_meta"):
        _verify_meta(connection, table, expected)
    for family in SCHEMA_COLUMNS:
        name = "dw_" + family
        sql = tables[name]
        if (type(sql) is not str or not sql.upper().startswith("CREATE TABLE ")
                or not connection.execute("PRAGMA table_info(" + name + ")").fetchall()):
            raise BundleUnavailable("wide table must be an ordinary table: " + name)


def open_bundle(root, *, repo_id: str, company_id: str) -> ProjectionBundle:
    """Open exactly the current standard generation and its trusted companion."""
    serving, standard = router.open_current(root, repo_id=repo_id, company_id=company_id)
    with ExitStack() as cleanup:
        cleanup.callback(standard.close)
        detail = "cannot resolve derived boundary directory"
        try:
            directory = router._regular(Path(serving.db_path)).parent
            detail = "cannot resolve derived companion path"
            path = router._regular(router._path(str(derived_path(serving)), "derived_db_path"))
            if (path.parent != directory
                    or path.name != "derived-" + serving.generation_id + ".sqlite"):
                raise BundleUnavailable("derived filename/directory/identity mismatch")
            router._no_symlinks(path)
            router._within(path, directory)
            detail = "cannot open derived companion: " + str(path)
            derived = router._connect(path)
            cleanup.callback(derived.close)
            detail = "cannot verify derived companion: " + str(path)
            _verify_derived(derived, path, serving, repo_id, company_id)
            result = ProjectionBundle(serving, standard, derived)
        except (BundleUnavailable, OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
            if isinstance(exc, BundleUnavailable):
                detail = str(exc)
            raise BundleUnavailable(router._message(
                repo_id, company_id, serving.generation_id, detail)) from exc
        cleanup.pop_all()
        return result
