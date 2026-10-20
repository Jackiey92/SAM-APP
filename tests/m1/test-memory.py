"""Synthetic M1 fixtures only; no real diligence corpus or production DB writes."""
import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.memory import paths
from app.memory.chunking import split_markdown
from app.memory.embedding import LocalBgeProvider, RemoteOpenAICompatibleProvider
from app.memory.projection import chunk_id
from tests.memory_setup import migrated_memory
from app.memory.provider import ProjectionError
from app.memory.company_facts.l2_consolidation import ConsolidationService
from app.memory.company_facts.l2_fact_extractor import ExtractedFact
from app.storage import ArchiveFileStore


class FakeEmbedding:
    dim, embedder, model_version = 3, 'synthetic', 'synthetic-v1'
    fail = False

    def embed_texts(self, texts):
        if self.fail:
            raise RuntimeError('synthetic embedding failure')
        return [[1.0, float('甲' in t), float('乙' in t)] for t in texts]

    def embed_query(self, text):
        return self.embed_texts([text])[0]


@pytest.fixture
def store(tmp_path):
    p = migrated_memory(tmp_path / 'test.db', embedding=FakeEmbedding())
    yield p
    p.close()


def fact(subject='甲', **extra):
    return dict(module_key='bizov', claim=subject + '产品', status='verified', **extra)


def seed(store, company='acc-A', key='1', subject='甲', module='bizov'):
    source = ArchiveFileStore(store.database.parent / 'objects', store.database).put_bytes(
        ('谷斗合成 ' + subject + key).encode(), original_name='gudou.txt')
    service = ConsolidationService(store.database)
    result = service.consolidate_candidates([ExtractedFact(
        company_id=company, metric=key, period='unspecified', value=subject + '产品',
        value_type='string', unit=None, source_file=source.file_hash, source_page=1,
        source_span='row/1', confidence=1, critical=False, module_key=module)])
    if result.todo_ids:
        service.resolve(result.todo_ids[0], choose='candidate')
    return next(f for f in store.list_facts(company) if f['attribute'] == key)


def project(store, company='acc-A', key='1', subject='甲', module='bizov'):
    row = seed(store, company, key, subject, module)
    store.put_fact(company, str(row['id']), row)
    return row


def test_put_search_read_idempotent(store):
    uri = paths.facts_uri('acc-A', 'bizov', 'brief.md')
    store.put(uri, '甲产品')
    before = dict(store.db.execute('SELECT * FROM m1_chunks').fetchone())
    store.put(uri, '甲产品')
    after = dict(store.db.execute('SELECT * FROM m1_chunks').fetchone())
    assert before['chunk_id'] == after['chunk_id'] == chunk_id(uri, 0)
    assert after['content_hash'] == hashlib.sha256('甲产品'.encode()).hexdigest()
    assert store.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 1
    assert store.read(uri) == '甲产品'
    result = store.search('甲', prefix=paths.facts_uri('acc-A'))
    assert result[0]['uri'] == uri
    assert result[0]['score'] == pytest.approx(1.0)
    assert set(result[0]) >= {'uri', 'score', 'snippet', 'module_key', 'company_id'}
    assert after['embedder'] == 'synthetic' and after['model_version'] == 'synthetic-v1'


def test_delete_recursive_boundary(store):
    for company in ['acc-A', 'acc-AA', 'acc-B']:
        project(store, company)
    store.delete(paths.facts_uri('acc-A'), recursive=True)
    # Deleting derived storage cannot erase the fact authority.
    assert len(store.list_facts('acc-A')) == 1
    assert store.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 2
    store.rebuild_projection('acc-A')
    assert store.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 3

def test_cross_company_and_host_scope(store, tmp_path):
    project(store, 'acc-A')
    project(store, 'acc-B', subject='乙')
    assert all(r['company_id'] == 'acc-A' for r in store.search('乙', prefix=paths.facts_uri('acc-A')))
    bound = migrated_memory(tmp_path / 'test.db', embedding=FakeEmbedding(), company_id='acc-A')
    try:
        assert bound.search('甲')[0]['company_id'] == 'acc-A'
        for call in [lambda: bound.search('乙', prefix=paths.facts_uri('acc-B')),
                     lambda: bound.get_fact('acc-B', '1'), lambda: bound.list_facts('acc-B'),
                     lambda: bound.put_fact('acc-B', '1', fact()),
                     lambda: bound.delete(paths.facts_uri('acc-B'), recursive=True),
                     lambda: bound.rebuild_projection('acc-B')]:
            with pytest.raises(ValueError): call()
    finally: bound.close()

@pytest.mark.parametrize('company', ['', ' ', '../', '/tmp', 'acc-A/acc-B',
                                      "x' OR 1=1--", 'x' * 129, 'acc_A', '..', 'x%2fy'])
def test_bad_company(store, company):
    for call in [lambda: store.put_fact(company, '1', fact()),
                 lambda: store.get_fact(company, '1'), lambda: store.list_facts(company),
                 lambda: store.rebuild_projection(company)]:
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize('uri', ['sam://facts/', 'sam://facts//acc-A',
                                'sam://facts/acc-A/../acc-B', 'sam://facts/acc-A//brief.md',
                                'sam://facts/acc-A/%2e%2e/x', 'sam://facts/acc-A/brief_name.md',
                                'sam://session/acc-A', 'sam://facts/' + 'x' * 3000])
def test_bad_uri(store, uri):
    for call in [lambda: store.put(uri, 'x'), lambda: store.read(uri),
                 lambda: store.search('x', prefix=uri), lambda: store.query(prefix=uri),
                 lambda: store.delete(uri, recursive=True), lambda: store.ensure_directory(uri)]:
        with pytest.raises(ValueError):
            call()


def test_missing_scope_and_empty_query(store):
    for prefix in ['sam://', 'sam://resources/legacy', paths.FACTS_ROOT]:
        with pytest.raises(ValueError):
            store.search('甲', prefix=prefix)
        with pytest.raises(ValueError):
            store.query(prefix=prefix)
    assert store.search('', prefix=paths.facts_uri('acc-A')) == []
    with pytest.raises(ValueError):
        store.search('x' * 8193, prefix=paths.facts_uri('acc-A'))


def test_status_and_fact_updates(store):
    old = project(store)
    with pytest.raises(ValueError): store.put_fact('acc-A', '2', dict(status='pending'))
    with pytest.raises(PermissionError): store.put_fact('acc-A', '2', fact())
    assert not hasattr(store, 'get_2b')
    new = project(store, subject='乙', module='hr')
    assert store.get_fact('acc-A', str(old['id'])) is None
    assert store.list_facts('acc-A') == [new]
    assert store.query(prefix=paths.facts_uri('acc-A', 'bizov')) == []
    assert store.search('甲', prefix=paths.facts_uri('acc-A', 'bizov')) == []
    assert store.db.execute('SELECT COUNT(*) FROM m1_documents').fetchone()[0] == 0
    with pytest.raises(ValueError): store.put_fact('acc-A', '3', fact(company_id='acc-B'))

def test_failure_preserves_authority_and_company_rebuild(store):
    project(store)
    project(store, 'acc-B', subject='乙')
    store.embedding.fail = True
    new = seed(store, subject='乙')
    with pytest.raises(ProjectionError) as error: store.put_fact('acc-A', str(new['id']), new)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert store.get_fact('acc-A', str(new['id'])) == new
    assert store.projection_status('acc-A') == 'stale'
    assert store.projection_status('acc-B') == 'ready'
    with pytest.raises(ProjectionError): store.search('甲', prefix=paths.facts_uri('acc-A'))
    with pytest.raises(ProjectionError): store.rebuild_projection('acc-A')
    store.embedding.fail = False
    store.rebuild_projection('acc-A')
    assert store.projection_status('acc-A') == 'ready'
    assert len(store.search('乙', prefix=paths.facts_uri('acc-A'))) == 1

@pytest.mark.parametrize('vector', [[1.0], [0, 0, 0], [float('nan'), 0, 1], [float('inf'), 0, 1]])
def test_invalid_embedding(store, vector):
    row = seed(store)
    store.embedding.embed_texts = lambda texts: [vector for _ in texts]
    with pytest.raises(ProjectionError): store.rebuild_projection('acc-A')
    assert store.get_fact('acc-A', str(row['id'])) == row
    assert store.projection_status('acc-A') == 'stale'

def test_wrong_count_and_empty_content(store):
    store.put(paths.facts_uri('acc-A', 'bizov', 'brief.md'), '')
    assert store.search('甲', prefix=paths.facts_uri('acc-A')) == []
    seed(store)
    store.embedding.embed_texts = lambda texts: []
    with pytest.raises(ProjectionError): store.rebuild_projection('acc-A')

def test_model_version_guard(store, tmp_path):
    other = FakeEmbedding(); other.model_version = 'synthetic-v2'
    with pytest.raises(ValueError, match='whole-store'):
        migrated_memory(tmp_path / 'test.db', embedding=other)


def test_concurrent_writes_and_rebuild(store):
    for i in range(12): seed(store, key=str(i))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: store.rebuild_projection('acc-A'), range(12)))
    assert len(store.list_facts('acc-A')) == 12
    assert store.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 12

def test_resource_deferred_and_session_reserved(store):
    for call in [lambda: store.add_resource('/not-read', parent='ignored'),
                 lambda: store.add_resource_to('/not-read', 'ignored'),
                 lambda: store.wait_for_resource('ignored'),
                 lambda: store.read(paths.original_uri('acc-A', 'source.pdf'))]:
        with pytest.raises(NotImplementedError, match='M3'):
            call()
    assert paths.SESSION_ROOT == 'sam://session/'


def test_chunking_and_determinism():
    text = '# 标题\n' + ('中文正文 123456.78 [链接](https://example.org/abc)\n' * 60)
    chunks = split_markdown(text)
    assert len(chunks) > 1
    assert all(text[c.source_span[0]:c.source_span[1]] == c.text for c in chunks)
    assert split_markdown(text, fact=True)[0].text == text
    assert len(split_markdown(text, fact=True)) == 1
    assert chunk_id('u', 0) != chunk_id('u', 1)
    table = '| 项目 | 值 |\n| --- | --- |\n' + ''.join(f'| 项目{i} | {i} |\n' for i in range(100))
    groups = split_markdown(table, limit=200)
    assert all(g.text.startswith('| 项目 | 值 |\n| --- | --- |\n') for g in groups)
    assert all(0 <= g.source_span[0] < g.source_span[1] <= len(table) for g in groups)
    link = '[长链接](' + 'https://example.org/' + 'a' * 1000 + ')'
    assert any(link in c.text for c in split_markdown('中' * 400 + link + '文' * 400))


def test_remote_disabled():
    with pytest.raises(ValueError):
        RemoteOpenAICompatibleProvider()
    with pytest.raises(ValueError):
        RemoteOpenAICompatibleProvider(enabled=True)
    p = RemoteOpenAICompatibleProvider(enabled=True, endpoint='https://example.invalid',
                                       api_key='synthetic-not-a-key', model='synthetic')
    with pytest.raises(NotImplementedError):
        p.embed_query('x')
    with pytest.raises(NotImplementedError):
        p.embed_texts(['x'])


def test_local_bge_offline(monkeypatch, tmp_path):
    cache = Path(__file__).resolve().parents[2] / 'poc/memory_m0/model-cache'
    if not (cache / 'models--BAAI--bge-small-zh-v1.5').is_dir():
        pytest.skip('pinned M0 model cache not installed')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    def denied(*args, **kwargs):
        raise AssertionError('offline test attempted network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    p = LocalBgeProvider()
    import torch
    torch.set_num_threads(4)
    assert p.embed_texts([]) == []
    vectors = p.embed_texts(['甲公司产品', '乙公司产品'])
    assert len(vectors) == 2 and len(vectors[0]) == 512
    assert sum(v * v for v in vectors[0]) == pytest.approx(1.0, abs=1e-5)
    assert len(p.embed_query('甲产品')) == 512
    store = migrated_memory(tmp_path / 'offline.db', embedding=p, company_id='acc-A')
    try:
        row = project(store)
        assert store.search('甲产品')[0]['company_id'] == 'acc-A'
        store.rebuild_projection('acc-A')
        assert store.get_fact('acc-A', str(row['id'])) == row
    finally:
        store.close()


def test_projection_render_failure_keeps_authority(store, monkeypatch):
    row = seed(store)
    def fail(row): raise RuntimeError('synthetic renderer failure')
    monkeypatch.setattr(store, '_project', fail)
    with pytest.raises(ProjectionError): store.rebuild_projection('acc-A')
    assert store.get_fact('acc-A', str(row['id'])) == row
    assert store.projection_status('acc-A') == 'stale'

def test_projection_sql_failure_keeps_authority(store):
    row = seed(store)
    store.db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON m1_chunks BEGIN SELECT RAISE(ABORT,'synthetic projection failure'); END")
    with pytest.raises(ProjectionError): store.rebuild_projection('acc-A')
    assert store.get_fact('acc-A', str(row['id'])) == row
    store.db.execute('DROP TRIGGER synthetic_failure')
    store.db.commit()
    store.rebuild_projection('acc-A')
    assert store.search('甲', prefix=paths.facts_uri('acc-A'))

def test_unverified_fact_rejected_without_write(store):
    for status in [None, 'pending', 'conflicting', 'rejected']:
        f = fact(); f['status'] = status
        with pytest.raises(ValueError, match='verified'):
            store.put_fact('acc-A', '1', f)
    assert store.list_facts('acc-A') == []
    assert store.projection_status('acc-A') is None


def test_single_paragraph_overlap_and_atomic_links():
    text = '中文' * 1400
    chunks = split_markdown(text)
    assert all(len(c.text) <= 800 for c in chunks)
    assert all(b.source_span[0] < a.source_span[1] for a, b in zip(chunks, chunks[1:]))
    assert all(text[c.source_span[0]:c.source_span[1]] == c.text for c in chunks)
    link = 'prefix[中文链接](https://example.org/' + 'a' * 900 + ')'
    chunks = split_markdown('中' * 400 + link + '文' * 400)
    assert any('[中文链接](https://example.org/' + 'a' * 900 + ')' in c.text for c in chunks)


def test_single_verified_integer_fact_rebuilds_documents_and_chunks(store):
    from app.memory.company_facts.l2_projection import L2ProjectionService
    row = seed(store)
    assert type(row['id']) is int
    store.put(paths.facts_uri('acc-A', 'bizov', 'brief.md'), '合成员工简报')
    L2ProjectionService(store.database, store).publish(company_id='acc-A')
    uri = paths.fact_uri('acc-A', 'bizov', str(row['id']))
    assert store.read(uri)
    assert store.db.execute('SELECT COUNT(*) FROM m1_documents').fetchone()[0] == 1
    assert store.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] >= 1
    with pytest.raises(ValueError):
        paths.identifier(row['id'])
    with pytest.raises(ValueError):
        paths.fact_uri('acc-A', 'bizov', '../1')
