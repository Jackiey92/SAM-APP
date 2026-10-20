"""Real leaf ledger + real vector provider, offline and temporary only."""
import pytest

from app import settings
from app.db.database import connect
from app.facts_tree.leaf_store import LeafPublishService
from app.facts_tree.rollup import FactsTreeRollup
from app.memory import paths
from app.memory.provider import ProjectionError
from app.module_tree import MODULES
from tests.memory_setup import migrated_memory
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_leaves import artifact, COMPANY, SHA, publish
from tests.test_facts_tree_build import builder, payload


@pytest.fixture
def real_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    memory = migrated_memory(tmp_path / 'ledger.db', embedding=Embedding(), company_id=COMPANY)
    store = LeafPublishService(memory.database, memory, company_id=COMPANY)
    store.binding.register(COMPANY, '临时事务公司')
    store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (SHA, 'sample.docx', 1, '/unused', 'now'))
    store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '测试指标', '2025-01', '9', '个', SHA, 'bizov', 'now'))
    store.db.commit()
    yield store
    store.close()
    memory.close()


def counts(store):
    return {t: store.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
            for t in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links', 'm1_documents', 'm1_chunks')}


def conflict(store):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict',
                    data=False, target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
    text = text[:text.index('# 测试叶子')] + f'## 候选\n有9个[^one]。\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    return store.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001',
        target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job',
        markdown=text, source_files=[SHA], fact_ids=[1])


def test_real_build_leaf_ledger_projection_and_search_same_transaction(real_store):
    store = real_store
    assert store.db is not store.memory.db
    assert store.memory.db.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
    trace = []
    store.db.set_trace_callback(trace.append)
    result = builder(store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    assert counts(store)['leaf_revisions'] == counts(store)['facts_tree_leaf_index'] == counts(store)['fact_leaf_links'] == 1
    assert counts(store)['m1_documents'] == 1 and counts(store)['m1_chunks'] > 0
    content = store.memory.read(result['uri'])
    assert '有9个' in content
    assert any(r['uri'] == result['uri'] for r in store.memory.search('测试', prefix=paths.facts_uri(COMPANY)))
    assert sum(q == 'BEGIN IMMEDIATE' for q in trace) == 1
    assert not store.db.in_transaction


@pytest.mark.parametrize('kind', ['leaf', 'conflict'])
def test_join_projection_failure_rolls_back_entire_ledger(real_store, monkeypatch, kind):
    prior_state = real_store.memory.projection_status(COMPANY)
    def fail(texts): raise RuntimeError('injected embedding failure')
    monkeypatch.setattr(real_store.memory.embedding, 'embed_texts', fail)
    with pytest.raises(ProjectionError, match='caller must roll back'):
        if kind == 'leaf':
            builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
        else:
            conflict(real_store)
    assert all(value == 0 for value in counts(real_store).values())
    assert not real_store.db.in_transaction
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    assert real_store.memory.projection_status(COMPANY) == prior_state
    assert real_store.memory.search('测试', prefix=paths.facts_uri(COMPANY)) == []


def test_real_conflict_publish_and_rollups(real_store):
    builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    result = conflict(real_store)
    assert '有9个' in real_store.memory.read(result['uri'])
    rollup = FactsTreeRollup(real_store)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    uris = [paths.trunk_doc_uri(COMPANY, MODULES[1].directory, name)
            for name in ('trunk-brief.md', 'trunk-guide.md')]
    uris += [paths.tree_brief_uri(COMPANY), paths.tree_guide_uri(COMPANY)]
    for uri in uris:
        assert real_store.memory.read(uri).strip()
    assert not rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert not real_store.db.in_transaction


def test_rollup_projection_failure_is_atomic(real_store, monkeypatch):
    builder(real_store).build_leaf(company_id=COMPANY, leaf_code='L-bizov-08', employee_payload=payload())
    before = counts(real_store)
    original = real_store.memory.embedding.embed_texts
    calls = []
    def fail_second(texts):
        calls.append(texts)
        if len(calls) == 2: raise RuntimeError('second rollup document failed')
        return original(texts)
    monkeypatch.setattr(real_store.memory.embedding, 'embed_texts', fail_second)
    with pytest.raises(ProjectionError):
        FactsTreeRollup(real_store).rollup_trunk(company_id=COMPANY, trunk='bizov')
    assert counts(real_store) == before
    for name in ('trunk-brief.md', 'trunk-guide.md'):
        with pytest.raises(FileNotFoundError):
            real_store.memory.read(paths.trunk_doc_uri(COMPANY, MODULES[1].directory, name))


@pytest.mark.parametrize('bad', ['uri', 'company', 'text'])
def test_join_preserves_document_validation(real_store, bad):
    uri = paths.leaf_uri(COMPANY, MODULES[1].directory, 'L-bizov-01-test.md')
    metadata = {'company_id': COMPANY}
    content = '正文'
    if bad == 'uri': uri = uri.replace('-01-', '-00-')
    elif bad == 'company': metadata['company_id'] = 'acc-other'
    else: content = 123
    real_store.db.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises((ValueError, PermissionError)):
            real_store.memory._publish_documents_join({uri: content}, metadata=metadata, connection=real_store.db)
        assert real_store.db.in_transaction
        assert all(v == 0 for v in counts(real_store).values())
    finally:
        real_store.db.rollback()


def test_join_does_not_commit_and_caller_can_rollback(real_store):
    uri = paths.tree_brief_uri(COMPANY)
    statements = []
    real_store.db.execute('BEGIN IMMEDIATE')
    real_store.db.set_trace_callback(statements.append)
    real_store.memory._publish_documents_join({uri: '正文'}, metadata={'company_id': COMPANY}, connection=real_store.db)
    assert real_store.db.in_transaction
    assert not any(q in ('BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK') for q in statements)
    assert real_store.memory.db.execute('SELECT count(*) FROM m1_documents').fetchone()[0] == 0
    real_store.db.rollback()
    with pytest.raises(FileNotFoundError): real_store.memory.read(uri)


def test_join_requires_active_same_database_and_gate(real_store, tmp_path, monkeypatch):
    kwargs = dict(documents={paths.tree_brief_uri(COMPANY): '正文'}, metadata={'company_id': COMPANY})
    with pytest.raises(ValueError, match='active'):
        real_store.memory._publish_documents_join(**kwargs, connection=real_store.db)
    other = connect(tmp_path / 'other.db')
    try:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='share'):
            real_store.memory._publish_documents_join(**kwargs, connection=other)
    finally:
        other.rollback(); other.close()
    real_store.db.execute('BEGIN IMMEDIATE')
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    try:
        assert not real_store._joins_memory()
        with pytest.raises(RuntimeError):
            real_store.memory._publish_documents_join(**kwargs, connection=real_store.db)
    finally:
        real_store.db.rollback()


def test_failed_update_keeps_previous_revision_and_projection(real_store, monkeypatch):
    result = publish(real_store)
    before = counts(real_store)
    content = real_store.memory.read(result['uri'])
    prior_state = real_store.memory.projection_status(COMPANY)
    def fail(texts): raise RuntimeError('update projection failed')
    monkeypatch.setattr(real_store.memory.embedding, 'embed_texts', fail)
    with pytest.raises(ProjectionError):
        publish(real_store, artifact(version=2))
    assert counts(real_store) == before
    assert real_store.memory.read(result['uri']) == content
    assert real_store.memory.projection_status(COMPANY) == prior_state
    assert any(r['uri'] == result['uri'] for r in real_store.memory.search('测试', prefix=paths.facts_uri(COMPANY)))


def test_join_failure_leaves_transaction_to_caller(real_store, monkeypatch):
    uri = paths.tree_brief_uri(COMPANY)
    def fail(texts): raise RuntimeError('failure without seam rollback')
    monkeypatch.setattr(real_store.memory.embedding, 'embed_texts', fail)
    real_store.db.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(ProjectionError):
            real_store.memory._publish_documents_join({uri: '正文'}, metadata={'company_id': COMPANY}, connection=real_store.db)
        assert real_store.db.in_transaction
    finally:
        real_store.db.rollback()
    with pytest.raises(FileNotFoundError): real_store.memory.read(uri)


def test_join_selection_normalizes_path_and_excludes_other_database(real_store, tmp_path):
    original = real_store.memory.database
    real_store.memory.database = original.parent / '.' / original.name
    assert real_store._joins_memory()
    real_store.memory.database = tmp_path / 'different.db'
    assert not real_store._joins_memory()
    real_store.memory.database = original
