"""B2b1 deterministic fake-worker tests; temporary ledgers and grants only."""
from copy import deepcopy
import importlib
import json

import pytest

from app import settings
from app.employees.worker import EmployeeResult, WorkerUnavailable
from app.runtime_errors import EmployeeTransportError
from app.facts_tree import leaf_plan
from app.facts_tree.leaf_plan import LeafPlanningService
from app.facts_tree.leaf_store import LeafRejected
from tests.test_facts_tree_leaves import kernel, COMPANY, SHA, OTHER, clean
from tests.test_facts_tree_build import documents, payload, LOC


class Worker:
    def __init__(self, plan=None, leaves=None, error=None, stage='plan'):
        self.plan = [{'leaves': [{'trunk': 'bizov', 'leaf_code': 'L-bizov-08'},
                                {'trunk': 'bizov', 'leaf_code': 'L-bizov-07'}]}] if plan is None else plan
        narrative = payload('L-bizov-07', '纳米级涂膜技术，提升产品精度。[^one]')
        narrative['fact_ids'] = []
        self.leaves = leaves or {'L-bizov-08': payload(), 'L-bizov-07': narrative}
        self.calls, self.error, self.stage = [], error, stage

    def run(self, *, skill, text, company_id):
        request = json.loads(text)
        assert skill == 'fact-leaf' and request['company_id'] == company_id
        self.calls.append(request)
        if self.error and request['action'] == self.stage:
            raise self.error
        if request['action'] == 'plan':
            return EmployeeResult(deepcopy(self.plan))
        return EmployeeResult([deepcopy(self.leaves[request['target']['leaf_code']])])


def service(kernel, worker, granted=None):
    return LeafPlanningService(kernel.db.execute('PRAGMA database_list').fetchone()['file'],
                               kernel.memory, worker=worker,
                               documents=documents() if granted is None else granted)


def test_multi_leaf_inputs_rollups_and_noop(kernel, monkeypatch):
    worker = Worker()
    calls = []
    for name in ('rollup_trunk', 'rollup_tree'):
        original = getattr(leaf_plan, name)
        def wrapped(_name=name, _original=original, **kwargs):
            calls.append((_name, kwargs.get('trunk')))
            return _original(**kwargs)
        monkeypatch.setattr(leaf_plan, name, wrapped)
    planner = service(kernel, worker)
    result = planner.plan_and_build(company_id=COMPANY, job_id='job-plan')
    assert len(result['built']) == 2 and not result['failed_leaves']
    assert result['tree_rolled'] and result['rolled_up_trunks'] == ['bizov']
    assert calls == [('rollup_trunk', 'bizov'), ('rollup_tree', None)]
    for built in result['built']:
        assert built['uri'].startswith(f'sam://facts/{COMPANY}/02-业务概览/leaves/')
    narrative = kernel.memory.read(result['built'][1]['uri'])
    assert '纳米级涂膜技术' in narrative and '## 数据明细' not in narrative
    plan, leaf = worker.calls[:2]
    assert len(plan['template']) == 98 and 'scope' not in plan['template'][0]
    assert {f['source_loc'] for f in plan['documents']} == {LOC, 'docx:body/16/table'}
    assert all(f['file_hash'] == SHA for f in plan['documents'])
    assert leaf['facts'][0]['fact_id'] == 1
    assert leaf['facts'][0]['source'] == dict(file_hash=SHA, source_page=None, source_span=None)
    assert 'source_loc' not in leaf['facts'][0]
    writes = kernel.memory.writes
    again = planner.plan_and_build(company_id=COMPANY)
    assert len(again['noop']) == 2 and all(not b['changed'] for b in again['built'])
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 1
    assert kernel.memory.writes == writes


def test_invalid_and_duplicate_plans_conflicts(kernel):
    items = [{'trunk': 'bizov', 'leaf_code': 'L-bizov-07'}] * 2 + [
        {'trunk': 'hr', 'leaf_code': 'L-bizov-08'}, {'trunk': 'bizov', 'leaf_code': 'L-bizov-99'},
        {'trunk': 'bizov', 'leaf_code': []}, None, {'leaf_code': 'L-bizov-08'}]
    conflict = dict(trunk='bizov', leaf_code='L-bizov-07', metric='测试', reason='不一致')
    result = service(kernel, Worker(plan=[dict(leaves=items, conflicts=[conflict])])).plan_and_build(company_id=COMPANY)
    assert result['planned'] == ['L-bizov-07'] and len(result['built']) == 1
    assert len(result['rejected_plans']) == 5 and result['conflicts'] == [conflict]
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


@pytest.mark.parametrize('bad', ['coordinate', 'number', 'trunk', 'outside', 'shape', 'empty'])
def test_bad_leaf_preserves_success(kernel, bad):
    worker = Worker()
    data = worker.leaves['L-bizov-07']
    if bad == 'coordinate': data['markdown'] = data['markdown'].replace(LOC, 'missing')
    elif bad == 'number': data['markdown'] = data['markdown'].replace('纳米级涂膜技术', '999个')
    elif bad == 'trunk':
        kernel.db.execute("INSERT INTO facts(company_id,entity,attribute,value,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?)", (COMPANY, '主体', '属性', '9', SHA, 'hr', 'now'))
        kernel.db.commit(); data['fact_ids'] = [2]
    elif bad == 'outside': data['source_files'] = [OTHER]
    elif bad == 'shape': del data['markdown']
    elif bad == 'empty':
        original = worker.run
        worker.run = lambda **kw: EmployeeResult([]) if json.loads(kw['text']).get('target', {}).get('leaf_code') == 'L-bizov-07' else original(**kw)
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert len(result['built']) == 1 and result['built'][0]['leaf_code'] == 'L-bizov-08'
    assert len(result['failed_leaves']) == 1 and result['tree_rolled']
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


@pytest.mark.parametrize('error', [WorkerUnavailable('offline'), EmployeeTransportError('offline')])
@pytest.mark.parametrize('stage', ['plan', 'leaf'])
def test_transport_escapes(kernel, error, stage):
    with pytest.raises(type(error), match='offline'):
        service(kernel, Worker(error=error, stage=stage)).plan_and_build(company_id=COMPANY)
    clean(kernel)


@pytest.mark.parametrize('plan', [[], [{'leaves': []}], [{'leaves': 'bad'}]])
def test_empty_or_invalid_plan_unresolved_no_files(kernel, plan):
    worker = Worker(plan=plan)
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert not result['built'] and result['unresolved'] and not result['tree_rolled']
    assert len(worker.calls) == 1
    clean(kernel)


def test_unbound_company(kernel):
    worker = Worker()
    with pytest.raises(LeafRejected, match='binding'):
        service(kernel, worker).plan_and_build(company_id='acc-unbound')
    assert not worker.calls
    clean(kernel)


def test_disabled_constructor_import_call_zero_writes(kernel, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    before = '\n'.join(kernel.db.iterdump())
    files = {p: p.read_bytes() for p in kernel.memory.root.iterdir()}
    writes = kernel.memory.writes
    importlib.reload(leaf_plan)
    worker = Worker()
    planner = service(kernel, worker)
    with pytest.raises(RuntimeError, match='disabled'):
        planner.plan_and_build(company_id=COMPANY)
    assert '\n'.join(kernel.db.iterdump()) == before
    assert {p: p.read_bytes() for p in kernel.memory.root.iterdir()} == files
    assert kernel.memory.writes == writes and not worker.calls


def test_rollup_failure_exposes_partial_result(kernel, monkeypatch):
    def fail(**kwargs): raise OSError('rollup failure')
    monkeypatch.setattr(leaf_plan, 'rollup_tree', fail)
    with pytest.raises(OSError, match='rollup failure') as caught:
        service(kernel, Worker()).plan_and_build(company_id=COMPANY)
    result = caught.value.planning_result
    assert len(result['built']) == 2 and result['rolled_up_trunks'] == ['bizov']
    assert result['rollup_error'] == 'rollup failure' and not result['tree_rolled']
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2


def test_two_trunks_only_selected_and_current_facts(kernel):
    kernel.db.execute("UPDATE facts SET source_page=7, source_span='span'")
    for company, trunk, status, valid_to, superseded in [
        (COMPANY, 'hr', 'verified', None, None),
        (COMPANY, 'hr', 'claimed', None, None),
        (COMPANY, 'hr', 'verified', 'past', None),
        (COMPANY, 'hr', 'verified', None, 1),
        ('acc-other', 'hr', 'verified', None, None),
    ]:
        kernel.db.execute('''INSERT INTO facts(company_id,entity,attribute,value,source_file,
            module_key,status,valid_to,superseded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (company, '主体', '属性', '9', SHA, trunk, status, valid_to, superseded, 'now'))
    kernel.db.commit()
    hr = payload('L-hr-02'); hr['fact_ids'] = [2]
    worker = Worker(plan=[{'leaves': [{'trunk': 'bizov', 'leaf_code': 'L-bizov-07'},
                                    {'trunk': 'hr', 'leaf_code': 'L-hr-02'}]}])
    worker.leaves['L-hr-02'] = hr
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert result['rolled_up_trunks'] == ['bizov', 'hr'] and result['tree_rolled']
    assert len(result['built']) == 2
    assert '/03-人员管理/leaves/' in result['built'][1]['uri']
    assert [f['fact_id'] for f in worker.calls[1]['facts']] == [1]
    assert [f['fact_id'] for f in worker.calls[2]['facts']] == [2]
    assert worker.calls[1]['facts'][0]['source']['source_page'] == 7
    assert worker.calls[1]['facts'][0]['source']['source_span'] == 'span'


def test_known_company_file_not_granted_cannot_be_read_or_cited(kernel):
    kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                      (OTHER, 'secret.docx', 1, '/unused', 'now'))
    kernel.db.execute('INSERT INTO facts(company_id,entity,attribute,value,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?)',
                      (COMPANY, '主体', '属性', '9', OTHER, 'bizov', 'now'))
    kernel.db.commit()
    worker = Worker()
    data = worker.leaves['L-bizov-07']
    data['source_files'] = [OTHER]
    data['markdown'] = data['markdown'].replace('sample.docx', 'secret.docx')
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert len(result['failed_leaves']) == 1 and len(result['built']) == 1
    for request in worker.calls:
        assert all(f['file_hash'] == SHA for f in request.get('documents', request.get('fragments', [])))
    assert 'outside task evidence' in result['failed_leaves'][0]['reason']


def test_grants_snapshot_multiple_files_and_dossier_read(kernel, monkeypatch):
    kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                      (OTHER, 'second.docx', 1, '/unused', 'now'))
    kernel.db.execute('INSERT INTO facts(company_id,entity,attribute,value,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?)',
                      (COMPANY, '主体', '属性', '9', OTHER, 'bizov', 'now'))
    kernel.db.commit()
    granted = documents()
    granted[OTHER] = deepcopy(granted[SHA])
    worker = Worker(plan=[{'leaves': []}])
    planner = service(kernel, worker, granted)
    granted.clear()
    reads = []
    original = leaf_plan.Dossier.read
    def read(self, loc):
        reads.append((self.file_hash, loc))
        return original(self, loc)
    monkeypatch.setattr(leaf_plan.Dossier, 'read', read)
    planner.plan_and_build(company_id=COMPANY)
    fragments = worker.calls[0]['documents']
    assert len(reads) == len(fragments) == 4
    assert {f['original_name'] for f in fragments} == {'sample.docx', 'second.docx'}
    assert all(f['md'] == original(leaf_plan.Dossier(planner.documents[f['file_hash']],
               file_hash=f['file_hash'], objects_root='.'), f['source_loc']) for f in fragments)


def test_transport_after_first_publication_preserves_it(kernel):
    worker = Worker()
    original = worker.run
    def run(**kwargs):
        if json.loads(kwargs['text']).get('target', {}).get('leaf_code') == 'L-bizov-07':
            raise WorkerUnavailable('second leaf offline')
        return original(**kwargs)
    worker.run = run
    with pytest.raises(WorkerUnavailable, match='second leaf offline'):
        service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 1
    uri = kernel.db.execute('SELECT uri FROM facts_tree_leaf_index').fetchone()[0]
    assert kernel.memory.read(uri)


def test_all_failed_no_rollup(kernel):
    worker = Worker()
    for data in worker.leaves.values():
        data['markdown'] = data['markdown'].replace(LOC, 'missing')
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert len(result['failed_leaves']) == 2 and not result['built']
    assert not result['tree_rolled'] and not result['rolled_up_trunks']
    clean(kernel)
