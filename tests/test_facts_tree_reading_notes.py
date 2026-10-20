"""Protocol V4 V1: read the whole mapped MD into structured notes, zero leaf writes."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app import settings
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.document_read.contracts import response_contract_v4
from app.document_read.notes import NoteStore, NoteRejected
from app.facts_tree.md_reader import MDWindows
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT

LOC0, LOC1 = 'docx:body/1/paragraph', 'docx:body/2/table'
JOB = 'v4-job'
ZERO_TABLES = ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links',
               'md_leaf_bodies', 'md_leaf_operations', 'md_leaf_todos', 'md_leaf_notifications')


def formal_table_count(db, name):
    # Lazy gate tables (e.g. md_leaf_*) are created only when the publisher runs.
    # The reading phase must not create them: an absent table counts as zero rows,
    # while an existing table is really queried and must still be empty.
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] if exists else 0


def dossier():
    doc = dict(markdown='技术描述可回查，有9个。\n| 项目 | 值 |\n| --- | --- |\n| 参数 | 9 |',
               document_map=dict(elements=[dict(source_loc=LOC0, start_line=1, end_line=1),
                   dict(source_loc=LOC1, start_line=2, end_line=4, kind='table')]), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


@pytest.fixture
def v4_job(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL', 'v4')
    real_store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (JOB, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    real_store.db.commit()
    return real_store


def fact(window):
    if window == '0':
        return dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='个',
                    value_type='number', quote='有9个', source_loc=LOC0, attribution_index=0, module_key=None)
    return dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='',
                value_type='number', quote='| 参数 | 9 |', source_loc=LOC1, attribution_index=0, module_key=None)


class NotesModel:
    """Scripted V4 employee; records every request and the skill text used."""
    def __init__(self, plans=None):
        self.requests, self.skills, self.plan_calls = [], [], 0
        self.plans = plans or {}

    def execute(self, *, skill_text, text, timeout=180):
        request = json.loads(text)
        action = request['action']
        self.requests.append(deepcopy(request))
        self.skills.append(skill_text)
        grant = request.get('window', {})
        wid = grant.get('grant_id')
        if action == 'classify':
            return dict(classification=dict(description='独立资料'))
        if action == 'attribute':
            line = 1 if wid == '0' else 4
            return dict(ranges=[dict(entity='临时主体', start_line=1 if wid == '0' else 2, end_line=line)])
        if action == 'extract':
            return dict(facts=[fact(wid)], complete=True, remaining_source_locs=[])
        assert action == 'plan'
        assert request['protocol'] == 'mapped-md-v4' and request['operation'] == 'note_receipt'
        self.plan_calls += 1
        if wid in self.plans:
            return deepcopy(self.plans[wid])
        note_id = 'n-0001' if wid == '0' else 'n-0002'
        return dict(outcome='noted',
            creates=[dict(note_id=note_id, candidate_index=0, status='verified',
                          topic_hint='员工主题提示不是叶子', relations=[], open_questions=[])],
            revisions=[], retain_context_windows=[],
            retention_reason='离线员工显式判断保留或释放原文限定')


def reader(store, model):
    return DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=dossier(),
        memory=store.memory, model=model)


LIMITS = dict.fromkeys(('classify', 'attribute', 'extract', 'plan', 'leaf'), 8)


def test_full_read_notebook_complete_and_zero_leaf_or_fact_writes(v4_job):
    store = v4_job
    model = NotesModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete']
    # Stage A alone is never publishable: the gate belongs to the review stage.
    assert not progress.get('review_complete') and not progress['awaiting_publication']
    assert progress['window_count'] == 2 and progress['verified_notes'] == 2
    assert progress['unresolved_readings'] == 0 and progress['technical_rejections'] == 0
    # Reading phase never touches the L2 ledger, links, leaves or L1 consumption.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1  # pre-existing seed only
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0
    job = store.db.execute('SELECT status,l2_committed FROM parse_jobs WHERE job_id=?', (JOB,)).fetchone()
    assert job['status'] == 'done' and job['l2_committed'] == 0
    # Notebook rows carry auditable evidence, never a leaf decision.
    assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_read_note_revisions').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_read_window_receipts').fetchone()[0] == 2
    payloads = {r['note_id']: json.loads(r['payload']) for r in
                store.db.execute('SELECT note_id,payload FROM md_read_notes').fetchall()}
    for payload in payloads.values():
        assert 'target_leaf_code' not in payload and 'leaf_code' not in payload
        primary = payload['evidence']['primary']
        assert primary['quote'] and primary['grant_id'] in ('0', '1')
        assert primary['mapped_fingerprint']
    assert payloads['n-0001']['evidence']['primary']['source_loc'] == LOC0
    extract_requests = [r for r in model.requests if r['action'] == 'extract']
    assert extract_requests and all('module_options' not in r for r in extract_requests)
    contract = response_contract_v4('extract')
    assert 'module_key' not in contract['fact_required']
    assert 'non-null module_key' in contract['forbidden']
    assert all('module_key' not in candidate for record in progress['records']
               for candidate in record.get('preparation', {}).get('accepted', []))
    # Later window saw the earlier notebook entry (without its original text).
    plans = [r for r in model.requests if r['action'] == 'plan']
    assert [p['window']['grant_id'] for p in plans] == ['0', '1']
    assert plans[0]['existing_notes'] == []
    assert plans[1]['existing_notes'][0]['note_id'] == 'n-0001'
    assert all('template' not in p and 'current' not in p and 'grants' not in p for p in plans)
    # Future window text is never shipped early; the notebook view carries no
    # original quotes (recent rolling context is the only prior-text channel).
    assert '| 参数 | 9 |' not in json.dumps(plans[0], ensure_ascii=False)
    assert '技术描述可回查' not in json.dumps(plans[1]['existing_notes'], ensure_ascii=False)
    assert '有9个' not in json.dumps(plans[1]['existing_notes'], ensure_ascii=False)
    assert all('quote' not in n['fact'] for n in plans[1]['existing_notes'])
    assert plans[1]['candidates'][0]['quote'] == '| 参数 | 9 |'
    # V4 uses the separate reading skill; closed-path shared skill bytes untouched.
    assert all('note_receipt' in skill for skill in model.skills)
    shared = (ROOT / 'skills/file-processor/SKILL.md').read_text()
    assert model.skills[0] != shared and 'mapped-md-v4' in model.skills[-1]
    # Completed notebook replays with zero new charged requests.
    before = len(model.requests)
    again = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert again['done'] and len(model.requests) == before


def test_v4_rejects_employee_trunk_choice_before_publish(v4_job):
    service = reader(v4_job, NotesModel())
    item = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9',
                unit='个', quote='有9个', source_loc=LOC0, module_key='finance')
    with pytest.raises(ValueError, match='V4 extract cannot select module_key'):
        service._verify(item, dict(protocol=4, company_id=COMPANY))


def test_budget_interrupt_resume_without_duplicate_notes(v4_job):
    store = v4_job
    model = NotesModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB,
        limits=dict.fromkeys(LIMITS, 1), note_review=False)
    assert result['status'] == 'partial' and not result.get('done')
    assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM md_read_window_receipts').fetchone()[0] == 1
    partial_actions = model.requests
    assert partial_actions[-1]['action'] in ('classify', 'extract', 'plan')
    model2 = NotesModel()
    result = reader(store, model2).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done'] and result['mapped_notes']['verified_notes'] == 2
    assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_read_window_receipts').fetchone()[0] == 2
    # Window 0 preparation/notes are not re-billed.
    assert model2.requests[0]['window']['grant_id'] == '1'
    assert [r['action'] for r in model2.requests] == ['classify', 'attribute', 'extract', 'plan']


@pytest.mark.parametrize('bad', ['missing-note', 'leaf-field'])
def test_bad_note_receipt_rejects_with_zero_side_effects(v4_job, bad):
    store = v4_job
    bad_first = dict(outcome='noted', revisions=[], retain_context_windows=[],
        retention_reason='员工理由')
    if bad == 'missing-note':
        bad_first['creates'] = [dict(note_id='n-bad', candidate_index=0, status='verified',
            relations=[dict(rel='supports', to_note_id='n-ghost', reason='员工关联尚不存在的笔记')])]
    else:
        bad_first['creates'] = [dict(note_id='n-bad', candidate_index=0, status='verified',
            target_leaf_code='L-bizov-07')]
    plans = {'0': bad_first}
    model = NotesModel(plans=plans)
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done']  # coverage gate derives from window receipts, not employee claims
    progress = result['mapped_notes']
    assert progress['unresolved_readings'] == 1 and progress['technical_rejections'] == 1
    assert progress['records'][0]['status'] == 'unresolved'
    assert progress['records'][1]['status'] == 'noted'
    # Failed window transaction left nothing behind; later window is intact.
    rows = {r['note_id'] for r in store.db.execute('SELECT note_id FROM md_read_notes').fetchall()}
    assert rows == {'n-0002'}
    assert store.db.execute('SELECT count(*) FROM md_read_window_receipts').fetchone()[0] == 1
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table


def test_one_format_correction_then_noted(v4_job):
    store = v4_job

    class CorrectingModel(NotesModel):
        def execute(self, **kwargs):
            request = json.loads(kwargs['text'])
            if request.get('action') == 'plan' and request['window']['grant_id'] == '0' \
                    and self.plan_calls == 0 and 'format_feedback' not in request:
                self.requests.append(deepcopy(request))
                self.skills.append(kwargs['skill_text'])
                return dict(outcome='noted', creates=[], revisions=[])  # missing required fields
            return super().execute(**kwargs)

    model = CorrectingModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done'] and result['mapped_notes']['verified_notes'] == 2
    record = result['mapped_notes']['records'][0]
    assert record['note_format_attempts'] == 1
    assert len(record['note_rejected_receipts']) == 1
    plan_requests = [r for r in model.requests if r.get('action') == 'plan' and r['window']['grant_id'] == '0']
    assert len(plan_requests) == 2 and 'format_feedback' in plan_requests[1]
    assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 2


def test_unresolved_window_notes_are_needs_check_not_verified(v4_job):
    store = v4_job
    plans = {'0': dict(outcome='unresolved',
        creates=[dict(note_id='n-0001', candidate_index=0,
                      open_questions=['期间适用性待后窗确认'])],
        revisions=[], reason='员工无法确认期间限定',
        retain_context_windows=[], retention_reason='保留当前窗待后窗核对')}
    model = NotesModel(plans=plans)
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    progress = result['mapped_notes']
    assert progress['unresolved_readings'] == 1 and progress['open_notes'] == 1
    status = store.db.execute("SELECT status FROM md_read_notes WHERE note_id='n-0001'").fetchone()[0]
    assert status == 'needs_check'


def test_note_revision_is_append_only_and_relates(v4_job):
    store = v4_job
    plans = {'1': dict(outcome='noted',
        creates=[dict(note_id='n-0002', candidate_index=0, status='verified',
            relations=[dict(rel='same_metric_diff_period', to_note_id='n-0001', reason='员工判断不同期间同指标')])],
        revisions=[dict(note_id='n-0001', action='question',
                        open_questions=['员工要求后窗复核口径'], reason='补充待核实问题')],
        retain_context_windows=[], retention_reason='无需保留')}
    model = NotesModel(plans=plans)
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done']
    revisions = store.db.execute(
        "SELECT note_id,rev,action FROM md_read_note_revisions WHERE note_id='n-0001' ORDER BY rev").fetchall()
    assert [(r['note_id'], r['rev'], r['action']) for r in revisions] == [('n-0001', 1, 'create'), ('n-0001', 2, 'question')]
    payload = json.loads(store.db.execute("SELECT payload,rev FROM md_read_notes WHERE note_id='n-0001'").fetchone()['payload'])
    assert payload['open_questions'] == ['员工要求后窗复核口径']
    with pytest.raises(Exception, match='append-only'):
        store.db.execute('UPDATE md_read_note_revisions SET action=? WHERE rev=1', ('x',))
    store.db.rollback()


def test_window_receipt_idempotent_but_request_key_change_rejected(v4_job):
    store = v4_job
    d = dossier()
    window = MDWindows(d).windows[0]
    grant = MDWindows(d).grant(window, 'sample.docx')
    grant['company_id'] = COMPANY
    candidate = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='个',
        value_type='number', quote='有9个', source_loc=LOC0, start_line=1, end_line=1,
        grant_id='0', file_hash=SHA, mapped_fingerprint=grant['mapped_fingerprint'],
        original_name='sample.docx', field_evidence=[])
    notes = NoteStore(store.memory.database, company_id=COMPANY, job_id='unit-job',
        file_hash=SHA, mapped_fingerprint=d.fingerprint)
    kwargs = dict(window_id='0', outcome='noted',
        creates=[dict(note_id='n-x', candidate_index=0)], revisions=[],
        grants={'0': grant}, candidates=[candidate], request_sha='sha-one')
    first = notes.apply_window(**kwargs)
    second = notes.apply_window(**kwargs)
    assert first == second
    assert notes.note_count() == 1
    with pytest.raises(NoteRejected, match='another request'):
        notes.apply_window(**{**kwargs, 'request_sha': 'sha-two'})
    assert notes.note_count() == 1
    notes.close()


def test_v4_checkpoint_cannot_be_downgraded_without_explicit_selector(v4_job, monkeypatch):
    store = v4_job
    model = NotesModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB,
        limits=dict.fromkeys(LIMITS, 1), note_review=False)
    assert result['status'] == 'partial'
    monkeypatch.delenv('SAM_MD_READ_PROTOCOL')
    with pytest.raises(ValueError, match='V4 notes checkpoint requires explicit'):
        reader(store, NotesModel()).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)


def test_v4_partial_checkpoint_cannot_enter_legacy_flow(v4_job):
    store = v4_job
    result = reader(store, NotesModel()).run(company_id=COMPANY, job_id=JOB,
        limits=dict.fromkeys(LIMITS, 1), note_review=False)
    assert result['status'] == 'partial' and not result.get('done')
    # run_legacy hard-refuses the V4 checkpoint before any charged legacy call.
    legacy_model = NotesModel()
    with pytest.raises(ValueError, match='V4 notes checkpoint'):
        reader(store, legacy_model).run_legacy(company_id=COMPANY, job_id=JOB, limits=LIMITS)
    assert legacy_model.requests == []
    # No formal ledger/leaf writes leaked through the legacy entry.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1  # pre-existing seed only
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table
    job = store.db.execute('SELECT status,l2_committed FROM parse_jobs WHERE job_id=?', (JOB,)).fetchone()
    assert job['l2_committed'] == 0
    kv = json.loads(store.db.execute("SELECT content FROM memory_kv WHERE uri=?",
        ('sam://kv/rt/md-direct/' + JOB,)).fetchone()[0])
    assert kv['mapped_notes']['reading_complete'] is False
    # The refused legacy attempt did not poison the checkpoint: V4 resume completes.
    resumed = reader(store, NotesModel()).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert resumed['done'] and resumed['mapped_notes']['verified_notes'] == 2


@pytest.mark.parametrize('protocol_key,match', [
    ('mapped_notes', 'V4 notes checkpoint'),
    ('mapped_md', 'V3 mapped-MD checkpoint'),
])
def test_mapped_checkpoint_presence_alone_blocks_legacy(v4_job, protocol_key, match):
    # Even a null/malformed protocol value blocks the legacy entry: key presence,
    # not usable content, is the isolation criterion.
    store = v4_job
    state = dict(company_id=COMPANY, fingerprint=dossier().fingerprint, calls=dict.fromkeys(LIMITS, 0),
        complete=False, done=False)
    state[protocol_key] = None
    store.db.execute('INSERT OR REPLACE INTO memory_kv(uri,content,metadata) VALUES(?,?,?)',
        ('sam://kv/rt/md-direct/' + JOB, json.dumps(state, ensure_ascii=False), '{}'))
    store.db.commit()
    legacy_model = NotesModel()
    with pytest.raises(ValueError, match=match):
        reader(store, legacy_model).run_legacy(company_id=COMPANY, job_id=JOB, limits=LIMITS)
    assert legacy_model.requests == []


QUALIFIED_MARKDOWN = '统计日期2024年。参数值为9。\n| 项目 | 值 |\n| --- | --- |\n| 参数 | 9 |'


def qualified_dossier():
    doc = dict(markdown=QUALIFIED_MARKDOWN,
               document_map=dict(elements=[dict(source_loc=LOC0, start_line=1, end_line=1),
                   dict(source_loc=LOC1, start_line=2, end_line=4, kind='table')]), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def period_proof(d, *, grant_id='0', source_loc=LOC0, quote='统计日期2024年'):
    # Employee-input proof shape (absolute lines); reading normalizes it to ranges.
    return dict(field='period', grant_id=grant_id, company_id=COMPANY, file_hash=SHA,
                mapped_fingerprint=d.fingerprint, source_loc=source_loc,
                start_line=1, end_line=1, quote=quote)


class QualifiedNotesModel(NotesModel):
    """Two windows whose period lives only in window 0; window 1 must reuse it
    through a cross-window field witness (the reading-failure-01 scenario)."""
    def __init__(self, d):
        super().__init__()
        self.d = d

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'extract':
            wid = request['window']['grant_id']
            if wid == '0':
                item = dict(entity='临时主体', attribute='参数值', period='2024年', value='9', unit='',
                    value_type='number', quote='值为9', source_loc=LOC0, attribution_index=0,
                    module_key=None, field_evidence=[period_proof(self.d)])
            else:
                item = dict(entity='临时主体', attribute='表格参数', period='2024年', value='9', unit='',
                    value_type='number', quote='| 参数 | 9 |', source_loc=LOC1, attribution_index=0,
                    module_key=None, field_evidence=[period_proof(self.d)])
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            return dict(facts=[item], complete=True, remaining_source_locs=[])
        return super().execute(**kwargs)


def test_cross_window_field_evidence_is_preserved_in_notes(v4_job):
    store = v4_job
    d = qualified_dossier()
    service = DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=store.memory, model=QualifiedNotesModel(d))
    result = service.run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done'] and result['mapped_notes']['verified_notes'] == 2
    payloads = {r['note_id']: json.loads(r['payload']) for r in
                store.db.execute('SELECT note_id,payload FROM md_read_notes').fetchall()}
    for note_id, attribute in (('n-0001', '参数值'), ('n-0002', '表格参数')):
        payload = payloads[note_id]
        assert payload['fact']['attribute'] == attribute
        assert payload['fact']['period'] == '2024年'
        proofs = payload['evidence']['field_evidence']
        assert proofs == [dict(field='period', company_id=COMPANY, grant_id='0', file_hash=SHA,
            mapped_fingerprint=d.fingerprint, original_name='sample.docx', source_loc=LOC0,
            ranges=[[1, 1]], quote='统计日期2024年')]
    # Qualifier preservation still writes nothing formal during reading.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1  # seed only
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table
    assert store.db.execute('SELECT l2_committed FROM parse_jobs WHERE job_id=?', (JOB,)).fetchone()[0] == 0


def _normalized_period_proof(d, *, mutation=None):
    proof = dict(field='period', company_id=COMPANY, grant_id='0', file_hash=SHA,
        mapped_fingerprint=d.fingerprint, original_name='sample.docx', source_loc=LOC0,
        ranges=[[1, 1]], quote='统计日期2024年')
    if mutation == 'quote':
        proof['quote'] = '统计日期2030年'
    elif mutation == 'fingerprint':
        proof['mapped_fingerprint'] = 'b' * 64
    elif mutation == 'range':
        proof['ranges'] = [[1]]
    elif mutation == 'grant':
        proof['grant_id'] = '9'
    return proof


@pytest.mark.parametrize('mutation,accepted', [
    (None, True),
    ('missing', False),
    ('quote', False),
    ('fingerprint', False),
    ('range', False),
    ('grant', False),
])
def test_note_store_reverifies_normalized_field_proofs(v4_job, mutation, accepted):
    store = v4_job
    d = qualified_dossier()
    grant = MDWindows(d).grant(MDWindows(d).windows[0], 'sample.docx')
    grant['company_id'] = COMPANY
    candidate = dict(entity='临时主体', attribute='参数值', period='2024年', value='9', unit='',
        value_type='number', quote='值为9', source_loc=LOC0, start_line=1, end_line=1,
        grant_id='0', file_hash=SHA, mapped_fingerprint=d.fingerprint,
        original_name='sample.docx',
        field_evidence=[] if mutation == 'missing' else [_normalized_period_proof(d, mutation=mutation)])
    notes = NoteStore(store.memory.database, company_id=COMPANY, job_id='proof-job',
        file_hash=SHA, mapped_fingerprint=d.fingerprint)
    try:
        if accepted:
            receipt = notes.apply_window(window_id='0', outcome='noted',
                creates=[dict(note_id='n-x', candidate_index=0)], revisions=[],
                grants={'0': grant}, candidates=[candidate], request_sha='proof-ok')
            assert receipt['created'] == ['n-x']
            row = store.db.execute("SELECT payload FROM md_read_notes WHERE note_id='n-x'").fetchone()
            payload = json.loads(row['payload'])
            assert payload['evidence']['field_evidence'][0]['ranges'] == [[1, 1]]
        else:
            with pytest.raises(NoteRejected):
                notes.apply_window(window_id='0', outcome='noted',
                    creates=[dict(note_id='n-x', candidate_index=0)], revisions=[],
                    grants={'0': grant}, candidates=[candidate], request_sha='proof-bad')
            assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 0
    finally:
        notes.close()


# ---------------------------------------------------------------------------
# V4 stage A: visible notes/index, bounded request_context revisit tool,
# bounded extract no-progress stops, and rejected-note discard counting.
# ---------------------------------------------------------------------------

P02_LINES = ['截至2022年12月31日。', '本段无事实内容。', '截至2023年12月31日。', '更多说明文字仅供阅读。',
             '| 项目 | 金额 |', '| --- | --- |', '| 资本 | 100元 |',
             '| 项目 | 金额 |', '| --- | --- |', '| 资本 | 200元 |']
P02_LOCS = [f'docx:body/p{i}/paragraph' for i in range(4)] + [f'docx:body/t{i}/table' for i in range(2)]
P02_RANGES = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 7), (8, 10)]
CHECKPOINT_URI = 'sam://kv/rt/md-direct/' + JOB
# Tool receipts add charged same-phase retries (up to 3 extracts/window); the
# baseline per-phase limit of 8 is exhausted by the p02 scenarios (up to 9).
P02_LIMITS = dict(LIMITS, extract=16)


def p02_dossier():
    elements = [dict(source_loc=loc, start_line=a, end_line=b,
                     kind='table' if loc.startswith('docx:body/t') else 'paragraph')
                for loc, (a, b) in zip(P02_LOCS, P02_RANGES)]
    doc = dict(markdown='\n'.join(P02_LINES), document_map=dict(elements=elements), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def p02_service(store, d, model):
    return DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=store.memory, model=model)


def period_witness(d, grant_id, loc, line, quote):
    return dict(field='period', grant_id=str(grant_id), company_id=COMPANY, file_hash=SHA,
                mapped_fingerprint=d.fingerprint, source_loc=loc, start_line=line, end_line=line, quote=quote)


def table_fact(d, loc, *, value, witness, period):
    return dict(entity='主体', attribute='资本', period=period, value=value, unit='元',
                value_type='number', quote=f'| 资本 | {value}元 |', source_loc=loc,
                attribution_index=0, module_key=None, field_evidence=[witness])


def no_content_plan():
    return dict(outcome='no_content', creates=[], revisions=[], reason='本窗无原子事实',
                retain_context_windows=[], retention_reason='员工显式释放前文限定')


def load_checkpoint(store):
    return json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?',
                                       (CHECKPOINT_URI,)).fetchone()[0])


def save_checkpoint(store, state):
    store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',
                     (json.dumps(state, ensure_ascii=False), CHECKPOINT_URI))
    store.db.commit()


class Partial02Model:
    """Shrunk reading-failure partial-02: date literals were released with the
    rolling window; the table windows must use request_context to revisit the
    earlier qualifier grants before they can verify period evidence."""

    def __init__(self, d):
        self.d = d
        self.requests, self.skills = [], []

    def tool_attempt(self, wid, request):
        return None

    def handle_extract(self, wid, request):
        if wid in ('0', '1', '2', '3'):
            return dict(facts=[], complete=True, remaining_source_locs=[])
        override = self.tool_attempt(wid, request)
        if override is not None:
            return override
        if not request.get('context_tool_results'):
            wanted = '0' if wid == '4' else '2'
            return dict(outcome='request_context', request_context_windows=[wanted],
                        reason=f'本窗表格未写截止日期，申请回看窗口{wanted}的日期字面限定')
        if wid == '4':
            witness = period_witness(self.d, 0, P02_LOCS[0], 1, '截至2022年12月31日')
            return dict(facts=[table_fact(self.d, P02_LOCS[4], value='100',
                        witness=witness, period='2022年12月31日')],
                        complete=True, remaining_source_locs=[])
        witness = period_witness(self.d, 2, P02_LOCS[2], 3, '截至2023年12月31日')
        return dict(facts=[table_fact(self.d, P02_LOCS[5], value='200',
                    witness=witness, period='2023年12月31日')],
                    complete=True, remaining_source_locs=[])

    def plan_for(self, wid):
        if wid == '4':
            return dict(outcome='noted',
                creates=[dict(note_id='n-cap-2022', candidate_index=0, status='verified',
                              relations=[], open_questions=[])],
                revisions=[], retain_context_windows=[], retention_reason='日期限定已并入笔记，显式释放')
        if wid == '5':
            return dict(outcome='noted',
                creates=[dict(note_id='n-cap-2023', candidate_index=0, status='verified',
                    relations=[dict(rel='same_metric_diff_period', to_note_id='n-cap-2022',
                                    reason='同主体同指标不同截止日期，各自出处完整')],
                    open_questions=[])],
                revisions=[], retain_context_windows=[], retention_reason='日期限定已并入笔记，显式释放')
        return no_content_plan()

    def execute(self, *, skill_text, text, timeout=180):
        request = json.loads(text)
        action = request['action']
        self.requests.append(deepcopy(request))
        self.skills.append(skill_text)
        wid = request.get('window', {}).get('grant_id')
        if action == 'classify':
            return dict(classification=dict(description='独立资料'))
        if action == 'attribute':
            if wid in ('4', '5'):
                a, b = (5, 7) if wid == '4' else (8, 10)
                return dict(ranges=[dict(entity='主体', start_line=a, end_line=b)])
            return dict(ranges=[], outcome='no_attribution', reason='纯限定或说明文字，无可归属实体')
        if action == 'extract':
            return self.handle_extract(wid, request)
        receipt = self.plan_for(wid)
        assert request['protocol'] == 'mapped-md-v4' and request['operation'] == 'note_receipt'
        return receipt


def test_request_context_revisits_released_date_grants_and_periods_stay_distinct(v4_job):
    store = v4_job
    d = p02_dossier()
    model = Partial02Model(d)
    result = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete']
    assert progress['window_count'] == 6 and progress['verified_notes'] == 2
    # The index offered coordinates of all earlier read windows, never text.
    first_w4 = next(r for r in model.requests
                    if r['action'] == 'extract' and r['window']['grant_id'] == '4')
    assert [g['grant_id'] for g in first_w4['read_grant_index']] == ['0', '1', '2', '3']
    assert all(set(g) == {'grant_id', 'source_loc', 'ranges'} for g in first_w4['read_grant_index'])
    assert '截至2022年12月31日' not in json.dumps(first_w4['read_grant_index'], ensure_ascii=False)
    assert all('quote' not in n['fact'] for n in first_w4['existing_notes'])
    # Tool audit: request ids and independently delivered actual grant views.
    records = progress['records']
    assert records[4]['revisit_context_windows'] == ['0']
    assert records[5]['revisit_context_windows'] == ['2']
    for index, gid in ((4, '0'), (5, '2')):
        audit = records[index]['context_tool_requests']
        assert len(audit) == 1 and audit[0]['granted'] == [gid]
        assert audit[0]['granted_views'][0]['grant_id'] == gid
        assert audit[0]['granted_views'][0]['md'].strip()
    # The granted original redisplayed on the next same-phase extract request.
    second_w4 = [r for r in model.requests
                 if r['action'] == 'extract' and r['window']['grant_id'] == '4'][1]
    assert second_w4['context_tool_results'][0]['granted_views'][0]['md'] == '截至2022年12月31日。'
    assert [g['grant_id'] for g in second_w4['context_grants']] == ['2', '3', '0']
    # Two periods stay distinct notes; the employee relates them structurally.
    payloads = {r['note_id']: json.loads(r['payload']) for r in
                store.db.execute('SELECT note_id,payload FROM md_read_notes').fetchall()}
    assert payloads['n-cap-2022']['fact']['period'] == '2022年12月31日'
    assert payloads['n-cap-2023']['fact']['period'] == '2023年12月31日'
    assert payloads['n-cap-2022']['evidence']['field_evidence'][0]['grant_id'] == '0'
    assert payloads['n-cap-2023']['evidence']['field_evidence'][0]['grant_id'] == '2'
    relation = payloads['n-cap-2023']['relations'][0]
    assert relation['rel'] == 'same_metric_diff_period' and relation['to_note_id'] == 'n-cap-2022'
    # Reading phase still writes nothing formal.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table
    # Same-job replay skips all six windows, including revisit windows.
    before = len(model.requests)
    again = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    assert again['done'] and len(model.requests) == before


def test_revisit_id_cannot_enter_retain_context_windows(v4_job):
    store = v4_job
    d = p02_dossier()

    class Model(Partial02Model):
        def plan_for(self, wid):
            receipt = super().plan_for(wid)
            if wid == '4':
                receipt['retain_context_windows'] = ['0']  # a revisit id, not rolling/current
            return receipt

    model = Model(d)
    with pytest.raises(Exception, match='not displayed'):
        p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    # The rejected plan receipt created no note side effects.
    assert store.db.execute('SELECT count(*) FROM md_read_notes').fetchone()[0] == 0
    assert formal_table_count(store.db, 'md_leaf_bodies') == 0
    assert model.requests[-1]['action'] == 'plan'  # failed at the plan guard, not a later window


@pytest.mark.parametrize('illegal', ['future', 'fake', 'mixed', 'toomany'])
def test_illegal_context_tool_requests_refused_with_zero_escalation(v4_job, illegal):
    store = v4_job
    d = p02_dossier()

    class Model(Partial02Model):
        def __init__(self, d):
            super().__init__(d)
            self.illegal_done = False

        def tool_attempt(self, wid, request):
            if wid != '4' or self.illegal_done:
                return None
            if illegal == 'future':
                return dict(outcome='request_context', request_context_windows=['9'], reason='尝试未来窗口')
            if illegal == 'fake':
                return dict(outcome='request_context', request_context_windows=['x'], reason='伪造ID')
            if illegal == 'mixed':
                return dict(outcome='request_context', request_context_windows=['0'],
                            reason='混写普通交付', facts=[])
            return dict(outcome='request_context', request_context_windows=['0', '1', '5'], reason='超过两个')

        def handle_extract(self, wid, request):
            if wid == '4' and not self.illegal_done and request.get('context_tool_feedback'):
                self.illegal_done = True
            return super().handle_extract(wid, request)

    model = Model(d)
    result = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    assert result['done']
    # The illegal turn returned no grant text; the correction request carried feedback.
    w4_extracts = [r for r in model.requests if r['action'] == 'extract' and r['window']['grant_id'] == '4']
    rejected_turn = next(r for r in w4_extracts if r.get('context_tool_feedback'))
    assert rejected_turn['context_tool_results'] == []
    preparation = result['mapped_notes']['records'][4]['preparation']
    if illegal in ('future', 'fake'):
        # Well-formed receipt pointing at an unauthorized ID: denied by the tool.
        assert preparation['context_tool_rejections']
        assert any(item['phase'] == 'extract' for item in preparation['context_tool_rejections'])
    else:
        # Mutex/key-count violations are structural corrections; no tool audit
        # entry is produced because the closure is never entered.
        assert not preparation.get('context_tool_rejections')
        assert preparation['extract_tool_rejections'] >= 1
    # Zero privilege escalation: only the later legitimate ['0'] grant is
    # requested-and-granted; denied/malformed turns append no grant audit.
    record4 = result['mapped_notes']['records'][4]
    assert record4['revisit_context_windows'] == ['0']
    assert [a['granted'] for a in record4['context_tool_requests']] == [['0']]
    # The later legitimate revisit still displayed only the authorized grant.
    assert any(r['context_tool_results'] for r in w4_extracts)
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table


def test_third_distinct_revisit_refused_and_over_byte_bound_refused(v4_job, monkeypatch):
    store = v4_job
    d = p02_dossier()

    class ThreeModel(Partial02Model):
        def __init__(self, d):
            super().__init__(d)
            self.calls_5 = 0

        def handle_extract(self, wid, request):
            if wid != '5':
                return super().handle_extract(wid, request)
            self.calls_5 += 1
            if self.calls_5 == 1:
                return dict(outcome='request_context', request_context_windows=['0', '1'], reason='一次占两个回看槽')
            if self.calls_5 == 2 and request.get('context_tool_feedback') is None:
                return dict(outcome='request_context', request_context_windows=['2'], reason='第三个回看')
            if request.get('context_tool_feedback'):
                # Period cannot come from a filler line verbatim; close with unknown period.
                return dict(facts=[dict(entity='主体', attribute='资本', period='unknown', value='200', unit='元',
                    value_type='number', quote='| 资本 | 200元 |', source_loc=P02_LOCS[5],
                    attribution_index=0, module_key=None)], complete=True, remaining_source_locs=[])
            return super().handle_extract(wid, request)

    model = ThreeModel(d)
    result = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    assert result['done']
    record = result['mapped_notes']['records'][5]
    assert record['revisit_context_windows'] == ['0', '1']
    assert any('at most two' in item['error'] for item in record['preparation']['context_tool_rejections'])
    assert all('截至2023年12月31日' not in json.dumps(r, ensure_ascii=False)
               for r in record['preparation']['context_tool_rejections'])

    # Byte bound: the host never truncates; an over-byte combined context is refused outright.
    monkeypatch.setattr('app.document_read.notes.MAX_CONTEXT_BYTES', 10)

    class ByteModel(Partial02Model):
        def handle_extract(self, wid, request):
            if wid in ('4', '5'):
                if not request.get('context_tool_feedback'):
                    wanted = '0' if wid == '4' else '2'
                    return dict(outcome='request_context', request_context_windows=[wanted], reason='申请回看')
                value = '100' if wid == '4' else '200'
                return dict(facts=[dict(entity='主体', attribute='资本', period='unknown', value=value, unit='元',
                    value_type='number', quote=f'| 资本 | {value}元 |', source_loc=P02_LOCS[int(wid)],
                    attribution_index=0, module_key=None)], complete=True, remaining_source_locs=[])
            return super().handle_extract(wid, request)

    # Separate job scope needs its own parse_jobs row.
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (JOB + '-byte', COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    byte_result = p02_service(store, d, ByteModel(d)).run(company_id=COMPANY, job_id=JOB + '-byte',
                                                          limits=P02_LIMITS, note_review=False)
    assert byte_result['done']
    record = byte_result['mapped_notes']['records'][4]
    assert any('byte bound' in item['error'] for item in record['preparation']['context_tool_rejections'])
    assert record.get('revisit_context_windows', []) == []


@pytest.mark.parametrize('mutation', ['ranges', 'company', 'fingerprint'])
def test_checkpoint_grant_tamper_rejected_on_replay_with_zero_calls(v4_job, mutation):
    store = v4_job
    d = p02_dossier()
    result = p02_service(store, d, Partial02Model(d)).run(
        company_id=COMPANY, job_id=JOB, limits=dict.fromkeys(LIMITS, 1), note_review=False)
    assert result['status'] == 'partial'
    state = load_checkpoint(store)
    grant = state['mapped_notes']['records'][0]['grant']
    if mutation == 'ranges':
        grant['ranges'] = [[9, 9]]
    elif mutation == 'company':
        grant['company_id'] = 'acc-other'
    else:
        grant['mapped_fingerprint'] = 'f' * 64
    save_checkpoint(store, state)
    model = Partial02Model(d)
    with pytest.raises(Exception, match='differ'):
        p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert model.requests == []


def test_checkpoint_revisit_view_tamper_rejected_with_zero_calls(v4_job):
    store = v4_job
    d = p02_dossier()
    p02_service(store, d, Partial02Model(d)).run(
        company_id=COMPANY, job_id=JOB, limits=dict.fromkeys(LIMITS, 1), note_review=False)
    state = load_checkpoint(store)
    current = state['mapped_notes']['records'][1]
    current['revisit_context_windows'] = ['0']
    current['revisit_context_grants'] = [dict(grant_id='0', md='伪造原文')]
    save_checkpoint(store, state)
    model = Partial02Model(d)
    with pytest.raises(Exception, match='persisted revisit context differs'):
        p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert model.requests == []


class NoProgressModel(NotesModel):
    """Window 1 delivers two invalid facts without coverage advance."""

    def __init__(self, recover=False):
        super().__init__()
        self.recover = recover
        self.bad = 0

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        wid = request.get('window', {}).get('grant_id')
        if request.get('action') == 'extract' and wid == '1':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            if self.recover:
                return dict(facts=[fact('1')], complete=True, remaining_source_locs=[])
            self.bad += 1
            return dict(facts=[dict(fact('1'), quote='原文中不存在的坏引用xyz')],
                        complete=False, remaining_source_locs=[LOC1])
        return super().execute(**kwargs)


def test_two_no_progress_extracts_block_partial_and_only_explicit_resume_recovers(v4_job):
    store = v4_job
    blocked_model = NoProgressModel()
    result = reader(store, blocked_model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['status'] == 'partial' and not result.get('done')
    record = result['mapped_notes']['records'][1]
    assert record['extract_progress_blocked'] is True
    assert len(record['extract_no_progress_drafts']) == 2
    assert record['extract_no_progress_feedback']
    assert record['extract_block_reason']
    extract_calls_after_block = result['calls']['extract']
    # Ordinary resume re-stops before any model call.
    silent = NoProgressModel()
    resumed = reader(store, silent).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert resumed['status'] == 'partial' and silent.requests == []
    # Explicit unresolved retry clears only the stop markers/streaks, not budget.
    recovered = reader(store, NoProgressModel(recover=True)).run(
        company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False,
        resume_unresolved=True, retry_request_id='resume-no-progress-1')
    assert recovered['done'] and recovered['mapped_notes']['verified_notes'] == 2
    assert recovered['calls']['extract'] > extract_calls_after_block
    record = recovered['mapped_notes']['records'][1]
    assert 'extract_progress_blocked' not in record
    assert record['reading_block_retry_history'][0]['markers'] == {'extract_progress_blocked': True}
    assert record['preparation']['extract_no_progress_streak'] == 0
    assert recovered['mapped_notes']['records'][0]['status'] == 'noted'
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table


class RepeatedCandidateModel(NotesModel):
    def __init__(self):
        super().__init__()
        self.deliveries = 0

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        wid = request.get('window', {}).get('grant_id')
        if request.get('action') == 'extract' and wid == '1':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            self.deliveries += 1
            if self.deliveries <= 3:
                return dict(facts=[fact('1')], complete=False, remaining_source_locs=[LOC1])
            return dict(facts=[], complete=True, remaining_source_locs=[])
        return super().execute(**kwargs)


def test_identical_candidate_repeat_is_structural_idempotency_not_progress(v4_job):
    store = v4_job
    model = RepeatedCandidateModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['status'] == 'partial'
    record = result['mapped_notes']['records'][1]
    assert record['extract_progress_blocked'] is True
    preparation = record['preparation']
    # Only the first delivery created a candidate; repeats are recorded as duplicates.
    assert len(preparation['candidates']) == 1
    assert record['extract_no_progress_drafts'][0]['duplicate_indices'] == [0]
    # Explicit retry keeps the successful candidate and closes on an empty
    # title-style delivery; the streak was reset, calls and candidates were not.

    class CloseModel(NotesModel):
        def execute(self, **kwargs):
            request = json.loads(kwargs['text'])
            wid = request.get('window', {}).get('grant_id')
            if request.get('action') == 'extract' and wid == '1':
                self.requests.append(deepcopy(request))
                self.skills.append(kwargs['skill_text'])
                assert request['already_delivered']
                return dict(facts=[], complete=True, remaining_source_locs=[])
            return super().execute(**kwargs)

    close_model = CloseModel()
    recovered = reader(store, close_model).run(
        company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False,
        resume_unresolved=True, retry_request_id='resume-dup-1')
    assert recovered['done'] and recovered['mapped_notes']['verified_notes'] == 2
    record = recovered['mapped_notes']['records'][1]
    assert 'extract_progress_blocked' not in record
    assert len(record['preparation']['candidates']) == 1
    assert close_model.requests[0]['window']['grant_id'] == '1'


class ToolStallModel(NotesModel):
    """Requests the same earlier grant repeatedly without consuming it."""

    def __init__(self, stop=True):
        super().__init__()
        self.stop = stop

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        wid = request.get('window', {}).get('grant_id')
        if request.get('action') == 'extract' and wid == '1':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            if self.stop:
                return dict(outcome='request_context', request_context_windows=['0'], reason='反复要同一窗')
            return dict(facts=[fact('1')], complete=True, remaining_source_locs=[])
        return super().execute(**kwargs)


def test_repeated_identical_tool_request_twice_stalls_then_resume_recovers(v4_job):
    store = v4_job
    result = reader(store, ToolStallModel()).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['status'] == 'partial'
    record = result['mapped_notes']['records'][1]
    assert record['context_tool_blocked'] is True
    assert [a['granted'] for a in record['context_tool_requests']] == [['0'], [], []]
    # Explicit unresolved retry clears the stall marker; recovery then completes.
    recovered = reader(store, ToolStallModel(stop=False)).run(
        company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False,
        resume_unresolved=True, retry_request_id='resume-stall-1')
    assert recovered['done'] and recovered['mapped_notes']['verified_notes'] == 2
    record = recovered['mapped_notes']['records'][1]
    assert 'context_tool_blocked' not in record
    assert record['reading_block_retry_history'][0]['markers'] == {'context_tool_blocked': True}


def test_one_revisit_then_one_extract_delivery_is_not_false_progress_stop(v4_job):
    store = v4_job
    d = p02_dossier()

    class RecoveryModel(Partial02Model):
        def __init__(self, d):
            super().__init__(d)
            self.w4_extracts = 0

        def handle_extract(self, wid, request):
            if wid != '4':
                return super().handle_extract(wid, request)
            self.w4_extracts += 1
            if self.w4_extracts == 1 and not request.get('context_tool_results'):
                # First delivery makes no progress (empty, not closed).
                return dict(facts=[], complete=False, remaining_source_locs=[P02_LOCS[4]])
            if self.w4_extracts == 2 and not request.get('context_tool_results'):
                return dict(outcome='request_context', request_context_windows=['0'], reason='需要日期字面')
            return super().handle_extract(wid, request)

    model = RecoveryModel(d)
    result = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=P02_LIMITS, note_review=False)
    assert result['done'] and result['mapped_notes']['records'][4]['status'] == 'noted'


def title_dossier():
    doc = dict(markdown='仅章节标题文字',
               document_map=dict(elements=[dict(source_loc='docx:body/title', start_line=1, end_line=1)]),
               assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


class TitleModel:
    def __init__(self):
        self.requests, self.skills = [], []

    def execute(self, *, skill_text, text, timeout=180):
        request = json.loads(text)
        self.requests.append(deepcopy(request))
        self.skills.append(skill_text)
        if request['action'] == 'classify':
            return dict(classification=dict(description='标题窗'))
        if request['action'] == 'attribute':
            return dict(ranges=[], outcome='no_attribution', reason='标题无实体')
        if request['action'] == 'extract':
            return dict(facts=[], complete=True, remaining_source_locs=[])
        return no_content_plan()


def test_title_window_explicit_empty_close_is_progress_not_blocked(v4_job):
    store = v4_job
    d = title_dossier()
    model = TitleModel()
    service = DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=store.memory, model=model)
    result = service.run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done']
    progress = result['mapped_notes']
    assert progress['window_count'] == 1 and progress['verified_notes'] == 0
    assert progress['open_notes'] == 0 and progress['discarded_notes'] == 0
    assert progress['records'][0]['status'] == 'no_content'


REJECT_LINES = ['a有9个。', '更多文字。', '结尾文字。']
REJECT_LOCS = [f'docx:body/r{i}/paragraph' for i in range(3)]


def reject_dossier():
    elements = [dict(source_loc=loc, start_line=i, end_line=i)
                for i, loc in enumerate(REJECT_LOCS, 1)]
    doc = dict(markdown='\n'.join(REJECT_LINES), document_map=dict(elements=elements), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


class RejectModel(NotesModel):
    def plan_for_wid(self, wid, default):
        if wid == '0':
            return dict(outcome='noted',
                creates=[dict(note_id='n-0001', candidate_index=0, status='verified',
                              relations=[], open_questions=[])],
                revisions=[], retain_context_windows=[],
                retention_reason='无需保留')
        if wid == '1':
            return dict(outcome='noted', creates=[],
                revisions=[dict(note_id='n-0001', action='update_status',
                                status='rejected', reason='员工复核后判定该草稿是误抽取，废弃')],
                retain_context_windows=[], retention_reason='无需保留')
        return dict(outcome='noted', creates=[],
            revisions=[dict(note_id='n-0001', action='update_status',
                            status='verified', reason='试图把废弃笔记改回 verified')],
            retain_context_windows=[], retention_reason='无需保留')

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        action = request['action']
        wid = request['window']['grant_id']
        if action == 'plan':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            return self.plan_for_wid(wid, None)
        if action == 'attribute':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            line = int(wid) + 1
            return dict(ranges=[dict(entity='临时主体', start_line=line, end_line=line)])
        if action == 'extract':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            if wid == '0':
                return dict(facts=[dict(entity='临时主体', attribute='测试指标', period='unknown', value='9',
                    unit='个', value_type='number', quote='a有9个', source_loc=REJECT_LOCS[0],
                    attribution_index=0, module_key=None)], complete=True, remaining_source_locs=[])
            return dict(facts=[], complete=True, remaining_source_locs=[])
        return super().execute(**kwargs)


def test_rejected_note_is_terminal_discard_counted_separately(v4_job):
    store = v4_job
    d = reject_dossier()
    model = RejectModel()
    service = DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=store.memory, model=model)
    result = service.run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert result['done']
    progress = result['mapped_notes']
    assert progress['verified_notes'] == 0
    assert progress['open_notes'] == 0 and progress['discarded_notes'] == 1
    assert progress['unresolved_readings'] == 1 and progress['technical_rejections'] == 1
    assert progress['records'][2]['status'] == 'unresolved'
    assert 'terminal' in progress['records'][2]['reason']
    status = store.db.execute("SELECT status FROM md_read_notes WHERE note_id='n-0001'").fetchone()[0]
    assert status == 'rejected'
    revisions = store.db.execute(
        "SELECT rev,action FROM md_read_note_revisions WHERE note_id='n-0001' ORDER BY rev").fetchall()
    assert [(r['rev'], r['action']) for r in revisions] == [(1, 'create'), (2, 'update_status')]
    # Discards never block publication readiness and never masquerade as facts.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table


def test_cross_company_note_store_is_scoped(v4_job):
    store = v4_job
    d = dossier()
    window = MDWindows(d).windows[0]
    grant = MDWindows(d).grant(window, 'sample.docx')
    grant['company_id'] = 'acc-other'
    candidate = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='个',
        value_type='number', quote='有9个', source_loc=LOC0, start_line=1, end_line=1,
        grant_id='0', file_hash=SHA, mapped_fingerprint=grant['mapped_fingerprint'],
        original_name='sample.docx', field_evidence=[])
    notes = NoteStore(store.memory.database, company_id=COMPANY, job_id='unit-other',
        file_hash=SHA, mapped_fingerprint=d.fingerprint)
    with pytest.raises(NoteRejected):
        notes.apply_window(window_id='0', outcome='noted',
            creates=[dict(note_id='n-x', candidate_index=0)], revisions=[],
            grants={'0': grant}, candidates=[candidate], request_sha='sha-x')
    notes.close()


# ---------------------------------------------------------------------------
# V4 stage B: post-read note review (action='review'), an independent durable
# phase with its own budget; reading is never replayed or recharged.
# ---------------------------------------------------------------------------

REVIEW_LIMITS = dict(LIMITS, extract=16, review=8)
CONFIRM_REASON = '员工复核：主证据与字段旁证逐字一致，结论保持不变'


class ConfirmedReviewModel(NotesModel):
    """Reading is scripted by NotesModel; every review turn confirms the note."""

    def __init__(self):
        super().__init__()
        self.review_requests = []

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'review':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            self.review_requests.append(deepcopy(request))
            return dict(note_id=request['note']['note_id'], action='confirmed', reason=CONFIRM_REASON)
        return super().execute(**kwargs)


def assert_formal_zero(store):
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1  # seed only
    for table in ZERO_TABLES:
        assert formal_table_count(store.db, table) == 0, table


def test_review_runs_by_default_and_replay_is_idempotent_zero_call(v4_job):
    store = v4_job
    model = ConfirmedReviewModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete'] and progress['review_complete']
    assert progress['awaiting_publication'] and progress['open_notes'] == 0
    assert progress['reviewed_notes'] == 2 and progress['discarded_notes'] == 0
    assert tuple(result['calls'][p] for p in ('classify', 'attribute', 'extract', 'plan')) == (2, 2, 2, 2)
    assert result['calls']['review'] == 2 and result['calls']['leaf'] == 0
    # Confirm writes a receipt but no revision; rev stays 1.
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_read_note_revisions').fetchone()[0] == 2
    receipts = [json.loads(r['receipt']) for r in store.db.execute(
        'SELECT receipt FROM md_read_note_review_receipts ORDER BY note_id,round')]
    assert [(r['note_id'], r['round'], r['action'], r['rev'], r['status']) for r in receipts] == [
        ('n-0001', 1, 'confirmed', 1, 'verified'),
        ('n-0002', 1, 'confirmed', 1, 'verified')]
    # The compact index carries coordinates/fields but never quotes or md text.
    first = model.review_requests[0]
    assert first['operation'] == 'note_review' and first['note']['note_id'] == 'n-0001'
    assert [n['note_id'] for n in first['review_index']] == ['n-0001', 'n-0002']
    dump = json.dumps(first['review_index'], ensure_ascii=False)
    assert 'quote' not in dump and '技术描述可回查' not in dump and '| 参数 | 9 |' not in dump
    assert first['read_grant_index'][0].keys() == {'grant_id', 'source_loc', 'ranges'}
    # The origin window is the only body text; no other grant is shipped.
    assert '技术描述可回查' in first['origin_window']['md']
    assert first['context_grants'] == []
    assert any('note_review' in skill for skill in model.skills)
    assert_formal_zero(store)
    # Full replay: decided notes cost zero model calls in every phase.
    before = len(model.requests)
    again = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert again['done'] and len(model.requests) == before
    assert again['mapped_notes']['review']['cursor'] == 2
    assert again['calls']['review'] == 2


def test_missing_review_budget_stops_partial_with_zero_review_calls_then_resumes(v4_job):
    store = v4_job
    model = ConfirmedReviewModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=LIMITS)
    assert result['status'] == 'partial' and not result.get('done')
    progress = result['mapped_notes']
    assert progress['reading_complete'] and not progress.get('review_complete')
    assert not progress['awaiting_publication']
    assert result['calls'].get('review', 0) == 0 and model.review_requests == []
    # Resuming with the explicit sixth budget completes review; the reading
    # prefix is not replayed and its phase counters do not move.
    result2 = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert result2['done'] and result2['mapped_notes']['review_complete']
    assert tuple(result2['calls'][p] for p in ('classify', 'attribute', 'extract', 'plan')) == (2, 2, 2, 2)
    assert result2['calls']['review'] == 2


def test_explicit_stage_a_switch_skips_review_and_is_not_publishable(v4_job):
    store = v4_job
    model = ConfirmedReviewModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB,
                                      limits=REVIEW_LIMITS, note_review=False)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete']
    assert not progress.get('review_complete') and not progress['awaiting_publication']
    assert result['calls'].get('review', 0) == 0 and model.review_requests == []
    with pytest.raises(ValueError, match='explicit bool'):
        reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS, note_review='yes')


def review_proof(d, *, field, grant_id, loc, line, quote):
    return dict(field=field, grant_id=str(grant_id), company_id=COMPANY, file_hash=SHA,
                mapped_fingerprint=d.fingerprint, source_loc=loc,
                start_line=line, end_line=line, quote=quote)


NINES_LINES = ['截至2022年12月31日，单位为人。',
               '| 项目 | 人数 |', '| --- | --- |', '| 员工总数 | 82 |',
               '截至2023年12月31日，单位为人。',
               '| 项目 | 人数 |', '| --- | --- |', '| 员工总数 | 90 |']
NINES_LOCS = ['docx:body/p0/paragraph', 'docx:body/t0/table',
              'docx:body/p1/paragraph', 'docx:body/t1/table']
NINES_RANGES = [(1, 1), (2, 4), (5, 5), (6, 8)]


def nines_dossier():
    elements = [dict(source_loc=loc, start_line=a, end_line=b,
                     kind='table' if loc.startswith('docx:body/t') else 'paragraph')
                for loc, (a, b) in zip(NINES_LOCS, NINES_RANGES)]
    doc = dict(markdown='\n'.join(NINES_LINES), document_map=dict(elements=elements), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


class NinesModel:
    """The 90/82 scenario: the 90 table note is saved verified with unknown
    period/empty unit (the captured reading failure). Review must revisit the
    2023 qualifier grant, revise with verbatim proofs and declare the conflict."""

    def __init__(self):
        self.requests, self.skills, self.review_requests = [], [], []

    def execute(self, *, skill_text, text, timeout=180):
        request = json.loads(text)
        action = request['action']
        self.requests.append(deepcopy(request))
        self.skills.append(skill_text)
        wid = request.get('window', {}).get('grant_id')
        if action == 'classify':
            return dict(classification=dict(description='员工人数表'))
        if action == 'attribute':
            if wid in ('1', '3'):
                a, b = (2, 4) if wid == '1' else (6, 8)
                return dict(ranges=[dict(entity='主体', start_line=a, end_line=b)])
            return dict(ranges=[], outcome='no_attribution', reason='纯限定文字，无实体表')
        if action == 'extract':
            self.d = getattr(self, 'd', None)
            if wid in ('0', '2'):
                return dict(facts=[], complete=True, remaining_source_locs=[])
            if wid == '1':
                proofs = [review_proof(self.d, field='period', grant_id=0, loc=NINES_LOCS[0],
                                       line=1, quote=NINES_LINES[0]),
                          review_proof(self.d, field='unit', grant_id=0, loc=NINES_LOCS[0],
                                       line=1, quote=NINES_LINES[0])]
                item = dict(entity='主体', attribute='员工总数', period='2022年12月31日',
                    value='82', unit='人', value_type='number', quote='| 员工总数 | 82 |',
                    source_loc=NINES_LOCS[1], attribution_index=0, module_key=None,
                    field_evidence=proofs)
                return dict(facts=[item], complete=True, remaining_source_locs=[])
            # w3: reproduced artifact — verified note with unknown period/unit.
            item = dict(entity='主体', attribute='员工总数', period='unknown', value='90',
                unit='', value_type='number', quote='| 员工总数 | 90 |',
                source_loc=NINES_LOCS[3], attribution_index=0, module_key=None, field_evidence=[])
            return dict(facts=[item], complete=True, remaining_source_locs=[])
        if action == 'plan':
            if wid == '1':
                return dict(outcome='noted',
                    creates=[dict(note_id='n-2022', candidate_index=0, status='verified',
                                  relations=[], open_questions=[])],
                    revisions=[], retain_context_windows=[], retention_reason='日期限定已并入笔记')
            if wid == '3':
                return dict(outcome='noted',
                    creates=[dict(note_id='n-unknown', candidate_index=0, status='verified',
                                  relations=[], open_questions=[])],
                    revisions=[], retain_context_windows=[], retention_reason='无适用限定保留')
            return no_content_plan()
        # review
        self.review_requests.append(deepcopy(request))
        nid = request['note']['note_id']
        if nid == 'n-2022':
            return dict(note_id=nid, action='confirmed', reason=CONFIRM_REASON)
        granted = [g['grant_id'] for g in request.get('context_grants', [])]
        if '2' not in granted:
            return dict(outcome='request_context', request_context_windows=['2'],
                        reason='90表未写截止日期与单位，需要回看2023年限定窗原文')
        # The 2023 qualifier is element NINES_LOCS[2] (p1) but text line
        # NINES_LINES[4]: the paragraph/table loc index is not the text index.
        proofs = [review_proof(self.d, field='period', grant_id=2, loc=NINES_LOCS[2],
                               line=5, quote=NINES_LINES[4]),
                  review_proof(self.d, field='unit', grant_id=2, loc=NINES_LOCS[2],
                               line=5, quote=NINES_LINES[4])]
        return dict(note_id=nid, action='revise_fact',
            reason='员工在授权回看grant 2逐字看到截止日期与单位，补证修订，不做格式推断',
            fact=dict(entity='主体', attribute='员工总数', period='2023年12月31日',
                      value='90', unit='人', value_type='number'),
            field_evidence=proofs,
            relations=[dict(rel='conflicts_with', to_note_id='n-2022',
                            reason='同一公司同一指标不同截止日期且数值不同，员工显式标记冲突，不裁决胜出值')])

    def bind(self, d):
        self.d = d
        return self


def test_90_82_review_revises_with_verbatim_revisit_evidence_and_declares_conflict(v4_job):
    store = v4_job
    d = nines_dossier()
    model = NinesModel().bind(d)
    result = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete'] and progress['review_complete']
    assert progress['awaiting_publication'] and progress['open_notes'] == 0
    assert tuple(result['calls'][p] for p in ('classify', 'attribute', 'extract', 'plan')) == (4, 4, 4, 4)
    assert result['calls']['review'] == 3  # confirm 82, tool turn + revise for 90
    payloads = {r['note_id']: json.loads(r['payload']) for r in
                store.db.execute('SELECT note_id,payload FROM md_read_notes').fetchall()}
    n90, n82 = payloads['n-unknown'], payloads['n-2022']
    assert n90['status'] == 'verified'
    assert n90['fact']['period'] == '2023年12月31日' and n90['fact']['unit'] == '人'
    assert n90['fact']['value'] == '90'
    assert [p['field'] for p in n90['evidence']['field_evidence']] == ['period', 'unit']
    assert {p['grant_id'] for p in n90['evidence']['field_evidence']} == {'2'}
    # Primary evidence is immutable and still the 90 table origin.
    assert n90['evidence']['primary']['grant_id'] == '3'
    assert n90['evidence']['primary']['quote'] == '| 员工总数 | 90 |'
    # Both values are preserved; the conflict is an employee-declared edge.
    assert n82['fact']['value'] == '82' and n82['status'] == 'verified'
    assert n90['relations'] == [dict(rel='conflicts_with', to_note_id='n-2022',
        reason='同一公司同一指标不同截止日期且数值不同，员工显式标记冲突，不裁决胜出值')]
    assert n82.get('relations', []) == []  # host adds no reverse edge
    revisions = store.db.execute(
        "SELECT rev,action,payload FROM md_read_note_revisions WHERE note_id='n-unknown' ORDER BY rev").fetchall()
    assert [(r['rev'], r['action']) for r in revisions] == [(1, 'create'), (2, 'revise_fact')]
    audit = json.loads(revisions[1]['payload'])
    assert audit['old']['fact']['period'] == 'unknown' and audit['old']['fact']['unit'] == ''
    assert audit['new']['fact']['period'] == '2023年12月31日' and audit['new']['fact']['unit'] == '人'
    assert audit['reason'] and audit['new']['relations'][0]['rel'] == 'conflicts_with'
    receipt = json.loads(store.db.execute(
        "SELECT receipt FROM md_read_note_review_receipts WHERE note_id='n-unknown'").fetchone()['receipt'])
    assert receipt['action'] == 'revise_fact' and receipt['round'] == 1 and receipt['rev'] == 2
    # Review revisit audits stay scoped to this one note: request, reason, views.
    entry = progress['review']['items']['n-unknown']
    assert entry['revisit_windows'] == ['2']
    assert entry['tool_requests'][0]['granted_views'][0]['md'] == NINES_LINES[4]
    assert '2023' not in json.dumps(progress['review']['items']['n-2022'], ensure_ascii=False)
    assert_formal_zero(store)
    # Replay spends nothing and changes nothing.
    before = len(model.requests)
    again = p02_service(store, d, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert again['done'] and len(model.requests) == before
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 2


class ScriptedReviewModel(ConfirmedReviewModel):
    """Review hook for subclasses: decide(nid, request) returns a response."""

    def decide(self, nid, request):
        return dict(note_id=nid, action='confirmed', reason=CONFIRM_REASON)

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'review':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            self.review_requests.append(deepcopy(request))
            return self.decide(request['note']['note_id'], request)
        return super().execute(**kwargs)


def test_review_mark_needs_check_blocks_publication_gate(v4_job):
    store = v4_job

    class Model(ScriptedReviewModel):
        def decide(self, nid, request):
            if nid == 'n-0002':
                return dict(note_id=nid, action='mark_needs_check', reason='员工复核：口径依据仍不足，保持待核')
            return super().decide(nid, request)

    result = reader(store, Model()).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['review_complete']
    assert progress['open_notes'] == 1 and progress['discarded_notes'] == 0
    assert not progress['awaiting_publication']
    status = store.db.execute("SELECT status,rev FROM md_read_notes WHERE note_id='n-0002'").fetchone()
    assert status['status'] == 'needs_check' and status['rev'] == 2
    assert store.db.execute("SELECT count(*) FROM md_read_note_review_receipts").fetchone()[0] == 2
    # Replay stays blocked from publication but costs nothing.
    model2 = Model()
    again = reader(store, model2).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert again['done'] and model2.requests == []
    assert not again['mapped_notes']['awaiting_publication']
    assert_formal_zero(store)


def test_bare_confirm_cannot_upgrade_needs_check_then_recovers(v4_job):
    store = v4_job
    plans = {'0': dict(outcome='unresolved',
        creates=[dict(note_id='n-0001', candidate_index=0, open_questions=['期间待后窗确认'])],
        revisions=[], reason='员工无法确认期间限定',
        retain_context_windows=[], retention_reason='保留待核')}

    class Model(ConfirmedReviewModel):
        def execute(self, **kwargs):
            request = json.loads(kwargs['text'])
            if request.get('action') == 'review':
                self.requests.append(deepcopy(request))
                self.skills.append(kwargs['skill_text'])
                self.review_requests.append(deepcopy(request))
                nid = request['note']['note_id']
                if nid == 'n-0001' and request.get('review_feedback') is None:
                    return dict(note_id=nid, action='confirmed', reason='试图无证据升级待核笔记')
                if nid == 'n-0001':
                    return dict(note_id=nid, action='mark_needs_check', reason='补证失败，保持待核')
                return dict(note_id=nid, action='confirmed', reason=CONFIRM_REASON)
            return super().execute(**kwargs)

    model = Model()
    model.plans = plans
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['review_complete'] and progress['open_notes'] == 1
    entry = progress['review']['items']['n-0001']
    assert entry['attempts'] == 1 and len(entry['rejected_decisions']) == 1
    assert 'cannot be confirmed' in entry['rejected_decisions'][0]['error']
    row = store.db.execute("SELECT status,rev FROM md_read_notes WHERE note_id='n-0001'").fetchone()
    assert row['status'] == 'needs_check' and row['rev'] == 2
    assert store.db.execute(
        "SELECT count(*) FROM md_read_note_review_receipts WHERE note_id='n-0001'").fetchone()[0] == 1


@pytest.mark.parametrize('attack', ['date', 'quote', 'fingerprint', 'coordinate',
                                    'null-unit', 'number-type', 'unknown-verified'])
def test_review_illegal_evidence_rejected_zero_change_then_needs_check(v4_job, attack):
    store = v4_job
    d = dossier()

    class Model(ScriptedReviewModel):
        def decide(self, nid, request):
            if nid != 'n-0001':
                return super().decide(nid, request)
            if request.get('review_feedback') is not None:
                return dict(note_id=nid, action='mark_needs_check', reason='逐字补证失败，员工标记待核')
            line = '技术描述可回查，有9个。'
            if attack == 'date':
                fact = dict(entity='临时主体', attribute='测试指标', period='2030年', value='9',
                            unit='个', value_type='number')
                proofs = [review_proof(d, field='period', grant_id=0, loc=LOC0, line=1, quote='有2030个')]
            elif attack == 'quote':
                fact = dict(entity='临时主体', attribute='测试指标', period='2030年', value='9',
                            unit='个', value_type='number')
                proofs = [dict(review_proof(d, field='period', grant_id=1, loc=LOC1,
                                            line=4, quote='| 参数 | 9 |'))]
            elif attack == 'fingerprint':
                fact = dict(entity='临时主体', attribute='测试指标', period='2030年', value='9',
                            unit='个', value_type='number')
                forged = review_proof(d, field='period', grant_id=0, loc=LOC0, line=1, quote=line)
                forged['mapped_fingerprint'] = 'z' * 64
                proofs = [forged]
            elif attack == 'coordinate':
                fact = dict(entity='临时主体', attribute='测试指标', period='2030年', value='9',
                            unit='个', value_type='number')
                proofs = [review_proof(d, field='period', grant_id=0, loc=LOC0, line=4, quote=line)]
            elif attack == 'null-unit':
                fact = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9',
                            unit=None, value_type='number')
                proofs = []
            elif attack == 'number-type':
                fact = dict(entity='临时主体', attribute='测试指标', period='unknown', value=900,
                            unit='个', value_type='number')
                proofs = []
            else:  # unknown-verified
                fact = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9',
                            unit='个', value_type='number')
                proofs = []
            return dict(note_id=nid, action='revise_fact', reason='攻击：非法/伪造证据',
                        fact=fact, field_evidence=proofs)

    model = Model()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['review_complete']
    entry = progress['review']['items']['n-0001']
    assert entry['attempts'] == 1 and entry['receipt']['action'] == 'mark_needs_check'
    # The illegal revision created no revision row/receipt. The audit keeps the
    # employee's own forged receipt (as stage A keeps bad receipts), but the
    # feedback channel delivered back to the employee leaks no other-window text.
    row = store.db.execute("SELECT status,rev,payload FROM md_read_notes WHERE note_id='n-0001'").fetchone()
    assert row['rev'] == 2 and json.loads(row['payload'])['status'] == 'needs_check'
    followups = [r for r in model.review_requests
                 if r['note']['note_id'] == 'n-0001' and r.get('review_feedback') is not None]
    assert len(followups) == 1
    feedback = json.dumps(followups[0]['review_feedback'], ensure_ascii=False)
    assert set(followups[0]['review_feedback']) == {'error', 'contract', 'instruction'}
    assert '| 参数 | 9 |' not in feedback and '有2030个' not in feedback
    receipts = [json.loads(r['receipt']) for r in store.db.execute(
        'SELECT receipt FROM md_read_note_review_receipts').fetchall()]
    assert [r['action'] for r in receipts if r['note_id'] == 'n-0001'] == ['mark_needs_check']
    assert_formal_zero(store)


@pytest.mark.parametrize('illegal', ['future', 'fake', 'toomany'])
def test_review_context_tool_illegal_ids_refused_then_confirmed(v4_job, illegal):
    store = v4_job

    class Model(ScriptedReviewModel):
        def decide(self, nid, request):
            if nid != 'n-0001':
                return super().decide(nid, request)
            if request.get('context_tool_feedback') is None and not request.get('context_tool_results'):
                ids = {'future': ['9'], 'fake': ['x'], 'toomany': ['0', '1', '2']}[illegal]
                return dict(outcome='request_context', request_context_windows=ids, reason='非法回看申请')
            return super().decide(nid, request)

    result = reader(store, Model()).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['awaiting_publication']
    entry = progress['review']['items']['n-0001']
    assert entry['tool_rejections'] == 1
    assert entry['receipt']['action'] == 'confirmed'
    assert entry['revisit_windows'] == []
    error = json.dumps(entry['tool_feedback'], ensure_ascii=False)
    assert '技术描述可回查' not in error and '| 参数 | 9 |' not in error


def test_review_tool_two_stalls_partial_then_explicit_retry_resumes(v4_job):
    store = v4_job

    class StallModel(ScriptedReviewModel):
        def decide(self, nid, request):
            return dict(outcome='request_context', request_context_windows=['0'], reason='反复要本窗origin')

    partial = reader(store, StallModel()).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert partial['status'] == 'partial' and not partial.get('done')
    progress = partial['mapped_notes']
    assert progress['review']['items']['n-0001']['stalls'] == 2
    assert progress['review']['items']['n-0001']['blocked'] is True
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 0
    reading = tuple(partial['calls'][p] for p in ('classify', 'attribute', 'extract', 'plan'))
    assert reading == (2, 2, 2, 2) and partial['calls']['review'] == 2
    # Ordinary resume re-stops with zero model calls.
    silent = StallModel()
    again = reader(store, silent).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert again['status'] == 'partial' and silent.requests == []
    # Explicit retry clears review counters only; reading budget stays frozen.
    recovered = reader(store, ConfirmedReviewModel()).run(company_id=COMPANY, job_id=JOB,
        limits=REVIEW_LIMITS, resume_unresolved=True, retry_request_id='resume-review-stall-1')
    assert recovered['done'] and recovered['mapped_notes']['review_complete']
    assert tuple(recovered['calls'][p] for p in ('classify', 'attribute', 'extract', 'plan')) == reading
    assert recovered['mapped_notes']['review']['items']['n-0001']['stalls'] == 0
    assert recovered['mapped_notes']['review']['items']['n-0001']['receipt']['action'] == 'confirmed'


def test_review_over_byte_bound_refuses_without_truncation(v4_job, monkeypatch):
    store = v4_job
    monkeypatch.setattr('app.document_read.notes.MAX_CONTEXT_BYTES', 10)

    class ByteModel(ScriptedReviewModel):
        def decide(self, nid, request):
            if nid != 'n-0001':
                return super().decide(nid, request)
            if request.get('context_tool_feedback') is None:
                return dict(outcome='request_context', request_context_windows=['1'], reason='需要另一窗原文')
            return dict(note_id=nid, action='mark_needs_check', reason='回看被字节边界拒绝，保持待核')

    result = reader(store, ByteModel()).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert result['done']
    entry = result['mapped_notes']['review']['items']['n-0001']
    assert entry['tool_rejections'] == 1 and 'byte bound' in entry['tool_feedback']['error']
    assert entry['revisit_windows'] == []
    assert result['mapped_notes']['open_notes'] == 1


def test_note_rejected_during_reading_is_excluded_from_review(v4_job):
    store = v4_job
    d = reject_dossier()
    model = RejectModel()
    service = DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=store.memory, model=model)
    result = service.run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    assert result['done'] and progress['reading_complete'] and progress['review_complete']
    assert progress['review']['order'] == [] and progress['reviewed_notes'] == 0
    assert progress['discarded_notes'] == 1 and progress['open_notes'] == 0
    assert progress['awaiting_publication'] is True
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 0
    assert result['calls'].get('review', 0) == 0
    assert_formal_zero(store)


@pytest.mark.parametrize('bad', ['bignote', 'extra', 'confirffact', 'ghost', 'noreason', 'nonobject'])
def test_review_malformed_decision_twice_blocks_partial_then_explicit_retry(v4_job, bad):
    store = v4_job

    class BadModel(ScriptedReviewModel):
        def decide(self, nid, request):
            if nid != 'n-0001':
                return super().decide(nid, request)
            if bad == 'bignote':
                return dict(note_id='n-other', action='confirmed', reason='错评别的笔记')
            if bad == 'extra':
                return dict(note_id=nid, action='confirmed', reason='x', rogue=True)
            if bad == 'confirffact':
                return dict(note_id=nid, action='confirmed', reason='x',
                            fact=dict(entity='e', attribute='a', period='p', value='v', unit='', value_type='string'))
            if bad == 'ghost':
                return dict(note_id=nid, action='relate', reason='x',
                            relations=[dict(rel='related', to_note_id='n-ghost', reason='幽灵目标')])
            if bad == 'noreason':
                return dict(note_id=nid, action='confirmed')
            return ['not', 'an', 'object']

    partial = reader(store, BadModel()).run(company_id=COMPANY, job_id=JOB, limits=REVIEW_LIMITS)
    assert partial['status'] == 'partial'
    entry = partial['mapped_notes']['review']['items']['n-0001']
    assert entry['attempts'] == 2 and entry['blocked'] is True
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 0
    recovered = reader(store, ConfirmedReviewModel()).run(company_id=COMPANY, job_id=JOB,
        limits=REVIEW_LIMITS, resume_unresolved=True, retry_request_id=f'resume-review-{bad}')
    assert recovered['done'] and recovered['mapped_notes']['review_complete']
    assert recovered['mapped_notes']['awaiting_publication']
    assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 2
    assert_formal_zero(store)


def test_apply_review_receipt_rounds_are_idempotent_and_terminal(v4_job):
    store = v4_job
    d = dossier()
    window = MDWindows(d).windows[0]
    grant = MDWindows(d).grant(window, 'sample.docx')
    grant['company_id'] = COMPANY
    candidate = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='个',
        value_type='number', quote='有9个', source_loc=LOC0, start_line=1, end_line=1,
        grant_id='0', file_hash=SHA, mapped_fingerprint=grant['mapped_fingerprint'],
        original_name='sample.docx', field_evidence=[])
    notes = NoteStore(store.memory.database, company_id=COMPANY, job_id='review-unit',
        file_hash=SHA, mapped_fingerprint=d.fingerprint)
    try:
        notes.apply_window(window_id='0', outcome='noted',
            creates=[dict(note_id='n-x', candidate_index=0, status='needs_check')], revisions=[],
            grants={'0': grant}, candidates=[candidate], request_sha='win-sha')
        kwargs = dict(note_id='n-x', reason='员工复核原因', grants={'0': grant})
        # A needs_check note cannot be confirmed without new evidence.
        with pytest.raises(NoteRejected, match='cannot be confirmed'):
            notes.apply_review(**kwargs, action='confirmed', expected_round=1, request_sha='r1')
        notes.apply_review(**kwargs, action='mark_needs_check', expected_round=1, request_sha='r1')
        # Same round/sha replays the stored receipt; different sha is refused.
        again = notes.apply_review(**kwargs, action='mark_needs_check', expected_round=1, request_sha='r1')
        assert again['round'] == 1 and again['status'] == 'needs_check'
        with pytest.raises(NoteRejected, match='another request'):
            notes.apply_review(**kwargs, action='mark_needs_check', expected_round=1, request_sha='r1b')
        # Round gaps and bad round types are rejected.
        with pytest.raises(NoteRejected, match='round mismatch'):
            notes.apply_review(**kwargs, action='mark_needs_check', expected_round=3, request_sha='r3')
        with pytest.raises(NoteRejected):
            notes.apply_review(**kwargs, action='mark_needs_check', expected_round=1, request_sha='rx')
        # Reject is terminal: later rounds can only reject? nothing can revert it.
        notes.apply_review(**kwargs, action='reject', expected_round=2, request_sha='r2')
        with pytest.raises(NoteRejected, match='terminal'):
            notes.apply_review(**kwargs, action='reject', expected_round=3, request_sha='r3')
        with pytest.raises(NoteRejected, match='terminal'):
            notes.apply_review(**kwargs, action='confirmed', expected_round=3, request_sha='r3c')
        row = store.db.execute("SELECT status,rev FROM md_read_notes WHERE note_id='n-x'").fetchone()
        assert row['status'] == 'rejected' and row['rev'] == 3
        # Review receipts are append-only audit.
        with pytest.raises(Exception, match='append-only'):
            store.db.execute("UPDATE md_read_note_review_receipts SET request_sha='x'")
        store.db.rollback()
        with pytest.raises(Exception, match='append-only'):
            store.db.execute('DELETE FROM md_read_note_review_receipts')
        store.db.rollback()
    finally:
        notes.close()


def test_apply_review_illegal_revise_and_ghost_relation_leave_zero_change(v4_job):
    store = v4_job
    d = dossier()
    window = MDWindows(d).windows[0]
    grant = MDWindows(d).grant(window, 'sample.docx')
    grant['company_id'] = COMPANY
    candidate = dict(entity='临时主体', attribute='测试指标', period='unknown', value='9', unit='个',
        value_type='number', quote='有9个', source_loc=LOC0, start_line=1, end_line=1,
        grant_id='0', file_hash=SHA, mapped_fingerprint=grant['mapped_fingerprint'],
        original_name='sample.docx', field_evidence=[])
    notes = NoteStore(store.memory.database, company_id=COMPANY, job_id='review-unit-2',
        file_hash=SHA, mapped_fingerprint=d.fingerprint)
    try:
        notes.apply_window(window_id='0', outcome='noted',
            creates=[dict(note_id='n-x', candidate_index=0)], revisions=[],
            grants={'0': grant}, candidates=[candidate], request_sha='win-sha')
        fact = dict(entity='临时主体', attribute='测试指标', period='2030年', value='9',
                    unit='个', value_type='number')
        forged = [review_proof(d, field='period', grant_id=0, loc=LOC0, line=1, quote='有2030个')]
        with pytest.raises(NoteRejected):
            notes.apply_review(note_id='n-x', action='revise_fact', reason='伪造期间',
                expected_round=1, request_sha='bad', grants={'0': grant}, fact=fact, proofs=forged)
        with pytest.raises(NoteRejected, match='existing note'):
            notes.apply_review(note_id='n-x', action='relate', reason='幽灵关系',
                expected_round=1, request_sha='bad2', grants={'0': grant},
                relations=[dict(rel='related', to_note_id='n-ghost', reason='x')])
        with pytest.raises(NoteRejected, match='outside the host company'):
            foreign = deepcopy(grant)
            foreign['company_id'] = 'acc-other'
            notes.apply_review(note_id='n-x', action='confirmed', reason='跨公司grant',
                expected_round=1, request_sha='bad3', grants={'0': foreign})
        row = store.db.execute("SELECT status,rev FROM md_read_notes WHERE note_id='n-x'").fetchone()
        assert row['status'] == 'verified' and row['rev'] == 1
        assert store.db.execute('SELECT count(*) FROM md_read_note_review_receipts').fetchone()[0] == 0
    finally:
        notes.close()

    # Post-close atomic zero-change view for THIS job: the illegal revise, ghost
    # relation and foreign grant left the one created note intact with only its
    # create audit row; the rejected attempts wrote no review receipt.
    scope = "company_id=? AND job_id='review-unit-2'"
    assert store.db.execute(
        f'SELECT count(*) FROM md_read_notes WHERE {scope}', (COMPANY,)).fetchone()[0] == 1
    revision_rows = store.db.execute(
        f'SELECT action FROM md_read_note_revisions WHERE {scope}', (COMPANY,)).fetchall()
    assert [r['action'] for r in revision_rows] == ['create']
    assert store.db.execute(
        f'SELECT count(*) FROM md_read_note_review_receipts WHERE {scope}', (COMPANY,)).fetchone()[0] == 0

@pytest.mark.parametrize('mutation', ['missing', 'malformed', 'mismatch'])
def test_v4_resume_rejects_binding_identity_before_done_shortcut(v4_job, mutation):
    store = v4_job
    first = reader(store, NotesModel()).run(
        company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    assert first['done'] and first['mapped_notes']['reading_complete']
    state = load_checkpoint(store)
    binding = store.binding.get(COMPANY)
    identity = state['mapped_notes']['binding_identity']
    assert identity == dict(binding_revision=binding['binding_revision'],
        pack_id=binding['template_pack'], pack_version=binding['pack_version'],
        manifest_sha256=binding['pack_digest'])
    if mutation == 'missing':
        state['mapped_notes'].pop('binding_identity')
    elif mutation == 'malformed':
        identity['pack_version'] = True
    else:
        identity['binding_revision'] += 1
    save_checkpoint(store, state)
    before = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (CHECKPOINT_URI,)).fetchone()[0]
    replay = NotesModel()
    with pytest.raises(ValueError, match='V4 notes checkpoint'):
        reader(store, replay).run(company_id=COMPANY, job_id=JOB, limits=LIMITS, note_review=False)
    after = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (CHECKPOINT_URI,)).fetchone()[0]
    assert after == before
    assert replay.requests == []
