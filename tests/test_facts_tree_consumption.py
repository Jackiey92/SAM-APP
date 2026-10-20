"""U3 mechanical consumption, explicit rejection repair and QC-bound uploads."""
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import settings
from app.db.database import connect
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.document_read.upload import phase_limits, run_mapped_upload, v4_phase_limits
from app.facts_tree.leaf_store import LeafRejected
from app.facts_tree.md_publish import MDLeafPublisher
from app.facts_tree.rollup import FactsTreeRollup
from app.memory import paths
from app.memory.company_facts.l2_consolidation import ConsolidationService
from app.memory.provider import LocalVectorMemoryProvider, ProjectionError
from app.parse_jobs import ParseJobCanceled, ParseJobPartial
from app.module_tree import MODULES
from tests.test_facts_tree_mapped_md import mapped, block, setup, submit, Employee, LOC0
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_reading_notes import formal_table_count
from tests.test_facts_tree_notes_publish import PublishModel, GateReviewModel
from tests.test_facts_tree_transactions import real_store

PHASES=('classify','attribute','extract','plan','leaf')


def register_job(store):
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',('u3',COMPANY,SHA,'sample.docx','docx','docx',1,'now','now'));store.db.commit()


def manifest(d):
    return dict(file_hash=SHA,format='docx',document=deepcopy(d.document),document_quality=dict(status='pass',
        evidence_binding=dict(company_id=COMPANY,job_id='u3',file_hash=SHA,document_fingerprint=d.fingerprint)))


class Model:
    def __init__(self, bad=0, cancel=None): self.bad=bad; self.actions=[]; self.prompts=[]; self.employee=Employee(); self.cancel=cancel
    def execute(self,**kwargs):
        q=json.loads(kwargs['text']); action=q.pop('action');self.actions.append(action);self.prompts.append(q)
        if action=='classify':return dict(classification=dict(description='测试'))
        if action=='attribute':return dict(ranges=[dict(entity='临时主体',start_line=a,end_line=b) for a,b in q['window']['ranges']])
        if action=='extract':return dict(facts=[],complete=True)
        result=self.employee(action,q)
        if action=='leaf' and self.cancel is not None:self.cancel.set()
        if action=='leaf' and self.bad:
            self.bad-=1
            result['blocks'][-1]['text']='无出处的拒稿'
        return result


def configured(store, tmp_path):
    return SimpleNamespace(main_db=store.memory.database,objects_dir=tmp_path,skill_root=ROOT/'skills')


def test_two_leaf_same_footnote_namespaces_and_notification_ack_failure(mapped,monkeypatch):
    store,d=mapped;pub,grants=setup(mapped)
    submit(pub,grants,[block(grants['0'])],op='leaf-a',code='L-bizov-07')
    submit(pub,grants,[block(grants['1'])],op='leaf-b',code='L-bizov-09')
    rollup=FactsTreeRollup(store)
    original=store.memory.embedding.embed_texts
    def fail(texts):raise RuntimeError('offline injected consumption projection failure')
    monkeypatch.setattr(store.memory.embedding,'embed_texts',fail)
    with pytest.raises(ProjectionError):rollup.consume_notifications(company_id=COMPANY)
    assert store.db.execute('SELECT count(*) FROM md_leaf_consumption').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM md_leaf_notifications').fetchone()[0]==2
    monkeypatch.setattr(store.memory.embedding,'embed_texts',original)
    result=rollup.consume_notifications(company_id=COMPANY)
    assert len(result['consumed'])==2
    uri=paths.trunk_doc_uri(COMPANY,MODULES[1].directory,'trunk-brief.md')
    text=store.memory.read(uri)
    for code in ('L-bizov-07','L-bizov-09'):
        assert text.count(f'[^{code}-a]:')==1 and text.count(f'[^{code}-a]')==2
    assert 'sample.docx → '+grants['0']['source_loc'] in text
    assert 'sample.docx → '+grants['1']['source_loc'] in text
    assert store.memory.read(paths.tree_brief_uri(COMPANY))==text
    assert not rollup.consume_notifications(company_id=COMPANY)['consumed']
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0


def test_notification_old_consumer_always_reads_latest_version(mapped):
    store,d=mapped;pub,grants=setup(mapped); first=block(grants['0'])
    submit(pub,grants,[first],op='version-one')
    old_consumer=FactsTreeRollup(store)
    assert old_consumer._active(COMPANY)[0][2]['version']==1
    second=block(grants['1'],identity='other',ref='b')
    submit(pub,grants,[first,second],op='version-two')
    result=old_consumer.consume_notifications(company_id=COMPANY)
    assert result['consumed']==[dict(leaf_code='L-bizov-07',version=2)]
    assert store.db.execute('SELECT version FROM md_leaf_consumption').fetchone()[0]==2
    text=store.memory.read(paths.tree_brief_uri(COMPANY))
    assert '[^L-bizov-07-a]' in text and '[^L-bizov-07-b]' in text


def test_md_todo_scoped_read_and_escaped_non_resolving_card(mapped):
    store,d=mapped;pub,grants=setup(mapped); old=block(grants['0'])
    submit(pub,grants,[old],op='initial')
    new=block(grants['1'],identity='candidate',ref='b')
    submit(pub,grants,[old],op='pending',conflicts=[dict(old_block_id=old['block_id'],new_block=new,reason='<script>employee reason</script>')])
    service=ConsolidationService(store.memory.database)
    todos=service.list_todos(company_id=COMPANY)
    md=[t for t in todos if t.get('kind')=='md_content_conflict']
    assert len(md)==1 and md[0]['review_status']=='pending'
    assert md[0]['details']['old']==old and md[0]['details']['new']==new
    assert not service.list_todos(company_id='another-company')
    assert not service.list_todos(company_id=COMPANY,status='resolved')
    env=Environment(loader=FileSystemLoader(ROOT/'webapp/templates'),autoescape=select_autoescape())
    html=env.get_template('_todo_card.html').render(todo=md[0])
    assert '资料内容待确认' in html and '&lt;script&gt;' in html and '<script>' not in html
    assert 'data-todo-action' not in html and '采用候选值' not in html
    primary, records = html.split('<details class="md-todo-records">', 1)
    assert 'md-content-todo' in primary and '来源和处理记录' in records
    for value in (md[0]['leaf_code'], md[0]['operation_id'], md[0]['details']['job_id'], SHA):
        assert value not in primary and value in records
    assert old['evidence'][0]['source_loc'] in html and new['evidence'][0]['source_loc'] in html
    assert 'job-md' in html and 'pending' in md[0]['id']
    assert store.db.execute('SELECT status FROM md_leaf_todos').fetchone()[0]=='pending'


@pytest.mark.parametrize('change',['markdown','document_map','assets','file_hash','company','job','status'])
def test_quality_binding_rejects_changed_document_or_scope(mapped,tmp_path,change):
    store,d=mapped; m=manifest(d);document=m['document'];kw=dict(file_hash=SHA,objects_root=tmp_path,company_id=COMPANY,job_id='u3',quality=m['document_quality'])
    if change=='markdown': document['markdown']+='\n变更'
    elif change=='document_map': document['document_map']['changed']=True
    elif change=='assets':document['assets'].append(dict(uri='new'))
    elif change=='file_hash':kw['file_hash']='b'*64
    elif change=='company':kw['company_id']='another'
    elif change=='job':kw['job_id']='different'
    elif change=='status':kw['quality']['status']='failed'
    with pytest.raises(ValueError,match='quality proof differs'):Dossier.from_quality(document,**kw)


def test_rejection_two_charged_repairs_then_explicit_resume_keeps_bad_audit(mapped):
    store,d=mapped;register_job(store);model=Model(bad=3)
    def run(**extra):
        return DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=model).run(
            company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,8),**extra)
    state=run()
    assert state['mapped_md']['technical_rejections']==1 and state['mapped_md']['pending_todos']==0
    a=state['mapped_md']['records'][0]['assignments'][0]
    assert len(a['rejected_drafts'])==3 and a['status']=='unresolved'
    prompts=[q for q in model.prompts if q.get('operation')=='compare_submit']
    assert prompts[1]['correction_feedback']['reason']
    assert '无出处的拒稿' not in json.dumps(prompts[1],ensure_ascii=False)
    assert state['calls']['leaf']==4 # three rejected drafts plus independent sibling
    before=len(model.actions); run(); assert len(model.actions)==before
    state=run(resume_unresolved=True)
    assert not state['mapped_md']['technical_rejections'] and len(state['leaves_built'])==2
    assert state['calls']['leaf']==5
    assert len(state['mapped_md']['records'][0]['assignments'][0]['rejected_drafts'])==3
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==2


def test_upload_seam_partial_resume_and_cancel_no_publish(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);cfg=configured(store,tmp_path);job=dict(job_id='u3',company_id=COMPANY,file_hash=SHA)
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,1)))
    events=[];model=Model()
    def execute(model):return run_mapped_upload(job=job,manifest=manifest(d),config=cfg,memory=store.memory,cancel=lambda:None,progress=lambda **k:events.append(k),model=model)
    with pytest.raises(ParseJobPartial):execute(model)
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,3)))
    state=execute(model)
    assert state['done'] and model.actions==list(PHASES)*2
    assert not state['brief_done'] and not state['guide_done']
    assert store.memory.read(paths.tree_brief_uri(COMPANY))


def test_cancel_after_leaf_transport_rolls_back_no_leaf(mapped):
    store,d=mapped;register_job(store);event=Event()
    def check():
        if event.is_set():raise ParseJobCanceled('stopped')
    model=Model(cancel=event)
    with pytest.raises(ParseJobCanceled):
        DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=model).run(
            company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,3),cancel=check)
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    state=json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/u3',)).fetchone()[0])
    assert state['calls']['leaf']==1 and not state.get('done')


@pytest.mark.parametrize('limits',[None,dict(classify=2),dict.fromkeys(PHASES,True)])
def test_upload_requires_explicit_bounded_totals(limits):
    with pytest.raises(ValueError):phase_limits({} if limits is None else {'SAM_MD_PHASE_LIMITS':json.dumps(limits)})


@pytest.mark.parametrize('mapping_changes',[False,True])
def test_actual_upload_gate_order_resume_subset_commit_not_full_legacy(mapped,tmp_path,monkeypatch,mapping_changes):
    from webapp import app as web
    import app.document_read.service as service
    store,d=mapped;register_job(store);cfg=configured(store,tmp_path)
    data=manifest(d);data.update(parse_summary=dict(status='parsed'),pages=[])
    calls=[]
    class Staging:
        def __init__(self,*a,**k):pass
        def get(self,identifier):return dict(payload=data)
    class Adapter:
        def ensure_runtime_ready(self,**kw):calls.append('engine')
        def run_parse(self,*a,**kw):calls.append('conversion');return 1
    def review(*a,**kw):calls.append('qc');return 1
    class Traced(Model):
        def execute(self,**kw):calls.append(json.loads(kw['text'])['action']);return super().execute(**kw)
    monkeypatch.setattr(web,'MAIN_DB',store.memory.database)
    monkeypatch.setattr(web,'OBJECTS_DIR',tmp_path/'objects')
    monkeypatch.setattr(web,'RUNTIME_CONFIG',cfg)
    monkeypatch.setattr(web,'StagingStore',Staging)
    monkeypatch.setattr(web,'_PARSE_ADAPTER',Adapter())
    monkeypatch.setattr(web,'_review_parsed_document',review)
    monkeypatch.setattr(web,'_has_declared_self',lambda co:True)
    monkeypatch.setattr(service,'EmployeeModelTool',lambda config:Traced())
    monkeypatch.setattr(web,'memory_provider',lambda cfg:LocalVectorMemoryProvider(cfg.main_db,embedding=Embedding(),company_id=COMPANY))
    def forbidden(*a,**k):raise AssertionError('old full document employee path invoked')
    monkeypatch.setattr(web,'EntityBridgeService',forbidden)
    monkeypatch.setattr(web,'_classify_committed_document',forbidden)
    monkeypatch.setattr(web,'_rebuild_fact_projections',forbidden)
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,1)))
    job=dict(job_id='u3',company_id=COMPANY,file_hash=SHA,harness_format='docx',l2_committed=True)
    with pytest.raises(ParseJobPartial):web._parse_job_pipeline(job,lambda **k:None,Event())
    assert calls==['engine','conversion','qc']+list(PHASES)
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,3)))
    if mapping_changes:
        data['document']['assets'].append(dict(uri='changed'))
        with pytest.raises(ValueError,match='quality proof differs'):web._parse_job_pipeline(job,lambda **k:None,Event())
        assert calls==['engine','conversion','qc']+list(PHASES)
    else:
        result=web._parse_job_pipeline(job,lambda **k:None,Event())
        assert result['done'] and calls==['engine','conversion','qc']+list(PHASES)*2
        assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0


def test_actual_md_todos_api_host_scope_no_numeric_resolve_and_snapshot(mapped,tmp_path,monkeypatch):
    from webapp import app as web
    from app import cloud_export
    store,d=mapped;pub,grants=setup(mapped);old=block(grants['0'])
    submit(pub,grants,[old],op='initial')
    submit(pub,grants,[old],op='todo',conflicts=[dict(old_block_id=old['block_id'],new_block=block(grants['1'],identity='candidate',ref='b'),reason='员工确认需审核')])
    monkeypatch.setattr(web,'MAIN_DB',store.memory.database)
    monkeypatch.setenv('SAM_COMPANY_ID',COMPANY)
    client=web.app.test_client()
    data=client.get('/api/todos?company_id='+COMPANY).get_json()['todos']
    md=[t for t in data if t.get('kind')=='md_content_conflict'];assert len(md)==1
    assert not client.get('/api/todos?company_id=other').get_json()['todos']
    assert client.post('/api/todos/'+md[0]['id']+'/resolve',json=dict(choose='candidate')).status_code==404
    # An existing foreign record cannot be selected via a query-string override.
    store.db.execute('INSERT INTO md_leaf_todos SELECT ?,operation_id,ordinal,leaf_code,base_version,payload,status FROM md_leaf_todos WHERE company_id=?',('other',COMPANY));store.db.commit()
    assert not client.get('/api/todos?company_id=other').get_json()['todos']
    cfg=SimpleNamespace(cloud_readonly=False,main_db=store.memory.database,objects_dir=tmp_path)
    monkeypatch.setattr(cloud_export.RuntimeConfig,'from_env',lambda **kw:cfg)
    monkeypatch.setattr(cloud_export,'memory_provider',lambda c:store.memory)
    snapshot=cloud_export.build_home_snapshot(company_id=COMPANY)
    exported=[t for t in snapshot['payload']['page_context']['todos'] if t.get('kind')=='md_content_conflict']
    assert exported==md
    assert store.db.execute('SELECT status FROM md_leaf_todos WHERE company_id=?',(COMPANY,)).fetchone()[0]=='pending'


def test_parse_manager_retains_partial_and_explicit_retry_options(mapped,monkeypatch):
    from app.parse_jobs import ParseJobManager
    store,d=mapped;register_job(store)
    def worker(*a):raise ParseJobPartial('explicit continuation required')
    manager=ParseJobManager(store.memory.database,worker,workers=1)
    manager._update('u3',status='queued',stage='queued')
    try:
        with pytest.raises(ParseJobPartial):manager._run('u3')
        assert manager.get('u3')['status']=='partial'
        monkeypatch.setattr(manager,'_submit',lambda job:None)
        manager.retry('u3',resume_unresolved=True,renew_deadline_seconds=300)
        row=store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-upload-options/u3',)).fetchone()
        options=json.loads(row[0])
        assert options['resume_unresolved'] is True and options['renew_deadline_seconds']==300 and options['request_id']
    finally:manager.close()


def test_untrusted_cloud_md_view_only_scoped_typed_fields(mapped):
    from app.facts_tree.todos import safe_md_view
    store,d=mapped;pub,grants=setup(mapped);old=block(grants['0'])
    submit(pub,grants,[old],op='initial')
    submit(pub,grants,[old],op='pending',conflicts=[dict(old_block_id=old['block_id'],new_block=block(grants['1'],identity='candidate',ref='b'),reason='需要审核')])
    todo=[t for t in ConsolidationService(store.memory.database).list_todos(company_id=COMPANY) if t.get('kind')=='md_content_conflict'][0]
    assert safe_md_view(todo,company_id=COMPANY)['details']['old']['text']==old['text']
    assert safe_md_view(todo,company_id='other') is None
    bad=deepcopy(todo);bad['details']['new']['evidence']=[False]
    assert safe_md_view(bad,company_id=COMPANY) is None
    todo['template']='untrusted';todo['details']['new']['evidence'][0]['fact_ids']=[{},3]
    view=safe_md_view(todo,company_id=COMPANY)
    assert 'template' not in view and view['details']['new']['evidence'][0]['fact_ids']==[3]


def test_explicit_retry_intent_applies_deadline_only_once(mapped):
    store,d=mapped;register_job(store)
    def run(request):
        return DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model()).run(
            company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,0),resume_unresolved=True,
            renew_deadline_seconds=300,retry_request_id=request)
    first=run('user-click-one')
    second=run('user-click-one')
    assert first['mapped_md']['deadline_at']==second['mapped_md']['deadline_at']
    assert second['mapped_md']['deadline_renewals']==1
    third=run('user-click-two')
    assert third['mapped_md']['deadline_renewals']==2 and not sum(third['calls'].values())


def test_legacy_shell_without_identity_rejected_before_any_state_change(mapped):
    # An old empty shell without binding identity fails closed before the
    # resume/deadline progress writes and before any employee call; the
    # persisted checkpoint is preserved field by field, never auto-upgraded.
    store,d=mapped;register_job(store)
    model=Model()
    service=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=model)
    key='sam://kv/rt/md-direct/u3'
    old=dict(company_id=COMPANY,fingerprint=d.fingerprint,calls=dict.fromkeys(PHASES,0),
        classification=None,ranges=None,complete=False,accepted=[],fact_ids=[],unresolved=[],
        brief_done=False,guide_done=False,
        mapped_md=dict(protocol=3,company_id=COMPANY,file_hash=SHA,fingerprint=d.fingerprint,
            cursor=0,records=[],unresolved=[],affected=[],deadline_at=123.0,deadline_renewals=1))
    store.db.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?)',(key,json.dumps(old,ensure_ascii=False),'{}'))
    store.db.commit()
    with pytest.raises(LeafRejected,match='predates binding identity'):
        service.run(company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,0),resume_unresolved=True,
            renew_deadline_seconds=300,retry_request_id='user-click-one')
    persisted=json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?',(key,)).fetchone()[0])
    assert persisted==old
    assert not model.actions
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_deadline_shell_persisted_with_verified_identity(mapped,monkeypatch):
    # The service-created deadline shell carries the verified binding identity
    # from its very first persistence; identity is minted only at creation.
    store,d=mapped;register_job(store)
    service=DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model())
    saved=[]
    original=DirectReadService._save
    def spy(self,conn,state):
        saved.append(deepcopy(state));return original(self,conn,state)
    monkeypatch.setattr(DirectReadService,'_save',spy)
    service.run(company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,0),resume_unresolved=True,
        renew_deadline_seconds=300,retry_request_id='user-click-one')
    binding=store.binding.get(COMPANY)
    identity=dict(binding_revision=binding['binding_revision'],pack_id=binding['template_pack'],
        pack_version=binding['pack_version'],manifest_sha256=binding['pack_digest'])
    shells=[s['mapped_md'] for s in saved if 'mapped_md' in s]
    assert shells and all(s.get('binding_identity')==identity for s in shells)
    persisted=json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/u3',)).fetchone()[0])
    assert persisted['mapped_md']['binding_identity']==identity
    assert persisted['mapped_md']['deadline_renewals']==1


def test_qc_gate_emits_exact_document_binding(tmp_path,monkeypatch):
    import hashlib
    from app.document_quality.gate import DocumentQualityGate
    from app.document_quality.sandbox import QualitySandbox
    source=tmp_path/'original.txt';source.write_text('source\n')
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    doc=dict(markdown='source',document_map=dict(elements=[dict(source_loc='txt:line/1',start_line=1,end_line=1)]),assets=[])
    manifest_value=dict(file_hash=sha,format='txt',document=doc)
    responses=iter([dict(candidates=[dict(action='verify',script='from pathlib import Path\nimport json\nassert Path("/evidence/original.txt").read_text()=="source\\n"\nprint(json.dumps({"status":"pass","issues":[]}))')]),
                    dict(candidates=[dict(action='conclude',status='pass',issues=[])])])
    cfg=SimpleNamespace(objects_dir=tmp_path/'objects',skill_root=ROOT/'skills',project_root=ROOT)
    monkeypatch.setattr(settings,'SAM_FACTS_TREE',True)
    gate=DocumentQualityGate(config=cfg,sandbox=QualitySandbox(ROOT/'.sam-isolated/venv',temporary_root=tmp_path/'sandbox'),employee=lambda **k:next(responses),max_model_calls=2)
    result=gate.inspect(company_id=COMPANY,task_id='u3',source=source,manifest=manifest_value)
    assert result['status']=='pass'
    d=Dossier.from_quality(doc,file_hash=sha,objects_root=cfg.objects_dir,company_id=COMPANY,job_id='u3',quality=result)
    assert result['evidence_binding']['document_fingerprint']==d.fingerprint


def test_existing_module_read_consumes_v2_without_model(mapped):
    from app.memory.company_facts.module_projection import ModuleProjectionService
    store,d=mapped;pub,grants=setup(mapped)
    submit(pub,grants,[block(grants['0'])],op='initial')
    FactsTreeRollup(store).consume_notifications(company_id=COMPANY)
    service=ModuleProjectionService(store.memory.database,store.memory)
    brief=service.read(company_id=COMPANY,module_key='bizov',layer='brief')
    assert '[^L-bizov-07-a]' in brief
    guide=service.read(company_id=COMPANY,module_key='bizov',layer='guide')
    assert 'L-bizov-07' in guide
    value=json.loads(service.read(company_id=COMPANY,module_key='bizov',layer='brief',format='json'))
    assert value['content']==brief


V4_LIMITS=dict(review=8,publish=16)


def v4_env(monkeypatch,five=8,v4=V4_LIMITS):
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL','v4')
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,five)))
    if v4 is None:monkeypatch.delenv('SAM_MD_V4_PHASE_LIMITS',raising=False)
    else:monkeypatch.setenv('SAM_MD_V4_PHASE_LIMITS',json.dumps(v4))


def v4_upload(store,d,tmp_path,model):
    events=[];cfg=configured(store,tmp_path);job=dict(job_id='u3',company_id=COMPANY,file_hash=SHA)
    result=run_mapped_upload(job=job,manifest=manifest(d),config=cfg,memory=store.memory,
        cancel=lambda:None,progress=lambda **k:events.append(k),model=model)
    return result,events


def checkpoint(store):
    return json.loads(store.db.execute('SELECT content FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/u3',)).fetchone()[0])


def test_v4_upload_missing_phase_limits_fails_closed_before_any_employee_call(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);v4_env(monkeypatch,v4=None)
    model=PublishModel()
    with pytest.raises(ValueError,match='SAM_MD_V4_PHASE_LIMITS'):v4_upload(store,d,tmp_path,model)
    assert model.requests==[]
    assert store.db.execute('SELECT count(*) FROM memory_kv WHERE uri=?',('sam://kv/rt/md-direct/u3',)).fetchone()[0]==0
    assert formal_table_count(store.db,'md_read_notes')==0
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


@pytest.mark.parametrize('raw',[None,'not-json',json.dumps(dict(review=1)),
    json.dumps(dict(review=1,publish=1,extract=1)),json.dumps(dict(review=True,publish=1)),
    json.dumps(dict(review=-1,publish=1)),json.dumps(dict(review=1,publish=10001)),
    json.dumps(dict(review='8',publish=1)),json.dumps([1,2])])
def test_v4_phase_limits_reject_missing_malformed_bool_extra_and_out_of_range(raw):
    env={} if raw is None else {'SAM_MD_V4_PHASE_LIMITS':raw}
    with pytest.raises(ValueError,match='SAM_MD_V4_PHASE_LIMITS'):v4_phase_limits(env)


def test_v4_phase_limits_accept_exact_two_phase_bounds():
    assert v4_phase_limits({'SAM_MD_V4_PHASE_LIMITS':json.dumps(dict(review=0,publish=10000))})==dict(review=0,publish=10000)


def test_v4_upload_reports_success_only_after_review_and_publication_complete(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);v4_env(monkeypatch)
    model=PublishModel()
    result,events=v4_upload(store,d,tmp_path,model)
    progress=result['mapped_notes']
    assert result['done'] and progress['review_complete'] is True and progress['open_notes']==0
    assert progress['publication']['complete'] is True
    assert events[-1]==dict(stage='reading',message='整份资料阅读、读后复核与事实发布完成')
    assert store.db.execute("SELECT status FROM parse_jobs WHERE job_id='u3'").fetchone()[0]=='done'
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==3
    actions=[r['action'] for r in model.requests]
    assert 'review' in actions and 'publish' in actions


def test_v4_upload_publish_budget_exhausted_stays_partial_and_resumes(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);v4_env(monkeypatch,v4=dict(review=8,publish=0))
    model=PublishModel()
    with pytest.raises(ParseJobPartial):v4_upload(store,d,tmp_path,model)
    state=checkpoint(store);progress=state['mapped_notes']
    assert progress['reading_complete'] and progress['review_complete'] and progress['open_notes']==0
    assert not progress.get('publication',{}).get('complete')
    assert state['calls']['publish']==0 and model.publish_requests==[]
    assert store.db.execute("SELECT status FROM parse_jobs WHERE job_id='u3'").fetchone()[0]=='partial'
    assert formal_table_count(store.db,'md_note_publications')==0
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    v4_env(monkeypatch,v4=dict(review=8,publish=16))
    result,_=v4_upload(store,d,tmp_path,model)
    assert result['done'] and result['mapped_notes']['publication']['complete'] is True
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0]==2
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==3


def test_v4_upload_review_budget_exhausted_stays_partial_and_resumes(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);v4_env(monkeypatch,v4=dict(review=0,publish=16))
    model=PublishModel()
    with pytest.raises(ParseJobPartial):v4_upload(store,d,tmp_path,model)
    state=checkpoint(store);progress=state['mapped_notes']
    assert progress['reading_complete'] and not progress.get('review_complete')
    assert state['calls']['review']==0 and state['calls']['publish']==0
    assert model.review_requests==[] and model.publish_requests==[]
    assert formal_table_count(store.db,'md_note_publications')==0
    v4_env(monkeypatch,v4=dict(review=8,publish=16))
    result,_=v4_upload(store,d,tmp_path,model)
    assert result['done'] and result['mapped_notes']['publication']['complete'] is True
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0]==2


def test_v4_upload_open_notes_block_success_even_with_publish_budget(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store);v4_env(monkeypatch)
    model=GateReviewModel()
    with pytest.raises(ParseJobPartial):v4_upload(store,d,tmp_path,model)
    progress=checkpoint(store)['mapped_notes']
    assert progress['review_complete'] is True and progress['open_notes']==1
    assert not progress.get('publication')
    assert model.publish_requests==[]
    assert formal_table_count(store.db,'md_note_publications')==0
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0


def test_v3_upload_ignores_v4_only_phase_limits(mapped,tmp_path,monkeypatch):
    store,d=mapped;register_job(store)
    monkeypatch.delenv('SAM_MD_READ_PROTOCOL',raising=False)
    monkeypatch.setenv('SAM_MD_PHASE_LIMITS',json.dumps(dict.fromkeys(PHASES,3)))
    monkeypatch.setenv('SAM_MD_V4_PHASE_LIMITS','not json at all')
    events=[];cfg=configured(store,tmp_path);job=dict(job_id='u3',company_id=COMPANY,file_hash=SHA);model=Model()
    result=run_mapped_upload(job=job,manifest=manifest(d),config=cfg,memory=store.memory,
        cancel=lambda:None,progress=lambda **k:events.append(k),model=model)
    assert result['done'] and 'mapped_md' in result and 'mapped_notes' not in result
    assert model.actions==list(PHASES)*2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2


def test_cancel_during_real_projection_rolls_back_entire_leaf(mapped,monkeypatch):
    store,d=mapped;register_job(store);event=Event()
    original=store.memory.embedding.embed_texts
    def stop(texts):
        vectors=original(texts)
        event.set()
        return vectors
    monkeypatch.setattr(store.memory.embedding,'embed_texts',stop)
    def check():
        if event.is_set():raise ParseJobCanceled('stopped during projection')
    with pytest.raises(ParseJobCanceled):
        DirectReadService(store.memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=d,memory=store.memory,model=Model()).run(
            company_id=COMPANY,job_id='u3',limits=dict.fromkeys(PHASES,3),cancel=check)
    for table in ('leaf_revisions','facts_tree_leaf_index','fact_leaf_links','md_leaf_operations','md_leaf_notifications','m1_documents','m1_chunks'):
        assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1
