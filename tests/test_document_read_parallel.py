"""U2 offline host workers, genuine providers and shared SQLite processes."""
from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from app import settings
from app.db.database import connect
from app.document_read.dossier import Dossier
from app.document_read.execution import JobLease, JobBusy, LeaseLost, run_files
from app.document_read.service import DirectReadService
from app.facts_tree.leaf_store import LeafPublishService
from app.facts_tree.md_publish import MDLeafPublisher
from app.memory.provider import LocalVectorMemoryProvider
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_leaves import COMPANY
from tests.test_document_read import ROOT


PHASES = ('classify', 'attribute', 'extract', 'plan', 'leaf')


def jobs(store, count=2):
    for i in range(count):
        sha = str(i+2)*64
        store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                         (sha, f'file-{i}.docx', 1, '/unused', 'now'))
        store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (f'u2-{i}', COMPANY, sha, f'file-{i}.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()


def mapped(i):
    d = Dossier(dict(markdown=f'文件资料{chr(65+i)}。', document_map=dict(elements=[dict(source_loc='docx:body/1/paragraph', start_line=1, end_line=1)]), assets=[]), file_hash=str(i+2)*64, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


class FirstDraftMeeting:
    """Two independent arrival events, no Barrier shared-memory arena."""
    def __init__(self, own, peer):
        self.own, self.peer = own, peer

    def wait(self, timeout):
        self.own.set()
        assert self.peer.wait(timeout), 'other first draft did not arrive'


class Model:
    def __init__(self, i, *, code='L-bizov-07', rendezvous=None, wait=None, conflict=False, seen=None):
        self.i, self.code, self.rendezvous, self.wait = i, code, rendezvous, wait
        self.conflict, self.seen, self.actions = conflict, seen, []
    def execute(self, **kwargs):
        q = json.loads(kwargs['text']); action = q.pop('action'); self.actions.append(action)
        assert mapped(self.i).document['markdown'] not in json.dumps(q.get('directory', []), ensure_ascii=False)
        if action=='classify': return dict(classification=dict(description='独立资料'))
        if action=='attribute': return dict(ranges=[dict(entity='临时主体', start_line=1, end_line=1)])
        if action=='extract': return dict(facts=[], complete=True)
        if action=='plan': return dict(outcome='assigned', assignments=[dict(leaf_code=self.code, ranges=q['window']['ranges'], context_windows=[])], retain_context_windows=[], retention_reason='离线员工显式判断保留或释放原文限定')
        current = q['current']
        if self.seen is not None: self.seen.append(current['version'])
        if self.actions.count('leaf') == 1:
            if self.rendezvous is not None: self.rendezvous.wait(timeout=15)
            if self.wait is not None: assert self.wait.wait(15), 'other worker stalled behind model'
        grant = q['grants'][0]
        new = dict(block_id=f'file-{self.i}', text=grant['md']+f'[^ref-{self.i}]', evidence=[dict(ref=f'ref-{self.i}', grant_id=grant['grant_id'],
            file_hash=grant['file_hash'], original_name=grant['original_name'], source_loc=grant['source_loc'], start_line=1,end_line=1,fact_ids=[])])
        conflicts = []
        blocks = deepcopy(current['blocks'])
        if self.conflict and current['blocks']:
            conflicts = [dict(old_block_id=current['blocks'][0]['block_id'], new_block=new, reason='员工认为内容冲突')]
        else: blocks.append(new)
        return dict(leaf_code=self.code, base_version=current['version'], base_sha=current['content_sha'], blocks=blocks, conflicts=conflicts, note='资料处理')


def reader(store, i, model=None):
    return DirectReadService(store.memory.database, config=SimpleNamespace(skill_root=ROOT/'skills'), dossier=mapped(i), memory=store.memory, model=model or Model(i))


def process_worker(path, i, meeting, ready, output, conflict=False, other_leaf=False):
    """No inherited DB handle is used; each process opens a genuine provider."""
    settings.SAM_FACTS_TREE = True
    memory = LocalVectorMemoryProvider(path, embedding=Embedding(), company_id=COMPANY)
    versions = []
    model = Model(i, rendezvous=meeting, wait=ready if i else None, conflict=conflict and bool(i), seen=versions,
                  code='L-bizov-09' if other_leaf and i else 'L-bizov-07')
    try:
        service = DirectReadService(path, config=SimpleNamespace(skill_root=ROOT/'skills'), dossier=mapped(i), memory=memory, model=model)
        state = service.run(company_id=COMPANY, job_id=f'u2-{i}', limits=dict.fromkeys(PHASES, 5))
        if not i: ready.set()
        output.put(dict(i=i, state=state, versions=versions, actions=model.actions))
    except Exception as exc:
        output.put(dict(i=i, error=repr(exc)))
        ready.set()
    finally: memory.close()


@pytest.mark.parametrize('conflict,other_leaf', [(False,False),(True,False),(False,True)])
def test_two_process_jobs_real_provider_same_leaf_recompare_and_slow_model(real_store, conflict, other_leaf):
    jobs(real_store)
    context = multiprocessing.get_context('fork')
    arrivals = [context.Event(), context.Event()]
    ready, output = context.Event(), context.Queue()
    processes = [context.Process(target=process_worker, args=(str(real_store.memory.database), i,
        FirstDraftMeeting(arrivals[i], arrivals[1-i]), ready, output, conflict, other_leaf)) for i in range(2)]
    for p in processes: p.start()
    try:
        results = [output.get(timeout=25) for _ in processes]
        for p in processes: p.join(5)
        assert all(p.exitcode == 0 for p in processes)
        assert all('error' not in r for r in results), results
        by_id = {r['i']:r for r in results}
        assert all(r['state']['done'] for r in results)
        # Slow second employee blocks in transport while the first publishes.
        assert ready.is_set()
        if not other_leaf:
            assert by_id[1]['versions'] == [0,1]
            assert by_id[1]['actions'].count('leaf') == 2
            assert by_id[1]['state']['mapped_md']['records'][0]['assignments'][0]['base_changes'] == 1
        pub = MDLeafPublisher(real_store)
        current = pub.current(COMPANY, 'L-bizov-07')
        if conflict:
            assert len(current['blocks'])==1 and current['blocks'][0]['block_id']=='file-0'
            todo = json.loads(real_store.db.execute('SELECT payload FROM md_leaf_todos').fetchone()[0])
            assert todo['old']['evidence'][0]['file_hash']=='2'*64
            assert todo['new']['evidence'][0]['file_hash']=='3'*64
            assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
        elif not other_leaf:
            assert {b['block_id'] for b in current['blocks']} == {'file-0','file-1'}
            assert {e['file_hash'] for b in current['blocks'] for e in b['evidence']} == {'2'*64,'3'*64}
            assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==2
        else:
            assert pub.current(COMPANY,'L-bizov-09')['version']==1
        assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==2
        assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0]==0
    finally:
        for p in processes:
            if p.is_alive(): p.terminate()
            p.join(5)
        output.close()


def test_job_lease_claim_expiry_generation_fences_checkpoint_billing_and_publish(real_store):
    jobs(real_store)
    first = JobLease(real_store.memory.database, 'u2-0', COMPANY)
    with pytest.raises(JobBusy): JobLease(real_store.memory.database, 'u2-0', COMPANY)
    real_store.db.execute('UPDATE md_job_leases SET expires_at=0 WHERE job_id=?', ('u2-0',)); real_store.db.commit()
    second = JobLease(real_store.memory.database, 'u2-0', COMPANY)
    assert second.generation == first.generation+1
    r = reader(real_store,0); r._lease=first; r.key='sam://kv/rt/md-direct/u2-0'
    state=dict(calls=dict.fromkeys(PHASES,0))
    with connect(real_store.memory.database) as conn:
        with pytest.raises(LeaseLost): r._save(conn,state)
    with pytest.raises(LeaseLost): r._call('classify',{},state,dict.fromkeys(PHASES,5))
    assert not r.model.actions
    from app.facts_tree.md_reader import MDWindows
    d=mapped(0); window=MDWindows(d); grant=window.grant(window.windows[0],'file-0.docx')
    pub=MDLeafPublisher(real_store,fence=first)
    with pytest.raises(LeaseLost):
        pub.submit(company=COMPANY,code='L-bizov-07',operation_id='expired-op',base_version=0,base_sha=pub.current(COMPANY,'L-bizov-07')['content_sha'],
            blocks=[],conflicts=[],grants={'0':grant},job_id='u2-0',note='拒绝')
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==0
    first.release()
    with connect(real_store.memory.database) as conn: second.check(conn)
    second.release()


def test_deadline_explicit_renewal_preserves_budget_and_finished_receipts(real_store):
    jobs(real_store,1)
    r=reader(real_store,0)
    state=r.run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,0))
    assert state['status']=='partial'
    state['mapped_md']['deadline_at']=time.time()-1
    with connect(real_store.memory.database) as conn:
        conn.execute('UPDATE memory_kv SET content=? WHERE uri=?',(json.dumps(state),r.key))
    state=reader(real_store,0).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
    assert state['status']=='partial' and not sum(state['calls'].values())
    model=Model(0); r=reader(real_store,0,model)
    state=r.run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3),renew_deadline_seconds=300)
    assert state['done'] and state['mapped_md']['deadline_renewals']==1
    assert all(state['calls'][p]==1 for p in PHASES)
    again=reader(real_store,0).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
    assert again['calls']==state['calls']
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==1


def test_bounded_parallel_entry_real_readers(real_store):
    jobs(real_store)
    # Factories construct independent providers in their executor thread.
    from contextlib import contextmanager
    def factory(i):
        @contextmanager
        def build():
            memory=LocalVectorMemoryProvider(real_store.memory.database,embedding=Embedding(),company_id=COMPANY)
            try:
                yield DirectReadService(memory.database,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=mapped(i),memory=memory,model=Model(i,code='L-bizov-07' if i==0 else 'L-bizov-09'))
            finally: memory.close()
        return build
    result=run_files([dict(reader_factory=factory(i),company_id=COMPANY,job_id=f'u2-{i}',limits=dict.fromkeys(PHASES,3)) for i in range(2)],workers=2)
    assert all(isinstance(r,dict) and r['done'] for r in result), result


@pytest.mark.parametrize('workers', [0,9,True])
def test_parallel_entry_bounds(real_store, workers):
    with pytest.raises(ValueError): run_files([],workers=workers)


def holding_worker(path, started, resume, output):
    settings.SAM_FACTS_TREE=True
    memory=LocalVectorMemoryProvider(path,embedding=Embedding(),company_id=COMPANY)
    class Holding(Model):
        def execute(self, **kwargs):
            if json.loads(kwargs['text'])['action']=='classify':
                started.set()
                assert resume.wait(20)
            return super().execute(**kwargs)
    try:
        r=DirectReadService(path,config=SimpleNamespace(skill_root=ROOT/'skills'),dossier=mapped(0),memory=memory,model=Holding(0))
        state=r.run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
        output.put(dict(state=state))
    except Exception as exc: output.put(dict(error=type(exc).__name__))
    finally: memory.close()


@pytest.mark.parametrize('crash', [False,True])
def test_same_job_process_claim_crash_takeover_budget_and_old_host_fenced(real_store,crash):
    jobs(real_store,1)
    context=multiprocessing.get_context('fork')
    started,resume,output=context.Event(),context.Event(),context.Queue()
    process=context.Process(target=holding_worker,args=(str(real_store.memory.database),started,resume,output))
    process.start()
    try:
        assert started.wait(10)
        with pytest.raises(JobBusy):
            reader(real_store,0).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
        key='sam://kv/rt/md-direct/u2-0'
        before=json.loads(real_store.db.execute('SELECT content FROM memory_kv WHERE uri=?',(key,)).fetchone()[0])
        assert before['calls']['classify']==1
        if crash:
            process.terminate(); process.join(5)
        real_store.db.execute('UPDATE md_job_leases SET expires_at=0 WHERE job_id=?',('u2-0',));real_store.db.commit()
        state=reader(real_store,0).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
        assert state['done'] and state['calls']['classify']==2
        if not crash:
            resume.set()
            assert output.get(timeout=10)==dict(error='LeaseLost')
            process.join(5)
        persisted=json.loads(real_store.db.execute('SELECT content FROM memory_kv WHERE uri=?',(key,)).fetchone()[0])
        assert persisted==state
        assert real_store.db.execute('SELECT generation FROM md_job_leases WHERE job_id=?',('u2-0',)).fetchone()[0]==2
        assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]==1
    finally:
        if process.is_alive(): process.terminate()
        process.join(5); output.close()


def test_contention_exhaustion_is_partial_then_reread_resumes_with_bill(real_store,monkeypatch):
    from app.facts_tree.md_publish import BaseChanged
    jobs(real_store,1)
    original=MDLeafPublisher.submit
    attempts=[]
    def fail(self,**kwargs):
        attempts.append(kwargs)
        if len(attempts)<=2: raise BaseChanged('injected competing base')
        return original(self,**kwargs)
    monkeypatch.setattr(MDLeafPublisher,'submit',fail)
    model=Model(0); r=reader(real_store,0,model)
    state=r.run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,5),compare_retries=2)
    assert state['status']=='partial' and not state.get('done')
    a=state['mapped_md']['records'][0]['assignments'][0]
    assert a['status']=='allocated' and 'draft' not in a and a['base_changes']==2
    assert state['calls']['leaf']==2
    state=reader(real_store,0,model).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,5),compare_retries=2)
    assert state['done'] and state['calls']['leaf']==3
    assert model.actions.count('plan')==1 and model.actions.count('extract')==1
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==1


def test_leased_submit_lost_receipt_replay_no_model_or_history_duplication(real_store,monkeypatch):
    jobs(real_store,1)
    original=MDLeafPublisher.submit
    fired=False
    def lost(self,**kwargs):
        nonlocal fired
        result=original(self,**kwargs)
        if not fired:
            fired=True
            raise OSError('lost receipt after atomic publish')
        return result
    monkeypatch.setattr(MDLeafPublisher,'submit',lost)
    model=Model(0)
    with pytest.raises(OSError,match='lost receipt'):
        reader(real_store,0,model).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
    state=reader(real_store,0,model).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,3))
    assert state['done'] and state['calls']['leaf']==1 and model.actions.count('leaf')==1
    for table in ('leaf_revisions','md_leaf_operations','md_leaf_notifications'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==1


def test_interrupted_transport_budget_exhaustion_never_resets_on_new_generation(real_store):
    jobs(real_store,1)
    class Timeout(Model):
        def execute(self, **kwargs): raise TimeoutError('offline injected transport interruption')
    state=reader(real_store,0,Timeout(0)).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,1))
    assert state['status']=='partial' and state['calls']['classify']==1
    assert state['classification'] is None
    model=Model(0)
    state=reader(real_store,0,model).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,1))
    assert state['status']=='partial' and state['calls']['classify']==1 and not model.actions
    state=reader(real_store,0,model).run(company_id=COMPANY,job_id='u2-0',limits=dict.fromkeys(PHASES,2))
    assert state['done'] and state['calls']['classify']==2
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0]==1
