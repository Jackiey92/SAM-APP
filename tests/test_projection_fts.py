"""D10 integration checks against real SQLite FTS5 and standard generations."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.facts_tree import projection_fts as fts, projection_wide as wide
from app.facts_tree.record_schema import SCHEMA_COLUMNS, SEARCHABLE_COLUMNS
from tests.test_projection_wide import (
    EXPECTED, build_standard, connect, inventory, rewrite_source, build as build_wide,
)


def build(source, **kwargs):
    return fts.build_projection_fts(source.db_path, directory=Path(source.db_path).parent,
                                    **{**EXPECTED, **kwargs})


def chinese_source(tmp_path):
    source = build_standard(tmp_path)
    def change(payloads):
        for payload in payloads.values():
            if payload.get("record_type") == "fact_revision":
                payload["statement_raw"] = '苏州创新投资集团采购半导体合同 120.50 30%'
    rewrite_source(source, change)
    return source


def verify(source, result):
    with connect(source.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        meta, source_rows, records = wide._source(conn, **EXPECTED)
    fts._verify_fts(Path(result.db_path), fts._rows(records), fts._meta(meta, len(records)),
                    wide._assemble(records, source_rows), wide._wide_meta(meta), source_rows)


def wide_bytes(connection):
    return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
            for table in ['wide_meta', *('dw_' + f for f in SCHEMA_COLUMNS)]}


def test_full_build_paths_contents_meta_and_idempotence(tmp_path):
    source = chinese_source(tmp_path)
    before = Path(source.db_path).read_bytes()
    result = build(source)
    verify(source, result)
    with connect(source.db_path) as src, connect(result.db_path) as conn:
        rows = src.execute('SELECT path,family,canonical_bytes FROM record_canonical ORDER BY path').fetchall()
        from app.facts_tree import git_format
        expected = []
        for path, family, raw in rows:
            if family not in SCHEMA_COLUMNS:
                continue
            payload = git_format.loads_record(raw)
            raw_text = ' '.join(payload[name] for name in SEARCHABLE_COLUMNS[family]
                                if type(payload.get(name)) is str and payload[name].strip())
            expected.append((path, family, fts.tokenize_text(raw_text)))
        assert conn.execute('SELECT path,family,record_text FROM fts_records ORDER BY path').fetchall() == expected
        assert result.record_count == len(expected) == 18
        meta = dict(conn.execute('SELECT * FROM fts_meta'))
        assert set(meta) == {'generation_id', 'projected_commit', 'render_sha256', 'repo_id',
                             'company_id', 'fts_schema_version', 'tokenizer', 'jieba_dict_sha256',
                             'indexed_column_count', 'fts_table_count', 'record_count', 'status'}
        assert meta['indexed_column_count'] == '11' and meta['fts_table_count'] == '2'
        assert conn.execute("SELECT count(*) FROM fts_records WHERE record_text=''").fetchone()[0] > 0
        for query in ['投资', '半导体', '投资 合同', '120.50', '30%']:
            assert len(fts.search_records(conn, query)) == 2
        assert not fts.search_records(conn, 'zzzzzzunmatchedtoken')
        assert len(fts.search_records(conn, '投资', family='fact_revision', leaf_code='leaf-test')) == 1
        assert not fts.search_records(conn, '投资', family='fact_revision', leaf_code='wrong')
        assert not fts.search_records(conn, '投资', family='entity_revision', status='active')
        assert len(fts.search_records(conn, '投资', limit=1)) == 1
    assert Path(source.db_path).read_bytes() == before
    snapshot = inventory(tmp_path)
    assert build(source) == result
    assert inventory(tmp_path) == snapshot


def test_backup_upgrade_preserves_wide_and_source(tmp_path):
    source = chinese_source(tmp_path)
    base = build_wide(source)
    with connect(base.db_path) as conn:
        before = wide_bytes(conn)
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        pages = [r[0] for r in conn.execute(
            "SELECT pageno FROM dbstat WHERE name='wide_meta' OR name LIKE 'dw_%'")]
    old_bytes = Path(base.db_path).read_bytes()
    original_source = Path(source.db_path).read_bytes()
    result = build(source)
    new_bytes = Path(result.db_path).read_bytes()
    assert pages and all(old_bytes[(p-1)*page_size:p*page_size]
                         == new_bytes[(p-1)*page_size:p*page_size] for p in pages)
    with connect(result.db_path) as conn:
        assert wide_bytes(conn) == before
    verify(source, result)
    assert Path(source.db_path).read_bytes() == original_source
    assert {p.name for p in tmp_path.iterdir()} == {Path(source.db_path).name, Path(result.db_path).name}


@pytest.mark.parametrize('query', ['', '  ', None, 123])
def test_invalid_query(query):
    with pytest.raises(fts.FTSBuildError):
        fts.build_match_expression(query)


def test_quotes_and_deterministic_pure_kernel():
    text = '苏州创新投资集团 "合同" 120.50 30%'
    assert len({fts.tokenize_text(text) for _ in range(100)}) == 1
    assert '""""' in fts.build_match_expression('"')
    tree = ast.parse(Path(fts.__file__).read_text())
    kernel = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'tokenize_text')
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(kernel))


@pytest.mark.parametrize('kwargs', [dict(family='bad'), dict(family=[]), dict(status='active'),
    dict(family='event', status='active'), dict(family='entity_revision', leaf_code='leaf'),
    dict(leaf_code='leaf'), dict(family='fact_current'), dict(limit=0), dict(limit=True)])
def test_invalid_filters(tmp_path, kwargs):
    result = build(build_standard(tmp_path))
    with connect(result.db_path) as conn, pytest.raises(fts.FTSBuildError):
        fts.search_records(conn, '投资', **kwargs)


@pytest.mark.parametrize('upgrade', [False, True])
@pytest.mark.parametrize('phase', ['write', 'verify-temp', 'verify-final', 'sync', 'replace'])
def test_failure_preserves_old_bytes_and_inode(tmp_path, monkeypatch, upgrade, phase):
    source = build_standard(tmp_path)
    if upgrade:
        build_wide(source)
    before = inventory(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('injected ' + phase)
    if phase == 'write':
        original = fts._write_fts
        def write(*args):
            original(*args)
            fail()
        monkeypatch.setattr(fts, '_write_fts', write)
    elif phase.startswith('verify'):
        original = fts._verify_fts
        def check(path, *args):
            original(path, *args)
            if path.name.startswith('.') == (phase == 'verify-temp'):
                fail()
        monkeypatch.setattr(fts, '_verify_fts', check)
    elif phase == 'sync':
        monkeypatch.setattr(wide, '_sync_file', fail)
    else:
        original = fts.os.replace
        def replace(src, dst):
            if not str(src).endswith('.rollback'):
                fail()
            original(src, dst)
        monkeypatch.setattr(fts.os, 'replace', replace)
    with pytest.raises(fts.FTSBuildError, match='injected'):
        build(source)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize('sql', ["UPDATE fts_meta SET value='bad' WHERE key='tokenizer'",
    "UPDATE fts_records SET record_text='bad'", 'DELETE FROM fts_records',
    'CREATE TABLE unexpected(x)', 'DROP TABLE fts_meta'])
def test_invalid_existing_component_is_preserved(tmp_path, sql):
    source = build_standard(tmp_path)
    result = build(source)
    with closing(sqlite3.connect(result.db_path)) as conn, conn:
        conn.execute(sql)
    before = inventory(tmp_path)
    with pytest.raises(fts.FTSBuildError):
        build(source)
    assert inventory(tmp_path) == before


def test_dictionary_mismatch_fails_closed(tmp_path, monkeypatch):
    source = build_standard(tmp_path)
    before = inventory(tmp_path)
    monkeypatch.setattr(fts, '_DICT_SHA256', '0' * 64)
    with pytest.raises(fts.FTSBuildError, match='dictionary'):
        build(source)
    assert inventory(tmp_path) == before


def test_import_does_not_touch_jieba_cache():
    cache = Path('/tmp/jieba.cache')
    before = (cache.stat().st_mtime_ns, cache.read_bytes()) if cache.exists() else None
    subprocess.run([sys.executable, '-c', 'from app.facts_tree.projection_fts import tokenize_text; '
                    'assert "投资" in tokenize_text("苏州创新投资集团")'], check=True)
    after = (cache.stat().st_mtime_ns, cache.read_bytes()) if cache.exists() else None
    assert before == after


def test_status_filter_and_parameter_binding(tmp_path):
    result = build(chinese_source(tmp_path))
    with connect(result.db_path) as conn:
        assert len(fts.search_records(conn, '投资', family='fact_revision', status='active',
                                      leaf_code='leaf-test')) == 1
        assert not fts.search_records(conn, '投资', family='fact_revision', status='retracted')
        assert not fts.search_records(conn, '投资', family='fact_revision',
                                      status="active' OR 1=1 --")
        assert not fts.search_records(conn, '投资', family='fact_revision',
                                      leaf_code="leaf-test' OR 1=1 --")


def test_empty_generation(tmp_path):
    from tests.test_projection_generation import _build, _input, META_PATH
    from tests.facts_tree_full_fixture import SCOPE
    source = _build(tmp_path, _input({META_PATH: dict(SCOPE)}))
    result = build(source)
    assert result.record_count == 0
    verify(source, result)
    with connect(result.db_path) as conn:
        assert not fts.search_records(conn, '投资')


def test_concurrent_builds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    source = build_standard(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: build(source), range(4)))
    assert all(r == results[0] for r in results)
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize('mutation', ['source-symlink', 'derived-symlink', 'outside', 'identity', 'not-ready'])
def test_source_boundaries(tmp_path, mutation):
    source = build_standard(tmp_path)
    source_path = Path(source.db_path)
    kwargs = dict(directory=tmp_path, **EXPECTED)
    if mutation == 'source-symlink':
        link = tmp_path / 'link.sqlite'
        link.symlink_to(source_path)
        source_path = link
    elif mutation == 'derived-symlink':
        (tmp_path / ('derived-' + source.generation_id + '.sqlite')).symlink_to(source_path)
    elif mutation == 'outside':
        kwargs['directory'] = tmp_path.parent
    elif mutation == 'identity':
        kwargs['expected_repo_id'] = 'wrong'
    else:
        with closing(sqlite3.connect(source_path)) as conn, conn:
            conn.execute("UPDATE generation_meta SET value='staging' WHERE key='status'")
    before = inventory(tmp_path)
    with pytest.raises(fts.FTSBuildError):
        fts.build_projection_fts(source_path, **kwargs)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("filters", [dict(status="active"), dict(leaf_code="leaf-test"),
                                     dict(status="active", leaf_code="leaf-test")])
def test_current_revision_isolation_and_historical_audit(tmp_path, filters):
    source = build_standard(tmp_path)
    def change(payloads):
        for payload in payloads.values():
            if payload.get("record_type") == "fact_revision":
                payload["statement_raw"] = ("投资 苹果" if payload["revision"] == 1
                                            else "投资 香蕉")
    rewrite_source(source, change)
    result = build(source)
    with connect(result.db_path) as conn:
        assert not fts.search_records(conn, "苹果", family="fact_revision", **filters)
        for query in ("投资", "香蕉"):
            hits = fts.search_records(conn, query, family="fact_revision", **filters)
            assert len(hits) == 1
            assert (hits[0].fact_uid, hits[0].revision, hits[0].status) == ("fact-1", 2, "active")
        assert not fts.search_records(conn, "投资", family="fact_revision", leaf_code="wrong")
        assert not fts.search_records(conn, "投资", family="fact_revision", status="retracted")
        queries = []
        conn.set_trace_callback(queries.append)
        old = fts.search_records(conn, "苹果", family="fact_revision")
        conn.set_trace_callback(None)
        assert len(old) == 1
        assert (old[0].fact_uid, old[0].revision, old[0].status) == ("fact-1", 1, None)
        assert all("dw_fact_current" not in sql for sql in queries)
        history = fts.search_records(conn, "投资", family="fact_revision")
        assert {hit.revision for hit in history} == {1, 2}
        assert all(hit.status is None for hit in history)


def test_non_fact_status_filter(tmp_path):
    source = build_standard(tmp_path)
    def change(payloads):
        for payload in payloads.values():
            if payload.get("record_type") == "entity_revision":
                payload["raw_name"] = "投资"
    rewrite_source(source, change)
    result = build(source)
    with connect(result.db_path) as conn:
        assert fts.search_records(conn, "投资", family="entity_revision", status="active")
        assert not fts.search_records(conn, "投资", family="entity_revision", status="not-a-status")
