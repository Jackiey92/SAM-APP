from hashlib import sha256
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.db.database import connect, init_db
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService

ROOT = Path(__file__).resolve().parents[1]


class Memory:
    memory_root = 'sam://'
    def __init__(self): self.docs = {}
    def put(self, uri, content, **kwargs): self.docs[uri] = content
    def read(self, uri):
        if uri not in self.docs: raise FileNotFoundError(uri)
        return self.docs[uri]
    def delete(self, uri): self.docs.pop(uri, None)


@pytest.fixture
def task(tmp_path):
    db = tmp_path / 'app.db'
    init_db(db)
    digest = sha256(b'test').hexdigest()
    with connect(db) as conn:
        conn.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (digest,'acc.docx',4,'not-used','now'))
        conn.execute('INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     ('job','acc',digest,'acc.docx','docx','docx',4,'now','now'))
    document = {'markdown':'| 指标 | 年份 |\n| --- | --- |\n| 测试量 | 12.00 |',
                'document_map':{'elements':[{'source_loc':'table','start_line':1,'end_line':3}]}, 'assets':[]}
    dossier = Dossier(document, file_hash=digest, objects_root=tmp_path / 'objects')
    # Unit transport fixtures; real kernel QC is tested separately below.
    dossier.quality_fingerprint = dossier.fingerprint
    return db, dossier, Memory()


def fact(**kwargs):
    return {'entity':'测试公司','attribute':'测试量','period':'2024','value':'12.00','unit':'万元',
            'value_type':'number','quote':'| 测试量 | 12.00 |','source_loc':'table', **kwargs}


class Model:
    def __init__(self, responses=None): self.actions=[]; self.responses=iter(responses or [])
    def execute(self, **kwargs):
        request = json.loads(kwargs['text']); action=request['action']; self.actions.append(action)
        if action == 'classify': return {'classification':{'description':'测试资料'}}
        if action == 'attribute': return {'ranges':[{'entity':'测试公司','start_line':1,'end_line':3}]}
        if action == 'extract': return next(self.responses, {'facts':[fact()], 'complete':True})
        if action == 'brief': return {'items':[{'topic':'测试量','conclusion':'测试量有原文证据。','fact_ids':[request['facts'][0]['id']]}]}
        return {'items':[{'conclusion':'测试量可查阅。','l1_entry_ids':['brief-1']}]}


def service(task, model):
    db,dossier,memory=task
    return DirectReadService(db, config=SimpleNamespace(skill_root=ROOT/'skills'), dossier=dossier, memory=memory, model=model)


def test_single_reader_complete_and_idempotent(task):
    model=Model(); reader=service(task, model)
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert result['done'] and result['calls']=={'classify':1,'attribute':1,'extract':3}
    assert model.actions==['classify','attribute','extract','brief','guide']
    again=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert again==result and len(model.actions)==5
    with connect(task[0]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0]==1
        job=conn.execute('SELECT * FROM parse_jobs').fetchone()
        assert job['status']=='done' and job['l2_committed']==1


@pytest.mark.parametrize('bad', [fact(quote='fabricated'),fact(source_loc='missing'),fact(entity='stranger'),
                                fact(confidence='invalid'),fact(unit=None), {}, None])
def test_bad_candidate_is_quarantined_not_batch_failure(task,bad):
    reader=service(task,Model([{'facts':[bad,fact()], 'complete':True}]))
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done'] and len(result['unresolved'])==1 and len(result['fact_ids'])==1
    with connect(task[0]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0]==1


def test_budget_preserves_and_resume_no_duplicate(task):
    model=Model([{'facts':[fact()], 'complete':False},{'facts':[fact()], 'complete':True}])
    reader=service(task,model)
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':1})
    assert result['status']=='partial' and len(result['fact_ids'])==1
    assert model.actions==['classify','attribute','extract']
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert result['done'] and len(result['fact_ids'])==1


def test_zero_budget_never_calls(task):
    model=Model(); result=service(task,model).run(company_id='acc',job_id='job',limits={'classify':0,'attribute':0,'extract':0})
    assert not model.actions and result['status']=='partial'


def test_host_scope_cannot_be_changed(task):
    with pytest.raises(ValueError,match='scope'):
        service(task,Model()).run(company_id='stranger',job_id='job',limits={'classify':1,'attribute':1,'extract':3})


def test_reader_cannot_self_pass(task):
    task[1].quality_fingerprint = None
    with pytest.raises(ValueError,match='independent fidelity'):
        service(task,Model()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})


def test_document_mutation_invalidates_certificate(task):
    task[1].document['markdown'] += 'changed'
    with pytest.raises(ValueError,match='independent fidelity'):
        service(task,Model()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})


def test_failed_transaction_does_not_checkpoint_uncommitted_facts(task, monkeypatch):
    model=Model();reader=service(task,model)
    original=reader.facts.consolidate_candidates
    def fail(*args, **kwargs): raise RuntimeError('simulated commit failure')
    monkeypatch.setattr(reader.facts,'consolidate_candidates',fail)
    with pytest.raises(RuntimeError,match='simulated'):
        reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    with connect(task[0]) as conn:
        state=json.loads(conn.execute('SELECT content FROM memory_kv').fetchone()[0])
        assert not state['complete'] and not state['accepted'] and not state['fact_ids']
        assert conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0]==0
    monkeypatch.setattr(reader.facts,'consolidate_candidates',original)
    assert reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})['done']


def test_failed_transport_is_counted_and_no_fact_is_invented(task):
    class Failing:
        def execute(self, **kwargs): raise RuntimeError('transport unavailable')
    with pytest.raises(RuntimeError,match='transport'):
        service(task,Failing()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    with connect(task[0]) as conn:
        state=json.loads(conn.execute('SELECT content FROM memory_kv').fetchone()[0])
        assert state['calls']['classify']==1 and state['status']=='partial'
        assert conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0]==0


def test_all_rejected_never_done_and_can_correct_on_resume(task):
    model=Model([{'facts':[fact(quote='invented')],'complete':True}, {'facts':[fact()],'complete':True}])
    reader=service(task,model)
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':1})
    assert result['status']=='partial' and not result['complete'] and not result['fact_ids']
    assert result['correction_feedback']
    with connect(task[0]) as conn:
        assert conn.execute('SELECT l2_committed FROM parse_jobs').fetchone()[0]==0
    resumed=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert resumed['done'] and len(resumed['fact_ids'])==1


def test_empty_delivery_never_reports_done(task):
    result=service(task,Model([{'facts':[],'complete':True}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':1})
    assert result['status']=='partial' and not result['complete']


def test_subprocess_timeout_is_partial_and_preserves_committed_facts(task):
    import subprocess
    class TimeoutModel(Model):
        def execute(self, **kwargs):
            request=json.loads(kwargs['text'])
            if request['action']=='brief': raise subprocess.TimeoutExpired(['python'],180)
            return super().execute(**kwargs)
    reader=service(task,TimeoutModel())
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['status']=='partial' and result['complete'] and len(result['fact_ids'])==1
    assert result['stage_failure']['kind']=='employee_transport'
    assert result['calls']['extract']==2
    reader.model=Model()
    resumed=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert resumed['done'] and len(resumed['fact_ids'])==1


def test_employee_chooses_subentity_with_explicit_attribution_reference(task):
    result=service(task,Model([{'facts':[fact(entity='股东甲',attribution_index=0)],'complete':True}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done'] and result['accepted'][0]['entity']=='股东甲'


def test_employee_owned_remaining_coordinates_resume(task):
    class ProgressModel(Model):
        def execute(self, **kwargs):
            request=json.loads(kwargs['text'])
            if request['action']=='extract' and request['already_delivered']:
                assert 'document' not in request
                assert request['source_sections'][0]['source_loc']=='table'
                assert 'quote' not in request['already_delivered'][0]
            return super().execute(**kwargs)
    reader=service(task,ProgressModel([
        {'facts':[fact()],'complete':False,'remaining_source_locs':['table']},
        {'facts':[],'complete':True,'remaining_source_locs':[]}]))
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':1})
    assert result['status']=='partial' and result['remaining_source_locs']==['table']
    result=reader.run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert result['done'] and result['remaining_source_locs']==[] and len(result['fact_ids'])==1


def test_invalid_employee_progress_is_recoverable_not_completion(task):
    result=service(task,Model([{'facts':[fact()],'complete':True,'remaining_source_locs':['table']}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['status']=='partial' and not result['complete'] and result['stage_failure']


def test_required_seven_fields_accept_optional_type_missing_without_numeric_inference(task):
    item=fact();del item['value_type']
    result=service(task,Model([{'facts':[item],'complete':True}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done']
    with connect(task[0]) as conn:
        assert conn.execute('SELECT value_type FROM facts').fetchone()[0]=='string'


def test_single_bad_reading_item_does_not_block_valid_projection(task):
    class BadBrief(Model):
        def execute(self, **kwargs):
            request=json.loads(kwargs['text']);result=super().execute(**kwargs)
            if request['action']=='brief':
                result['items'].append({'topic':'坏条目','conclusion':'无出处量99万元','fact_ids':[request['facts'][0]['id']]})
            return result
    result=service(task,BadBrief()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done'] and len(result['summary_unresolved'])==1 and result['brief_done'] and result['guide_done']


def test_unknown_period_does_not_infer_a_year_or_drop_source_backed_fact(task):
    result=service(task,Model([{'facts':[fact(period='')],'complete':True}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done']
    with connect(task[0]) as conn:
        assert conn.execute('SELECT period FROM facts').fetchone()[0]=='unspecified'


def test_quote_must_lie_in_declared_row_not_anywhere_in_its_table(task):
    reader=service(task,Model())
    state={'company_id':'acc','ranges':[{'entity':'测试公司','start_line':3,'end_line':3}]}
    assert reader._verify(fact(),state) is not None
    assert reader._verify(fact(quote='| 指标 | 年份 |'),state) is None


def test_out_of_bounds_attribution_gets_one_counted_correction(task):
    class Correcting(Model):
        failed=False
        def execute(self,**kwargs):
            request=json.loads(kwargs['text'])
            if request['action']=='attribute' and not self.failed:
                self.failed=True
                return {'ranges':[{'entity':'测试公司','start_line':1,'end_line':99}]}
            if request['action']=='attribute':
                assert request['format_feedback']['last_line']==3
            return super().execute(**kwargs)
    result=service(task,Correcting()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':2,'extract':3})
    assert result['done'] and result['calls']['attribute']==2


def test_repeated_invalid_attribution_stops_without_clipping_or_crash(task):
    class Invalid(Model):
        def execute(self,**kwargs):
            if json.loads(kwargs['text'])['action']=='attribute':
                return {'ranges':[{'entity':'测试公司','start_line':1,'end_line':99}]}
            return super().execute(**kwargs)
    result=service(task,Invalid()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':8,'extract':3})
    assert result['status']=='partial' and result['calls']['attribute']==2 and result['ranges'] is None


def test_json_numeric_scalar_is_serialized_without_business_conversion(task):
    result=service(task,Model([{'facts':[fact(value=12.0,period=2024)],'complete':True}])).run(
        company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':3})
    assert result['done']
    with connect(task[0]) as conn:
        row=conn.execute('SELECT value,period,unit FROM facts').fetchone()
        assert tuple(row)==('12.0','2024','万元')


def test_guide_bad_reference_shape_gets_one_counted_model_correction(task):
    class CorrectingGuide(Model):
        failed=False
        def execute(self,**kwargs):
            request=json.loads(kwargs['text'])
            if request['action']=='guide' and not self.failed:
                self.failed=True
                return {'items':[{'conclusion':'概述','l1_entry_ids':[{'brief-1':'brief-1'}]}]}
            if request['action']=='guide':
                assert request['format_feedback']
            return super().execute(**kwargs)
    result=service(task,CorrectingGuide()).run(company_id='acc',job_id='job',limits={'classify':1,'attribute':1,'extract':4})
    assert result['done'] and result['calls']['extract']==4 and len(result['summary_unresolved'])==1
