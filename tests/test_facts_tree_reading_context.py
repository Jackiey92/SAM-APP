"""Protocol-3 rolling source authority, not a domain-specific context selector."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.facts_tree.md_reader import MappedMDService
from app.facts_tree.md_publish import MDLeafPublisher
from app.facts_tree.leaf_store import LeafRejected
from app.facts_tree.reading_context import field_evidence as verify_field_evidence
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY,SHA
from tests.test_document_read import ROOT

PHASES=('classify','attribute','extract','plan','leaf')
PERIODS=['2024-01-01','2025-01-01','2025-01-01']
VALUES=['60','82','70']


def dossier(*,units=False):
    texts=[];elements=[];line=1
    if units:
        pieces=['不相关记录日期：2011-02-03。','测量单位：毫米。','样品宽度。','| 对象 | 读数 |\n| --- | --- |\n| 样品A | 7 |']
    else:
        pieces=[]
        for n,(period,value) in enumerate(zip(PERIODS,VALUES)):
            pieces += [f'统计时点：{period}。',f'甲方股份比例 · 材料{chr(65+n)}。',f'| 股东 | 比例 |\n| --- | --- |\n| 甲方 | {value}% |']
    for n,text in enumerate(pieces):
        texts.append(text);end=line+len(text.splitlines())-1
        elements.append(dict(source_loc=f'docx:body/{n+1}/'+('table' if '\n' in text else 'paragraph'),start_line=line,end_line=end))
        line=end+1
    d=Dossier(dict(markdown='\n'.join(texts),document_map=dict(elements=elements),assets=[]),file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint
    return d


def setup(store,*,units=False):
    d=dossier(units=units)
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('context-job',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()
    return d


def proof(field,g,value):
    return dict(field=field,grant_id=g['grant_id'],company_id=COMPANY,file_hash=SHA,mapped_fingerprint=g['mapped_fingerprint'],
        source_loc=g['source_loc'],start_line=g['ranges'][0][0],end_line=g['ranges'][-1][1],quote=value)


def source(ref,g,fids):
    return dict(ref=ref,grant_id=g['grant_id'],file_hash=SHA,original_name='sample.docx',source_loc=g['source_loc'],
        start_line=g['ranges'][0][0],end_line=g['ranges'][-1][1],fact_ids=fids)


class Employee:
    def __init__(self,d,*,units=False,mutate=None,omit_context_evidence=False):
        self.d=d;self.units=units;self.mutate=mutate;self.omit_context_evidence=omit_context_evidence;self.requests=[]
    def execute(self,**kw):
        q=json.loads(kw['text']);action=q.pop('action');self.requests.append((action,deepcopy(q)))
        g=q.get('window') or q['grants'][0];n=int(g['grant_id'])
        def strings(x):
            if isinstance(x,str):yield x
            elif isinstance(x,dict):
                for value in x.values():yield from strings(value)
            elif isinstance(x,list):
                for value in x:yield from strings(value)
        assert self.d.document['markdown'] not in list(strings(q))
        if action!='leaf':
            assert [int(c['grant_id']) for c in q['context_grants']]==list(range(max(0,n-2),n))
            # Body authority, not navigation coordinates: no current/future grant in context.
            for c in q['context_grants']:
                assert c['company_id']==COMPANY and c['mapped_fingerprint']==self.d.fingerprint
                assert c['md']==self.d.read(c['source_loc'])
        is_table=(n==3) if self.units else n%3==2
        if action=='classify':return dict(classification=dict(description='员工阅读并判断关联'))
        if action=='attribute':return dict(ranges=[dict(entity='样品A' if self.units else '甲方',start_line=a,end_line=b) for a,b in g['ranges']])
        if action=='extract':
            if not is_table:return dict(facts=[],complete=True)
            ctx=q['context_grants'][0]
            period='unknown' if self.units else PERIODS[n//3]
            value='7' if self.units else VALUES[n//3]
            unit='毫米' if self.units else '%'
            item=dict(entity='样品A' if self.units else '甲方',attribute='宽度' if self.units else '比例',period=period,
                value=value,unit=unit,value_type='number',quote=f'| 样品A | 7 |' if self.units else f'| 甲方 | {value}% |',
                source_loc=g['source_loc'],module_key='bizov' if self.units else 'info',
                field_evidence=[proof('unit' if self.units else 'period',ctx,unit if self.units else period)])
            if self.mutate:self.mutate(item,q)
            return dict(facts=[item],complete=True)
        code='L-bizov-07' if self.units else 'L-info-04'
        if action=='plan':
            if not is_table:return dict(outcome='no_content',assignments=[],reason='本窗保留为后续限定上下文，不造独立叶', retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
            return dict(outcome='assigned',assignments=[dict(leaf_code=code,ranges=g['ranges'],context_windows=[c['grant_id'] for c in q['context_grants']])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
        assert action=='leaf' and is_table
        assert [int(c['grant_id']) for c in q['grants']]==[n,n-2,n-1]
        current=q['current'];ctx=q['grants'][1]
        period='unknown' if self.units else PERIODS[n//3]
        value='7' if self.units else VALUES[n//3]
        fids=[f['fact_id'] for f in q['facts']]
        for f in q['facts']:
            assert f['period']==period and f['field_evidence'][0]['grant_id']==ctx['grant_id']
        text=(f'样品A宽度7毫米[^t{n}][^c{n}]。' if self.units else f'截至{period}，甲方股份比例{value}%[^t{n}][^c{n}]。')
        ev=[source(f't{n}',g,fids)]
        if not self.omit_context_evidence:ev.append(source(f'c{n}',ctx,[]))
        new=dict(block_id=f'block-{n}',text=text,evidence=ev)
        conflicts=[];blocks=deepcopy(current['blocks'])
        # Employee fixture decides cross-period independence vs same-period conflict.
        if not self.units and n==8:
            conflicts=[dict(old_block_id='block-5',new_block=new,reason='员工判断同一统计时点数值冲突，保留原内容')]
        else:blocks.append(new)
        return dict(leaf_code=code,base_version=current['version'],base_sha=current['content_sha'],blocks=blocks,conflicts=conflicts,note='由员工判断期间/单位与关系')


def run(store,d,employee,**extra):
    limits=extra.pop('limits',dict.fromkeys(PHASES,20))
    return DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=employee).run(
        company_id=COMPANY,job_id='context-job',limits=limits,**extra)


def test_dates_titles_tables_keep_period_double_sources_and_same_period_todo(real_store):
    d=setup(real_store);employee=Employee(d)
    state=run(real_store,d,employee)
    assert state['done'] and state['mapped_md']['protocol']==3 and not state['mapped_md']['technical_rejections']
    assert state['calls']==dict(classify=9,attribute=9,extract=9,plan=9,leaf=3)
    current=MDLeafPublisher(real_store).current(COMPANY,'L-info-04')
    assert len(current['blocks'])==2 and current['version']==2
    for block,period,value,window in zip(current['blocks'],PERIODS,VALUES,[2,5]):
        assert period in block['text'] and value+'%' in block['text']
        assert len(block['evidence'])==2 and {int(e['grant_id']) for e in block['evidence']}=={window,window-2}
    todo=json.loads(real_store.db.execute('SELECT payload FROM md_leaf_todos').fetchone()[0])
    assert '2025-01-01' in todo['old']['text'] and '82%' in todo['old']['text']
    assert '2025-01-01' in todo['new']['text'] and '70%' in todo['new']['text']
    facts=real_store.db.execute("SELECT * FROM facts WHERE attribute='比例' ORDER BY id").fetchall()
    assert [(f['period'],f['value']) for f in facts]==[('2024-01-01','60'),('2025-01-01','82')]
    for f in facts:
        span=json.loads(f['source_span']);assert span['reading_protocol']==3 and span['field_evidence'][0]['field']=='period'
    before=len(employee.requests);again=run(real_store,d,employee)
    assert len(employee.requests)==before and again['calls']==state['calls']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0]==1
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0


def test_unrelated_date_not_auto_applied_unit_context_generic(real_store):
    d=setup(real_store,units=True);employee=Employee(d,units=True)
    state=run(real_store,d,employee)
    assert state['done']
    fact=real_store.db.execute("SELECT * FROM facts WHERE attribute='宽度'").fetchone()
    assert fact['period']=='unknown' and fact['unit']=='毫米' and fact['value']=='7'
    current=MDLeafPublisher(real_store).current(COMPANY,'L-bizov-07')
    assert '2011' not in current['blocks'][0]['text'] and '7毫米' in current['blocks'][0]['text']
    assert {e['grant_id'] for e in current['blocks'][0]['evidence']}=={'3','1'}


@pytest.mark.parametrize('tamper',['future','company','fingerprint','locator','huge','gap','quote','period','missing','context_as_primary'])
def test_bad_field_context_rejected_zero_new_facts_or_leaves(real_store,tamper):
    d=setup(real_store)
    def mutate(item,q):
        e=item['field_evidence'][0]
        if tamper=='future':e['grant_id']='8'
        elif tamper=='company':e['company_id']='another-company'
        elif tamper=='fingerprint':e['mapped_fingerprint']='b'*64
        elif tamper=='locator':e['source_loc']='docx:body/999/paragraph'
        elif tamper=='huge':e['end_line']=10**15
        elif tamper=='gap':e['end_line']=e['start_line']+2
        elif tamper=='quote':e['quote']='invented date'
        elif tamper=='period':item['period']='2099-01-01'
        elif tamper=='missing':item.pop('field_evidence')
        else:item['source_loc']=e['source_loc'];item['quote']=e['quote']
    state=run(real_store,d,Employee(d,mutate=mutate),limits=dict(classify=20,attribute=20,extract=3,plan=20,leaf=20))
    assert state['status']=='partial' and not state['fact_ids'] and state['unresolved']
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
    for table in ('leaf_revisions','md_leaf_operations','md_leaf_todos','m1_documents','m1_chunks'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_fact_period_context_must_be_cited_in_same_block_then_repair(real_store):
    d=setup(real_store);employee=Employee(d,omit_context_evidence=True)
    state=run(real_store,d,employee,limits=dict(classify=3,attribute=3,extract=3,plan=3,leaf=3),correction_attempts=0)
    assignment=state['mapped_md']['records'][2]['assignments'][0]
    assert assignment['status']=='unresolved' and 'field-context' in assignment['reason']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    employee.omit_context_evidence=False
    # Finish only this failed leaf, then stop before the next window's classify.
    state=run(real_store,d,employee,limits=dict(classify=3,attribute=3,extract=3,plan=3,leaf=3),resume_unresolved=True)
    assert state['status']=='partial' and len(state['leaves_built'])==1
    assert state['calls']==dict(classify=3,attribute=3,extract=3,plan=3,leaf=2)


def test_reconstructed_reader_keeps_context_and_does_not_rebill_completed_windows(real_store):
    d=setup(real_store);employee=Employee(d)
    first=run(real_store,d,employee,limits=dict.fromkeys(PHASES,3))
    assert first['status']=='partial' and first['mapped_md']['cursor']==3 and first['calls']['leaf']==1
    before=len(employee.requests)
    final=run(real_store,d,employee)
    assert final['done'] and final['calls']==dict(classify=9,attribute=9,extract=9,plan=9,leaf=3)
    assert employee.requests[before][0]=='classify' and employee.requests[before][1]['window']['grant_id']=='3'
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==3


@pytest.mark.parametrize('tamper',['company','fingerprint','lines'])
def test_forged_persisted_read_context_rejected_before_new_transport(real_store,tamper):
    d=setup(real_store);employee=Employee(d)
    state=run(real_store,d,employee,limits=dict.fromkeys(PHASES,2))
    grant=state['mapped_md']['records'][0]['grant']
    if tamper=='company':grant['company_id']='other'
    elif tamper=='fingerprint':grant['mapped_fingerprint']='b'*64
    else:grant['lines']['1']='invented source'
    real_store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',(json.dumps(state),'sam://kv/rt/md-direct/context-job'));real_store.db.commit()
    before=len(employee.requests)
    with pytest.raises(LeafRejected,match='context scope/fingerprint/lines differ'):
        run(real_store,d,employee)
    assert len(employee.requests)==before and real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_oversized_context_unresolved_not_truncated_or_declared_read(real_store,monkeypatch):
    import app.facts_tree.reading_context as context
    d=setup(real_store);employee=Employee(d)
    monkeypatch.setattr(context,'MAX_CONTEXT_BYTES',100)
    state=run(real_store,d,employee)
    assert state['mapped_md']['unresolved_readings'] and not state['mapped_md']['content_complete']
    assert all(q['window']['grant_id']=='0' for a,q in employee.requests)
    assert all('grant' not in r for r in state['mapped_md']['records'][1:])
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_protocol2_checkpoint_not_upgraded_or_rewritten_even_with_retry(real_store):
    d=setup(real_store);employee=Employee(d)
    state=run(real_store,d,employee,limits=dict.fromkeys(PHASES,0))
    state['mapped_md']['protocol']=2
    raw=json.dumps(state)
    real_store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',(raw,'sam://kv/rt/md-direct/context-job'));real_store.db.commit()
    before=len(employee.requests)
    with pytest.raises(ValueError,match='no automatic upgrade'):
        run(real_store,d,employee,resume_unresolved=True,renew_deadline_seconds=300)
    assert len(employee.requests)==before
    assert real_store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/context-job',)).fetchone()[0]==raw
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_whole_request_bound_partial_before_transport_or_charge(real_store,monkeypatch):
    import app.facts_tree.reading_context as context
    d=setup(real_store);employee=Employee(d)
    monkeypatch.setattr(context,'MAX_READING_REQUEST_BYTES',100)
    before=real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]
    state=run(real_store,d,employee)
    assert not state.get('done') and state['reading_limit']['action']=='classify'
    assert not employee.requests and not any(state['calls'].values())
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==before
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


@pytest.mark.parametrize('selection',[['8'],['2'],['0','1','2'],['0','0'],[False]])
def test_plan_cannot_select_future_current_or_malformed_context(real_store,selection):
    d=setup(real_store)
    class InvalidSelection(Employee):
        def execute(self,**kw):
            result=super().execute(**kw)
            if result.get('outcome')=='assigned':
                result['assignments'][0]['context_windows']=selection
            return result
    employee=InvalidSelection(d)
    if selection in (['8'],['2']):
        with pytest.raises(LeafRejected,match='context window'):
            run(real_store,d,employee)
    else:
        state=run(real_store,d,employee)
        record=state['mapped_md']['records'][2]
        assert state['status']=='partial' and record['plan_format_blocked']
        assert len(record['plan_rejected_receipts'])==2 and not record['assignments']
        assert 'assignments[0].context_windows' in record['plan_rejected_receipts'][0]['error']
    assert not any(action=='leaf' for action,_ in employee.requests)
    for table in ('leaf_revisions','md_leaf_operations','md_leaf_todos','m1_documents'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def extended_dossier():
    pieces=['统计时点：2024-01-01。']+['独立阅读材料。']*5+[
        '| 股东 | 比例 |\n| --- | --- |\n| 甲方 | 60% |',
        '| 股东 | 比例 |\n| --- | --- |\n| 甲方 | 82% |',
        '统计时点：2025-01-01。']+['其它阅读材料。']*4+[
        '| 股东 | 比例 |\n| --- | --- |\n| 甲方 | 70% |','话题结束。','新主题。']
    elements=[];line=1
    for n,p in enumerate(pieces):
        end=line+len(p.splitlines())-1
        elements.append(dict(source_loc=f'docx:body/{n+1}/'+('table' if '\n' in p else 'paragraph'),start_line=line,end_line=end))
        line=end+1
    d=Dossier(dict(markdown='\n'.join(pieces),document_map=dict(elements=elements),assets=[]),file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint
    return d


class RetainingEmployee:
    def __init__(self,d):self.d=d;self.requests=[]
    def execute(self,**kw):
        q=json.loads(kw['text']);action=q.pop('action');self.requests.append((action,deepcopy(q)))
        g=q.get('window') or q['grants'][0];n=int(g['grant_id'])
        if action!='leaf':
            retained= '0' if 1<=n<=8 else ('8' if 9<=n<=14 else None)
            expected=list(dict.fromkeys([str(i) for i in range(max(0,n-2),n)]+([retained] if retained else [])))
            assert [c['grant_id'] for c in q['context_grants']]==expected
            assert len(q['context_grants'])<=4
            for c in q['context_grants']:
                assert int(c['grant_id'])<n and c['md']==self.d.read(c['source_loc'])
            assert self.d.document['markdown'] not in json.dumps(q,ensure_ascii=False)
        table=n in (6,7,13)
        if action=='classify':return dict(classification=dict(description='员工判断关联'))
        if action=='attribute':return dict(ranges=[dict(entity='甲方',start_line=a,end_line=b) for a,b in g['ranges']])
        if action=='extract':
            if not table:return dict(facts=[],complete=True)
            ctx=next(c for c in q['context_grants'] if c['grant_id']==('0' if n<8 else '8'))
            period='2024-01-01' if n<8 else '2025-01-01';value={6:'60',7:'82',13:'70'}[n]
            return dict(facts=[dict(entity='甲方',attribute='比例',period=period,value=value,unit='%',value_type='number',
                quote=f'| 甲方 | {value}% |',source_loc=g['source_loc'],module_key='info',field_evidence=[proof('period',ctx,period)])],complete=True)
        if action=='plan':
            result=dict(outcome='assigned' if table else 'no_content',assignments=[],reason='员工判断是否有新增内容', retain_context_windows=q['retained_context_windows'], retention_reason='离线员工显式判断保留或释放原文限定')
            if n in (0,8):result['retain_context_windows']=[str(n)]
            if n==6:result['retain_context_windows']=['0','6']
            if n==14:result['retain_context_windows']=[]
            if table:result['assignments']=[dict(leaf_code='L-info-04',ranges=g['ranges'],context_windows=['0' if n<8 else '8'])]
            return result
        assert action=='leaf' and table
        assert [c['grant_id'] for c in q['grants']]==[str(n),'0' if n<8 else '8']
        ctx=q['grants'][1];period='2024-01-01' if n<8 else '2025-01-01';value={6:'60',7:'82',13:'70'}[n]
        new=dict(block_id=f'long-{n}',text=f'截至{period}，甲方股份比例{value}%[^t{n}][^c{n}]。',
            evidence=[source(f't{n}',g,[f['fact_id'] for f in q['facts']]),source(f'c{n}',ctx,[])])
        blocks=deepcopy(q['current']['blocks']);conflicts=[]
        if n==7:conflicts=[dict(old_block_id='long-6',new_block=new,reason='员工判断同一统计时点矛盾')]
        else:blocks.append(new)
        return dict(leaf_code='L-info-04',base_version=q['current']['version'],base_sha=q['current']['content_sha'],blocks=blocks,conflicts=conflicts,note='按原文期间比较')


def test_retained_qualifier_survives_long_gap_conflict_new_period_release_and_resume(real_store):
    setup(real_store);d=extended_dossier();employee=RetainingEmployee(d)
    # First table plan saves selection and allocation, leaf cannot transport yet.
    state=run(real_store,d,employee,limits=dict(classify=30,attribute=30,extract=30,plan=30,leaf=0))
    assert state['mapped_md']['cursor']==6
    assert state['mapped_md']['records'][6]['status']=='allocated'
    snapshot=deepcopy(state['mapped_md']['records'][6]['rolling_context'])
    calls=deepcopy(state['calls'])
    state=run(real_store,d,employee,limits=dict.fromkeys(PHASES,30))
    assert state['done'] and state['mapped_md']['records'][6]['rolling_context']==snapshot
    assert state['calls']['plan']==16 and calls['plan']==7
    current=MDLeafPublisher(real_store).current(COMPANY,'L-info-04')
    assert len(current['blocks'])==2 and current['version']==2
    assert '2024-01-01' in current['blocks'][0]['text'] and '2025-01-01' in current['blocks'][1]['text']
    assert {e['grant_id'] for e in current['blocks'][1]['evidence']}=={'13','8'}
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0]==1
    before=len(employee.requests);again=run(real_store,d,employee,limits=dict.fromkeys(PHASES,30))
    assert len(employee.requests)==before and again['calls']==state['calls']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2


@pytest.mark.parametrize('selection',[['8'],['missing'],False,['0','0'],['0','1','2'],[{}]])
def test_invalid_retention_rejected_before_new_writes(real_store,selection):
    d=setup(real_store)
    class Invalid(Employee):
        def execute(self,**kw):
            result=super().execute(**kw)
            result['retain_context_windows']=selection
            return result
    if selection in (['8'],['missing']):
        with pytest.raises(LeafRejected,match='retain'):
            run(real_store,d,Invalid(d))
    else:
        state=run(real_store,d,Invalid(d))
        record=state['mapped_md']['records'][0]
        assert state['status']=='partial' and record['plan_format_blocked']
        assert len(record['plan_rejected_receipts'])==2 and not record['assignments']
        assert 'response.retain_context_windows' in record['plan_rejected_receipts'][0]['error']
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
    for table in ('leaf_revisions','md_leaf_operations','md_leaf_todos','m1_documents'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


@pytest.mark.parametrize('tamper',['company','fingerprint','lines','selection'])
def test_retained_checkpoint_tampering_rejected_before_transport(real_store,tamper):
    setup(real_store);d=extended_dossier();employee=RetainingEmployee(d)
    state=run(real_store,d,employee,limits=dict(classify=30,attribute=30,extract=30,plan=30,leaf=0))
    record=state['mapped_md']['records'][0]
    if tamper=='selection':record['retain_context_windows']=['6']
    elif tamper=='company':record['retained_context'][0]['company_id']='other'
    elif tamper=='fingerprint':record['retained_context'][0]['mapped_fingerprint']='f'*64
    else:record['retained_context'][0]['lines']['1']='invented evidence'
    real_store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',(json.dumps(state),'sam://kv/rt/md-direct/context-job'));real_store.db.commit()
    before=len(employee.requests)
    with pytest.raises(LeafRejected,match='retain'):
        run(real_store,d,employee,limits=dict.fromkeys(PHASES,30))
    assert len(employee.requests)==before
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_two_retained_plus_recent_bounded_and_unshown_old_id_rejected(real_store):
    setup(real_store);d=extended_dossier()
    class Reader:
        def __init__(self):self.requests=[]
        def execute(self,**kw):
            q=json.loads(kw['text']);action=q.pop('action');self.requests.append((action,q))
            n=int(q['window']['grant_id'])
            ids=[c['grant_id'] for c in q['context_grants']]
            assert len(ids)<=4 and all(int(i)<n for i in ids)
            if n==4:assert ids==['2','3','0','1']
            if n==6:assert ids==['4','5']  # Explicit release took effect only after window 5.
            if action=='classify':return dict(classification=dict(description='独立主题由员工判断'))
            if action=='attribute':return dict(outcome='no_attribution',ranges=[],reason='员工判定无事实归属')
            if action=='extract':return dict(facts=[],complete=True)
            result=dict(outcome='no_content',assignments=[],reason='员工判断本窗无归叶内容', retain_context_windows=q['retained_context_windows'], retention_reason='离线员工显式判断保留或释放原文限定')
            if n==0:result['retain_context_windows']=['0']
            elif n==1:result['retain_context_windows']=['0','1']
            elif n==5:result['retain_context_windows']=[]
            elif n==6:result['retain_context_windows']=['0']  # Read in past but no longer displayed.
            return result
    employee=Reader()
    with pytest.raises(LeafRejected,match='not displayed'):
        run(real_store,d,employee,limits=dict.fromkeys(PHASES,30))
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
    for table in ('leaf_revisions','md_leaf_operations','md_leaf_todos','m1_documents'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


class FormatEmployee(Employee):
    """Fake worker intentionally violates only receipt structure, not evidence."""
    def __init__(self,d,bad,*,repeat=False):
        super().__init__(d);self.bad=bad;self.repeat=repeat;self.bad_calls=0
    def execute(self,**kw):
        result=super().execute(**kw)
        q=json.loads(kw['text'])
        if q['action']=='plan' and q['window']['grant_id']=='0':
            if not self.bad_calls or self.repeat:
                self.bad_calls+=1
                if self.bad=='missing_memory':result.pop('retain_context_windows')
                elif self.bad=='missing_reason':result.pop('retention_reason')
                elif self.bad=='empty_reason':result['retention_reason']='  '
                elif self.bad=='top_context':result['context_windows']=[]
                else:
                    result.update(outcome='assigned',assignments=[dict(leaf_code='L-info-04',ranges=q['window']['ranges'],context_windows=[])])
                    if self.bad=='nested_memory':result['assignments'][0]['retain_context_windows']=[]
                    else:result['assignments'][0].pop('context_windows')
            else:
                assert q['format_feedback']['error']
                assert q['format_feedback']['contract']==q['response_contract']
                first=next(r for a,r in self.requests if a=='plan')
                assert q['window']==first['window'] and q['context_grants']==first['context_grants']
        return result


@pytest.mark.parametrize('bad,path',[
    ('missing_memory','response.retain_context_windows'),('missing_reason','response.retention_reason'),
    ('empty_reason','response.retention_reason'),('top_context','response.context_windows'),
    ('nested_memory','assignments[0].retain_context_windows'),('missing_context','assignments[0].context_windows')])
def test_plan_format_feedback_counted_same_grants_then_valid(real_store,bad,path):
    d=setup(real_store);employee=FormatEmployee(d,bad)
    state=run(real_store,d,employee)
    assert state['done'] and state['calls']==dict(classify=9,attribute=9,extract=9,plan=10,leaf=3)
    record=state['mapped_md']['records'][0]
    assert len(record['plan_rejected_receipts'])==1 and path in record['plan_rejected_receipts'][0]['error']
    assert record['retain_context_windows']==[] and record['retention_reason'].strip()
    assert len(MDLeafPublisher(real_store).current(COMPANY,'L-info-04')['blocks'])==2


def test_plan_format_budget_reconstruct_keeps_bill_and_rejected_receipt(real_store):
    d=setup(real_store);employee=FormatEmployee(d,'top_context')
    state=run(real_store,d,employee,limits=dict(classify=20,attribute=20,extract=20,plan=1,leaf=20))
    assert state['status']=='partial' and state['calls']['plan']==1
    r=state['mapped_md']['records'][0]
    assert r['status']=='read_pending' and len(r['plan_rejected_receipts'])==1
    assert not r['assignments'] and 'retain_context_windows' not in r
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    state=run(real_store,d,employee)
    assert state['done'] and state['calls']==dict(classify=9,attribute=9,extract=9,plan=10,leaf=3)
    assert len(state['mapped_md']['records'][0]['plan_rejected_receipts'])==1
    before=len(employee.requests);run(real_store,d,employee)
    assert len(employee.requests)==before


def test_plan_format_exhausted_requires_explicit_retry_preserves_audit(real_store):
    d=setup(real_store);employee=FormatEmployee(d,'missing_memory',repeat=True)
    state=run(real_store,d,employee)
    r=state['mapped_md']['records'][0]
    assert state['status']=='partial' and not state.get('done')
    assert state['calls']==dict(classify=1,attribute=1,extract=1,plan=2,leaf=0)
    assert r['plan_format_blocked'] and len(r['plan_rejected_receipts'])==2 and not r['assignments']
    before=len(employee.requests);again=run(real_store,d,employee)
    assert len(employee.requests)==before and again['calls']==state['calls']
    employee.repeat=False
    state=run(real_store,d,employee,resume_unresolved=True,retry_request_id='format-repair-1')
    assert state['done'] and state['calls']['plan']==11
    r=state['mapped_md']['records'][0]
    assert len(r['plan_rejected_receipts'])==2 and r['plan_format_retry_history']==[2]
    assert state['calls']['classify']==9 and state['calls']['extract']==9


def test_older_v3_success_receipt_not_silently_certified(real_store):
    d=setup(real_store);employee=Employee(d)
    state=run(real_store,d,employee)
    state['mapped_md']['records'][0].pop('retention_reason')
    raw=json.dumps(state)
    real_store.db.execute('UPDATE memory_kv SET content=? WHERE uri=?',(raw,'sam://kv/rt/md-direct/context-job'));real_store.db.commit()
    before=len(employee.requests)
    with pytest.raises(ValueError,match='plan receipt contract changed'):
        run(real_store,d,employee,resume_unresolved=True)
    assert len(employee.requests)==before
    assert real_store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/context-job',)).fetchone()[0]==raw


def test_missing_period_unit_support_reports_authorized_block_fields_then_repairs(real_store):
    original=setup(real_store,units=True)
    doc=deepcopy(original.document)
    doc['markdown']=doc['markdown'].replace('测量单位：毫米。','统计时点：2025-01-01，测量单位：毫米。')
    d=Dossier(doc,file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint
    class Repair(Employee):
        def __init__(self):super().__init__(d,units=True);self.leaf_calls=0
        def execute(self,**kw):
            q=json.loads(kw['text']);action=q['action']
            if action!='leaf':
                result=super().execute(**kw)
                if action=='extract' and result['facts']:
                    result['facts'][0]['period']='2025-01-01'
                    result['facts'][0]['field_evidence'].append(proof('period',q['context_grants'][0],'2025-01-01'))
                return result
            self.requests.append((action,deepcopy(q)));self.leaf_calls+=1
            g,ctx=q['grants'][:2]
            evidence=[source('new-value',g,[f['fact_id'] for f in q['facts']])]
            if self.leaf_calls>1:
                message=q['correction_feedback']['reason']
                assert 'block_id=qualified' in message and 'fact_id=' in message
                assert 'period: grant_id=1, ranges=[[2, 2]]' in message
                assert 'unit: grant_id=1, ranges=[[2, 2]]' in message
                assert 'old-block refs' in message and len(message)<=1000
                assert 'field_support' in q['correction_feedback']['contract']
                evidence.append(source('new-scope',ctx,[]))
            current=q['current']
            return dict(leaf_code=q['target']['leaf_code'],base_version=current['version'],base_sha=current['content_sha'],
                blocks=current['blocks']+[dict(block_id='qualified',text='统计时点：2025-01-01，样品A宽度7毫米[^new-value][^new-scope]。',evidence=evidence)],conflicts=[],note='按准确缺失字段反馈补同块旁证，不删除fact ID')
    employee=Repair();state=run(real_store,d,employee)
    assert state['done'] and state['calls']==dict(classify=4,attribute=4,extract=4,plan=4,leaf=2)
    a=state['mapped_md']['records'][3]['assignments'][0]
    assert len(a['rejected_drafts'])==1 and len(a['rejected_drafts'][0]['draft']['blocks'][-1]['evidence'])==1
    current=MDLeafPublisher(real_store).current(COMPANY,'L-bizov-07')
    assert len(current['blocks'])==1 and len(current['blocks'][0]['evidence'])==2
    assert current['blocks'][0]['evidence'][0]['fact_ids']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1


@pytest.mark.parametrize('field',['context_windows','retain_context_windows'])
@pytest.mark.parametrize('bad',[['0','1','2'],['0','0'],'0',[False]])
def test_context_shape_counted_feedback_then_correct_same_window(real_store,field,bad):
    d=setup(real_store)
    class Repair(Employee):
        rejected=False
        def execute(self,**kw):
            result=super().execute(**kw);q=json.loads(kw['text'])
            if q['action']=='plan' and q['window']['grant_id']=='2':
                if not self.rejected:
                    self.rejected=True
                    if field=='context_windows':result['assignments'][0][field]=bad
                    else:result[field]=bad
                else:
                    path='assignments[0].context_windows' if field=='context_windows' else 'response.retain_context_windows'
                    message=q['format_feedback']['error']
                    assert path in message
                    if bad==['0','1','2']:assert 'actual count 3 exceeds maximum 2' in message
                    elif bad==['0','0']:assert 'duplicate grant IDs' in message
                    elif bad=='0':assert 'expected array, actual str' in message
                    else:assert '[0]: expected quoted string grant ID, actual bool' in message
            return result
    state=run(real_store,d,Repair(d))
    assert state['done'] and state['calls']==dict(classify=9,attribute=9,extract=9,plan=10,leaf=3)
    r=state['mapped_md']['records'][2]
    assert len(r['plan_rejected_receipts'])==1 and r['plan_rejected_receipts'][0]['receipt']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2


def test_string_fact_id_precise_location_then_employee_uses_input_integer(real_store):
    d=setup(real_store)
    class Repair(Employee):
        bad_sent=False
        def execute(self,**kw):
            result=super().execute(**kw);q=json.loads(kw['text'])
            if q['action']=='leaf' and q['grants'][0]['grant_id']=='5':
                fid=q['facts'][0]['fact_id']
                assert type(fid) is int
                if not self.bad_sent:
                    self.bad_sent=True
                    result['blocks'][-1]['evidence'][0]['fact_ids']=[str(fid)]
                else:
                    message=q['correction_feedback']['reason']
                    assert message.startswith('invalid fact ids:')
                    assert 'block_id=block-5, evidence[0].fact_ids[0]' in message
                    assert 'expected positive JSON integer' in message and f"actual str '{fid}'" in message
                    assert result['blocks'][-1]['evidence'][0]['fact_ids']==[fid]
                    assert result['blocks'][:-1]==q['current']['blocks']
            return result
    state=run(real_store,d,Repair(d))
    assert state['done'] and state['calls']==dict(classify=9,attribute=9,extract=9,plan=9,leaf=4)
    a=state['mapped_md']['records'][5]['assignments'][0]
    bad=a['rejected_drafts'][0]['draft']['blocks'][-1]['evidence'][0]['fact_ids'][0]
    assert type(bad) is str  # The raw bad receipt is retained, never cast in-place.
    current=MDLeafPublisher(real_store).current(COMPANY,'L-info-04')
    assert len(current['blocks'])==2 and current['version']==2
    assert all(type(fid) is int for b in current['blocks'] for e in b['evidence'] for fid in e['fact_ids'])
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2


@pytest.mark.parametrize('field',['context_windows','retain_context_windows'])
def test_two_integer_context_ids_feedback_types_then_exact_string_choices(real_store,field):
    d=setup(real_store)
    class Repair(Employee):
        bad_sent=False
        def execute(self,**kw):
            result=super().execute(**kw);q=json.loads(kw['text'])
            if q['action']=='plan' and q['window']['grant_id']=='2':
                if not self.bad_sent:
                    self.bad_sent=True
                    if field=='context_windows':result['assignments'][0][field]=[0,1]
                    else:result[field]=[0,1]
                else:
                    message=q['format_feedback']['error']
                    path='assignments[0].context_windows' if field=='context_windows' else 'response.retain_context_windows'
                    assert path+'[0]' in message and 'expected quoted string grant ID, actual int 0' in message
                    assert 'maximum' not in message and 'at most' not in message
                    if field=='context_windows':result['assignments'][0][field]=['0','1']
                    else:result[field]=['0','1']
            return result
    state=run(real_store,d,Repair(d),limits=dict(classify=3,attribute=3,extract=3,plan=4,leaf=3))
    assert state['status']=='partial' and state['calls']==dict(classify=3,attribute=3,extract=3,plan=4,leaf=1)
    r=state['mapped_md']['records'][2]
    assert r['assignments'][0]['status']=='submitted'
    assert [g['grant_id'] for g in r['assignments'][0]['context_grants']]==['0','1']
    if field=='retain_context_windows':assert r[field]==['0','1']
    assert len(r['plan_rejected_receipts'])==1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1


# --- field literal rejection diagnostics: direct calls, no model/store ---

def _literal_item(field, value, witness_quote, *, line='截至2023年12月31日，以下记录验收公司员工总数，单位为人。'):
    # unit defaults empty so the period diagnostic case carries no unrelated,
    # unproven field; the unit mismatch case sets the bad unit explicitly.
    grant=dict(grant_id='0',company_id='co',file_hash='h',mapped_fingerprint='m',
        source_loc='docx:body/0/paragraph',lines={'1':line},original_name='a.docx')
    item=dict(entity='验收公司',period='2023年12月31日',unit='',quote='| 员工总数 | 1 |',
        field_evidence=[dict(field=field,company_id='co',grant_id='0',file_hash='h',
        mapped_fingerprint='m',source_loc='docx:body/0/paragraph',start_line=1,end_line=1,
        quote=witness_quote)])
    item[field]=value
    return grant,item


def test_reformatted_chinese_date_rejected_with_field_value_verified_quote():
    grant,item=_literal_item('period','2023-12-31','截至2023年12月31日')
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert msg.startswith('field literal/quote absent from declared read evidence')
    assert '[period]' in msg and '2023-12-31' in msg and '2023年12月31日' in msg
    assert 'verbatim' in msg


def test_verbatim_field_value_still_accepted():
    grant,item=_literal_item('period','2023年12月31日','截至2023年12月31日')
    proofs=verify_field_evidence(item,grant,[],'co')
    assert len(proofs)==1 and proofs[0]['field']=='period'


def test_non_date_field_mismatch_gets_same_generic_diagnostic():
    grant,item=_literal_item('unit','个','单位为人')
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert msg.startswith('field literal/quote absent from declared read evidence')
    assert '[unit]' in msg and '"个"' in msg and '单位为人' in msg


def test_forged_quote_not_echoed_and_granted_text_not_leaked():
    grant,item=_literal_item('period','2099-01-01','本句完全不在授权原文中2099年1月1日')
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert msg.startswith('field literal/quote absent from declared read evidence')
    assert 'not verified in the actually granted scope' in msg
    assert '本句完全不在授权原文中2099年1月1日' not in msg
    assert '2023年12月31日' not in msg and '单位为人' not in msg
    assert '"2099-01-01"' in msg


def test_nonstring_field_value_rejected_without_crash():
    grant,item=_literal_item('period',20231231,'截至2023年12月31日')
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert msg.startswith('field literal/quote absent from declared read evidence')
    assert '[period]' in msg and 'int' in msg and len(msg)<=1000


def test_huge_value_and_forged_quote_diagnostics_are_bounded():
    huge='甲'*5000
    grant,item=_literal_item('period',huge,'截至2023年12月31日')
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert len(msg)<=1000 and huge not in msg
    forged='X'*5000
    grant,item=_literal_item('period','unknown',forged)
    with pytest.raises(ValueError) as exc:
        verify_field_evidence(item,grant,[],'co')
    msg=str(exc.value)
    assert len(msg)<=1000 and 'XXXXXXXXXXXXXXXXXXXX' not in msg
    assert '2023年12月31日' not in msg
