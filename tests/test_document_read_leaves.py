"""B2b2 uses the existing direct-reader task/model/service, never real models."""
from copy import deepcopy
import json

import pytest

from app import settings
from app.db.database import connect
from app.document_read.dossier import Dossier
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree import leaf_plan
from app.runtime_errors import EmployeeTransportError
from app.employees.worker import WorkerUnavailable
from tests.test_document_read import task, Model, service, fact, ROOT

LOC = 'docx:body/16/table'
CODES = ['L-bizov-07', 'L-bizov-10']
OLD_LIMITS = dict(classify=1, attribute=1, extract=3)
LIMITS = dict(**OLD_LIMITS, plan=1, leaf=2)


@pytest.fixture
def open_task(task, monkeypatch):
    # Explicit v1 compatibility suite; U1 default is covered with its real provider tests.
    from app.document_read.service import DirectReadService
    monkeypatch.setattr(DirectReadService, 'run', DirectReadService.run_legacy)
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    db, old, memory = task
    document = deepcopy(old.document)
    document['document_map']['elements'][0]['source_loc'] = LOC
    dossier = Dossier(document, file_hash=old.file_hash, objects_root=old.objects_root)
    dossier.quality_fingerprint = dossier.fingerprint
    binding = CompanyBindingStore(db)
    try:
        binding.register('acc', 'acc-open')
    finally:
        binding.close()
    return db, dossier, memory


def stored(task):
    with connect(task[0]) as conn:
        return json.loads(conn.execute("SELECT content FROM memory_kv WHERE uri='sam://kv/rt/md-direct/job'").fetchone()[0])


def leaf_payload(task, code, *, loc=LOC):
    return dict(leaf_code=code,
                markdown='## 概览\n测试量有原文证据。[^one]\n## 正文\n测试量为12.00。[^one]\n\n[^one]: acc.docx → ' + loc,
                source_files=[task[1].file_hash], fact_ids=[1] if code.startswith('L-bizov-') else [],
                note='首建')


class LeafModel(Model):
    def __init__(self, task, *, codes=None, faults=None, plan=None):
        super().__init__([dict(facts=[fact(source_loc=LOC, module_key='bizov')], complete=True)])
        self.task, self.codes = task, CODES if codes is None else codes
        self.faults, self.plan_response = dict(faults or {}), plan
        self.targets, self.requests = [], []

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        action = request['action']
        self.requests.append(request)
        # Charge and active action must be durably visible BEFORE transport.
        state = stored(self.task)
        phase = action if action in ('classify', 'attribute', 'plan', 'leaf') else 'extract'
        counted = sum(1 for a in self.actions if (a if a in ('classify', 'attribute', 'plan', 'leaf') else 'extract') == phase)
        assert state['active_action'] == action and state['calls'][phase] == counted + 1
        if action not in ('plan', 'leaf'):
            assert kwargs['skill_text'] == (ROOT/'skills/file-processor/SKILL.md').read_text()
            return super().execute(**kwargs)
        assert kwargs['skill_text'] == (ROOT/'skills/fact-leaf/SKILL.md').read_text()
        assert kwargs['timeout'] == 180
        assert state['complete'] and state['fact_ids'] == [1]
        with connect(self.task[0]) as conn:
            assert conn.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        self.actions.append(action)
        if action == 'plan':
            assert {f['source_loc'] for f in request['documents']} == {LOC}
            assert all(f['file_hash'] == self.task[1].file_hash for f in request['documents'])
            return self.plan_response if self.plan_response is not None else dict(
                leaves=[dict(trunk=c.split('-')[1], leaf_code=c) for c in self.codes])
        code = request['target']['leaf_code']
        self.targets.append(code)
        assert {f['source_loc'] for f in request['fragments']} == {LOC}
        fault = self.faults.pop(code, None)
        if isinstance(fault, Exception):
            raise fault
        if fault == 'coordinate':
            return leaf_payload(self.task, code, loc='docx:body/999/table')
        return leaf_payload(self.task, code)


def run(reader, **limits):
    return reader.run(company_id='acc', job_id='job', limits={**LIMITS, **limits})


def revisions(task):
    with connect(task[0]) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM leaf_revisions ORDER BY leaf_code')]


def test_open_complete_same_channel_and_order(open_task):
    model = LeafModel(open_task, codes=[CODES[0]])
    reader = service(open_task, model)
    result = run(reader)
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    assert result['done'] and result['leaves_done']
    assert result['calls'] == dict(classify=1, attribute=1, extract=1, plan=1, leaf=1)
    assert len(result['leaves_built']) == 1 and not result['leaves_failed']
    assert result['leaves_rollup'] == dict(rolled_up_trunks=['bizov'], tree_rolled=True)
    uri = result['leaves_built'][0]['uri']
    assert uri.startswith('sam://facts/acc/02-业务概览/leaves/')
    assert LOC in open_task[2].read(uri)
    assert 'sam://facts/acc/tree-brief.md' in open_task[2].docs
    assert 'sam://facts/acc/02-业务概览/trunk-guide.md' in open_task[2].docs
    with connect(open_task[0]) as conn:
        job = conn.execute('SELECT * FROM parse_jobs').fetchone()
        assert job['status'] == 'done' and job['l2_committed'] == 1
    assert run(reader) == result
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']


def test_leaf_budget_checkpoint_resume_without_duplicate(open_task):
    model = LeafModel(open_task)
    reader = service(open_task, model)
    result = run(reader, leaf=1)
    assert result['status'] == 'partial' and not result.get('leaves_done')
    assert not result['brief_done'] and not result['guide_done']
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    assert result['leaves_planned'] == CODES and len(result['leaves_built']) == 1
    assert stored(open_task) == result
    first = revisions(open_task)[0]
    # Reconstruct the service to prove progress lives in SQLite, not Python.
    result = run(service(open_task, model))
    assert result['done'] and result['leaves_done'] and len(result['leaves_built']) == 2
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']
    assert model.targets == CODES and model.actions.count('plan') == 1
    assert len(revisions(open_task)) == 2 and revisions(open_task)[0] == first
    assert result['calls']['leaf'] == 2 and result['calls']['plan'] == 1


@pytest.mark.parametrize('phase', ['plan', 'leaf'])
def test_zero_leaf_stage_budget_then_resume(open_task, phase):
    model = LeafModel(open_task, codes=[CODES[0]])
    result = run(service(open_task, model), **{phase: 0})
    expected = ['classify', 'attribute', 'extract'] + (['plan'] if phase == 'leaf' else [])
    assert model.actions == expected and result['status'] == 'partial'
    assert not revisions(open_task) and not result['brief_done']
    assert ('leaves_planned' in result) == (phase == 'leaf')
    assert result['calls'][phase] == 0
    result = run(service(open_task, model))
    assert result['done'] and model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']


def test_bad_leaf_is_terminal_and_does_not_block_other_trunk(open_task):
    codes = [CODES[0], 'L-hr-02']
    model = LeafModel(open_task, codes=codes, faults={CODES[0]: 'coordinate'})
    result = run(service(open_task, model))
    assert result['done'] and result['leaves_done']
    assert [b['leaf_code'] for b in result['leaves_built']] == ['L-hr-02']
    assert result['leaves_failed'][0]['leaf_code'] == CODES[0]
    assert 'coordinate unavailable' in result['leaves_failed'][0]['reason']
    assert result['leaves_rollup']['rolled_up_trunks'] == ['hr']
    assert len(revisions(open_task)) == 1
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']


def test_failed_terminal_not_retried_after_budget_partial(open_task):
    model = LeafModel(open_task, faults={CODES[0]: 'coordinate'})
    result = run(service(open_task, model), leaf=1)
    assert result['status'] == 'partial' and len(result['leaves_failed']) == 1
    assert not result['leaves_built']
    result = run(service(open_task, model))
    assert result['done'] and model.targets == CODES
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']


def test_transport_charge_partial_keeps_facts_and_first_leaf(open_task):
    model = LeafModel(open_task, faults={CODES[1]: EmployeeTransportError('synthetic leaf transport')})
    result = run(service(open_task, model))
    assert result['status'] == 'partial' and result['stage_failure']['action'] == 'leaf'
    assert result['stage_failure']['kind'] == 'employee_transport'
    assert len(result['leaves_built']) == 1 and not result['leaves_failed']
    assert result['calls']['leaf'] == 2 and not result['brief_done']
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']
    with connect(open_task[0]) as conn:
        assert conn.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        assert conn.execute('SELECT l2_committed FROM parse_jobs').fetchone()[0] == 1
    first = revisions(open_task)[0]
    result = run(service(open_task, model), leaf=3)
    assert result['done'] and 'stage_failure' not in result
    assert model.targets == [CODES[0], CODES[1], CODES[1]]
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf', 'leaf']
    assert revisions(open_task)[0] == first and len(revisions(open_task)) == 2
    assert result['calls']['leaf'] == 3


@pytest.mark.parametrize('changed', ['fingerprint', 'scope'])
def test_resume_evidence_and_scope_checked_before_leaf_calls(open_task, changed):
    model = LeafModel(open_task)
    reader = service(open_task, model)
    run(reader, leaf=1)
    before = list(model.actions)
    if changed == 'fingerprint':
        reader.dossier.document['markdown'] += '\nchanged'
        reader.dossier.quality_fingerprint = reader.dossier.fingerprint
        with pytest.raises(ValueError, match='resume evidence'):
            run(reader)
    else:
        with connect(open_task[0]) as conn:
            state = stored(open_task); state['company_id'] = 'other'
            reader._save(conn, state)
        with pytest.raises(ValueError, match='resume evidence or scope'):
            run(reader)
    assert model.actions == before


@pytest.mark.parametrize('phase', ['plan', 'leaf'])
@pytest.mark.parametrize('value', [None, -1, True, 1.0])
def test_open_requires_explicit_nonnegative_integer_new_budgets(open_task, phase, value):
    model = LeafModel(open_task)
    limits = dict(LIMITS)
    if value is None: del limits[phase]
    else: limits[phase] = value
    with pytest.raises(ValueError, match='phase limits'):
        service(open_task, model).run(company_id='acc', job_id='job', limits=limits)
    assert model.actions == []


def test_closed_state_and_old_outputs_no_leaf_stage(task, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    # Any leaf construction/call would fail this dedicated closed-path test.
    def forbidden(*args, **kwargs): raise AssertionError('closed path entered leaves')
    monkeypatch.setattr(leaf_plan.LeafPlanningService, '__init__', forbidden)
    from app.document_read.service import _LeafWorker
    monkeypatch.setattr(_LeafWorker, 'run', forbidden)
    model = Model()
    from app.memory.company_facts import l2_consolidation
    monkeypatch.setattr(l2_consolidation, '_now', lambda: '2026-10-14T00:00:00+00:00')
    reader = service(task, model)
    result = reader.run(company_id='acc', job_id='job', limits=OLD_LIMITS)
    assert result['done'] and result['calls'] == dict(classify=1, attribute=1, extract=3)
    assert model.actions == ['classify', 'attribute', 'extract', 'brief', 'guide']
    assert not any(k.startswith('leaves_') for k in result)
    assert stored(task) == result
    assert task[1].elements[0]['source_loc'] == 'table'
    assert not any('/leaves/' in uri or uri.endswith('/tree-brief.md') for uri in task[2].docs)
    with connect(task[0]) as conn:
        assert conn.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
        job = conn.execute('SELECT * FROM parse_jobs').fetchone()
        assert job['status'] == 'done' and job['l2_committed'] == 1
        state_bytes = conn.execute('SELECT content FROM memory_kv WHERE uri=?', (reader.key,)).fetchone()[0]
    docs = deepcopy(task[2].docs)
    golden = json.loads((ROOT/'tests/fixtures/document_read_closed_b2b1.json').read_text())
    assert state_bytes == golden['state_json']
    assert docs == golden['documents']
    assert reader.run(company_id='acc', job_id='job', limits=OLD_LIMITS) == result
    with connect(task[0]) as conn:
        assert conn.execute('SELECT content FROM memory_kv WHERE uri=?', (reader.key,)).fetchone()[0] == state_bytes
    assert task[2].docs == docs


def test_gate_open_resume_old_partial_state_adds_new_call_phases(open_task, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    model = LeafModel(open_task)
    reader = service(open_task, model)
    result = reader.run(company_id='acc', job_id='job', limits={**OLD_LIMITS, 'extract': 1})
    assert result['status'] == 'partial' and 'plan' not in result['calls']
    assert not any(k.startswith('leaves_') for k in result)
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    result = run(service(open_task, model))
    assert result['done'] and result['calls']['plan'] == 1 and result['calls']['leaf'] == 2
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']


def test_leaves_done_resume_is_noop_without_legacy_summaries(open_task):
    model = LeafModel(open_task)
    result = run(service(open_task, model), extract=1)
    assert result['status'] == 'done' and result['leaves_done']
    assert not result['brief_done'] and not result['guide_done']
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']
    before = revisions(open_task)
    result = run(service(open_task, model))
    assert result['done'] and revisions(open_task) == before
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']


@pytest.mark.parametrize('plan', [{'leaves': []}, {'leaves': [{'trunk': 'hr', 'leaf_code': CODES[0]}]}])
def test_empty_and_invalid_plan_checkpoint_without_leaf_calls(open_task, plan):
    model = LeafModel(open_task, plan=plan)
    result = run(service(open_task, model))
    assert result['done'] and result['leaves_done'] and result['leaves_planned'] == []
    assert result['leaves_diagnostics']['unresolved']
    assert result['leaves_built'] == [] and result['calls']['leaf'] == 0
    assert model.actions == ['classify', 'attribute', 'extract', 'plan']
    assert not revisions(open_task)
    assert not any('/leaves/' in uri for uri in open_task[2].docs)


@pytest.mark.parametrize('error', [EmployeeTransportError('plan transport'), WorkerUnavailable('plan unavailable')])
def test_plan_transport_failure_charged_and_recoverable(open_task, error):
    model = LeafModel(open_task, codes=[CODES[0]])
    original = model.execute
    def execute(**kwargs):
        request = json.loads(kwargs['text'])
        if request['action'] == 'plan':
            model.actions.append('plan')
            assert stored(open_task)['calls']['plan'] == 1
            raise error
        return original(**kwargs)
    model.execute = execute
    result = run(service(open_task, model))
    assert result['status'] == 'partial' and result['stage_failure']['action'] == 'plan'
    assert result['calls']['plan'] == 1 and 'leaves_planned' not in result
    assert result['fact_ids'] == [1] and not result['brief_done']
    model.execute = original
    result = run(service(open_task, model), plan=2)
    assert result['done'] and result['calls']['plan'] == 2
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'plan', 'leaf']


def test_rollup_failure_retries_only_rollup_without_legacy_summaries(open_task, monkeypatch):
    model = LeafModel(open_task)
    original = leaf_plan.rollup_tree
    def fail(**kwargs): raise OSError('synthetic rollup failure')
    monkeypatch.setattr(leaf_plan, 'rollup_tree', fail)
    with pytest.raises(OSError, match='synthetic rollup failure'):
        run(service(open_task, model))
    state = stored(open_task)
    assert state['status'] == 'partial' and not state.get('leaves_done')
    assert len(state['leaves_built']) == 2 and not state['brief_done']
    assert state['leaves_rollup']['rolled_up_trunks'] == ['bizov']
    assert state['leaves_rollup']['rollup_error'] == 'synthetic rollup failure'
    before = revisions(open_task)
    monkeypatch.setattr(leaf_plan, 'rollup_tree', original)
    result = run(service(open_task, model))
    assert result['done'] and revisions(open_task) == before
    assert model.targets == CODES and model.actions.count('plan') == 1
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']
