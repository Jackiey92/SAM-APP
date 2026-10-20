"""Employee module choice is preserved, never inferred by the host."""
import json
from types import SimpleNamespace

import pytest

from app import settings
from app.db.database import connect
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.module_tree import MODULES
from tests.test_document_read import task, service, fact, ROOT
from tests.test_document_read_leaves import open_task, LeafModel, run, LOC
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import kernel, COMPANY, SHA, clean
from tests.test_facts_tree_plan import Worker, service as planner_service
from tests.test_facts_tree_build import documents, payload


@pytest.mark.parametrize('key', [m.key for m in MODULES] + [None, 'wrong', 42, False, [], {}])
def test_open_employee_module_delivery_accept_or_feedback(open_task, key):
    accepted = key is None or isinstance(key, str) and key in {m.key for m in MODULES}
    model = LeafModel(open_task, codes=[])
    real = model.execute
    def execute(**kwargs):
        request = json.loads(kwargs['text'])
        result = real(**kwargs)
        if request['action'] == 'extract':
            assert request['module_options'] == [dict(key=m.key, directory=m.directory, hidden=m.hidden) for m in MODULES]
            return dict(facts=[fact(source_loc=LOC, module_key=key)], complete=True)
        return result
    model.execute = execute
    result = run(service(open_task, model), extract=2)
    with connect(open_task[0]) as db:
        rows = db.execute('SELECT module_key FROM facts').fetchall()
    if accepted:
        assert result['done']
        assert [r[0] for r in rows] == [key]
        assert not result['correction_feedback']
    else:
        assert result['status'] == 'partial' and not result.get('done')
        assert not result['complete'] and not result['accepted'] and not result['fact_ids']
        assert not rows
        assert model.actions == ['classify', 'attribute', 'extract', 'extract']
        assert result['calls']['extract'] == 2
        assert len(result['unresolved']) == 2
        for rejected in result['unresolved']:
            assert rejected['reason'] == 'invalid_candidate'
            assert 'invalid module_key' in rejected['blocking']
            assert rejected['candidate']['module_key'] == key
        assert result['correction_feedback'] == [result['unresolved'][-1]]
        requests = [r for r in model.requests if r['action'] == 'extract']
        assert len(requests) == 2 and not requests[0]['correction_feedback']
        assert requests[1]['correction_feedback'] == [result['unresolved'][0]]
        assert not model.targets and 'leaves_planned' not in result
        assert not open_task[2].docs
        with connect(open_task[0]) as db:
            for table in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links', 'm1_documents', 'm1_chunks'):
                assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_closed_unknown_module_still_unclassified_and_no_options(task, monkeypatch):
    from tests.test_document_read import Model
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    model = Model([dict(facts=[fact(module_key='wrong')], complete=True)])
    real = model.execute
    def execute(**kwargs):
        request = json.loads(kwargs['text'])
        assert 'module_options' not in request
        return real(**kwargs)
    model.execute = execute
    result = service(task, model).run(company_id='acc', job_id='job', limits=dict(classify=1, attribute=1, extract=3))
    assert result['done'] and not result['unresolved']
    with connect(task[0]) as db:
        assert db.execute('SELECT module_key FROM facts').fetchone()[0] is None


@pytest.mark.parametrize('cross', [False, 'table', 'declared'])
def test_leaf_gets_two_same_trunk_facts_cross_table_rejected(kernel, cross):
    for trunk in ('bizov', 'hr'):
        kernel.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '另一个指标', '2025-01', '9', '个', SHA, trunk, 'now'))
    kernel.db.commit()
    data = payload()
    data['fact_ids'] = [1, 2]
    if cross:
        if cross == 'declared':
            data['fact_ids'] = [3]
        data['markdown'] = data['markdown'].replace('F1', 'F3')
    else:
        data['markdown'] = data['markdown'].replace('| F1 | 测试指标 | 2025-01 | 9 | 个 | [^one] |',
            '| F1 | 测试指标 | 2025-01 | 9 | 个 | [^one] |\n| F2 | 另一个指标 | 2025-01 | 9 | 个 | [^one] |')
    worker = Worker(plan=[dict(leaves=[dict(trunk='bizov', leaf_code='L-bizov-08')])], leaves={'L-bizov-08': data})
    result = planner_service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert [f['fact_id'] for f in worker.calls[-1]['facts']] == [1, 2]
    if cross:
        assert not result['built'] and len(result['failed_leaves']) == 1
        expected = 'fact reference outside supplied trunk facts' if cross == 'declared' else 'undeclared table fact or row source'
        assert expected in result['failed_leaves'][0]['reason']
        clean(kernel)
    else:
        assert len(result['built']) == 1 and not result['failed_leaves']


def test_open_extract_to_leaf_real_ledger_projection_atomic(real_store, monkeypatch):
    # Retain this v1 module-delivery regression alongside the new U1 default.
    monkeypatch.setattr(DirectReadService, "run", DirectReadService.run_legacy)
    store = real_store
    store.db.execute('DELETE FROM facts')
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', ('job-t', COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    document = documents()[SHA]
    dossier = Dossier(document, file_hash=SHA, objects_root='.')
    dossier.quality_fingerprint = dossier.fingerprint
    actions = []
    class Model:
        def execute(self, **kwargs):
            request = json.loads(kwargs['text'])
            action = request['action']; actions.append(action)
            if action == 'classify': return dict(classification=dict(description='测试资料'))
            if action == 'attribute': return dict(ranges=[dict(entity='临时主体', start_line=1, end_line=2)])
            if action == 'extract':
                assert len(request['module_options']) == 10
                return dict(facts=[dict(entity='临时主体', attribute='测试指标', period='2025-01', value='9',
                    unit='个', value_type='number', quote='有9个', source_loc='docx:body/77/paragraph',
                    module_key='bizov')], complete=True)
            if action == 'plan': return dict(leaves=[dict(trunk='bizov', leaf_code='L-bizov-08')])
            assert action == 'leaf' and len(request['facts']) == 1
            data = payload()
            ref = request['facts'][0]['fact_id']
            data['fact_ids'] = [ref]
            data['markdown'] = data['markdown'].replace('F1', f'F{ref}')
            return data
    reader = DirectReadService(store.memory.database, config=SimpleNamespace(skill_root=ROOT/'skills'),
                               dossier=dossier, memory=store.memory, model=Model())
    result = reader.run(company_id=COMPANY, job_id='job-t', limits=dict(classify=1, attribute=1, extract=1, plan=1, leaf=1))
    assert result['done'] and len(result['leaves_built']) == 1 and not result['leaves_failed']
    assert actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    assert store.db.execute('SELECT module_key FROM facts').fetchone()[0] == 'bizov'
    for table in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links'):
        assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    uri = result['leaves_built'][0]['uri']
    assert store.memory.read(uri)
    assert store.db.execute('SELECT count(*) FROM m1_chunks WHERE uri=?', (uri,)).fetchone()[0] > 0
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0


def test_mixed_module_delivery_isolates_invalid_and_publishes_valid_leaf(open_task):
    model = LeafModel(open_task, codes=['L-bizov-07'])
    real = model.execute
    def execute(**kwargs):
        result = real(**kwargs)
        if json.loads(kwargs['text'])['action'] == 'extract':
            return dict(facts=[fact(source_loc=LOC, module_key='bizov'),
                               fact(source_loc=LOC, module_key='wrong', attribute='非法候选')], complete=True)
        return result
    model.execute = execute
    result = run(service(open_task, model), extract=1)
    assert result['done'] and result['complete']
    assert len(result['accepted']) == 1 and result['accepted'][0]['module_key'] == 'bizov'
    assert len(result['unresolved']) == 1
    assert result['unresolved'][0]['candidate']['module_key'] == 'wrong'
    assert 'invalid module_key' in result['unresolved'][0]['blocking']
    assert result['correction_feedback'] == result['unresolved']
    assert len(result['leaves_built']) == 1 and not result['leaves_failed']
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    with connect(open_task[0]) as db:
        assert [r[0] for r in db.execute('SELECT module_key FROM facts')] == ['bizov']
        for table in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    assert open_task[2].read(result['leaves_built'][0]['uri'])
