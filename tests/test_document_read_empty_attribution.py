"""Empty employee attribution is auditable, not a backend content classifier."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT

PHASES=('classify','attribute','extract','plan','leaf')


def task(store):
    document=dict(markdown='文档标题\n待填写提示\n可回查的技术描述。',document_map=dict(elements=[
        dict(source_loc=f'docx:body/{i}/paragraph',start_line=i,end_line=i) for i in range(1,4)]),assets=[])
    d=Dossier(document,file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('empty-attribute',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()
    return d


class Employee:
    def __init__(self, *, invalid=(), empty_narrative=False, forged=False):
        self.invalid=list(invalid);self.empty_narrative=empty_narrative;self.forged=forged;self.calls=[]
    def execute(self,**kwargs):
        q=json.loads(kwargs['text']);action=q.pop('action')
        self.calls.append((action,deepcopy(q)))
        grant=q.get('window',{})
        if action=='classify':return dict(classification=dict(description='员工判断依据'))
        if action=='attribute':
            if grant['grant_id']=='0' and self.invalid:return self.invalid.pop(0)
            if grant['grant_id'] in ('0','1') or self.empty_narrative:
                return dict(ranges=[],outcome='no_attribution',reason='本窗没有可以明确授予事实提取的实体范围')
            return dict(ranges=[dict(entity='资料主体',start_line=3,end_line=3)])
        if action=='extract':
            if self.forged and grant['grant_id']=='0':
                return dict(facts=[dict(entity='伪造默认主体',attribute='描述',period='unknown',value='文档标题',unit='',
                    quote='文档标题',source_loc=grant['source_loc'],module_key='info')],complete=True)
            return dict(facts=[],complete=True)
        if action=='plan':
            if grant['grant_id'] in ('0','1'):
                return dict(outcome='no_content',assignments=[],reason='员工判断只有提示，没有可归叶内容', retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
            return dict(outcome='assigned',assignments=[dict(leaf_code='L-bizov-07',ranges=grant['ranges'], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
        assert action=='leaf'
        g=q['grants'][0];current=q['current']
        return dict(leaf_code=q['target']['leaf_code'],base_version=current['version'],base_sha=current['content_sha'],
            blocks=current['blocks']+[dict(block_id='narrative',text=g['md']+'[^e]',evidence=[dict(ref='e',grant_id=g['grant_id'],
                file_hash=SHA,original_name='sample.docx',source_loc=g['source_loc'],start_line=3,end_line=3,fact_ids=[])])],
            conflicts=[],note='只归叶已读的定性叙述')


def run(store,d,employee,**limits):
    reader=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=employee)
    return reader.run(company_id=COMPANY,job_id='empty-attribute',limits={**dict.fromkeys(PHASES,6),**limits})


@pytest.mark.parametrize('empty_narrative',[False,True])
def test_empty_attribution_titles_continue_and_no_facts_narrative_publishes(real_store,empty_narrative):
    d=task(real_store);employee=Employee(empty_narrative=empty_narrative)
    state=run(real_store,d,employee)
    assert state['done'] and not state['mapped_md']['technical_rejections']
    records=state['mapped_md']['records']
    assert [r['status'] for r in records]==['no_content','no_content','processed']
    for r in records[:2]:
        assert r['preparation']['ranges']==[] and r['preparation']['attribution_decision']['outcome']=='no_attribution'
        assert r['preparation']['attribution_decision']['reason']
    assert not state['fact_ids'] and not state['accepted']
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1 # fixture baseline only
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
    assert len(state['leaves_built'])==1 and state['leaves_built'][0]['leaf_code']=='L-bizov-07'
    assert state['calls']==dict(classify=3,attribute=3,extract=3,plan=3,leaf=1)
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0


@pytest.mark.parametrize('invalid',[dict(ranges=[]),dict(ranges=[],outcome='no_attribution',reason=' '),
    dict(ranges={}),dict(ranges=[],outcome='attributed',reason='错误结构')])
def test_bad_empty_or_invalid_shape_gets_one_charged_format_correction(real_store,invalid):
    d=task(real_store);employee=Employee(invalid=[invalid])
    state=run(real_store,d,employee)
    assert state['done'] and state['calls']['attribute']==4
    calls=[q for action,q in employee.calls if action=='attribute' and q['window']['grant_id']=='0']
    assert len(calls)==2 and calls[1]['format_feedback']['error']
    prep=state['mapped_md']['records'][0]['preparation']
    assert len(prep['attribution_rejections'])==1 and prep['attribution_rejections'][0]['response']==invalid
    assert prep['ranges']==[] and prep['attribution_decision']['reason']


@pytest.mark.parametrize('budget',[1,2])
def test_format_limit_partial_reconstruct_resume_preserves_stage_and_bill(real_store,budget):
    d=task(real_store);employee=Employee(invalid=[dict(ranges=[])]*budget)
    state=run(real_store,d,employee,attribute=budget)
    assert state['status']=='partial' and not state.get('done')
    prep=state['mapped_md']['records'][0]['preparation']
    assert prep['classification'] and prep['ranges'] is None and not prep['complete']
    assert len(prep['attribution_rejections'])==budget
    assert state['calls']['attribute']==budget and state['calls']['classify']==1
    assert not state['calls']['extract'] and not state['fact_ids']
    resumed=run(real_store,d,employee)
    assert resumed['done'] and resumed['calls']['attribute']==budget+3
    assert resumed['calls']['classify']==3 and resumed['calls']['extract']==3
    assert sum(1 for a,q in employee.calls if a=='classify' and q['window']['grant_id']=='0')==1
    assert len(resumed['mapped_md']['records'][0]['preparation']['attribution_rejections'])==budget
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==1


def test_empty_attribution_cannot_authorize_nonempty_fact(real_store):
    d=task(real_store);employee=Employee(forged=True)
    state=run(real_store,d,employee,extract=1)
    assert state['status']=='partial' and not state.get('done')
    assert not state['fact_ids'] and not state['accepted']
    prep=state['mapped_md']['records'][0]['preparation']
    assert prep['ranges']==[] and not prep['complete'] and prep['feedback']
    assert 'attributed range' in prep['feedback'][0]['blocking']
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0
