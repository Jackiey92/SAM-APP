"""D10 deterministic Chinese FTS companion; source generations remain read-only."""
from contextlib import closing
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile

import jieba

from app.facts_tree import projection_wide as wide
from app.facts_tree.record_schema import SCHEMA_COLUMNS, SEARCHABLE_COLUMNS


FTS_TOKENIZER_VERSION = "jieba0.42.1-unicode61-v1"
_DICT_SHA256 = "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
jieba.dt.FREQ, jieba.dt.total = jieba.dt.gen_pfdict(jieba.dt.get_dict_file())
jieba.dt.initialized = True


class FTSBuildError(ValueError):
    """Invalid input or an unverifiable FTS component."""


@dataclass(frozen=True)
class FTSGeneration:
    db_path: str
    generation_id: str
    projected_commit: str
    render_sha256: str
    record_count: int
    ready: bool = True


@dataclass(frozen=True)
class SearchHit:
    path: str
    family: str
    score: float
    fact_uid: str | None = None
    revision: int | None = None
    status: str | None = None


def tokenize_text(text: str) -> str:
    """Segment using the fixed in-memory dictionary without disk caches."""
    if type(text) is not str:
        raise FTSBuildError("text must be a string")
    return " ".join(token for token in jieba.lcut(text) if token.strip())


def build_match_expression(query: str) -> str:
    tokens = tokenize_text(query).split()
    if not tokens:
        raise FTSBuildError("empty query")
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _dictionary_digest():
    if jieba.__version__ != "0.42.1":
        raise FTSBuildError("jieba version mismatch")
    with jieba.dt.get_dict_file() as stream:
        digest = sha256(stream.read()).hexdigest()
    if digest != _DICT_SHA256:
        raise FTSBuildError("jieba dictionary mismatch")
    return digest


def _rows(records):
    result = []
    for record in records:
        values = (record.payload.get(name) for name in SEARCHABLE_COLUMNS[record.family])
        raw = " ".join(value for value in values if type(value) is str and value.strip())
        result.append((record.path, record.family, tokenize_text(raw)))
    return sorted(result)


def _meta(source_meta, count):
    return {**{key: source_meta[key] for key in (
        "generation_id", "projected_commit", "render_sha256", "repo_id", "company_id")},
        "fts_schema_version": "1", "tokenizer": FTS_TOKENIZER_VERSION,
        "jieba_dict_sha256": _dictionary_digest(),
        "indexed_column_count": str(sum(map(len, SEARCHABLE_COLUMNS.values()))),
        "fts_table_count": "2", "record_count": str(count), "status": "ready"}


def _write_fts(path, rows, meta):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE fts_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE VIRTUAL TABLE fts_records USING fts5("
                               "path UNINDEXED, family UNINDEXED, record_text, tokenize='unicode61')")
            connection.executemany("INSERT INTO fts_records VALUES (?,?,?)", rows)
            connection.executemany("INSERT INTO fts_meta VALUES (?,?)",
                                   sorted((k, v) for k, v in meta.items() if k != "status"))
            connection.execute("INSERT INTO fts_meta VALUES ('status','ready')")


def _verify_fts(path, rows, meta, expected, wide_meta, source_rows):
    wide._verify(path, expected, wide_meta, source_rows)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        wide._healthy(connection)
        connection.execute("BEGIN")
        tables = wide._table_names(connection) - {r[0] for r in connection.execute(
            "SELECT name FROM pragma_table_list WHERE schema='main' AND type='shadow'")}
        if tables != {"wide_meta", "fts_meta", "fts_records", *("dw_" + f for f in SCHEMA_COLUMNS)}:
            raise FTSBuildError("FTS table set mismatch")
        sql, = connection.execute("SELECT sql FROM sqlite_master WHERE name='fts_records'").fetchone()
        if sql != ("CREATE VIRTUAL TABLE fts_records USING fts5("
                   "path UNINDEXED, family UNINDEXED, record_text, tokenize='unicode61')"):
            raise FTSBuildError("FTS definition mismatch")
        if connection.execute("SELECT * FROM fts_meta ORDER BY key").fetchall() != sorted(meta.items()):
            raise FTSBuildError("FTS metadata mismatch")
        if meta["jieba_dict_sha256"] != _dictionary_digest():
            raise FTSBuildError("FTS dictionary mismatch")
        stored = connection.execute("SELECT path,family,record_text FROM fts_records ORDER BY path").fetchall()
        if stored != rows:
            raise FTSBuildError("FTS record content/path mismatch")
        # unicode61 ignores punctuation-only tokens; sample a word-bearing token.
        for path_value, _, text in rows:
            token = next((t for t in text.split() if any(c.isalnum() for c in t)), None)
            if token is not None:
                expression = '"' + token.replace('"', '""') + '"'
                if not connection.execute("SELECT 1 FROM fts_records WHERE fts_records MATCH ? AND path=?",
                                          (expression, path_value)).fetchone():
                    raise FTSBuildError("FTS MATCH verification failed")


def search_records(connection, query, *, family=None, status=None, leaf_code=None, limit=100):
    """Search a pinned connection with structural filters and bound values."""
    expression = build_match_expression(query)
    if family is not None and (type(family) is not str or family not in SCHEMA_COLUMNS):
        raise FTSBuildError("unknown family")
    if family == "fact_current":
        raise FTSBuildError("fact_current has no searchable text")
    if type(limit) is not int or limit <= 0:
        raise FTSBuildError("limit must be a positive integer")
    if leaf_code is not None and family != "fact_revision":
        raise FTSBuildError("leaf_code requires a fact family")
    if status is not None and (family is None or (family != "fact_revision" and not any(
            c.name == "status" for c in SCHEMA_COLUMNS[family]))):
        raise FTSBuildError("status requires a family with a status column")
    joins, conditions, params = [], ["fts_records MATCH ?"], [expression]
    if family is not None:
        joins.append("JOIN dw_" + family + " d ON d.path=f.path")
        conditions.append("f.family=?")
        params.append(family)
    current_join = family == "fact_revision" and (status is not None or leaf_code is not None)
    if current_join:
        joins.append("JOIN dw_fact_current w ON w.fact_uid=d.fact_uid AND w.revision=d.revision")
    if status is not None:
        conditions.append(("w" if current_join else "d") + ".status=?")
        params.append(status)
    if leaf_code is not None:
        conditions.append("w.leaf_code=?")
        params.append(leaf_code)
    fields = "f.path,f.family,bm25(fts_records)"
    if family == "fact_revision":
        fields += ",d.fact_uid,d.revision," + ("w.status" if current_join else "NULL")
    sql = ("SELECT " + fields + " FROM fts_records f " + " ".join(joins)
           + " WHERE " + " AND ".join(conditions) + " ORDER BY bm25(fts_records),f.path LIMIT ?")
    return [SearchHit(*row) for row in connection.execute(sql, (*params, limit))]


def build_projection_fts(standard_db_path, *, directory, expected_repo_id,
                         expected_company_id) -> FTSGeneration:
    """Verify, copy and atomically upgrade a same-directory wide companion."""
    temp = rollback = final = None
    published = False
    directory_fd = lock = None
    try:
        source = wide._regular_path(standard_db_path)
        parent = Path(directory).resolve(strict=True)
        if not parent.is_dir() or source.parent != parent:
            raise FTSBuildError("standard and derived generation must share directory")
        lock = source.open("rb")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            meta, source_rows, records = wide._source(connection, expected_repo_id, expected_company_id)
        expected = wide._assemble(records, source_rows)
        wide_meta = wide._wide_meta(meta)
        rows = _rows(records)
        fts_meta = _meta(meta, len(rows))
        final = parent / ("derived-" + meta["generation_id"] + ".sqlite")
        exists = os.path.lexists(final)
        if exists:
            wide._regular_path(final)
            wide._verify(final, expected, wide_meta, source_rows)
            with closing(sqlite3.connect(final.as_uri() + "?mode=ro", uri=True)) as reader:
                has_fts = "fts_records" in wide._table_names(reader)
            if has_fts:
                _verify_fts(final, rows, fts_meta, expected, wide_meta, source_rows)
                return FTSGeneration(str(final), meta["generation_id"], meta["projected_commit"],
                                     meta["render_sha256"], len(rows))
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        fd, name = tempfile.mkstemp(prefix="." + final.name + ".tmp-", dir=parent)
        os.close(fd)
        temp = Path(name)
        if exists:
            with closing(sqlite3.connect(final.as_uri() + "?mode=ro", uri=True)) as reader:
                with closing(sqlite3.connect(temp)) as writer:
                    reader.backup(writer)
        else:
            wide._write(temp, expected, wide_meta)
        _write_fts(temp, rows, fts_meta)
        _verify_fts(temp, rows, fts_meta, expected, wide_meta, source_rows)
        wide._sync_file(temp)
        if exists:
            # Keep the old inode available until post-publication verification succeeds.
            rollback = Path(str(temp) + ".rollback")
            os.link(final, rollback)
        os.fsync(directory_fd)
        os.replace(temp, final)
        published = True
        _verify_fts(final, rows, fts_meta, expected, wide_meta, source_rows)
        wide._sync_file(final)
        os.fsync(directory_fd)
        if rollback is not None:
            rollback.unlink()
            rollback = None
        published = False
        return FTSGeneration(str(final), meta["generation_id"], meta["projected_commit"],
                             meta["render_sha256"], len(rows))
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError, RecursionError) as exc:
        if published:
            if rollback is not None:
                os.replace(rollback, final)
                rollback = None
            else:
                final.unlink(missing_ok=True)
            if directory_fd is not None:
                os.fsync(directory_fd)
        if isinstance(exc, FTSBuildError):
            raise
        raise FTSBuildError("cannot build/verify FTS generation: " + str(exc)) from exc
    finally:
        try:
            for path in (temp, rollback):
                if path is not None:
                    for suffix in ("-journal", "-wal", "-shm", ""):
                        Path(str(path) + suffix).unlink(missing_ok=True)
        except OSError as exc:
            raise FTSBuildError("cannot clean failed FTS generation: " + str(exc)) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if lock is not None:
                lock.close()
