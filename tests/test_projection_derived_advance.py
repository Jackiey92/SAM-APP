"""D12-3 COW row writes, full-build equality, and fail-closed fault probes."""
import ast
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree import git_format, projection_derived_advance as advance
from app.facts_tree import projection_fts as fts, projection_wide as wide
from app.facts_tree.projection_advance import build_standard_generation_for_commit
from app.facts_tree.projection_row_delta import wide_row_changes
from tests.test_projection_incremental import EXPECTED, _append, chain  # noqa: F401


def _standard(repo, commit, directory):
    return build_standard_generation_for_commit(repo, commit, directory, **EXPECTED)


def _full(std):
    directory = Path(std.db_path).parent
    wide.build_projection_wide(std.db_path, directory=directory, **EXPECTED)
    return fts.build_projection_fts(std.db_path, directory=directory, **EXPECTED)


def _contents(conn):
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'dw_*' ORDER BY name")]
    return {
        'wide': {name[3:]: conn.execute('SELECT * FROM ' + name + ' ORDER BY path').fetchall()
                 for name in tables},
        'wide_meta': conn.execute('SELECT * FROM wide_meta ORDER BY key').fetchall(),
        'fts_meta': conn.execute('SELECT * FROM fts_meta ORDER BY key').fetchall(),
        'fts': conn.execute('SELECT path,family,record_text FROM fts_records ORDER BY path').fetchall(),
        'schema': conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name').fetchall(),
    }


def _read(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=1')
        assert conn.execute('PRAGMA query_only').fetchone() == (1,)
        return _contents(conn)


def _inventory(directory):
    return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, sha256(p.read_bytes()).hexdigest())
            for p in directory.iterdir()}


def _pair(chain, directory, indices=(1, 2), upgrade=True):
    repo, commits = chain
    parent = _standard(repo, commits[indices[0]], directory)
    if upgrade:
        derived = _full(parent)
    else:
        derived = wide.build_projection_wide(parent.db_path, directory=directory, **EXPECTED)
    child = _standard(repo, commits[indices[1]], directory)
    return parent, derived, child


def _build(child, parent, **kwargs):
    return advance.build_incremental_derived(
        child.db_path, parent.db_path, directory=Path(child.db_path).parent,
        **{**EXPECTED, **kwargs})


@pytest.mark.parametrize('indices', [(0, 1), (1, 2), (2, 3), (0, 3)])
def test_cow_equals_full_all_rows_meta_schema_and_idempotence(chain, tmp_path, indices):
    directory = tmp_path / 'generations'
    parent, derived, child = _pair(chain, directory, indices)
    before = _inventory(directory)
    pointer = tmp_path / 'current.json'
    pointer.write_text('unchanged external pointer')
    result = _build(child, derived)
    assert type(result) is fts.FTSGeneration
    assert result.generation_id == child.generation_id and result.ready
    assert result.projected_commit == child.projected_commit
    assert result.render_sha256 == child.render_sha256
    actual = _read(result.db_path)
    repo, commits = chain
    oracle = _full(_standard(repo, commits[indices[1]], tmp_path / 'oracle'))
    assert actual == _read(oracle.db_path)
    assert len(actual['wide']) == 16
    after = _inventory(directory)
    assert set(after) == set(before) | {Path(result.db_path).name}
    assert all(after[name] == value for name, value in before.items())
    assert Path(result.db_path).stat().st_mode & 0o777 == 0o644
    assert _build(child, derived) == result
    assert _inventory(directory) == after
    assert pointer.read_text() == 'unchanged external pointer'
    if indices == (1, 2):
        old = _read(derived.db_path)['wide']['fact_current'][0]
        new = actual['wide']['fact_current'][0]
        assert old[-4] == new[-4]
        assert old[-8] == 'leaf-test' and new[-8] == 'next-leaf'
    if indices == (2, 3):
        old = _read(derived.db_path)
        assert actual['fts'] == old['fts']
        assert dict(actual['fts_meta'])['projected_commit'] == commits[3]
        assert dict(actual['fts_meta'])['projected_commit'] != dict(old['fts_meta'])['projected_commit']


def test_single_company_entity_change_writes_only_one_path(chain, tmp_path, monkeypatch):
    repo, commits = chain
    payloads = {r.path: git_format.loads_record(r.content)
                for r in repo.read_snapshot_at(commits[-1]).records}
    revision_path = git_format.record_path('entity_revision', entity_uid='entity-1', revision=2)
    assert payloads[revision_path]['entity_kind'] == 'company'
    path = git_format.record_path('entity_current', entity_uid='entity-1')
    payloads[path] = next(git_format.loads_record(r.content)
                          for r in repo.read_snapshot_at(commits[1]).records if r.path == path)
    parent_commit = _append(repo, payloads)
    payloads[path]['status'] = 'first_seen'
    commit = _append(repo, payloads)
    directory = tmp_path / 'generations'
    parent = _standard(repo, parent_commit, directory)
    derived = _full(parent)
    child = _standard(repo, commit, directory)
    statements = []
    original = sqlite3.connect

    def traced(database, *args, **kwargs):
        conn = original(database, *args, **kwargs)
        if not kwargs.get('uri'):
            conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(advance.sqlite3, 'connect', traced)
    result = _build(child, derived)
    delta = wide_row_changes(_read(derived.db_path)['wide'], _read(result.db_path)['wide'])
    assert {family for family, change in delta.items() if not change.is_empty()} == {'entity_current'}
    change = delta['entity_current']
    assert not change.added and not change.removed
    assert len(change.modified) == 1 and change.modified[0][-4] == path
    dml = [sql for sql in statements if sql.startswith(('DELETE FROM dw_', 'INSERT INTO dw_'))]
    assert len(dml) == 2 * len(change.modified) == 2
    assert sum(sql.startswith('DELETE FROM dw_entity_current WHERE path=') for sql in dml) == 1
    assert sum(sql.startswith('INSERT INTO dw_entity_current (') for sql in dml) == 1
    assert all(path in sql for sql in dml)
    assert not any(sql == 'DROP TABLE fts_records' for sql in statements)
    assert sum(sql == 'BEGIN IMMEDIATE' for sql in statements) == 1
    assert sum(sql == 'COMMIT' for sql in statements) == 1
    # FTS ready is inserted after all other FTS metadata.
    meta_inserts = [sql for sql in statements if sql.startswith('INSERT INTO fts_meta')]
    assert meta_inserts[-1] == "INSERT INTO fts_meta VALUES ('status','ready')"


@pytest.mark.parametrize('failure', ['fts-write', 'verify-temp', 'sync-temp', 'replace'])
def test_failure_cleans_temp_preserves_sources_and_unrelated_final(chain, tmp_path, monkeypatch, failure):
    directory = tmp_path / 'generations'
    _, parent, child = _pair(chain, directory)
    before = _inventory(directory)
    parent_contents = _read(parent.db_path)

    def fail(*args, **kwargs):
        raise OSError('injected ' + failure)

    if failure == 'fts-write':
        original = advance._apply

        def fail_write(writer, *args):
            def deny(action, name, *rest):
                if action == sqlite3.SQLITE_CREATE_VTABLE and name == 'fts_records':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            writer.set_authorizer(deny)
            try:
                original(writer, *args)
            except sqlite3.Error:
                writer.set_authorizer(None)
                assert not writer.in_transaction
                assert _contents(writer) == parent_contents
                raise
            pytest.fail('FTS creation must fail')
        monkeypatch.setattr(advance, '_apply', fail_write)
    elif failure == 'verify-temp':
        monkeypatch.setattr(fts, '_verify_fts', fail)
    elif failure == 'sync-temp':
        monkeypatch.setattr(wide, '_sync_file', fail)
    else:
        monkeypatch.setattr(advance.os, 'replace', fail)
    with pytest.raises(advance.DerivedAdvanceError) as error:
        _build(child, parent)
    assert error.value.__cause__ is not None
    assert _inventory(directory) == before


@pytest.mark.parametrize('failure', ['same-generation', 'outside', 'symlink', 'missing',
                                     'missing-fts', 'extra-table', 'repo', 'company', 'not-ready'])
def test_invalid_inputs_fail_closed_without_output(chain, tmp_path, failure):
    directory = tmp_path / 'generations'
    parent_std, parent, child = _pair(chain, directory, upgrade=failure != 'missing-fts')
    parent_path = Path(parent.db_path)
    child_path = Path(child.db_path)
    expected = dict(EXPECTED)
    if failure == 'same-generation':
        child_path = Path(parent_std.db_path)
    elif failure == 'outside':
        parent_path = tmp_path / parent_path.name
        parent_path.write_bytes(Path(parent.db_path).read_bytes())
    elif failure == 'symlink':
        parent_path = directory / 'linked.sqlite'
        parent_path.symlink_to(parent.db_path)
    elif failure == 'missing':
        parent_path = directory / 'missing.sqlite'
    elif failure == 'extra-table':
        with closing(sqlite3.connect(parent.db_path)) as conn, conn:
            conn.execute('CREATE TABLE forbidden (value TEXT)')
    elif failure in {'repo', 'company'}:
        expected['expected_' + failure + '_id'] = 'wrong'
    elif failure == 'not-ready':
        with closing(sqlite3.connect(child.db_path)) as conn, conn:
            conn.execute("UPDATE generation_meta SET value='staging' WHERE key='status'")
    before = _inventory(directory)
    with pytest.raises(advance.DerivedAdvanceError):
        advance.build_incremental_derived(child_path, parent_path, directory=directory, **expected)
    assert _inventory(directory) == before


@pytest.mark.parametrize('corruption', ['wide', 'fts', 'symlink'])
def test_preexisting_bad_final_is_never_overwritten(chain, tmp_path, corruption):
    directory = tmp_path / 'generations'
    _, parent, child = _pair(chain, directory)
    target = Path(_full(child).db_path)
    if corruption == 'symlink':
        saved = tmp_path / 'saved.sqlite'
        target.rename(saved)
        target.symlink_to(saved)
    else:
        with closing(sqlite3.connect(target)) as conn, conn:
            if corruption == 'wide':
                conn.execute('DELETE FROM dw_fact_current')
            else:
                conn.execute('DELETE FROM fts_records')
    before = _inventory(directory)
    with pytest.raises(advance.DerivedAdvanceError):
        _build(child, parent)
    assert _inventory(directory) == before


def test_postpublication_verify_failure_keeps_final_and_sources(chain, tmp_path, monkeypatch):
    directory = tmp_path / 'generations'
    _, parent, child = _pair(chain, directory)
    before = _inventory(directory)
    final = directory / ('derived-' + child.generation_id + '.sqlite')
    verify = fts._verify_fts

    def fail_final(path, *args):
        if Path(path) == final:
            raise fts.FTSBuildError('injected final verification failure')
        return verify(path, *args)

    monkeypatch.setattr(fts, '_verify_fts', fail_final)
    with pytest.raises(advance.DerivedAdvanceError, match='injected final verification failure'):
        _build(child, parent)
    after = _inventory(directory)
    assert set(after) == set(before) | {final.name}
    assert all(after[name] == value for name, value in before.items())
    monkeypatch.setattr(fts, '_verify_fts', verify)
    assert _build(child, parent).db_path == str(final)


def test_source_import_boundary():
    source = ast.parse(Path(advance.__file__).read_text())
    standard = {'contextlib', 'fcntl', 'os', 'pathlib', 'sqlite3', 'tempfile'}
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            assert all(alias.name in standard for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            if node.module == 'app.facts_tree':
                assert {alias.name for alias in node.names} <= {'projection_wide', 'projection_fts'}
            else:
                assert node.module in standard | {'app.facts_tree.projection_row_delta'}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'exec', 'eval', '__import__'}


def test_changed_fts_drops_rebuilds_and_wide_dml_matches_delta(chain, tmp_path, monkeypatch):
    directory = tmp_path / 'generations'
    _, parent, child = _pair(chain, directory)
    repo, commits = chain
    oracle = _full(_standard(repo, commits[2], tmp_path / 'oracle'))
    expected = _read(oracle.db_path)
    delta = wide_row_changes(_read(parent.db_path)['wide'], expected['wide'])
    statements = []
    apply = advance._apply

    def traced(writer, *args):
        writer.set_trace_callback(statements.append)
        return apply(writer, *args)

    monkeypatch.setattr(advance, '_apply', traced)
    result = _build(child, parent)
    assert _read(result.db_path) == expected
    for family, change in delta.items():
        deletes = [sql for sql in statements
                   if sql.startswith('DELETE FROM dw_' + family + ' WHERE path=')]
        inserts = [sql for sql in statements
                   if sql.startswith('INSERT INTO dw_' + family + ' (')]
        assert len(deletes) == len(change.removed) + len(change.modified)
        assert len(inserts) == len(change.added) + len(change.modified)
    assert statements.count('DROP TABLE fts_records') == 1
    assert sum(sql.startswith('CREATE VIRTUAL TABLE fts_records USING fts5(')
               for sql in statements) == 1
    assert statements.count('BEGIN IMMEDIATE') == statements.count('COMMIT') == 1
