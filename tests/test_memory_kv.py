"""Synthetic KV scope and projection separation; no external resources."""
import pytest
from app.memory import paths
from tests.memory_setup import migrated_memory
from app.null_memory import NullMemoryProvider, ReadOnlyMemoryError

class Embedding:
    dim, embedder, model_version = 2, 'synthetic', 'v1'
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, text): return [1., 1.]

@pytest.fixture
def memory(tmp_path):
    memory = migrated_memory(tmp_path / 'test.db', embedding=Embedding(), company_id='acc-gudou')
    yield memory
    memory.close()

@pytest.mark.parametrize('domain', sorted(paths.KV_DOMAINS))
def test_company_json_kv_is_not_a_vector(memory, domain):
    uri = paths.company_kv_uri('acc-gudou', domain, 'events.jsonl')
    text = '{"company":"谷斗科技（上海）有限公司"}\n{"value":1}\n'
    memory.put(uri, text)
    assert memory.read(uri) == text
    assert memory.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 0
    assert memory.search('谷斗') == []
    with pytest.raises(ValueError): memory.search('谷斗', prefix=uri)
    with pytest.raises(ValueError): memory.query(prefix=uri)
    memory.delete(paths.facts_uri('acc-gudou'), recursive=True)
    memory.rebuild_projection('acc-gudou')
    assert memory.read(uri) == text
    doc = paths.facts_uri('acc-gudou', 'fin', 'brief.md')
    memory.put(doc, '员工简报')
    memory.delete(uri)
    assert memory.read(doc) == '员工简报'
    assert memory.search('员工')[0]['uri'] == doc
    with pytest.raises(FileNotFoundError): memory.read(uri)

def test_runtime_is_separate_from_company_and_session(memory):
    uri = paths.runtime_kv_uri('s', 't', 'state.json')
    memory.put(uri, '{"status":"working"}')
    assert memory.read(uri) == '{"status":"working"}'
    assert memory.search('working') == []
    assert not memory.supports_session
    with pytest.raises(ValueError): memory.read('sam://session/s/t')
    memory.delete(paths.runtime_kv_uri('s', 't'), recursive=True)
    with pytest.raises(FileNotFoundError): memory.read(uri)

@pytest.mark.parametrize('uri', ['sam://kv/co/acc-other/cap_table/a.json', 'sam://kv/co/acc-gudou/unknown/a.json',
 'sam://kv/rt/s/t/../a', 'sam://kv/rt/s/t/%2e', 'sam://kv/rt//t/a', 'sam://kv/co/acc-gudou/conversations//x',
 'sam://kv/co/acc-gudou/conversations/x?scope=other'])
def test_kv_rejects_wrong_scope_and_unsafe_paths(memory, uri):
    for call in (lambda: memory.put(uri, '{}'), lambda: memory.read(uri), lambda: memory.delete(uri, recursive=True)):
        with pytest.raises(ValueError): call()

def test_host_scope_and_json_and_facts_authority(memory):
    uri = paths.company_kv_uri('acc-gudou', 'derivation', 'a.json')
    with pytest.raises(ValueError): memory.scoped('acc-other')
    with pytest.raises(ValueError): memory.put(uri, '{}', metadata={'company_id': 'acc-other'})
    for text in ('not JSON', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(ValueError): memory.put(uri, text)
    with pytest.raises(PermissionError): memory.put(paths.fact_uri('acc-gudou', 'fin', '1'), '{}')
    with pytest.raises(PermissionError): memory.put(paths.facts_uri('acc-gudou', 'fin', 'x.json'), '{}', metadata={'layer':'2b_L2'})
    memory.company_id = None
    with pytest.raises(ValueError): memory.put(uri, '{}')
    with pytest.raises(ValueError): memory.read(uri)
    with pytest.raises(ValueError): memory.delete(uri)
    host = memory.scoped('acc-gudou')
    host.put(uri, '{}')
    assert host.read(uri) == '{}'

def test_null_kv_stays_empty_and_rejects_writes():
    memory = NullMemoryProvider()
    uri = paths.company_kv_uri('acc-gudou', 'conversations', 'a.json')
    assert memory.read(uri) == ''
    assert memory.search('谷斗') == []
    for call in (lambda: memory.put(uri, '{}'), lambda: memory.delete(uri)):
        with pytest.raises(ReadOnlyMemoryError): call()


def test_business_stores_roundtrip_on_localvector(memory):
    import json
    from app.memory.company_facts import CapTableStore, FactDerivation
    from app.memory.user_memory.l2_conversation import ConversationStore
    from app.memory.user_memory.l1_memory_brief import RuntimeWorkingMemory
    from app.thread_manager import ThreadManager, ConversationTools
    from tests.test_facts import event, ref
    company = 'acc-gudou'
    caps = CapTableStore(memory)
    caps.append_events(company, [event(company, 'founding_issuance', 'holder-1', 100)])
    snapshot = caps.rebuild_snapshot(company, as_of='2026-02-01T00:00:00+00:00')
    assert snapshot['total_issued'] == 100
    derivation = FactDerivation(memory)
    facts = derivation.derive_manifest({'source_id': 's', 'file_hash': 'a' * 64,
        'candidate_facts': [{'company_id': company, 'entity_id':'holder-1', 'fact_type':'synthetic',
                            'value':1, 'status':'verified', 'source_refs':ref('s')}]})
    key = derivation.promote(facts)[0]
    assert json.loads(memory.read(paths.company_kv_uri(company, 'derivation', *key.split('/'))))['value'] == 1
    turns = ConversationStore(memory)
    turns.append(company, 'thread-1', role='user', text='谷斗科技（上海）有限公司', turn_id='turn-1')
    turns.fold_l1(company, 'thread-1')
    turns.fold_l0(company, 'thread-1', summary='合成摘要')
    ThreadManager(memory).create(company, 'thread-1', title='合成对话')
    tools = ConversationTools(memory, company, 'thread-1')
    assert tools.thread_open('thread-1') == '合成摘要'
    assert tools.conversation_read()[0]['turn_id'] == 'turn-1'
    assert tools.thread_list()[0]['thread_id'] == 'thread-1'
    ThreadManager(memory).archive(company, 'thread-1')
    assert tools.thread_list() == []
    runtime = RuntimeWorkingMemory(memory)
    runtime.create('session-1', 'task-1', goal='合成运行', refs=[paths.facts_uri(company, 'fin', 'brief.md')])
    runtime.append_event('session-1', 'task-1', 'state', {'state':'running'})
    assert runtime.get_runtime('session-1', 'task-1')['state'] == 'running'
    assert runtime.read_events('session-1', 'task-1')
    assert memory.search('谷斗') == []
    assert memory.db.execute('SELECT COUNT(*) FROM m1_chunks').fetchone()[0] == 0
    assert memory.list_facts(company) == []
    from app.knowledge import KnowledgeService
    assert KnowledgeService(memory.database, memory, objects_path=memory.database.parent / 'objects').read(company_id=company)['working_memory']['items'] == []
    runtime.cleanup('session-1', 'task-1')
    assert caps.read_events(company)[0].shares_delta == 100


def test_company_kv_stores_never_cross_injected_company(memory):
    from app.memory.company_facts import CapTableStore
    from app.memory.user_memory.l2_conversation import ConversationStore
    from app.thread_manager import ThreadManager, ConversationTools
    company = 'acc-other'
    for call in (lambda: CapTableStore(memory).read_events(company),
                 lambda: CapTableStore(memory).append_events(company, []),
                 lambda: ConversationStore(memory).read_range(company, 't'),
                 lambda: ConversationStore(memory).append(company, 't', role='user', text='x'),
                 lambda: ThreadManager(memory).list(company),
                 lambda: ThreadManager(memory).create(company, 't', title='x'),
                 lambda: ConversationTools(memory, company, 't')):
        with pytest.raises(ValueError): call()


def test_thread_index_is_company_partitioned(tmp_path):
    from app.thread_manager import ThreadManager
    unbound = migrated_memory(tmp_path / 'memory.db', embedding=Embedding())
    try:
        ThreadManager(unbound).create('acc-gudou', 't', title='合成')
        ThreadManager(unbound).create('acc-other', 't', title='隔离空壳')
        a, b = unbound.scoped('acc-gudou'), unbound.scoped('acc-other')
        assert ThreadManager(a).list('acc-gudou')[0]['title'] == '合成'
        assert ThreadManager(b).list('acc-other')[0]['title'] == '隔离空壳'
        a.delete(paths.company_kv_uri('acc-gudou', 'conversations'), recursive=True)
        assert ThreadManager(a).list('acc-gudou') == []
        assert ThreadManager(b).list('acc-other')[0]['title'] == '隔离空壳'
    finally:
        unbound.close()


def test_old_root_is_not_migrated_and_missing_business_data_is_empty(memory, tmp_path):
    from app.memory.company_facts import CapTableStore
    from app.memory.user_memory.l2_conversation import ConversationStore
    from app.memory.user_memory.l1_memory_brief import RuntimeWorkingMemory
    from app.thread_manager import ThreadManager
    from app.knowledge import KnowledgeService
    # Opaque historical fixture never submitted to a provider/connector.
    memory.db.execute("CREATE TABLE legacy_fixture(uri TEXT, content TEXT)")
    memory.db.execute("INSERT INTO legacy_fixture VALUES(?,?)", ('viking://sam-test/2b_facts/acc-gudou/events.jsonl', '{}'))
    memory.db.commit()
    assert CapTableStore(memory).read_events('acc-gudou') == []
    assert ConversationStore(memory).read_range('acc-gudou', 't') == []
    assert ThreadManager(memory).list('acc-gudou') == []
    runtime = RuntimeWorkingMemory(memory)
    assert runtime.read_events('s', 't') == []
    with pytest.raises(KeyError): runtime.get_runtime('s', 't', read_only=True)
    result = KnowledgeService(memory.database, memory, objects_path=tmp_path / 'objects').read(company_id='acc-gudou')
    assert result['facts']['cap_table']['events'] == []
    assert result['facts']['cap_table']['snapshot'] is None
    assert result['working_memory']['items'] == []
    assert memory.db.execute('SELECT COUNT(*) FROM memory_kv').fetchone()[0] == 0
    assert memory.db.execute('SELECT COUNT(*) FROM legacy_fixture').fetchone()[0] == 1


def test_kv_never_uses_embedding_even_when_it_fails(memory):
    def fail(*_): raise AssertionError('KV called embedding')
    memory.embedding.embed_texts = fail
    memory.embedding.embed_query = fail
    uri = paths.runtime_kv_uri('s', 't', 'state.json')
    memory.put(uri, '{}')
    assert memory.read(uri) == '{}'
    memory.delete(uri)


def test_runtime_ref_rejects_retired_or_reserved_namespace(memory):
    from app.memory.user_memory.l1_memory_brief import RuntimeRef
    assert RuntimeRef(paths.original_uri('acc-gudou', 'source.pdf')).uri.startswith('sam://')
    for uri in ('viking://legacy/source', 'sam://session/s/t', 'sam://facts/acc-gudou/../other'):
        with pytest.raises(ValueError): RuntimeRef(uri)
