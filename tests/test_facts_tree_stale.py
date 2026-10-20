"""Gate-open reads must not synthesize atomic cards, even in old mixed stores."""
import pytest

from app import settings
from app.memory import paths
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY
from tests.test_facts_tree_build import builder, payload


def forbid(*args, **kwargs):
    raise AssertionError('gate-open must not synthesize facts or auto-rebuild')


def snapshot(store):
    return {table: [tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('m1_documents', 'm1_chunks', 'm1_projection_state')}


@pytest.mark.parametrize('state', ['stale', 'error', 'missing'])
def test_open_search_empty_never_rebuilds_or_creates_atomic_cards(real_store, monkeypatch, state):
    memory = real_store.memory
    assert memory.projection_status(COMPANY) == 'stale'
    if state == 'error':
        real_store.db.execute("UPDATE m1_projection_state SET error='old error' WHERE company_id=?", (COMPANY,))
    elif state == 'missing':
        real_store.db.execute('DELETE FROM m1_projection_state WHERE company_id=?', (COMPANY,))
    real_store.db.commit()
    before = snapshot(real_store)
    monkeypatch.setattr(memory, 'rebuild_projection', forbid)
    monkeypatch.setattr(memory, '_fact_document', forbid)
    assert memory.search('测试', prefix=paths.facts_uri(COMPANY)) == []
    assert memory.query(prefix=paths.facts_uri(COMPANY)) == []
    assert snapshot(real_store) == before


def test_open_search_and_query_hit_only_existing_tree(real_store, monkeypatch):
    memory = real_store.memory
    result = builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    assert memory.projection_status(COMPANY) == 'stale'
    before = snapshot(real_store)
    monkeypatch.setattr(memory, 'rebuild_projection', forbid)
    monkeypatch.setattr(memory, '_fact_document', forbid)
    assert {r['uri'] for r in memory.search('测试', prefix=paths.facts_uri(COMPANY))} == {result['uri']}
    assert {r['uri'] for r in memory.query(prefix=paths.facts_uri(COMPANY))} == {result['uri']}
    assert snapshot(real_store) == before
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0


def test_open_explicit_rebuild_only_reprojects_tree(real_store, monkeypatch):
    memory = real_store.memory
    result = builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    real_store.db.execute('DELETE FROM m1_chunks')
    real_store.db.commit()
    state = memory.projection_status(COMPANY)
    monkeypatch.setattr(memory, '_fact_rows', forbid)
    monkeypatch.setattr(memory, '_fact_document', forbid)
    memory.rebuild_projection(COMPANY)
    assert memory.projection_status(COMPANY) == state
    assert {r['uri'] for r in memory.search('测试', prefix=paths.facts_uri(COMPANY))} == {result['uri']}
    assert real_store.db.execute('SELECT count(*) FROM m1_documents').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0


def test_closed_stale_search_still_rebuilds_atomic_cards(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    memory = real_store.memory
    rows = memory.search('测试', prefix=paths.facts_uri(COMPANY))
    assert any(r['uri'].endswith('/F1.md') for r in rows)
    assert any(r['uri'].endswith('/F1.md') for r in memory.query(prefix=paths.facts_uri(COMPANY)))
    assert memory.projection_status(COMPANY) == 'ready'


def test_open_mixed_history_hidden_before_top_k_without_cleanup(real_store, monkeypatch):
    memory = real_store.memory
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    memory.search('测试', prefix=paths.facts_uri(COMPANY))
    legacy = paths.facts_uri(COMPANY, 'bizov', 'brief.md')
    memory.publish_documents({legacy: '旧摘要'}, metadata={'company_id': COMPANY})
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    result = builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    before = snapshot(real_store)
    memory.top_k = 1
    assert [r['uri'] for r in memory.search('测试', prefix=paths.facts_uri(COMPANY))] == [result['uri']]
    assert [r['uri'] for r in memory.query(prefix=paths.facts_uri(COMPANY))] == [result['uri']]
    assert snapshot(real_store) == before
    # Explicit tree rebuild preserves old rows/chunks rather than cleaning history.
    old_chunks = [tuple(r) for r in real_store.db.execute('SELECT * FROM m1_chunks WHERE uri!=? ORDER BY chunk_id', (result['uri'],))]
    monkeypatch.setattr(memory, '_fact_rows', forbid)
    memory.rebuild_projection(COMPANY)
    assert [tuple(r) for r in real_store.db.execute('SELECT * FROM m1_chunks WHERE uri!=? ORDER BY chunk_id', (result['uri'],))] == old_chunks
    assert [r['uri'] for r in memory.search('测试', prefix=paths.facts_uri(COMPANY))] == [result['uri']]
