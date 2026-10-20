"""Adjacent authority, target-empty receipts, unchanged numeric matching."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.facts_tree.md_reader import MDWindows, MappedMDService
from app.facts_tree.md_publish import MDLeafPublisher
from app.facts_tree.quote_sources import DossierQuoteResolver
from app.facts_tree.leaf_store import LeafRejected
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT

PHASES=('classify','attribute','extract','plan','leaf')
LOC='docx:body/1/table'


def document():
    d=Dossier(dict(markdown='| 项目 | 内容 |\n| --- | --- |\n| 日期 | 2014-09-09 |\n| 名称 | 测试 |',
        document_map=dict(elements=[dict(source_loc=LOC,start_line=1,end_line=4)]),assets=[]),file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint
    return d


def setup(store):
    d=document()
    store.quote_resolver=DossierQuoteResolver({SHA:d.document})
    window=MDWindows(d);g=window.grant(window.windows[0],'sample.docx')
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('grant-receipt',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()
    return d,g,MDLeafPublisher(store)


def block(grant,*,text='日期为2014-09-09[^e]。',start=1,end=4,facts=None):
    return dict(block_id='date-block',text=text,evidence=[dict(ref='e',grant_id=grant['grant_id'],file_hash=SHA,
        original_name='sample.docx',source_loc=LOC,start_line=start,end_line=end,fact_ids=facts or [])])


def publish(pub,g,b,op='test'):
    current=pub.current(COMPANY,'L-bizov-07')
    return pub.submit(company=COMPANY,code='L-bizov-07',operation_id=op,base_version=current['version'],base_sha=current['content_sha'],
        blocks=current['blocks']+[b],conflicts=[],grants={g['grant_id']:g},job_id='grant-receipt',note='员工提交')


class Model:
    def __init__(self,*,ranges=None,no_content=False,bad=None,assert_facts=None):
        self.ranges=ranges;self.no_content=no_content;self.bad=bad;self.assert_facts=assert_facts;self.requests=[]
    def execute(self,**kwargs):
        q=json.loads(kwargs['text']);a=q.pop('action');self.requests.append((a,deepcopy(q)))
        if a=='classify':return dict(classification=dict(description='员工说明'))
        if a=='attribute':return dict(ranges=[dict(entity='资料主体',start_line=x,end_line=y) for x,y in q['window']['ranges']])
        if a=='extract':return dict(facts=[],complete=True)
        if a=='plan':return dict(outcome='assigned',assignments=[dict(leaf_code='L-bizov-07',ranges=self.ranges or [[1,4]], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
        assert a=='leaf'
        if self.assert_facts:self.assert_facts(q)
        if self.no_content:
            result=dict(leaf_code='L-bizov-07',outcome='no_content',assignments=[],reason='员工判断本段没有可归该叶内容')
            if self.bad is not None:result.update(self.bad)
            return result
        current=q['current'];g=q['grants'][0]
        return dict(leaf_code='L-bizov-07',base_version=current['version'],base_sha=current['content_sha'],
            blocks=current['blocks']+[block(g,start=g['ranges'][0][0],end=g['ranges'][-1][1])],conflicts=[],note='日期原格式')


def reader(store,d,model):
    return DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=model)


def test_default_adjacent_header_data_allocation_and_evidence_span_write(real_store):
    d,g,pub=setup(real_store);assert g['ranges']==[[1,2],[3,4]]
    model=Model()
    state=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert state['done'] and len(state['leaves_built'])==1
    selected=state['mapped_md']['records'][0]['assignments'][0]['grant']
    assert selected['ranges']==[[1,4]] and selected['complete_coordinate']
    assert pub.current(COMPANY,'L-bizov-07')['blocks'][0]['evidence'][0]['end_line']==4


@pytest.mark.parametrize('bounds,gap', [([1,4],True),([1,10**15],False),([True,4],False),([1,'4'],False),([4,1],False)])
def test_bad_evidence_gap_huge_wrong_type_zero_side_effects(real_store,bounds,gap):
    d,g,pub=setup(real_store)
    if gap:g.update(ranges=[[1,2],[4,4]],lines={k:v for k,v in g['lines'].items() if k!='3'},complete_coordinate=False)
    with pytest.raises(LeafRejected,match='evidence line range outside read grant'):
        publish(pub,g,block(g,start=bounds[0],end=bounds[1]))
    for table in ('leaf_revisions','facts_tree_leaf_index','fact_leaf_links','m1_documents','m1_chunks','md_leaf_operations','md_leaf_todos'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


@pytest.mark.parametrize('bounds,gap', [([1,4],True),([1,10**15],False),([True,4],False),([1,'4'],False),([4,1],False)])
def test_bad_allocation_gap_huge_wrong_type_never_reaches_leaf(real_store,bounds,gap,monkeypatch):
    d,g,pub=setup(real_store)
    if gap:
        original=MappedMDService.__init__
        def split(self,*args,**kw):
            original(self,*args,**kw)
            self.windows=MDWindows(d,max_lines=3)
        monkeypatch.setattr(MappedMDService,'__init__',split)
        class Second(Model):
            def execute(self,**kw):
                q=json.loads(kw['text'])
                if q['action']=='plan' and q['window']['grant_id']=='0':
                    return dict(outcome='no_content',assignments=[],reason='员工选择继续下一窗', retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
                return super().execute(**kw)
        model=Second(ranges=[bounds])
    else:model=Model(ranges=[bounds])
    with pytest.raises(LeafRejected,match='allocation outside read window'):
        reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert all(a!='leaf' for a,q in model.requests)
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_selected_subset_cannot_inherit_complete_coordinate_or_legacy_fact(real_store):
    d,g,pub=setup(real_store)
    real_store.db.execute('UPDATE facts SET source_span=?,value=?,unit=?,period=?,value_type=?',
        (json.dumps(dict(source_loc=LOC,quote='2014-09-09')),'2014-09-09','','2014','string'))
    real_store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,value_type,source_file,source_span,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(COMPANY,'资料主体','定位日期','2014','2014-09-09','','string',SHA,
        json.dumps(dict(source_loc=LOC,quote='2014-09-09',ranges=[[3,3]],mapped_fingerprint=d.fingerprint)),'bizov','now'))
    real_store.db.commit()
    def check(q):
        assert not q['grants'][0]['complete_coordinate']
        assert [f['fact_id'] for f in q['facts']]==[2]
    # Recover an old checkpoint with the formerly inherited True flag.
    first=reader(real_store,d,Model(ranges=[[3,3]])).run(company_id=COMPANY,job_id='grant-receipt',limits={**dict.fromkeys(PHASES,4),'leaf':0})
    assert first['status']=='partial'
    first['mapped_md']['records'][0]['assignments'][0]['grant']['complete_coordinate']=True
    real_store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',(json.dumps(first),'sam://kv/rt/md-direct/grant-receipt'));real_store.db.commit()
    model=Model(ranges=[[3,3]],assert_facts=check)
    state=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert state['done'] and not state['mapped_md']['records'][0]['assignments'][0]['grant']['complete_coordinate']


@pytest.mark.parametrize('existing',[False,True])
def test_leaf_no_content_keeps_original_or_no_leaf_and_replays_once(real_store,existing):
    d,g,pub=setup(real_store)
    if existing:publish(pub,g,block(g),op='previous')
    prior=pub.current(COMPANY,'L-bizov-07')
    model=Model(no_content=True)
    r=reader(real_store,d,model)
    state=r.run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    a=state['mapped_md']['records'][0]['assignments'][0]
    assert a['status']=='no_content' and a['draft']['outcome']=='no_content'
    assert a['receipt']['job_id']=='grant-receipt' and a['receipt']['reason'] and not a['receipt']['changed']
    assert state['leaves_empty']==[a['receipt']] and not state['leaves_built'] and not state['leaves_failed']
    assert pub.current(COMPANY,'L-bizov-07')==prior
    calls=len(model.requests)
    again=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert len(model.requests)==calls and again['leaves_empty']==state['leaves_empty']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==int(existing)
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==int(existing)


@pytest.mark.parametrize('bad',[dict(reason=' '),dict(reason=[]),dict(leaf_code='L-bizov-08'),dict(blocks=[{}]),dict(assignments=None)])
def test_invalid_empty_receipt_rejected_then_explicit_resume_retains_audit(real_store,bad):
    d,g,pub=setup(real_store);model=Model(no_content=True,bad=bad)
    state=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4),correction_attempts=0)
    a=state['mapped_md']['records'][0]['assignments'][0]
    assert a['status']=='unresolved' and len(a['rejected_drafts'])==1 and not state['leaves_empty']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    model.bad=None
    state=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4),resume_unresolved=True)
    a=state['mapped_md']['records'][0]['assignments'][0]
    assert a['status']=='no_content' and len(a['rejected_drafts'])==1 and state['calls']['leaf']==2
    assert state['calls']['classify']==state['calls']['attribute']==state['calls']['extract']==state['calls']['plan']==1


def test_date_original_passes_rewrite_reports_actual_literal_block_and_row(real_store):
    d,g,pub=setup(real_store)
    real_store.db.execute('UPDATE facts SET source_span=?,value=?,unit=?,period=?,value_type=?',
        (json.dumps(dict(source_loc=LOC,quote='2014-09-09')),'2014-09-09','','2014','string'));real_store.db.commit()
    rewritten=block(g,text='日期为2014年9月9日[^e]。',facts=[1])
    with pytest.raises(LeafRejected) as error:publish(pub,g,rewritten,op='bad-date')
    assert "literal='9月'" in str(error.value) and 'block date-block' in str(error.value) and 'row 1' in str(error.value)
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    result=publish(pub,g,block(g,facts=[1]),op='original-date')
    assert result['changed'] and '2014-09-09' in pub.current(COMPANY,'L-bizov-07')['markdown']


def test_leaf_no_content_checkpoint_failure_reconstructs_without_repeating_leaf(real_store,monkeypatch):
    d,g,pub=setup(real_store);model=Model(no_content=True);r=reader(real_store,d,model)
    original=r._save;fired=False
    def fail(conn,state):
        nonlocal fired
        assignments=[a for rec in state.get('mapped_md',{}).get('records',[]) for a in rec['assignments']]
        if not fired and any(a['status']=='no_content' for a in assignments):
            fired=True
            raise OSError('injected empty receipt checkpoint interruption')
        return original(conn,state)
    monkeypatch.setattr(r,'_save',fail)
    with pytest.raises(OSError,match='empty receipt checkpoint'):
        r.run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert fired
    state=reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    assert state['done'] and len(state['leaves_empty'])==1
    assert state['calls']['leaf']==1 and sum(a=='leaf' for a,q in model.requests)==1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==0


def test_old_generation_cannot_save_leaf_no_content_receipt(real_store):
    from app.document_read.execution import LeaseLost
    d,g,pub=setup(real_store)
    class Old(Model):
        def execute(self,**kw):
            result=super().execute(**kw)
            if json.loads(kw['text'])['action']=='leaf':
                real_store.db.execute('UPDATE md_job_leases SET generation=generation+1 WHERE job_id=?',('grant-receipt',))
                real_store.db.commit()
            return result
    model=Old(no_content=True)
    with pytest.raises(LeaseLost):
        reader(real_store,d,model).run(company_id=COMPANY,job_id='grant-receipt',limits=dict.fromkeys(PHASES,4))
    saved=json.loads(real_store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/grant-receipt',)).fetchone()[0])
    a=saved['mapped_md']['records'][0]['assignments'][0]
    assert a['status']=='allocated' and 'receipt' not in a and saved['calls']['leaf']==1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
