"""Single-hop COW derived generations; standard sources and parents stay read-only."""
from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile

from app.facts_tree import projection_wide as wide, projection_fts as fts
from app.facts_tree.projection_row_delta import wide_row_changes, fts_row_changes


class DerivedAdvanceError(ValueError):
    """Invalid inputs or failed construction/verification of a derived generation."""


def _unlink_quietly(temp):
    """Remove only this call's staging database and SQLite sidecars."""
    if temp is not None:
        for suffix in ('-journal', '-wal', '-shm', ''):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def _apply(writer, changes, rows, fts_changes, wide_meta, fts_meta):
    """Apply all row and metadata changes on one connection in one transaction."""
    writer.execute('PRAGMA foreign_keys=ON')
    with writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('DELETE FROM wide_meta')
        writer.executemany('INSERT INTO wide_meta VALUES (?, ?)', sorted(wide_meta.items()))
        for family, change in changes.items():
            # Families come from the shared assembler's fixed schema union.
            layout = wide._layout(family)
            table = 'dw_' + family
            columns = [info[1] for info in writer.execute('PRAGMA table_info(' + table + ')')]
            if columns != [column[0] for column in layout]:
                raise DerivedAdvanceError('wide columns mismatch: ' + family)
            delete = 'DELETE FROM ' + table + ' WHERE path=?'
            insert = ('INSERT INTO ' + table + ' (' + ','.join(columns) + ') VALUES ('
                      + ','.join('?' for _ in layout) + ')')
            writer.executemany(delete, [(row[-4],) for row in change.removed + change.modified])
            writer.executemany(insert, change.modified + change.added)
        if not fts_changes.is_empty():
            writer.execute('DROP TABLE fts_records')
            writer.execute("CREATE VIRTUAL TABLE fts_records USING fts5("
                           "path UNINDEXED, family UNINDEXED, record_text, tokenize='unicode61')")
            writer.executemany('INSERT INTO fts_records VALUES (?,?,?)', rows)
        writer.execute('DELETE FROM fts_meta')
        writer.executemany('INSERT INTO fts_meta VALUES (?,?)',
                           sorted((k, v) for k, v in fts_meta.items() if k != 'status'))
        writer.execute("INSERT INTO fts_meta VALUES ('status','ready')")


def build_incremental_derived(child_standard_db_path, parent_derived_db_path, *,
                              directory, expected_repo_id, expected_company_id) -> fts.FTSGeneration:
    """Build and verify a new immutable companion, never rewriting an existing one.

    Commit adjacency is the caller's responsibility. A preexisting target is
    verified read-only; failures clean only the owned staging file, never final.
    """
    temp = None
    directory_fd = None
    lock = None
    try:
        child_std = wide._regular_path(child_standard_db_path)
        parent_der = wide._regular_path(parent_derived_db_path)
        parent = Path(directory).resolve(strict=True)
        if not parent.is_dir() or child_std.parent != parent or parent_der.parent != parent:
            raise DerivedAdvanceError('standard and derived generations must share directory')
        lock = child_std.open('rb')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with closing(sqlite3.connect(child_std.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.execute('PRAGMA foreign_keys=ON')
            reader.execute('BEGIN')
            cmeta, csource, crecords = wide._source(reader, expected_repo_id, expected_company_id)
        cexpected = wide._assemble(crecords, csource)
        cwide_meta = wide._wide_meta(cmeta)
        crows = fts._rows(crecords)
        cfts_meta = fts._meta(cmeta, len(crows))
        with closing(sqlite3.connect(parent_der.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.execute('BEGIN')
            tables = wide._table_names(reader) - {row[0] for row in reader.execute(
                "SELECT name FROM pragma_table_list WHERE schema='main' AND type='shadow'")}
            if tables != {'wide_meta', 'fts_meta', 'fts_records',
                          *('dw_' + family for family in cexpected)}:
                raise DerivedAdvanceError('parent derived table set mismatch')
            for table in ('wide_meta', 'fts_meta'):
                gid = reader.execute('SELECT value FROM ' + table
                                     + " WHERE key='generation_id'").fetchone()
                if gid is None:
                    raise DerivedAdvanceError('parent generation_id missing: ' + table)
                if gid[0] == cmeta['generation_id']:
                    raise DerivedAdvanceError('parent and child must be different generations')
            passembled = {family: reader.execute(
                'SELECT * FROM dw_' + family + ' ORDER BY path').fetchall()
                for family in cexpected}
            prows = reader.execute(
                'SELECT path,family,record_text FROM fts_records ORDER BY path').fetchall()
        wide_changes = wide_row_changes(passembled, cexpected)
        fts_changes = fts_row_changes(prows, crows)
        final = parent / ('derived-' + cmeta['generation_id'] + '.sqlite')
        result = fts.FTSGeneration(str(final), cmeta['generation_id'],
                                   cmeta['projected_commit'], cmeta['render_sha256'], len(crows))

        def verify(path):
            wide._verify(path, cexpected, cwide_meta, csource)
            fts._verify_fts(path, crows, cfts_meta, cexpected, cwide_meta, csource)

        if os.path.lexists(final):
            wide._regular_path(final)
            verify(final)
            return result
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        fd, name = tempfile.mkstemp(prefix='.' + final.name + '.tmp-', dir=parent)
        temp = Path(name)
        os.close(fd)
        with closing(sqlite3.connect(temp)) as writer:
            with closing(sqlite3.connect(parent_der.as_uri() + '?mode=ro', uri=True)) as reader:
                reader.backup(writer)
            _apply(writer, wide_changes, crows, fts_changes, cwide_meta, cfts_meta)
        os.chmod(temp, 0o644)
        verify(temp)
        wide._sync_file(temp)
        os.fsync(directory_fd)
        os.replace(temp, final)
        temp = None
        verify(final)
        wide._sync_file(final)
        os.fsync(directory_fd)
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise DerivedAdvanceError('cannot build/verify incremental derived generation: ' + str(exc)) from exc
    finally:
        try:
            _unlink_quietly(temp)
        except OSError as exc:
            raise DerivedAdvanceError('cannot clean failed derived generation: ' + str(exc)) from exc
        finally:
            try:
                if directory_fd is not None:
                    os.close(directory_fd)
            finally:
                if lock is not None:
                    lock.close()
