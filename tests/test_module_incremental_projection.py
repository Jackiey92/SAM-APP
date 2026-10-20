"""M4.5 synthetic TEST/谷斗 data only; all ledgers live under tmp_path."""
import pytest
from app.memory.company_facts.paths import module_document_uri
from app.module_taxonomy import MODULES

@pytest.mark.parametrize('module', MODULES)
def test_module_uri(module):
    assert module_document_uri('acc-test', module.key, 'brief.json') == f'sam://facts/acc-test/{module.key}/brief.json'

@pytest.mark.parametrize('module,name', [('unknown', 'brief.json'), ('fin', '../brief.json')])
def test_module_uri_rejects_invalid(module, name):
    with pytest.raises(ValueError):
        module_document_uri('acc-test', module, name)

import json
from dataclasses import replace
from app.db import init_db, connect
from app.ports import LocalMemoryProvider
from app.storage import ArchiveFileStore
from app.memory.company_facts import ConsolidationService, L0GuideService
from app.memory.company_facts.module_projection import ModuleProjectionService
from app.memory.company_facts.paths import brief_json_uri, guide_json_uri, brief_md_uri, guide_md_uri
from tests.fact_candidates import fact_candidate

COMPANY = 'acc-test'

class Employee:
    def __init__(self):
        self.calls = []
        self.bad = set()
        self.unit_bad = False

    def run(self, *, skill, text, **kwargs):
        request = json.loads(text)
        self.calls.append((skill, request))
        if skill == 'fact-brief':
            key = request['module_key']
            old = (request.get('previous_brief') or {}).get('items', [])
            items = [dict(item, action='retain') for item in old]
            old_refs = {ref for item in old for ref in item['fact_ids']}
            for f in request['facts']:
                if f['id'] in old_refs:
                    continue
                conclusion = f"TEST主体 {f['value']}{f['unit'] or ''}{{f:{f['id']}}}"
                if key in self.bad:
                    conclusion = 'TEST主体 999万元' if not self.unit_bad else f"{f['value']}万元{{f:{f['id']}}}"
                items.append(dict(topic='TEST事实', conclusion=conclusion, fact_ids=[f['id']], action='add'))
            return items
        return [dict(conclusion=f"TEST入口 {n['text']}{{l:{item['entry_id']}}}", l1_entry_ids=[item['entry_id']])
                for item in request['l1']['items'] for n in item['verified_numbers'] if n['unit']]

@pytest.fixture
def env(tmp_path):
    db = tmp_path / 'app.db'
    init_db(db)
    memory = LocalMemoryProvider(tmp_path / 'memory')
    archive = ArchiveFileStore(tmp_path / 'objects', db)
    facts = ConsolidationService(db)
    worker = Employee()
    service = ModuleProjectionService(db, memory, worker=worker, token_budget=10000)
    def add(key, label, value='3.20', unit='亿元'):
        source = archive.put_bytes(f'TEST {key} {label} {value} {unit}'.encode(), original_name='TEST.txt')
        candidate = replace(fact_candidate(source.file_hash, label, value, company_id=COMPANY,
            entity='谷斗科技（上海）有限公司', unit=unit), module_key=key)
        return facts.consolidate_candidates([candidate]), candidate
    return db, memory, facts, worker, service, add

def snapshot(memory, keys):
    return {module_document_uri(COMPANY, key, name): memory.read(module_document_uri(COMPANY, key, name))
            for key in keys for name in ('brief.json', 'brief.md', 'guide.json', 'guide.md')}

def test_cold_start_incremental_all_nine_unchanged_and_ids_resolve(env):
    _, memory, _, worker, service, add = env
    ids = [add(m.key, m.key)[0].verified_fact_ids[0] for m in MODULES]
    assert not service.update(company_id=COMPANY, verified_fact_ids=ids)['unresolved']
    before = snapshot(memory, [m.key for m in MODULES if m.key != 'fin'])
    mtimes = {uri: memory._path(uri).stat().st_mtime_ns for uri in before}
    company_before = json.loads(memory.read(brief_json_uri(COMPANY)))
    worker.calls.clear()
    new = add('fin', 'TEST新增', '4.10')[0].verified_fact_ids
    assert not service.update(company_id=COMPANY, verified_fact_ids=new)['unresolved']
    after = snapshot(memory, [m.key for m in MODULES if m.key != 'fin'])
    assert before == after
    assert mtimes == {uri: memory._path(uri).stat().st_mtime_ns for uri in after}
    assert {u: v.encode() for u, v in before.items()} == {u: v.encode() for u, v in after.items()}
    assert {u: json.loads(v) for u, v in before.items() if u.endswith('.json')} == {u: json.loads(v) for u, v in after.items() if u.endswith('.json')}
    company = json.loads(memory.read(brief_json_uri(COMPANY)))
    assert [s for s in company_before['sections'] if s['module_key'] != 'fin'] == [s for s in company['sections'] if s['module_key'] != 'fin']
    brief_call = [req for skill, req in worker.calls if skill == 'fact-brief']
    assert len(brief_call) == 1 and brief_call[0]['module_key'] == 'fin'
    assert [f['id'] for f in brief_call[0]['facts']] == list(new)
    entries = {i['entry_id'] for i in company['items']}
    assert len(entries) == len(company['items'])
    guide = json.loads(memory.read(guide_json_uri(COMPANY)))
    for item in guide['items']:
        assert set(item['l1_entry_ids']) <= entries
        for pointer in item['l1_uris']:
            assert pointer.split('#')[0] == brief_md_uri(COMPANY)
            assert f'id="{pointer.split("#")[1]}"' in memory.read(brief_md_uri(COMPANY))
    assert L0GuideService(memory).read_guide(company_id=COMPANY) == memory.read(guide_md_uri(COMPANY))
    assert '{f:' not in memory.read(brief_md_uri(COMPANY))
    assert '{l:' not in memory.read(guide_md_uri(COMPANY))

@pytest.mark.parametrize('unit_bad', [False, True])
def test_rejection_is_atomic_sibling_survives_and_retry_no_rewrite(env, unit_bad):
    _, memory, facts, worker, service, add = env
    initial = [add(key, key)[0].verified_fact_ids[0] for key in ('fin', 'hr')]
    service.update(company_id=COMPANY, verified_fact_ids=initial)
    old_fin = snapshot(memory, ['fin'])
    old_hr = snapshot(memory, ['hr'])
    batch = [add(key, 'TEST新增-' + key, '4.10')[0].verified_fact_ids[0] for key in ('fin', 'hr')]
    worker.bad = {'fin'}; worker.unit_bad = unit_bad
    result = service.update(company_id=COMPANY, verified_fact_ids=batch)
    assert [e['module_key'] for e in result['unresolved']] == ['fin']
    assert result['unresolved'][0]['result']['status'] == 'rejected'
    assert snapshot(memory, ['fin']) == old_fin
    assert snapshot(memory, ['hr']) != old_hr
    sibling = snapshot(memory, ['hr'])
    before_facts = facts.list_facts(company_id=COMPANY)
    worker.bad.clear(); worker.calls.clear()
    assert not service.update(company_id=COMPANY, verified_fact_ids=batch)['unresolved']
    assert snapshot(memory, ['hr']) == sibling
    assert facts.list_facts(company_id=COMPANY) == before_facts
    assert [r['module_key'] for s, r in worker.calls if s == 'fact-brief'] == ['fin']

@pytest.mark.parametrize('value,unit', [('32000', '万元'), ('3.20', '万元'), ('9.10', '亿元')])
def test_conflict_keeps_old_and_decide_then_update(env, value, unit):
    db, memory, facts, worker, service, add = env
    first, _ = add('fin', 'TEST收入')
    service.update(company_id=COMPANY, verified_fact_ids=first.verified_fact_ids)
    old = snapshot(memory, ['fin'])
    conflict, candidate = add('fin', 'TEST收入', value, unit)
    assert not conflict.verified_fact_ids and conflict.todo_ids
    todo = facts.list_todos(company_id=COMPANY)[0]
    assert todo['reason'] == 'conflict' and todo['status'] == 'open'
    assert len(facts.list_verified_facts(company_id=COMPANY)) == 1
    assert not service.update(company_id=COMPANY, verified_fact_ids=[])['unresolved']
    assert snapshot(memory, ['fin']) == old
    facts.resolve(todo['id'], choose='candidate')
    current = facts.list_verified_facts(company_id=COMPANY)
    assert len(current) == 1 and current[0]['value'] == value
    with connect(db) as conn:
        old_fact = dict(conn.execute('SELECT * FROM facts WHERE id=?', (first.verified_fact_ids[0],)).fetchone())
    assert old_fact['superseded_by'] == current[0]['id'] and old_fact['valid_to']
    # Employee explicitly revises the existing entry after human decision.
    original = worker.run
    def revise(**kwargs):
        req = json.loads(kwargs['text'])
        if kwargs['skill'] != 'fact-brief':
            return original(**kwargs)
        f = req['facts'][0]; item = req['previous_brief']['items'][0]
        return [dict(topic=item['topic'], conclusion=f"TEST主体 {f['value']}{f['unit']}{{f:{f['id']}}}",
            fact_ids=[f['id']], action='revise', entry_id=item['entry_id'],
            revised_fact_ids=item['fact_ids'], reason='TEST人工裁决')]
    worker.run = revise
    assert not service.update(company_id=COMPANY, verified_fact_ids=[current[0]['id']])['unresolved']
    assert f'{value}{unit}' in memory.read(module_document_uri(COMPANY, 'fin', 'brief.md'))
    assert json.loads(memory.read(module_document_uri(COMPANY, 'fin', 'brief.json')))['items'][0]['entry_id'] == 'fin-1'

def test_unclassified_projects_without_brief(env):
    _, memory, _, worker, service, add = env
    from app.memory.company_facts.l2_projection import L2ProjectionService
    from app.memory.company_facts.paths import fact_uri
    result, _ = add(None, 'TEST待归类')
    L2ProjectionService(service.db_path, memory).publish(company_id=COMPANY)
    assert memory.read(fact_uri(COMPANY, result.verified_fact_ids[0]))
    assert not service.update(company_id=COMPANY, verified_fact_ids=result.verified_fact_ids)['touched_modules']
    assert not worker.calls
    assert service.read(company_id=COMPANY, module_key='fin', layer='brief') is None

def test_full_rebuild_and_legacy_cold_start(env):
    _, memory, _, worker, service, add = env
    add('fin', 'TEST收入')
    memory.put(brief_json_uri(COMPANY), json.dumps({'company_id': COMPANY, 'items': []}))
    assert service.update(company_id=COMPANY, verified_fact_ids=[])['published_modules'] == ['fin']
    before = memory.read(brief_md_uri(COMPANY))
    assert not service.rebuild(company_id=COMPANY)['unresolved']
    assert memory.read(brief_md_uri(COMPANY)) == before

def test_company_assembly_rejects_invented_number(env):
    _, memory, _, _, service, add = env
    ids = add('fin', 'TEST收入')[0].verified_fact_ids
    service.update(company_id=COMPANY, verified_fact_ids=ids)
    old_company = memory.read(brief_json_uri(COMPANY))
    uri = module_document_uri(COMPANY, 'fin', 'brief.json')
    bad = json.loads(memory.read(uri)); bad['items'][0]['conclusion'] = f'99亿元{{f:{ids[0]}}}'
    memory.put(uri, json.dumps(bad))
    with pytest.raises(Exception, match='assembled brief number'):
        service.assemble(company_id=COMPANY)
    assert memory.read(brief_json_uri(COMPANY)) == old_company

def test_read_api_host_scope_and_no_write(env, monkeypatch):
    import webapp.app as web
    db, memory, _, _, service, add = env
    service.update(company_id=COMPANY, verified_fact_ids=add('fin', 'TEST收入')[0].verified_fact_ids)
    monkeypatch.setattr(web, 'MAIN_DB', db)
    monkeypatch.setitem(web.app.extensions, 'sam_memory_provider', memory)
    monkeypatch.setenv('SAM_COMPANY_ID', COMPANY)
    before = snapshot(memory, ['fin'])
    client = web.app.test_client()
    for layer in ('brief', 'guide'):
        response = client.get(f'/api/facts/modules/fin/{layer}')
        assert response.status_code == 200 and response.mimetype == 'text/markdown'
        assert response.get_data(as_text=True) == service.read(company_id=COMPANY, module_key='fin', layer=layer)
        assert client.get(f'/api/facts/modules/fin/{layer}?format=json').get_json()['module_key'] == 'fin'
    assert client.get('/api/facts/modules/fin/brief?company_id=acc-other').status_code == 400
    assert client.get('/api/facts/modules/unknown/brief').status_code == 400
    assert client.get('/api/facts/modules/hr/brief').status_code == 404
    assert client.post('/api/facts/modules/fin/brief').status_code == 405
    assert snapshot(memory, ['fin']) == before
    assert client.post('/api/internal/facts/projections/rebuild', json={'confirm': True}).status_code == 403

class Embedding:
    dim, embedder, model_version = 2, 'TEST', 'v1'
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, query): return [1., 1.]


def test_localvector_unclassified_search_and_atomic_failure(env):
    from tests.memory_setup import migrated_memory
    from app.memory.provider import ProjectionError
    from app.memory.company_facts.l2_projection import L2ProjectionService
    from app.memory.paths import facts_uri
    db, _, _, worker, _, add = env
    memory = migrated_memory(db, embedding=Embedding(), company_id=COMPANY)
    try:
        ids = add(None, 'TEST待归类')[0].verified_fact_ids
        L2ProjectionService(db, memory).publish(company_id=COMPANY)
        hits = memory.search('TEST待归类', prefix=facts_uri(COMPANY))
        assert any(h['uri'].endswith(f'/F{ids[0]}.md') and h['module_key'] == '待归类' for h in hits)
        service = ModuleProjectionService(db, memory, worker=worker, token_budget=10000)
        assert not service.update(company_id=COMPANY, verified_fact_ids=ids)['touched_modules']
        assert not worker.calls
        fin = add('fin', 'TEST收入')[0].verified_fact_ids
        assert not service.update(company_id=COMPANY, verified_fact_ids=fin)['unresolved']
        old = snapshot(memory, ['fin'])
        docs = {uri: content + 'TEST' for uri, content in old.items()}
        count = [0]
        original = memory._project
        def fail(row):
            count[0] += 1
            if count[0] == 2:
                raise ValueError('TEST vector failure')
            return original(row)
        memory._project = fail
        with pytest.raises(ProjectionError, match='rolled back'):
            memory.publish_documents(docs, metadata={'company_id': COMPANY, 'layer': '2b_module'})
        assert snapshot(memory, ['fin']) == old
        assert memory.projection_status(COMPANY) == 'stale'
        with pytest.raises(PermissionError):
            memory.publish_documents({facts_uri(COMPANY, 'fin', 'F1.md'): 'bad'}, metadata={'company_id': COMPANY})
    finally:
        memory.close()


@pytest.mark.parametrize('bad_action', ['rewrite-retain', 'missing-entry', 'missing-revision-reason', 'foreign-module-ref'])
def test_incremental_audit_rejects_without_losing_old_pair(env, bad_action):
    _, memory, _, worker, service, add = env
    first = add('fin', 'TEST收入')[0].verified_fact_ids
    other = add('hr', 'TEST人数', '10', '人')[0].verified_fact_ids
    assert not service.update(company_id=COMPANY, verified_fact_ids=[*first, *other])['unresolved']
    old = snapshot(memory, ['fin'])
    batch = add('fin', 'TEST新增', '4.10')[0].verified_fact_ids
    original = worker.run
    def invalid(**kwargs):
        if kwargs['skill'] != 'fact-brief':
            return original(**kwargs)
        item = dict(json.loads(kwargs['text'])['previous_brief']['items'][0], action='retain')
        if bad_action == 'rewrite-retain': item['conclusion'] = 'TEST改写'
        if bad_action == 'missing-entry': return [dict(topic='TEST新增', conclusion='TEST', fact_ids=list(batch), action='add')]
        if bad_action == 'missing-revision-reason': item.update(action='revise', revised_fact_ids=list(first))
        if bad_action == 'foreign-module-ref': item.update(action='revise', reason='TEST', revised_fact_ids=list(first), fact_ids=list(other))
        return [item]
    worker.run = invalid
    assert service.update(company_id=COMPANY, verified_fact_ids=batch)['unresolved']
    assert snapshot(memory, ['fin']) == old


def test_l0_reject_rolls_back_new_l1_and_preserves_company(env):
    _, memory, _, worker, service, add = env
    service.update(company_id=COMPANY, verified_fact_ids=add('fin', 'TEST收入')[0].verified_fact_ids)
    old = snapshot(memory, ['fin'])
    old_company = {u: memory.read(u) for u in (brief_json_uri(COMPANY), brief_md_uri(COMPANY), guide_json_uri(COMPANY), guide_md_uri(COMPANY))}
    batch = add('fin', 'TEST新增', '4.10')[0].verified_fact_ids
    original = worker.run
    def bad_guide(**kwargs):
        if kwargs['skill'] == 'fact-guide':
            eid = json.loads(kwargs['text'])['l1']['items'][0]['entry_id']
            return [dict(conclusion=f'99亿元{{l:{eid}}}', l1_entry_ids=[eid])]
        return original(**kwargs)
    worker.run = bad_guide
    rejected = service.update(company_id=COMPANY, verified_fact_ids=batch)
    assert rejected['unresolved'][0]['result']['status'] == 'rejected'
    assert snapshot(memory, ['fin']) == old
    assert {u: memory.read(u) for u in old_company} == old_company


def test_admin_rebuild_requires_confirm_and_uses_only_host_scope(env, monkeypatch):
    import webapp.app as web
    db, memory, _, _, _, _ = env
    monkeypatch.setattr(web, 'MAIN_DB', db)
    monkeypatch.setitem(web.app.extensions, 'sam_memory_provider', memory)
    monkeypatch.setenv('SAM_COMPANY_ID', COMPANY)
    monkeypatch.setenv('SAM_INTERNAL_TOKEN', 'TEST-token')
    seen = []
    class Rebuild:
        def __init__(self, *args, **kwargs): pass
        def rebuild(self, *, company_id):
            seen.append(('modules', company_id))
            return {'published_modules': ['fin'], 'unresolved': []}
    class Project:
        def __init__(self, *args, **kwargs): pass
        def publish(self, *, company_id): seen.append(('l2', company_id))
    monkeypatch.setattr(web, 'ModuleProjectionService', Rebuild)
    monkeypatch.setattr(web, 'L2ProjectionService', Project)
    client = web.app.test_client()
    headers = {'X-SAM-Internal-Token': 'TEST-token'}
    assert client.post('/api/internal/facts/projections/rebuild', json={}, headers=headers).status_code == 400
    assert not seen
    result = client.post('/api/internal/facts/projections/rebuild', json={'confirm': True, 'company_id': 'acc-other'}, headers=headers)
    assert result.status_code == 200
    assert seen == [('l2', COMPANY), ('modules', COMPANY)]
    assert client.post('/api/internal/facts/projections/rebuild', json={'confirm': True}, headers=headers,
                       environ_overrides={'REMOTE_ADDR': '10.0.0.8'}).status_code == 403
