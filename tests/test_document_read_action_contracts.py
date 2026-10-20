"""Capture actual EmployeeModelTool transport payload, without any model/network."""
import json
from types import SimpleNamespace

from app import settings
from app.document_read.service import DirectReadService
from app.employees.model_tool import EmployeeModelTool
from tests.test_document_read import ROOT, Model, task
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_reading_context import setup, Employee, COMPANY, PHASES, VALUES


def tool(monkeypatch, worker, captured):
    config=SimpleNamespace(project_root=ROOT,skill_root=ROOT/'skills',model_base_url='unused',
        model_base_url_env='UNUSED_URL',model_default='offline',model_name_env='UNUSED_MODEL',model_api_key_env='UNUSED_KEY')
    def fake_run(command,**kw):
        assert command[1:]==['-m','app.employees.model_tool']
        data=json.loads(kw['input']);captured.append(data)
        # This replaces only the subprocess transport, exercising the real execute adapter.
        result=worker.execute(skill_text=data['skill_text'],text=data['text'])
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(raw=json.dumps(result))),stderr='')
    monkeypatch.setattr('app.employees.model_tool.subprocess.run',fake_run)
    return config,EmployeeModelTool(config)


def test_five_actions_actual_model_tool_have_own_skill_and_contract(real_store,monkeypatch):
    d=setup(real_store);captured=[];config,model=tool(monkeypatch,Employee(d),captured)
    result=DirectReadService(real_store.memory.database,config=config,dossier=d,memory=real_store.memory,model=model).run(
        company_id=COMPANY,job_id='context-job',limits=dict.fromkeys(PHASES,20))
    assert result['done']
    assert {json.loads(c['text'])['action'] for c in captured}==set(PHASES)
    expected={'classify':{'classification'},'attribute':{'ranges'},'extract':{'facts','complete','remaining_source_locs'},
              'plan':{'outcome','assignments','retain_context_windows','retention_reason'},
              'leaf':{'leaf_code','base_version','base_sha','blocks','conflicts','note'}}
    for c in captured:
        q=json.loads(c['text']);action=q['action'];contract=q['response_contract']
        assert c['skill_text'].startswith(f'CURRENT ACTION ONLY: {action}.')
        assert contract['action']==action and set(contract['required'])==expected[action]
        if action in ('classify','attribute','extract'):
            assert '## 三动作输出' in c['skill_text'] and '完整分配例' not in c['skill_text']
            assert {'assignments','retain_context_windows','retention_reason'}<=set(contract['forbidden'])
        elif action=='plan':assert 'assignments[i].context_windows' in contract['top_level_context_windows']
        else:assert 'assignments' in contract['forbidden'] and 'no_content' in contract


def test_wrong_action_extract_remains_partial_and_resumes_only_extract(real_store,monkeypatch):
    d=setup(real_store);captured=[]
    class WrongOnce(Employee):
        wrong=True
        def execute(self,**kw):
            q=json.loads(kw['text'])
            if q['action']=='extract' and self.wrong:
                self.wrong=False
                return dict(outcome='no_content',assignments=[],retain_context_windows=['0'],retention_reason='员工把动作交错')
            return super().execute(**kw)
    config,model=tool(monkeypatch,WrongOnce(d),captured)
    def run():
        return DirectReadService(real_store.memory.database,config=config,dossier=d,memory=real_store.memory,model=model).run(
            company_id=COMPANY,job_id='context-job',limits=dict.fromkeys(PHASES,20))
    state=run()
    assert state['status']=='partial' and state['stage_failure']['detail']=='invalid window fact delivery'
    assert state['calls']==dict(classify=1,attribute=1,extract=1,plan=0,leaf=0)
    assert state['mapped_md']['records'][0]['preparation']['classification'] is not None
    assert state['mapped_md']['records'][0]['preparation']['delivery_rejections'][0]['response']['outcome']=='no_content'
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0]==1  # Seed only.
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0]==0
    first=len(captured);state=run()
    assert state['done'] and json.loads(captured[first]['text'])['action']=='extract'
    assert state['calls']==dict(classify=9,attribute=9,extract=10,plan=9,leaf=3)


def test_leaf_contract_documents_unresolved_and_unresolved_receipt_persists(real_store,monkeypatch):
    d=setup(real_store);captured=[]
    # Snapshot the pre-run seed facts (temporary subject/metric), unrelated to
    # the extracted甲方比例 facts; unresolved leaves must not roll them back.
    seed={r['id']:dict(r) for r in real_store.db.execute('SELECT * FROM facts')}
    class UnresolvedLeaf(Employee):
        reason='员工缺少该表期间字段的已授权字面旁证，无法判定补充或冲突，留待解决。'
        def execute(self,**kw):
            q=json.loads(kw['text'])
            if q['action']=='leaf':
                return dict(leaf_code=q['target']['leaf_code'],outcome='unresolved',reason=self.reason)
            return super().execute(**kw)
    config,model=tool(monkeypatch,UnresolvedLeaf(d),captured)
    result=DirectReadService(real_store.memory.database,config=config,dossier=d,memory=real_store.memory,model=model).run(
        company_id=COMPANY,job_id='context-job',limits=dict.fromkeys(PHASES,20))
    leaf_contracts=[json.loads(c['text'])['response_contract'] for c in captured
                    if json.loads(c['text'])['action']=='leaf']
    assert leaf_contracts and result['calls']['leaf']==len(leaf_contracts)>0
    for contract in leaf_contracts:
        assert contract['unresolved']==dict(required=['leaf_code','outcome','reason'],outcome='unresolved',
            boundary=contract['unresolved']['boundary'])
        assert contract['no_content']['required']==['leaf_code','outcome','reason']
        assert 'normal submission only' in contract['required_applies_to']
    assignments=[a for r in result['mapped_md']['records'] for a in r['assignments']]
    failed=[a for a in assignments if a['status']=='unresolved']
    original_draft=lambda code:dict(leaf_code=code,outcome='unresolved',reason=UnresolvedLeaf.reason)
    assert failed and all(a['reason']==UnresolvedLeaf.reason and a.get('draft')==original_draft(a['leaf_code'])
                          for a in failed)
    assert {f['leaf_code'] for f in result['leaves_failed']}=={a['leaf_code'] for a in failed}
    # Seed facts survive field-by-field; verified upstream facts are not rolled back.
    for fid,row in seed.items():
        assert dict(real_store.db.execute('SELECT * FROM facts WHERE id=?',(fid,)).fetchone())==row
    assert result['fact_ids']
    assert all(real_store.db.execute('SELECT 1 FROM facts WHERE id=?',(fid,)).fetchone()
               for fid in result['fact_ids'])
    # Same-period same-metric numeric conflicts are kept by the existing L2
    # guard: old verified fact retained, newer candidate in legacy todos
    # (reason=conflict) — separate from md_leaf_todos, which stays empty.
    new_facts=[r for r in real_store.db.execute('SELECT * FROM facts').fetchall() if r['id'] not in seed]
    conflict_values={r['candidate_value'] for r in real_store.db.execute(
        "SELECT candidate_value FROM todos WHERE company_id=? AND entity='甲方' AND metric='比例' AND reason='conflict'",
        (COMPANY,)).fetchall()}
    assert {r['value'] for r in new_facts} | conflict_values == set(VALUES)
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0]==0
    tables={r[0] for r in real_store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'md_leaf_bodies' not in tables or real_store.db.execute('SELECT count(*) FROM md_leaf_bodies').fetchone()[0]==0


def test_closed_actual_model_tool_skill_and_payload_unchanged(task,monkeypatch):
    monkeypatch.setattr(settings,'SAM_FACTS_TREE',False)
    db,d,memory=task;captured=[];config,model=tool(monkeypatch,Model(),captured)
    result=DirectReadService(db,config=config,dossier=d,memory=memory,model=model).run(
        company_id='acc',job_id='job',limits=dict(classify=1,attribute=1,extract=4))
    assert result['done']
    for c in captured:
        assert c['skill_text']==(ROOT/'skills/file-processor/SKILL.md').read_text()
        assert 'response_contract' not in json.loads(c['text'])
        assert not c['skill_text'].startswith('CURRENT ACTION ONLY:')
