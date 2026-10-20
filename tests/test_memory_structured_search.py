import pytest

from tests.memory_setup import migrated_memory
from app.memory.provider import LocalVectorMemoryProvider as UninitializedMemoryProvider
from app.memory import paths
from app.memory.chunking import split_markdown
from app.memory.company_facts.l2_consolidation import ConsolidationService
from app.memory.company_facts.l2_fact_extractor import ExtractedFact
from app.storage import ArchiveFileStore
from app.memory_map import MemoryMapTools
from app.tool_bridge import ToolBridge, handle_request


class SyntheticEmbedding:
    dim, embedder, model_version = 2, 'synthetic', 'v1'
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, query): return [1., 1.]


@pytest.fixture
def store(tmp_path):
    provider = migrated_memory(tmp_path / 'ledger.db', embedding=SyntheticEmbedding())
    source = ArchiveFileStore(tmp_path / 'objects', provider.database).put_bytes(b'gudou synthetic', original_name='gudou.txt')
    service = ConsolidationService(provider.database)
    for company, entity, attribute, period, unit, module in (
        ('acc-gudou', '谷斗', '收入', '2024', '元', 'fin'),
        ('acc-gudou', '谷斗子公司', '营业收入', '2023', '万元', 'bizov'),
        ('acc-other', '外部合成主体', '收入', '2024', '元', 'fin'),
    ):
        service.consolidate_candidates([ExtractedFact(company, attribute, period, '3', 'number', unit,
            source.file_hash, 1, 'row/1', 1., False, entity=entity, module_key=module)])
    provider.rebuild_projection('acc-gudou')
    provider.rebuild_projection('acc-other')
    yield provider
    provider.close()


@pytest.mark.parametrize('filters', [
    {'period': '2024'}, {'entity': '谷斗'}, {'attribute': '收入'}, {'unit': '元'}, {'module_key': 'fin'},
    {'company_id': 'acc-gudou', 'period': '2024', 'entity': '谷斗', 'attribute': '收入', 'unit': '元', 'module_key': 'fin'},
])
def test_exact_fields_physically_filter_before_distance(store, filters):
    distances = []
    store.db.create_function('vec_distance_cosine', 2, lambda a, b: distances.append(a) or 0.5)
    rows = store.search('谷斗收入', prefix=paths.facts_uri('acc-gudou'), filters=filters)
    assert len(rows) == len(distances) == 1
    assert rows[0]['company_id'] == 'acc-gudou' and rows[0]['module_key'] == 'fin'


@pytest.mark.parametrize('filters', [None, {}, {'period': None}, {'period': ''}, {'period': []},
                                      {'year': '2024'}, {'module_key': 'finance'}, {'module_key': 'other'}])
def test_uncertain_or_unsupported_filters_fall_back_semantically(store, filters):
    rows = store.search('2024 财务 收入万元', prefix=paths.facts_uri('acc-gudou'), filters=filters)
    assert len(rows) == 2 and all(r['company_id'] == 'acc-gudou' for r in rows)


@pytest.mark.parametrize('filters', [{'period': '2024年'}, {'entity': '谷斗集团'},
                                     {'attribute': '营收'}, {'unit': '亿元'},
                                     {'period': "2024' OR 1=1--"}])
def test_no_year_alias_unit_conversion_or_sql_interpolation(store, filters):
    rows = store.search('收入', prefix=paths.facts_uri('acc-gudou'), filters=filters)
    assert len(rows) == 2 and all(r['company_id'] == 'acc-gudou' for r in rows)


def test_scope_cannot_be_overridden_even_with_uncertain_filter(store):
    with pytest.raises(ValueError, match='company'):
        store.search('收入', prefix=paths.facts_uri('acc-gudou'),
                     filters={'company_id': 'acc-other', 'period': None})
    tools = MemoryMapTools(store, object(), 'acc-gudou')
    assert len(tools.memory_search('收入', filters={'period': '2024'})) == 1
    bridge = ToolBridge(store, object(), company_id='acc-gudou', thread_id='reserved')
    response = handle_request(bridge, {'id': 1, 'method': 'sam_memory_search',
                                      'params': {'query': '收入', 'filters': {'company_id': 'acc-other'}}})
    assert response['ok'] is False and response['error']['code'] == 'bad_request'


def test_wide_table_row_preserves_model_atom(store):
    header = '| 型号 | 员工字段 | 值 |\n| --- | --- | --- |\n'
    row = '| GD-2024-A | ' + '谷斗员工已有字段' * 150 + ' | 123456.78万元 |\n'
    chunks = split_markdown(header + row, limit=200)
    assert any(row in c.text for c in chunks)
    uri = paths.facts_uri('acc-gudou', 'bizov', 'brief.md')
    store.put(uri, header + row)
    projected = store.db.execute('SELECT chunk_text FROM m1_chunks WHERE uri=?', (uri,)).fetchall()
    assert any(row in r[0] for r in projected)


def test_rerank_disabled_and_external_denied(tmp_path):
    class External:
        external = True
        def rerank(self, *args): pytest.fail('disabled reranker invoked')
    provider = migrated_memory(tmp_path / 'off.db', embedding=SyntheticEmbedding(), reranker=External())
    try:
        provider.put(paths.facts_uri('acc-gudou', None, 'brief.md'), '谷斗')
        assert provider.search('谷斗', prefix=paths.facts_uri('acc-gudou'))
    finally: provider.close()
    with pytest.raises(ValueError, match='external rerank'):
        UninitializedMemoryProvider(tmp_path / 'denied.db', embedding=SyntheticEmbedding(),
                                  reranker=External(), rerank_enabled=True)
    assert not (tmp_path / 'denied.db').exists()


def test_opt_in_local_rerank_cannot_change_candidates(store):
    class Local:
        external = False
        def rerank(self, query, candidates):
            assert all(r['company_id'] == 'acc-gudou' for r in candidates)
            return list(reversed(candidates))
    original = store.search('谷斗', prefix=paths.facts_uri('acc-gudou'))
    store.reranker = Local()
    assert store.search('谷斗', prefix=paths.facts_uri('acc-gudou')) == list(reversed(original))
    store.reranker.rerank = lambda query, candidates: [{'uri': paths.facts_uri('acc-other'), 'chunk_index': 0}]
    with pytest.raises(ValueError, match='candidate identities'):
        store.search('谷斗', prefix=paths.facts_uri('acc-gudou'))


@pytest.mark.parametrize('change', ['drop', 'duplicate', 'add', 'replace', 'malformed'])
def test_rerank_rejects_identity_changes(store, change):
    class Local:
        external = False
        def rerank(self, query, candidates):
            assert all(row['company_id'] == 'acc-gudou' for row in candidates)
            if change == 'drop': return candidates[:-1]
            if change == 'duplicate': return [candidates[0]] * len(candidates)
            if change == 'add': return candidates + [dict(candidates[0])]
            if change == 'malformed': return [None]
            candidates[0]['uri'] = paths.facts_uri('acc-other')
            return candidates
    store.reranker = Local()
    with pytest.raises(ValueError, match='candidate identities'):
        store.search('谷斗', prefix=paths.facts_uri('acc-gudou'))


def test_rerank_returns_original_payload_not_modified_candidate(store):
    original = store.search('谷斗', prefix=paths.facts_uri('acc-gudou'))
    class External:
        external = True
        def rerank(self, query, candidates):
            assert all(row['company_id'] == 'acc-gudou' for row in candidates)
            for row in candidates:
                row.update(company_id='acc-other', snippet='tampered', score=999)
            return list(reversed(candidates))
    allowed = migrated_memory(store.database, embedding=SyntheticEmbedding(),
        reranker=External(), rerank_enabled=True, allow_external_rerank=True)
    try:
        assert allowed.search('谷斗', prefix=paths.facts_uri('acc-gudou')) == list(reversed(original))
    finally:
        allowed.close()


def test_fact_id_upgrade_rebuild_and_reopen_are_idempotent(store):
    import sqlite3
    database = store.database
    # Simulate pre-M2-e chunks on a separate connection (no real database).
    with sqlite3.connect(database) as conn:
        conn.execute('ALTER TABLE m1_chunks DROP COLUMN fact_id')
    upgraded = migrated_memory(database, embedding=SyntheticEmbedding())
    try:
        assert upgraded.projection.upgraded_fact_ids is False  # explicit setup already migrated
        assert upgraded.projection_status('acc-gudou') == 'stale'
        rows = upgraded.search('谷斗', prefix=paths.facts_uri('acc-gudou'), filters={'period': '2024'})
        assert len(rows) == 1
        assert upgraded.db.execute('SELECT COUNT(*) FROM m1_chunks WHERE company_id=? AND fact_id IS NULL',
                                   ('acc-gudou',)).fetchone()[0] == 0
    finally:
        upgraded.close()
    reopened = migrated_memory(database, embedding=SyntheticEmbedding())
    try:
        assert reopened.projection.upgraded_fact_ids is False
        assert reopened.projection_status('acc-gudou') == 'ready'
        assert len(reopened.search('谷斗', prefix=paths.facts_uri('acc-gudou'))) == 2
    finally:
        reopened.close()


def test_bridge_fallback_and_matching_scope(store):
    bridge = ToolBridge(store, object(), company_id='acc-gudou', thread_id='reserved')
    for filters in ({'company_id': 'acc-gudou', 'period': '2024'},
                    {'year': '2024'}, {'period': "2024' OR 1=1--"}, ['2024']):
        result = bridge.dispatch('sam_memory_search', {'query': '谷斗', 'filters': filters})
        assert len(result) == (1 if isinstance(filters, dict) and 'company_id' in filters else 2)
    with pytest.raises(ValueError, match='company'):
        bridge.dispatch('sam_memory_search', {'query': '谷斗', 'filters': {'company_id': 'acc-other'}})


def test_exact_combination_is_not_inferred_or_relaxed(store):
    assert store.search('谷斗', prefix=paths.facts_uri('acc-gudou'),
                        filters={'period': '2024', 'unit': '万元'}) == []
    # One uncertain field abandons all otherwise valid filters.
    assert len(store.search('谷斗', prefix=paths.facts_uri('acc-gudou'),
                            filters={'period': '2024', 'attribute': '营收'})) == 2
