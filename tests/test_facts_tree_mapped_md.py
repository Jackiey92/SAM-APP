"""U1 default and atomic v2 publication, real SQLite/provider, fake employee."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app import settings
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.facts_tree.leaf_store import LeafRejected, parse_markdown
from app.facts_tree.md_reader import MDWindows, MappedMDService, checkpoint_binding_identity
from app.facts_tree.template_packs import load_pack
from app.facts_tree.md_publish import MDLeafPublisher, BaseChanged
from app.memory.provider import ProjectionError
from app.facts_tree.rollup import FactsTreeRollup
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT

LOC0, LOC1 = 'docx:body/1/paragraph', 'docx:body/2/table'


def dossier():
    doc = dict(markdown='技术描述可回查，有9个。\n| 项目 | 值 |\n| --- | --- |\n| 参数 | 9 |',
               document_map=dict(elements=[dict(source_loc=LOC0, start_line=1,end_line=1),
                   dict(source_loc=LOC1,start_line=2,end_line=4,kind='table')]), assets=[])
    d = Dossier(doc,file_hash=SHA,objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


@pytest.fixture
def mapped(real_store):
    from app.facts_tree.quote_sources import DossierQuoteResolver
    d = dossier()
    real_store.quote_resolver = DossierQuoteResolver({SHA:d.document})
    real_store.db.execute('UPDATE facts SET source_span=?', (json.dumps(dict(source_loc=LOC0,quote='有9个')),))
    real_store.db.commit()
    return real_store, d


def block(grant, *, identity='block-a', ref='a', text=None, facts=None):
    return dict(block_id=identity, text=text or (f'值为9[^{ref}]。' if grant['source_loc']==LOC1 else f'技术描述可回查，有9个[^{ref}]。'), evidence=[dict(ref=ref,
        grant_id=grant['grant_id'], file_hash=SHA,original_name='sample.docx',source_loc=grant['source_loc'],
        start_line=grant['ranges'][-1][0],end_line=grant['ranges'][-1][1],fact_ids=[] if facts is None else facts)])


def setup(mapped):
    store,d = mapped
    pub = MDLeafPublisher(store)
    w = MDWindows(d)
    grants = {x['window_id']:w.grant(x,'sample.docx') for x in w.windows}
    return pub, grants


def submit(pub, grants, blocks, *, op='op-1', base=None, conflicts=None, code='L-bizov-07'):
    base = base or pub.current(COMPANY,code)
    return pub.submit(company=COMPANY,code=code,operation_id=op,base_version=base['version'],
        base_sha=base['content_sha'],blocks=blocks,conflicts=conflicts or [],grants=grants,job_id='job-md',note='记录')


class Employee:
    def __init__(self): self.requests=[]
    def __call__(self,action,request):
        self.requests.append(deepcopy(request))
        if action=='plan':
            assert 'fragments' not in request and 'markdown' not in request
            grant=request['window']; code='L-bizov-07' if grant['grant_id']=='0' else 'L-bizov-08'
            return dict(outcome='assigned',assignments=[dict(leaf_code=code,ranges=grant['ranges'], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
        assert action=='leaf'
        grant=request['grants'][0]; current=request['current']; n=grant['grant_id']
        assert request['operation']=='compare_submit' and len(request['grants'])==1
        return dict(leaf_code=request['target']['leaf_code'],base_version=current['version'],base_sha=current['content_sha'],
            blocks=current['blocks']+[block(grant,identity='block-'+n,ref='r'+n)],conflicts=[],note='新增')


def test_two_windows_two_leaves_real_provider_and_coverage(mapped):
    store,d = mapped
    state={}; employee=Employee(); saves=[]
    flow=MappedMDService(store.memory.database,store.memory,d,call=employee,save=lambda:saves.append(deepcopy(state)))
    flow.run(company=COMPANY,job_id='job-md',state=state)
    assert state['mapped_md']['done'] and len(state['mapped_md']['records'])==2
    assert len(employee.requests)==4
    pub=MDLeafPublisher(store)
    for code in ('L-bizov-07','L-bizov-08'):
        current=pub.current(COMPANY,code)
        meta,body,headings,sections=parse_markdown(current['markdown'])
        assert headings==['内容','索引'] and meta['schema_version']==2
        assert '概览' not in body and '编辑历史' not in body
        assert current['blocks'][0]['evidence'][0]['file_hash']==SHA
        assert store.memory.read(store.db.execute('SELECT uri FROM facts_tree_leaf_index WHERE leaf_code=?',(code,)).fetchone()[0])==current['markdown']
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM md_leaf_notifications').fetchone()[0]==2
    assert len(FactsTreeRollup(store)._active(COMPANY))==2
    before=len(employee.requests)
    flow.run(company=COMPANY,job_id='job-md',state=state)
    assert len(employee.requests)==before


@pytest.mark.parametrize('bad', ['number','hash','name','coordinate','line','grant','fact','citation','shape','identifier','grant-type','name-type','missing-id','unhashable-id'])
def test_bad_evidence_zero_side_effects(mapped,bad):
    store,d=mapped; pub,grants=setup(mapped); data=block(grants['0'])
    e=data['evidence'][0]
    if bad=='number': data['text']='有999个[^a]。'
    elif bad=='hash': e['file_hash']='b'*64
    elif bad=='name': e['original_name']='invented.docx'
    elif bad=='coordinate': e['source_loc']='docx:fake'
    elif bad=='line': e['end_line']=2
    elif bad=='grant': e['grant_id']='never-read'
    elif bad=='fact': e['fact_ids']=[999]
    elif bad=='shape': data=None
    elif bad=='identifier': data['block_id']='unsafe_id'
    elif bad=='missing-id': del data['block_id']
    elif bad=='unhashable-id': data['block_id']={}
    elif bad=='grant-type': e['grant_id']={}
    elif bad=='name-type': e['original_name']=42
    else: data['text']='无脚注'
    with pytest.raises(LeafRejected): submit(pub,grants,[data])
    for t in ('leaf_revisions','facts_tree_leaf_index','fact_leaf_links','m1_documents','m1_chunks','md_leaf_operations','md_leaf_todos','md_leaf_bodies'):
        assert store.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]==0


def test_revision_append_replay_and_stale_base(mapped):
    store,d=mapped; pub,grants=setup(mapped)
    first=block(grants['0']); empty=pub.current(COMPANY,'L-bizov-07')
    one=submit(pub,grants,[first],base=empty)
    assert submit(pub,grants,[first],base=empty)==one
    current=pub.current(COMPANY,'L-bizov-07')
    second=block(grants['1'],identity='block-b',ref='b')
    with pytest.raises(BaseChanged): submit(pub,grants,[first,second],op='op-stale',base=empty)
    result=submit(pub,grants,[first,second],op='op-2',base=current)
    assert result['version']==2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert pub.current(COMPANY,'L-bizov-07')['blocks']==[first,second]
    # Existing evidence is retained even when only the second grant is current.
    current=pub.current(COMPANY,'L-bizov-07')
    before=store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]
    replaced=deepcopy(first)
    replaced['evidence'][0]=deepcopy(second['evidence'][0])
    with pytest.raises(LeafRejected,match='existing content/index must be retained'):
        submit(pub,{'1':grants['1']},[replaced,second],op='bad-regrant',base=current)
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==before
    noop=submit(pub,{'1':grants['1']},[first,second],op='confirmed-noop',base=current)
    assert not noop['changed'] and noop['version']==2 and noop['todos']==0
    assert pub.current(COMPANY,'L-bizov-07')['markdown']==current['markdown']
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    with pytest.raises(Exception,match='append-only'):
        store.db.execute('UPDATE md_leaf_operations SET receipt=?',('{}',))
    store.db.rollback()


def test_conflict_saves_both_evidence_keeps_original_allows_addition(mapped):
    store,d=mapped; pub,grants=setup(mapped)
    old=block(grants['0']); submit(pub,grants,[old])
    prior=pub.current(COMPANY,'L-bizov-07')
    candidate=block(grants['1'],identity='candidate',ref='c')
    todo=dict(old_block_id='block-a',new_block=candidate,reason='员工判断不一致')
    receipt=submit(pub,grants,[old],op='conflict-1',base=prior,conflicts=[todo])
    assert not receipt['changed'] and receipt['todos']==1
    assert pub.current(COMPANY,'L-bizov-07')['markdown']==prior['markdown']
    row=store.db.execute('SELECT payload,status FROM md_leaf_todos').fetchone()
    value=json.loads(row['payload']); assert value['old']==old and value['new']==candidate and row['status']=='pending'
    assert submit(pub,grants,[old],op='conflict-1',base=prior,conflicts=[todo])==receipt
    assert store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0]==1
    extra=block(grants['1'],identity='extra',ref='e')
    submit(pub,grants,[old,extra],op='with-todo',base=prior,conflicts=[todo])
    assert pub.current(COMPANY,'L-bizov-07')['blocks']==[old,extra]
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2


def test_projection_failure_rolls_back_todo_revision_and_receipt(mapped,monkeypatch):
    store,d=mapped; pub,grants=setup(mapped)
    def fail(texts): raise RuntimeError('projection failure')
    monkeypatch.setattr(store.memory.embedding,'embed_texts',fail)
    with pytest.raises(ProjectionError): submit(pub,grants,[block(grants['0'],facts=[1])])
    for t in ('leaf_revisions','facts_tree_leaf_index','fact_leaf_links','m1_documents','m1_chunks','md_leaf_operations'):
        assert store.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]==0


def test_read_interrupted_resume_and_changed_fingerprint(mapped):
    store,d=mapped; state={}; employee=Employee(); count=[]
    def call(action,request):
        if action=='leaf' and not count:
            count.append(1); raise OSError('uncertain transport')
        return employee(action,request)
    flow=MappedMDService(store.memory.database,store.memory,d,call=call,save=lambda:None)
    with pytest.raises(OSError): flow.run(company=COMPANY,job_id='job-md',state=state)
    assert state['mapped_md']['records'][0]['status']=='allocated'
    flow.run(company=COMPANY,job_id='job-md',state=state)
    assert state['mapped_md']['done']
    state['mapped_md']['fingerprint']='different'
    with pytest.raises(LeafRejected,match='cursor'):
        flow.run(company=COMPANY,job_id='job-md',state=state)


def test_table_windows_preserve_header_and_bounds():
    d=dossier();d.document['markdown']='| 项目 | 值 |\n|---|---|\n'+'\n'.join('| 参数 | 9 |' for _ in range(30))
    d=Dossier(dict(markdown=d.document['markdown'],document_map=dict(elements=[dict(source_loc=LOC1,start_line=1,end_line=32,kind='table')]),assets=[]),file_hash=SHA,objects_root='.')
    windows=MDWindows(d,max_lines=8,max_chars=1000)
    assert len(windows.windows)>2
    for window in windows.windows:
        grant=windows.grant(window,'sample.docx')
        assert list(grant['lines'].values())[:2]==d.lines[:2]
        assert len(grant['lines'])<=8


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('empty_facts', [False, True])
def test_default_gate_pipeline_uses_window_actions_not_old_leaf_loop(mapped,resume,empty_facts):
    store,d=mapped
    if empty_facts:
        store.db.execute('DELETE FROM facts')
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('job-md',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()
    employee=Employee();actions=[]
    class Model:
        def execute(self,**kwargs):
            q=json.loads(kwargs['text']);action=q.pop('action');actions.append(action)
            if action=='classify': return dict(classification=dict(description='测试'))
            if action=='attribute': return dict(ranges=[dict(entity='临时主体',start_line=a,end_line=b) for a,b in q['window']['ranges']])
            if action=='extract' and empty_facts: return dict(facts=[],complete=True)
            if action=='extract': return dict(facts=[dict(entity='临时主体',attribute='新测试-'+q['window']['grant_id'],period='unknown',value='9',unit='个' if q['window']['source_loc']==LOC0 else '',quote='有9个' if q['window']['source_loc']==LOC0 else '| 参数 | 9 |',source_loc=q['window']['source_loc'],module_key='bizov')],complete=True)
            return employee(action,q)
    reader=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model())
    state=reader.run(company_id=COMPANY,job_id='job-md',limits={p:1 if resume else 2 for p in ('classify','attribute','extract','plan','leaf')})
    if resume:
        assert state['status']=='partial' and len(state['leaves_built'])==1
        assert state['mapped_md']['cursor']==1
        reader=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model())
        state=reader.run(company_id=COMPANY,job_id='job-md',limits=dict(classify=2,attribute=2,extract=2,plan=2,leaf=2))
    assert state['done'] and state['mapped_md']['done'] and len(state['leaves_built'])==2
    assert actions==['classify','attribute','extract','plan','leaf']*2
    assert not state['brief_done'] and not state['guide_done']
    if empty_facts:
        assert not state['fact_ids']
        assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0]==0


def test_old_format_read_and_conflict_only_no_auto_migration(mapped):
    from tests.test_facts_tree_leaves import publish
    store,d=mapped
    # Legacy artifact coordinate must be a real granted coordinate for this fixture.
    from tests.test_facts_tree_leaves import artifact
    old=artifact().replace('docx:body/77/paragraph',LOC0)
    publish(store,old)
    pub,grants=setup(mapped)
    prior=pub.current(COMPANY,'L-bizov-08')
    assert prior['legacy'] and prior['markdown']==old
    new=block(grants['1'],identity='new-legacy',ref='new')
    with pytest.raises(LeafRejected,match='explicit format migration'):
        submit(pub,grants,[new],code='L-bizov-08',base=prior)
    receipt=submit(pub,grants,[],op='legacy-conflict',code='L-bizov-08',base=prior,
        conflicts=[dict(old_block_id='legacy',new_block=new,reason='员工比较冲突')])
    assert not receipt['changed'] and receipt['todos']==1
    saved=json.loads(store.db.execute('SELECT payload FROM md_leaf_todos').fetchone()[0])
    assert saved['old']['markdown']==old and saved['new']==new
    assert store.memory.read(receipt['uri'])==old
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1


def test_real_content_table_row_has_specific_index_and_number_guard(mapped):
    store,d=mapped;pub,grants=setup(mapped)
    row=block(grants['1'],text='| 项目 | 值 | 来源 |\n|---|---|---|\n| 参数 | 9 | [^a] |')
    result=submit(pub,grants,[row])
    assert '## 内容' in store.memory.read(result['uri']) and 'block block-a' in store.memory.read(result['uri'])
    wrong=block(grants['1'],identity='wrong',ref='w',text='| 项目 | 值 | 来源 |\n|---|---|---|\n| 参数 | 999 | [^w] |')
    before=store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]
    with pytest.raises(LeafRejected): submit(pub,grants,[row,wrong],op='wrong-row')
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==before


def test_no_content_and_unresolved_coverage_are_distinct(mapped):
    store,d=mapped;state={}
    def call(action,request):
        assert action=='plan'
        return dict(outcome='no_content' if request['window']['grant_id']=='0' else 'unresolved',assignments=[],reason='员工依据', retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
    MappedMDService(store.memory.database,store.memory,d,call=call,save=lambda:None).run(company=COMPANY,job_id='no-content',state=state)
    assert [r['status'] for r in state['mapped_md']['records']]==['no_content','unresolved']
    assert state['mapped_md']['coverage_complete'] and not state['mapped_md']['content_complete']
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_lost_submit_receipt_resume_replays_without_new_model_call(mapped):
    store,d=mapped;state={};employee=Employee();saved=[]
    def save():
        if any(a['status']=='submitted' for r in state['mapped_md']['records'] for a in r['assignments']):
            raise OSError('receipt delivery interrupted')
        saved[:] = [deepcopy(state)]
    flow=MappedMDService(store.memory.database,store.memory,d,call=employee,save=save)
    with pytest.raises(OSError): flow.run(company=COMPANY,job_id='lost',state=state)
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
    restored=saved[0]
    MappedMDService(store.memory.database,store.memory,d,call=employee,save=lambda:None).run(company=COMPANY,job_id='lost',state=restored)
    assert len(employee.requests)==4 and store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==2


def test_context_regrant_only_prior_read_windows(mapped):
    store,d=mapped;state={};employee=Employee()
    def call(action,request):
        if action=='plan' and request['window']['grant_id']=='1':
            response=employee(action,request)
            response['assignments'][0]['context_windows']=['0']
            return response
        if action=='leaf' and request['target']['leaf_code']=='L-bizov-08':
            assert {g['grant_id'] for g in request['grants']}=={'0','1'}
            current=request['current']
            return dict(leaf_code='L-bizov-08',base_version=current['version'],base_sha=current['content_sha'],
                blocks=[block(request['grants'][1],identity='context',ref='ctx')],conflicts=[],note='依据已读前文')
        return employee(action,request)
    MappedMDService(store.memory.database,store.memory,d,call=call,save=lambda:None).run(company=COMPANY,job_id='ctx',state=state)
    assert state['mapped_md']['content_complete']
    assert MDLeafPublisher(store).current(COMPANY,'L-bizov-08')['blocks'][0]['evidence'][0]['grant_id']=='0'


def test_gate_disabled_does_not_create_extension_schema(real_store,monkeypatch):
    monkeypatch.setattr(settings,'SAM_FACTS_TREE',False)
    with pytest.raises(RuntimeError): MDLeafPublisher(real_store)
    assert real_store.db.execute("SELECT count(*) FROM sqlite_master WHERE name='md_leaf_bodies'").fetchone()[0]==0


def test_fact_from_same_source_but_unread_coordinate_rejected(mapped):
    store,d=mapped;pub,grants=setup(mapped)
    # Fact 1 belongs to paragraph window0, not the table window1.
    wrong=block(grants['1'],facts=[1])
    with pytest.raises(LeafRejected,match='fact provenance not in read window'):
        submit(pub,grants,[wrong])
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_projection_failure_with_todo_and_addition_preserves_old_atomic_unit(mapped,monkeypatch):
    store,d=mapped;pub,grants=setup(mapped)
    old=block(grants['0']);submit(pub,grants,[old])
    prior=pub.current(COMPANY,'L-bizov-07')
    tables=('leaf_revisions','facts_tree_leaf_index','fact_leaf_links','m1_documents','m1_chunks','md_leaf_operations','md_leaf_todos','md_leaf_notifications','md_leaf_bodies')
    before={t:[tuple(r) for r in store.db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in tables}
    extra=block(grants['1'],identity='extra',ref='e')
    candidate=block(grants['1'],identity='candidate',ref='c')
    def fail(texts): raise RuntimeError('project after pending todo')
    monkeypatch.setattr(store.memory.embedding,'embed_texts',fail)
    with pytest.raises(ProjectionError):
        submit(pub,grants,[old,extra],op='failed-with-todo',base=prior,
               conflicts=[dict(old_block_id='block-a',new_block=candidate,reason='员工比较')])
    assert {t:[tuple(r) for r in store.db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in tables}==before
    assert pub.current(COMPANY,'L-bizov-07')['markdown']==prior['markdown']


def strings_recursive(value):
    if isinstance(value,str): yield value
    elif isinstance(value,dict):
        for item in value.values(): yield from strings_recursive(item)
    elif isinstance(value,list):
        for item in value: yield from strings_recursive(item)


def test_default_three_window_resume_recursive_requests_no_full_md_no_unread_facts(real_store):
    from app.facts_tree.quote_sources import DossierQuoteResolver
    store=real_store
    lines=['窗口甲只属于当前记录。','窗口乙提供9个。','UNREAD_THIRD_SENTINEL给出11个。']
    locs=[f'docx:body/{10+i}/paragraph' for i in range(3)]
    document=dict(markdown='\n'.join(lines),document_map=dict(elements=[dict(source_loc=loc,start_line=i+1,end_line=i+1) for i,loc in enumerate(locs)]),assets=[])
    d=Dossier(document,file_hash=SHA,objects_root='.');d.quality_fingerprint=d.fingerprint
    store.quote_resolver=DossierQuoteResolver({SHA:document})
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('three-windows',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'))
    store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,source_span,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''',(COMPANY,'临时主体','外部预存事实','unknown','UNREAD_FACT_TOKEN','',SHA,
            json.dumps(dict(source_loc=locs[2],quote=lines[2],ranges=[[3,3]],mapped_fingerprint=d.fingerprint)), 'bizov','now'))
    store.db.commit()
    requests=[];codes=['L-bizov-07','L-bizov-08','L-bizov-09']
    class Model:
        def execute(self,**kwargs):
            q=json.loads(kwargs['text']);action=q['action']
            grant=q['grants'][0] if action=='leaf' else q['window']
            ordinal=int(grant['grant_id'])
            observed=json.loads(store.db.execute("SELECT content FROM memory_kv WHERE uri='sam://kv/rt/md-direct/three-windows'").fetchone()[0])
            assert observed['active_action']==action
            assert observed['calls'][action]==sum(r['action']==action for r in requests)+1
            assert not store.db.in_transaction
            assert not any(document['markdown'] in value for value in strings_recursive(q))
            if ordinal<2:
                assert not any('UNREAD_THIRD_SENTINEL' in value or 'UNREAD_FACT_TOKEN' in value for value in strings_recursive(q))
            requests.append(deepcopy(q))
            if action=='classify': return dict(classification=dict(description='当前窗'))
            if action=='attribute': return dict(ranges=[dict(entity='临时主体',start_line=ordinal+1,end_line=ordinal+1)])
            if action=='extract': return dict(facts=[dict(entity='临时主体',attribute='当前窗-'+str(ordinal),period='unknown',value=lines[ordinal],unit='',value_type='string',quote=lines[ordinal],source_loc=locs[ordinal],module_key='bizov')],complete=True)
            if action=='plan': return dict(outcome='assigned',assignments=[dict(leaf_code=codes[ordinal],ranges=grant['ranges'], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
            assert action=='leaf'
            assert q['facts'] and all('UNREAD_FACT_TOKEN' not in f['value'] for f in q['facts']) if ordinal<2 else True
            current=q['current'];data=block(grant,identity='block-'+str(ordinal),ref='r'+str(ordinal),text=lines[ordinal]+'[^r'+str(ordinal)+']')
            return dict(leaf_code=codes[ordinal],base_version=current['version'],base_sha=current['content_sha'],blocks=current['blocks']+[data],conflicts=[],note='当前窗入叶')
    model=Model()
    def reader():
        return DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=model)
    state=reader().run(company_id=COMPANY,job_id='three-windows',limits={p:2 for p in ('classify','attribute','extract','plan','leaf')})
    assert state['status']=='partial' and state['mapped_md']['cursor']==2
    assert len(state['leaves_built'])==2 and len(requests)==10
    assert [r['action'] for r in requests]==['classify','attribute','extract','plan','leaf']*2
    state=reader().run(company_id=COMPANY,job_id='three-windows',limits={p:3 for p in ('classify','attribute','extract','plan','leaf')})
    assert state['done'] and state['mapped_md']['coverage_complete'] and len(state['leaves_built'])==3
    assert len(requests)==15 and [r['action'] for r in requests]==['classify','attribute','extract','plan','leaf']*3
    for action in ('classify','attribute','extract','plan','leaf'):
        seen=[(r['grants'][0] if action=='leaf' else r['window'])['grant_id'] for r in requests if r['action']==action]
        assert seen==['0','1','2']
    assert all(r.get('protocol')=='mapped-md-v3' for r in requests)
    assert 'document' not in requests[0]
    rows=store.db.execute("SELECT source_span FROM facts WHERE attribute LIKE '当前窗-%' ORDER BY id").fetchall()
    assert [json.loads(r[0])['ranges'] for r in rows]==[[[1,1]],[[2,2]],[[3,3]]]
    assert all(json.loads(r[0])['mapped_fingerprint']==d.fingerprint for r in rows)
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==3
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==3
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0


def test_same_coordinate_partial_window_cannot_leak_future_fact_even_quote_repeats(real_store,monkeypatch):
    import app.facts_tree.md_reader as md_reader
    from app.facts_tree.quote_sources import DossierQuoteResolver
    original=md_reader.MDWindows.__init__
    monkeypatch.setattr(md_reader.MDWindows,'__init__',lambda self,dossier:original(self,dossier,max_lines=3))
    store=real_store
    md='| 项目 | 值 |\n|---|---|\n| 重复值 | 9 |\n| 重复值 | 9 |\n| 重复值 | 9 |'
    loc='docx:body/20/table'
    d=Dossier(dict(markdown=md,document_map=dict(elements=[dict(source_loc=loc,start_line=1,end_line=5,kind='table')]),assets=[]),file_hash=SHA,objects_root='.')
    d.quality_fingerprint=d.fingerprint;store.quote_resolver=DossierQuoteResolver({SHA:d.document})
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('split-table',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'))
    for kind in ('bounded','legacy'):
        span=dict(source_loc=loc,quote='| 重复值 | 9 |')
        if kind=='bounded': span.update(ranges=[[5,5]],mapped_fingerprint=d.fingerprint)
        store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,source_span,module_key,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(COMPANY,'临时主体','外部-'+kind,'unknown','FUTURE_'+kind,'',SHA,json.dumps(span),'bizov','now'))
    store.db.commit();requests=[]
    class Model:
        def execute(self,**kwargs):
            q=json.loads(kwargs['text']);action=q['action'];requests.append(q)
            grant=q['grants'][0] if action=='leaf' else q['window'];n=int(grant['grant_id'])
            if n<2: assert not any('FUTURE_' in text for text in strings_recursive(q))
            if action=='classify': return dict(classification=dict(description='表格窗口'))
            if action=='attribute': return dict(ranges=[dict(entity='临时主体',start_line=3+n,end_line=3+n)])
            if action=='extract': return dict(facts=[dict(entity='临时主体',attribute='本行-'+str(n),period='unknown',value='9',unit='',value_type='number',quote='| 重复值 | 9 |',source_loc=loc,module_key='bizov')],complete=True)
            if action=='plan': return dict(outcome='assigned',assignments=[dict(leaf_code='L-bizov-07',ranges=[[3+n,3+n]], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
            assert action=='leaf'
            current=q['current'];data=block(grant,identity='row-'+str(n),ref='r'+str(n),text='值为9[^r'+str(n)+']。')
            # Explicit new, position-bound fact can link only this row.
            selected=next(f['fact_id'] for f in q['facts'] if f['attribute']=='本行-'+str(n))
            data['evidence'][0]['fact_ids']=[selected]
            return dict(leaf_code='L-bizov-07',base_version=current['version'],base_sha=current['content_sha'],blocks=current['blocks']+[data],conflicts=[],note='当前行')
    reader=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model())
    state=reader.run(company_id=COMPANY,job_id='split-table',limits={p:3 for p in ('classify','attribute','extract','plan','leaf')})
    assert state['done'] and len(state['leaves_built'])==3
    assert store.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0]==3
    assert len([r for r in requests if r['action']=='extract'])==3


def test_window_fact_retry_never_echoes_rejected_ungranted_quote(mapped):
    store,d=mapped
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('bad-window-quote',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()
    employee=Employee();requests=[];extracts=[]
    class Model:
        def execute(self,**kwargs):
            q=json.loads(kwargs['text']);action=q['action'];requests.append(q)
            assert not any(d.document['markdown'] in text for text in strings_recursive(q))
            if action=='classify': return dict(classification=dict(description='当前窗口'))
            if action=='attribute': return dict(ranges=[dict(entity='临时主体',start_line=a,end_line=b) for a,b in q['window']['ranges']])
            if action=='extract':
                g=q['window'];extracts.append(g['grant_id'])
                if len(extracts)==1:
                    return dict(facts=[dict(entity='临时主体',attribute='当前值',period='unknown',value='9',unit='个',quote=d.document['markdown'],source_loc=g['source_loc'],module_key='bizov')],complete=True)
                if len(extracts)==2:
                    assert q['correction_feedback'][0]['candidate_index']==0
                    assert 'candidate' not in q['correction_feedback'][0]
                return dict(facts=[],complete=True)
            return employee(action,{k:v for k,v in q.items() if k!='action'})
    reader=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model())
    state=reader.run(company_id=COMPANY,job_id='bad-window-quote',limits=dict(classify=2,attribute=2,extract=3,plan=2,leaf=2))
    assert state['done'] and len(state['unresolved'])==1
    assert state['unresolved'][0]['candidate']['quote']==d.document['markdown']
    assert extracts==['0','0','1']
    assert len(state['leaves_built'])==2


@pytest.mark.parametrize('failure', ['save', 'commit', 'candidate', 'after_commit'])
def test_default_fact_checkpoint_failure_atomic_resume(mapped, monkeypatch, failure):
    """Faults in the real default entry cannot publish rolled-back progress."""
    import app.document_read.service as service_module
    store, d = mapped
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', ('job-fault', COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    baseline = {r[0] for r in store.db.execute('SELECT id FROM facts')}
    actions = []
    employee = Employee()
    class Model:
        def execute(self, **kwargs):
            q = json.loads(kwargs['text']); action = q.pop('action')
            actions.append((action, q['window']['grant_id'] if 'window' in q else None))
            if action == 'classify': return dict(classification=dict(description='测试'))
            if action == 'attribute': return dict(ranges=[dict(entity='临时主体', start_line=a, end_line=b) for a,b in q['window']['ranges']])
            if action == 'extract':
                return dict(facts=[dict(entity='临时主体', attribute='故障-'+q['window']['grant_id'], period='unknown', value='9',
                    unit='个' if q['window']['source_loc']==LOC0 else '', quote='有9个' if q['window']['source_loc']==LOC0 else '| 参数 | 9 |',
                    source_loc=q['window']['source_loc'], module_key='bizov')], complete=True)
            return employee(action, q)
    def make_reader():
        return DirectReadService(store.memory.database, config=SimpleNamespace(skill_root=ROOT/'skills'), dossier=d, memory=store.memory, model=Model())
    reader = make_reader()
    fired = False
    commit_target = None
    original_save = reader._save
    def faulty_save(conn, state):
        nonlocal fired, commit_target
        complete = state.get('mapped_md', {}).get('records', [{}])[0].get('preparation', {}).get('complete', False)
        if complete and not fired:
            if failure == 'save' and conn.in_transaction:
                fired = True
                original_save(conn, state)
                raise OSError('injected checkpoint save')
            if failure == 'commit' and conn.in_transaction:
                commit_target = conn
            if failure == 'after_commit' and not conn.in_transaction:
                fired = True
                raise OSError('injected postcommit receipt')
        return original_save(conn, state)
    monkeypatch.setattr(reader, '_save', faulty_save)
    original_connect = service_module.connect
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def __enter__(self): return self
        def __exit__(self, typ, value, tb):
            nonlocal fired
            if self is commit_target and not fired and typ is None:
                fired = True
                self.conn.rollback()
                raise OSError('injected commit failure')
            return self.conn.__exit__(typ, value, tb)
    if failure == 'commit':
        monkeypatch.setattr(service_module, 'connect', lambda path: Connection(original_connect(path)))
    original_consolidate = reader.facts.consolidate_candidates
    if failure == 'candidate':
        def faulty_candidates(*args, **kwargs):
            nonlocal fired
            result = original_consolidate(*args, **kwargs)
            fired = True
            raise OSError('injected candidate partial write')
        monkeypatch.setattr(reader.facts, 'consolidate_candidates', faulty_candidates)
    active_states = []
    original_finish = reader._finish
    def finish(state, *args):
        active_states.append(state)
        return original_finish(state, *args)
    monkeypatch.setattr(reader, '_finish', finish)
    limits = dict.fromkeys(('classify','attribute','extract','plan','leaf'), 5)
    with pytest.raises(OSError, match='injected'):
        reader.run(company_id=COMPANY, job_id='job-fault', limits=limits)
    assert fired
    persisted = json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (reader.key,)).fetchone()[0])
    actual = {r[0] for r in store.db.execute('SELECT id FROM facts')}
    for state in [persisted, active_states[-1]]:
        preparation = state['mapped_md']['records'][0]['preparation']
        assert set(state['fact_ids']) <= actual
        assert state['calls']['extract'] == 1
        assert preparation['complete'] is (failure == 'after_commit')
        assert bool(state['accepted']) is (failure == 'after_commit')
        assert bool(preparation['accepted']) is (failure == 'after_commit')
        assert bool(preparation['fact_ids']) is (failure == 'after_commit')
        assert not preparation['feedback']
    job = store.db.execute('SELECT l2_committed,projection_fact_ids FROM parse_jobs WHERE job_id=?', ('job-fault',)).fetchone()
    if failure != 'after_commit':
        assert actual == baseline and not job['l2_committed']
        assert not json.loads(job['projection_fact_ids'] or '[]')
    else:
        assert len(actual-baseline) == 1 and job['l2_committed']
        assert set(json.loads(job['projection_fact_ids'])) == set(persisted['fact_ids'])
    resumed = make_reader().run(company_id=COMPANY, job_id='job-fault', limits=limits)
    assert resumed['done'] and len(resumed['leaves_built']) == 2
    assert len([a for a,w in actions if a=='extract' and w=='0']) == (1 if failure=='after_commit' else 2)
    assert len([a for a,w in actions if a=='classify']) == 2
    assert len([a for a,w in actions if a=='attribute']) == 2
    assert len([a for a,w in actions if a=='leaf']) == 2
    assert len({r[0] for r in store.db.execute('SELECT id FROM facts')}-baseline) == 2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2
    assert set(resumed['fact_ids']) <= {r[0] for r in store.db.execute('SELECT id FROM facts')}
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0


GENERIC_DIGEST = load_pack('generic').manifest_sha256


def interrupted_once(mapped, job='job-bind'):
    """First leaf call fails once; the checkpoint then holds processed records."""
    store, d = mapped
    state = {}; employee = Employee(); fired = []
    def call(action, request):
        if action == 'leaf' and not fired:
            fired.append(1); raise OSError('uncertain transport')
        return employee(action, request)
    flow = MappedMDService(store.memory.database, store.memory, d, call=call, save=lambda: None)
    with pytest.raises(OSError): flow.run(company=COMPANY, job_id=job, state=state)
    assert state['mapped_md']['records'][0]['status'] == 'allocated'
    return store, d, state, employee


def resume(mapped, state, employee, job='job-bind'):
    store, d = mapped
    return MappedMDService(store.memory.database, store.memory, d, call=employee, save=lambda: None).run(
        company=COMPANY, job_id=job, state=state)


def test_new_checkpoint_records_verified_binding_identity(mapped):
    store, d = mapped
    state = {}
    MappedMDService(store.memory.database, store.memory, d, call=Employee(), save=lambda: None).run(
        company=COMPANY, job_id='job-bind', state=state)
    binding = store.binding.get(COMPANY)
    assert state['mapped_md']['company_id'] == COMPANY
    assert state['mapped_md']['binding_identity'] == dict(
        binding_revision=binding['binding_revision'], pack_id=binding['template_pack'],
        pack_version=binding['pack_version'], manifest_sha256=binding['pack_digest'])


def test_matching_binding_identity_resumes_to_completion(mapped):
    store, d, state, employee = interrupted_once(mapped)
    before = len(employee.requests)
    resume(mapped, state, employee)
    assert state['mapped_md']['done'] and len(employee.requests) > before


def simulate_rebind_new_revision(store):
    digest = store.binding.get(COMPANY)['pack_digest']
    store.db.execute('UPDATE company_tree_meta SET binding_revision=2 WHERE company_id=?', (COMPANY,))
    store.db.execute('INSERT INTO company_template_binding_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (COMPANY, 2, 'generic', 1, digest, 'generic', 1, digest,
         '2026-10-17T00:00:00+00:00', 'test', 'simulated controlled rebind', None, None, 'rebind'))
    store.db.commit()
    assert store.binding.get(COMPANY)['binding_revision'] == 2


def test_binding_revision_change_rejects_resume_without_employee(mapped):
    store, d, state, employee = interrupted_once(mapped)
    snapshot = deepcopy(state); before = len(employee.requests)
    simulate_rebind_new_revision(store)
    with pytest.raises(LeafRejected, match='binding identity mismatch'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [('binding_revision', 2), ('pack_id', 'other'),
                                         ('pack_version', 99), ('manifest_sha256', 'b' * 64)])
def test_recorded_identity_field_mismatch_rejects_resume(mapped, field, value):
    store, d, state, employee = interrupted_once(mapped)
    state['mapped_md']['binding_identity'][field] = value
    snapshot = deepcopy(state); before = len(employee.requests)
    with pytest.raises(LeafRejected, match='binding identity mismatch'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before


@pytest.mark.parametrize('column,value', [('template_pack', 'other'), ('pack_version', 99), ('pack_digest', 'b' * 64)])
def test_unverifiable_current_binding_rejects_resume(mapped, column, value):
    store, d, state, employee = interrupted_once(mapped)
    snapshot = deepcopy(state); before = len(employee.requests)
    store.db.execute(f'UPDATE company_tree_meta SET {column}=? WHERE company_id=?', (value, COMPANY))
    store.db.commit()
    with pytest.raises(LeafRejected, match='checkpoint binding failed verification'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


def test_deleted_binding_rejects_resume(mapped):
    store, d, state, employee = interrupted_once(mapped)
    snapshot = deepcopy(state); before = len(employee.requests)
    store.db.execute('DELETE FROM company_tree_meta WHERE company_id=?', (COMPANY,))
    store.db.commit()
    with pytest.raises(LeafRejected, match='binding no longer exists'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before


def test_old_checkpoint_without_identity_rejected_state_preserved(mapped):
    store, d, state, employee = interrupted_once(mapped)
    state['mapped_md'].pop('binding_identity')
    snapshot = deepcopy(state); before = len(employee.requests)
    with pytest.raises(LeafRejected, match='predates binding identity'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


@pytest.mark.parametrize('bad', ['generic', ['generic'], dict(pack_id='generic'),
    dict(binding_revision='1', pack_id='generic', pack_version=1, manifest_sha256=GENERIC_DIGEST),
    dict(binding_revision=1, pack_id='generic', pack_version=1, manifest_sha256=GENERIC_DIGEST, extra=1)])
def test_malformed_recorded_identity_rejects_resume(mapped, bad):
    store, d, state, employee = interrupted_once(mapped)
    state['mapped_md']['binding_identity'] = bad
    snapshot = deepcopy(state); before = len(employee.requests)
    with pytest.raises(LeafRejected, match='binding identity'):
        resume(mapped, state, employee)
    assert state == snapshot and len(employee.requests) == before


@pytest.mark.parametrize('flaw', ['missing', 'malformed', 'mismatch'])
def test_precreated_shell_with_flawed_identity_rejected_state_preserved(mapped, flaw):
    # A pre-existing unread shell is still an old checkpoint: without a valid
    # verified binding identity it fails closed before any employee call, leaf
    # write or progress write; it is never auto-upgraded.
    store, d = mapped
    state = {'mapped_md': dict(protocol=3, company_id=COMPANY, file_hash=d.file_hash,
        fingerprint=d.fingerprint, cursor=0, records=[], unresolved=[], affected=[],
        deadline_at=123.0, deadline_renewals=1)}
    if flaw == 'malformed':
        state['mapped_md']['binding_identity'] = 'generic'
    elif flaw == 'mismatch':
        binding = store.binding.get(COMPANY)
        state['mapped_md']['binding_identity'] = dict(
            binding_revision=binding['binding_revision'], pack_id=binding['template_pack'],
            pack_version=99, manifest_sha256=binding['pack_digest'])
    snapshot = deepcopy(state); employee = Employee()
    with pytest.raises(LeafRejected, match='binding identity'):
        MappedMDService(store.memory.database, store.memory, d, call=employee, save=lambda: None).run(
            company=COMPANY, job_id='job-shell', state=state)
    assert state == snapshot and not employee.requests
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


def test_service_created_shell_with_identity_resumes(mapped):
    # The deadline-renewal pre-save persists a new shell that already carries
    # the verified binding identity; the reader resumes it as an old
    # checkpoint whose identity matches the current binding.
    store, d = mapped
    state = {'mapped_md': dict(protocol=3, company_id=COMPANY, file_hash=d.file_hash,
        fingerprint=d.fingerprint, cursor=0, records=[], unresolved=[], affected=[],
        binding_identity=checkpoint_binding_identity(store.binding.get(COMPANY)),
        deadline_at=123.0, deadline_renewals=1)}
    MappedMDService(store.memory.database, store.memory, d, call=Employee(), save=lambda: None).run(
        company=COMPANY, job_id='job-shell', state=state)
    assert state['mapped_md']['done']
    assert state['mapped_md']['binding_identity']['pack_id'] == 'generic'
    assert state['mapped_md']['deadline_renewals'] == 1


def completed_once(mapped, job='job-done'):
    """A fully completed checkpoint with its verified binding identity."""
    store, d = mapped
    state = {}; employee = Employee()
    MappedMDService(store.memory.database, store.memory, d, call=employee, save=lambda: None).run(
        company=COMPANY, job_id=job, state=state)
    assert state['mapped_md']['done']
    return store, d, state, employee


@pytest.mark.parametrize('flaw', ['missing', 'malformed', 'mismatch'])
def test_done_checkpoint_with_flawed_identity_rejected_state_preserved(mapped, flaw):
    # done=true never bypasses the identity gate: a completed checkpoint with
    # a missing/malformed/mismatched identity fails closed with zero employee
    # calls and the state preserved field by field.
    store, d, state, employee = completed_once(mapped)
    if flaw == 'missing':
        state['mapped_md'].pop('binding_identity')
    elif flaw == 'malformed':
        state['mapped_md']['binding_identity'] = 'generic'
    else:
        state['mapped_md']['binding_identity']['pack_version'] = 99
    snapshot = deepcopy(state); before = len(employee.requests)
    revisions = store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]
    with pytest.raises(LeafRejected, match='binding identity'):
        resume(mapped, state, employee, job='job-done')
    assert state == snapshot and len(employee.requests) == before
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == revisions


def test_done_checkpoint_with_matching_identity_short_circuits(mapped):
    # A completed checkpoint whose identity matches the current verified
    # binding still short-circuits idempotently past the gate: no employee
    # call, no state change.
    store, d, state, employee = completed_once(mapped)
    snapshot = deepcopy(state); before = len(employee.requests)
    resume(mapped, state, employee, job='job-done')
    assert state == snapshot and len(employee.requests) == before
