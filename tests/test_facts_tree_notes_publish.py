"""Protocol V4 V2-1: post-review note publication to facts/sources/leaves.

Real isolated SQLite ledgers plus offline scripted employees; no models, no
credentials, no network. V3 semantics are only exercised through the unchanged
publisher guards.
"""
from copy import deepcopy
import json
import runpy
from types import SimpleNamespace

import pytest

from app import settings
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.leaf_store import LeafPublishService
from app.facts_tree.md_publish import MDLeafPublisher
from app.facts_tree.md_reader import MDWindows, ComparisonStopped
from app.facts_tree.notes_publish import NotesPublishService, PublishRejected
from app.facts_tree.quote_sources import DossierQuoteResolver
from app.facts_tree.source_registry import SourceRegistry
from app.facts_tree.template_packs import available_packs, load_pack
from app.facts_tree.tree_read import TreeReadService
from tests.memory_setup import migrated_memory
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT
from tests.test_facts_tree_reading_notes import (
    NotesModel, ConfirmedReviewModel, dossier, reader,
    LOC0, LOC1, JOB, LIMITS, REVIEW_LIMITS, formal_table_count, assert_formal_zero)

MERGE_LOC1 = 'docx:body/2/paragraph'
PUBLISH_LIMITS = dict(REVIEW_LIMITS, publish=16)
KV_KEY = f'sam://kv/rt/md-direct/{JOB}'


@pytest.fixture
def v4_job(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL', 'v4')
    real_store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (JOB, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    real_store.db.commit()
    return real_store


def load_state(store):
    row = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (KV_KEY,)).fetchone()
    return json.loads(row['content'])


def kv_save(store, state):
    store.db.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?) '
                     'ON CONFLICT(uri) DO UPDATE SET content=excluded.content',
                     (KV_KEY, json.dumps(state, ensure_ascii=False), '{}'))
    store.db.commit()


class PublishModel(ConfirmedReviewModel):
    """Offline stage C employee: one top1 leaf per note, append-only blocks."""

    leaf_for = {'n-0001': 'L-bizov-07', 'n-0002': 'L-bizov-08'}

    def __init__(self, plans=None):
        NotesModel.__init__(self, plans)
        self.review_requests = []
        self.publish_requests = []

    def assign(self, request):
        return dict(assignments=[
            dict(group_ref='grp-' + nid, note_ids=[nid], leaf_code=self.leaf_for[nid],
                 reason='员工为每条原子事实指定唯一 top1 叶子')
            for nid in (n['note_id'] for n in request['notes'] if n['status'] == 'verified')])

    def submit_leaf(self, request):
        current = request['current']
        blocks = [deepcopy(block) for block in current['blocks']]
        for index, group in enumerate(request['groups']):
            member = group['members'][0]
            ref = chr(ord('a') + index)
            grant = next(g for g in request['grants']
                         if g['grant_id'] == member['primary_ref']['grant_id'])
            unit = group['fact'].get('unit') or ''
            text = (f'技术描述可回查，有9个[^{ref}]。' if grant['source_loc'] == LOC0
                    else f'值为9{unit}[^{ref}]。')
            blocks.append(dict(block_id='pub-' + group['group_ref'], text=text,
                evidence=[dict(ref=ref, grant_id=grant['grant_id'], file_hash=grant['file_hash'],
                    original_name=grant['original_name'], source_loc=grant['source_loc'],
                    start_line=member['primary_ref']['start_line'],
                    end_line=member['primary_ref']['end_line'],
                    fact_refs=[group['group_ref']], note_id=member['note_id'])]))
        return dict(leaf_code=request['target']['leaf_code'],
            base_version=current['version'], base_sha=current['content_sha'],
            blocks=blocks, conflicts=[], note='员工发布说明：仅追加新块')

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'publish':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            self.publish_requests.append(deepcopy(request))
            if request['operation'] == 'assign_leaves':
                return self.assign(request)
            return self.submit_leaf(request)
        return super().execute(**kwargs)


def run_full(store, model, limits=PUBLISH_LIMITS, **kwargs):
    return reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=limits, **kwargs)


def direct_publish(store, model, state, d=None, save=None):
    d = d or dossier()

    def call(action, payload):
        return model.execute(skill_text='stage-c-skill',
            text=json.dumps({'action': action, **payload}, ensure_ascii=False))

    service = NotesPublishService(store.memory.database, store.memory, d,
        call=call, save=save or (lambda: kv_save(store, state)), original_name='sample.docx')
    service.run(company=COMPANY, job_id=JOB, state=state)
    return service


def merge_dossier():
    doc = dict(markdown='技术描述可回查，有9个。\n再次陈述：有9个。',
               document_map=dict(elements=[dict(source_loc=LOC0, start_line=1, end_line=1),
                   dict(source_loc=MERGE_LOC1, start_line=2, end_line=2)]), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def merge_reader(store, model):
    return DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=merge_dossier(),
        memory=store.memory, model=model)


class MergePublishModel(PublishModel):
    """Two windows state the same atomic fact; window 1 may declare same_fact."""

    relate = True

    def assign(self, request):
        return dict(assignments=[dict(group_ref='grp-merged', note_ids=['n-0001', 'n-0002'],
            leaf_code='L-bizov-07', reason='员工显式 same_fact 组：两段陈述同一原子事实')])

    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        action = request.get('action')
        wid = (request.get('window') or {}).get('grant_id')
        if action in ('attribute', 'extract'):
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            if action == 'attribute':
                line = 1 if wid == '0' else 2
                return dict(ranges=[dict(entity='临时主体', start_line=line, end_line=line)])
            return dict(facts=[dict(entity='临时主体', attribute='测试指标', period='unknown',
                value='9', unit='个', value_type='number', quote='有9个',
                source_loc=LOC0 if wid == '0' else MERGE_LOC1,
                attribution_index=0, module_key=None)], complete=True, remaining_source_locs=[])
        if action == 'plan' and wid == '1':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            self.plan_calls += 1
            relations = ([dict(rel='same_fact', to_note_id='n-0001',
                               reason='员工判断两段陈述同一原子事实')] if self.relate else [])
            return dict(outcome='noted',
                creates=[dict(note_id='n-0002', candidate_index=0, status='verified',
                              relations=relations, open_questions=[])],
                revisions=[], retain_context_windows=[], retention_reason='无需保留')
        return super().execute(**kwargs)


def external_bump(store, *, operation_id='md-external-bump'):
    """A concurrent publisher advances L-bizov-07 outside the publication job."""
    ext = LeafPublishService(store.memory.database, store.memory, company_id=COMPANY,
        quote_resolver=DossierQuoteResolver({SHA: dossier().document}))
    try:
        publisher = MDLeafPublisher(ext, reading_protocol=2)
        windows = MDWindows(dossier())
        grant = windows.grant(windows.windows[0], 'sample.docx')
        grant['company_id'] = COMPANY
        publisher.submit(company=COMPANY, code='L-bizov-07', operation_id=operation_id,
            base_version=0, base_sha='',
            blocks=[dict(block_id='ext-base', text='外部基线陈述[^x]。',
                evidence=[dict(ref='x', grant_id='0', file_hash=SHA, original_name='sample.docx',
                    source_loc=LOC0, start_line=1, end_line=1, fact_ids=[])])],
            conflicts=[], grants={'0': grant}, job_id=JOB, note='外部并发发布')
    finally:
        ext.close()


def registry_counts(store):
    def count(table):
        exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)).fetchone()
        return store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] if exists else 0
    return {table: count(table) for table in
            ('sources', 'source_versions', 'source_evidence', 'fact_evidence_links')}


# ---------------------------------------------------------------------------
# Happy path: full A->B->C chain, atomic landing, idempotent replay.
# ---------------------------------------------------------------------------

def test_happy_path_publish_lands_facts_sources_leaves_and_replay_zero_call(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model)
    progress = result['mapped_notes']
    publication = progress['publication']
    assert result['done'] and publication['complete']
    assert result['calls']['publish'] == 3  # one assignment turn + two leaf turns
    job = store.db.execute('SELECT status,l2_committed,projection_fact_ids,message FROM parse_jobs WHERE job_id=?',
                           (JOB,)).fetchone()
    assert job['status'] == 'done' and job['l2_committed'] == 1
    assert '唯一归叶发布完成' in job['message']
    new_ids = sorted(json.loads(job['projection_fact_ids']))
    assert len(new_ids) == 2 and all(i > 1 for i in new_ids)
    # One ordinary note -> one formal fact, trunk from the chosen leaf only.
    facts = {r['id']: r for r in store.db.execute(
        'SELECT * FROM facts WHERE company_id=? ORDER BY id', (COMPANY,)).fetchall()}
    assert len(facts) == 3  # seed + two published
    for fact_id in new_ids:
        row = facts[fact_id]
        # 决策13A：V4单来源事实以 claimed 入账；confirm_mode='auto' 只表示
        # 自动处理，绝不等于业务核验，事实状态与来源关系是两条独立的轴。
        assert row['module_key'] == 'bizov' and row['status'] == 'claimed'
        assert row['confirm_mode'] == 'auto' and row['source_file'] == SHA
        span = json.loads(row['source_span'])
        assert span['reading_protocol'] == 4 and len(span['sources']) == 1
        assert span['sources'][0]['note_id'].startswith('n-000')
    by_note = {r['note_id']: r for r in store.db.execute(
        'SELECT * FROM md_note_publications WHERE company_id=? AND job_id=?', (COMPANY, JOB)).fetchall()}
    assert sorted(by_note) == ['n-0001', 'n-0002']
    assert by_note['n-0001']['leaf_code'] == 'L-bizov-07'
    assert by_note['n-0002']['leaf_code'] == 'L-bizov-08'
    assert by_note['n-0001']['fact_id'] != by_note['n-0002']['fact_id']
    identity_rows = store.db.execute(
        'SELECT fact_row_id,fact_uid,fact_version FROM fact_revisions '
        'WHERE company_id=? ORDER BY fact_row_id', (COMPANY,)).fetchall()
    assert {row['fact_row_id'] for row in identity_rows} == set(new_ids)
    placements = {row['fact_row_id']: row for row in store.db.execute(
        'SELECT * FROM fact_placements WHERE company_id=? ORDER BY fact_row_id',
        (COMPANY,)).fetchall()}
    assert set(placements) == set(new_ids)
    binding_revision = store.db.execute(
        'SELECT binding_revision FROM company_tree_meta WHERE company_id=?',
        (COMPANY,)).fetchone()['binding_revision']
    for note_publication in by_note.values():
        placement = placements[note_publication['fact_id']]
        assert placement['leaf_code'] == note_publication['leaf_code']
        assert placement['trunk_code'] == facts[note_publication['fact_id']]['module_key']
        assert placement['binding_revision'] == binding_revision
        assert placement['record_type'] == 'initial' and placement['migration_id'] is None
    assert store.db.execute(
        'SELECT count(*) FROM fact_current_placement WHERE company_id=?', (COMPANY,)
    ).fetchone()[0] == 2
    assert {r['leaf_code'] for r in store.db.execute('SELECT leaf_code FROM leaf_revisions').fetchall()} == \
        {'L-bizov-07', 'L-bizov-08'}
    assert store.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_bodies').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_notifications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_todos').fetchone()[0] == 0
    # Structured body evidence carries host-minted integer fact ids and note ids.
    body = json.loads(store.db.execute(
        "SELECT blocks FROM md_leaf_bodies WHERE leaf_code='L-bizov-07'").fetchone()[0])
    assert len(body) == 1 and body[0]['evidence'][0]['fact_ids'] == [by_note['n-0001']['fact_id']]
    assert body[0]['evidence'][0]['note_id'] == 'n-0001'
    # S1 registry: one distinct source/version, active supports links, no backfill.
    counts = registry_counts(store)
    assert counts == {'sources': 1, 'source_versions': 1,
                      'source_evidence': 2, 'fact_evidence_links': 2}
    registry = SourceRegistry(store.memory.database, company_id=COMPANY)
    try:
        for fact_id in new_ids:
            links = registry.links_for_fact(fact_id)
            assert len(links) == 1 and links[0]['relation'] == 'supports'
            assert links[0]['status'] == 'active'
            assert registry.distinct_source_ids_for_fact(fact_id) and \
                len(registry.distinct_source_ids_for_fact(fact_id)) == 1
        assert store.db.execute(
            "SELECT count(*) FROM fact_evidence_links WHERE status='legacy_backfilled'").fetchone()[0] == 0
    finally:
        registry.close()
    # Rollup consumption is part of the same stage C completion.
    assert len(publication['md_consumption']['consumed']) == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_consumption').fetchone()[0] == 2
    # Employee saw the template and current leaf, never fabricated fact ids.
    assign = next(r for r in model.publish_requests if r['operation'] == 'assign_leaves')
    assert {n['note_id'] for n in assign['notes']} == {'n-0001', 'n-0002'}
    assert any(leaf['leaf_code'] == 'L-bizov-07' for leaf in assign['template'])
    # Trunk name and verbatim scope come straight from the bound pack.
    bound = load_pack('generic')
    assert len(assign['template']) == len(bound.leaves)
    for item in assign['template']:
        leaf = bound.leaf_for(item['leaf_code'])
        assert item['trunk'] == leaf.trunk and item['title'] == leaf.title
        assert item['trunk_name'] == bound.trunk_directory(leaf.trunk)
        assert item['scope'] == leaf.scope
    compares = [r for r in model.publish_requests if r['operation'] == 'compare_submit']
    assert [r['target']['leaf_code'] for r in compares] == ['L-bizov-07', 'L-bizov-08']
    for compare in compares:
        leaf = bound.leaf_for(compare['target']['leaf_code'])
        assert compare['target']['trunk_name'] == bound.trunk_directory(leaf.trunk)
        assert compare['target']['scope'] == leaf.scope
    assert compares[0]['current']['version'] == 0 and compares[0]['groups'][0]['group_ref'] == 'grp-n-0001'
    # Full replay: durable receipts make every stage a zero-call no-op.
    before = len(model.requests)
    again = run_full(store, model)
    assert again['done'] and len(model.requests) == before
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM fact_revisions WHERE company_id=?',
                            (COMPANY,)).fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM fact_placements WHERE company_id=?',
                            (COMPANY,)).fetchone()[0] == 2


def test_assign_payload_passes_unreleased_generic_v2_scope_verbatim(v4_job):
    # Explicit load of the unreleased pack only; available_packs/binding untouched.
    store = v4_job
    pack = load_pack('generic-v2')
    service = NotesPublishService(store.memory.database, store.memory, dossier(),
        call=None, save=lambda: None, original_name='sample.docx')
    payload = service._assign_payload(dict(company_id=COMPANY), {}, {}, pack,
        {'fingerprint': 'fp'})
    assert len(payload['template']) == 60
    for item in payload['template']:
        leaf = pack.leaf_for(item['leaf_code'])
        assert item['trunk'] == leaf.trunk and item['title'] == leaf.title
        assert item['shape'] is None
        assert item['trunk_name'] == pack.trunk_directory(leaf.trunk)
        assert item['scope'] == leaf.scope and item['scope'].strip()
    assert all(item['trunk_name'].strip() for item in payload['template'])


# ---------------------------------------------------------------------------
# Unreleased generic-v2 draft pack: test-scoped binding injection, then the
# full V4 publish chain and the placement ledger under the restored gate.
# ---------------------------------------------------------------------------

@pytest.fixture
def v4_job_generic_v2(tmp_path, monkeypatch):
    """Isolated V4 job with NO binding yet; the test injects the draft
    generic-v2 binding in test scope only (the formal gate is patched for the
    single register call and restored before the chain runs)."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL', 'v4')
    memory = migrated_memory(tmp_path / 'ledger-v2.db', embedding=Embedding(), company_id=COMPANY)
    store = LeafPublishService(memory.database, memory, company_id=COMPANY)
    store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (SHA, 'sample.docx', 1, '/unused', 'now'))
    store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '测试指标', '2025-01', '9', '个', SHA, 'finance', 'now'))
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (JOB, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    yield store
    store.close()
    memory.close()


class FinanceV2PublishModel(PublishModel):
    """Same scripted stage C employee, assigning generic-v2 finance leaves."""

    leaf_for = {'n-0001': 'L-finance-01', 'n-0002': 'L-finance-02'}


def test_generic_v2_v4_publish_placement(v4_job_generic_v2, monkeypatch):
    store = v4_job_generic_v2
    pack = load_pack('generic-v2')
    # Test-scoped draft binding injection: the formal gate accepts only
    # "generic"; patch it for this single register call, restore immediately,
    # and run the whole V4 chain under the restored formal gate. Production
    # code, the release set and the SAM_FACTS_TREE default stay untouched.
    formal_pack = CompanyBindingStore._pack

    def draft_pack(name):
        return load_pack(name) if name == 'generic-v2' else formal_pack(name)

    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(draft_pack))
    store.binding.register(COMPANY, '临时草稿绑定公司', pack='generic-v2')
    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(formal_pack))

    # The packed-tree URI projection in the memory provider is a second
    # production guard that resolves only released packs. Allow the draft pack
    # through this seam for the duration of the chain only, then restore it;
    # neither bypass is written into any production implementation.
    from app.facts_tree import template_packs
    formal_released = template_packs.load_released_pack
    monkeypatch.setattr(template_packs, 'load_released_pack',
        lambda name='generic': load_pack(name) if name == 'generic-v2' else formal_released(name))
    model = FinanceV2PublishModel()
    result = run_full(store, model)
    monkeypatch.setattr(template_packs, 'load_released_pack', formal_released)
    publication = result['mapped_notes']['publication']
    assert result['done'] and publication['complete']
    job = store.db.execute('SELECT status,l2_committed,projection_fact_ids FROM parse_jobs WHERE job_id=?',
                           (JOB,)).fetchone()
    assert job['status'] == 'done' and job['l2_committed'] == 1
    new_ids = sorted(json.loads(job['projection_fact_ids']))
    assert len(new_ids) == 2 and all(i > 1 for i in new_ids)
    # One ordinary note -> one formal fact, trunk from the generic-v2 leaf.
    facts = {r['id']: r for r in store.db.execute(
        'SELECT * FROM facts WHERE company_id=?', (COMPANY,)).fetchall()}
    assert len(facts) == 3  # seed + two published
    for fact_id in new_ids:
        assert facts[fact_id]['module_key'] == 'finance'
        assert facts[fact_id]['status'] == 'claimed'
    by_note = {r['note_id']: r for r in store.db.execute(
        'SELECT * FROM md_note_publications WHERE company_id=? AND job_id=?', (COMPANY, JOB)).fetchall()}
    assert sorted(by_note) == ['n-0001', 'n-0002']
    assert by_note['n-0001']['leaf_code'] == 'L-finance-01'
    assert by_note['n-0002']['leaf_code'] == 'L-finance-02'
    meta = store.db.execute(
        'SELECT template_pack,pack_version,pack_digest,binding_revision FROM company_tree_meta '
        'WHERE company_id=?', (COMPANY,)).fetchone()
    assert meta['template_pack'] == 'generic-v2' and meta['pack_version'] == pack.version
    assert meta['pack_digest'] == pack.manifest_sha256
    placements = {r['fact_row_id']: r for r in store.db.execute(
        'SELECT * FROM fact_placements WHERE company_id=?', (COMPANY,)).fetchall()}
    assert set(placements) == set(new_ids)
    for note_publication in by_note.values():
        placement = placements[note_publication['fact_id']]
        assert placement['trunk_code'] == 'finance'
        assert placement['leaf_code'] == note_publication['leaf_code']
        assert placement['pack_id'] == 'generic-v2' and placement['pack_version'] == pack.version
        assert placement['manifest_sha256'] == pack.manifest_sha256
        assert placement['binding_revision'] == meta['binding_revision']
        assert placement['record_type'] == 'initial' and placement['migration_id'] is None
    # Exactly one current primary placement per published fact.
    current_rows = store.db.execute(
        'SELECT * FROM fact_current_placement WHERE company_id=?', (COMPANY,)).fetchall()
    assert len(current_rows) == 2
    assert {row['fact_row_id'] for row in current_rows} == set(new_ids)
    for row in current_rows:
        note_publication = next(p for p in by_note.values() if p['fact_id'] == row['fact_row_id'])
        assert row['trunk_code'] == 'finance' and row['leaf_code'] == note_publication['leaf_code']
        assert row['binding_revision'] == meta['binding_revision']
    # The employee saw the full 60-leaf draft template verbatim.
    assign = next(r for r in model.publish_requests if r['operation'] == 'assign_leaves')
    assert len(assign['template']) == 60
    compares = [r for r in model.publish_requests if r['operation'] == 'compare_submit']
    assert [r['target']['leaf_code'] for r in compares] == ['L-finance-01', 'L-finance-02']
    # The draft pack stays hidden and every restored production guard refuses
    # it again; the injected draft binding itself is untouched.
    assert 'generic-v2' not in available_packs()
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.register(COMPANY, '草稿门核验公司', pack='generic-v2')
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.bind_pack(COMPANY, 'generic-v2')
    with pytest.raises(ValueError, match='not released'):
        template_packs.load_released_pack('generic-v2')
    assert store.binding.get(COMPANY)['template_pack'] == 'generic-v2'


def test_generic_v2_v4_publish_readback(v4_job_generic_v2, monkeypatch):
    """P7 prerequisite chain: one isolated synthetic company completes the
    generic-v2 V4 publish under test-scoped draft seams, then the published
    facts replay through the rollup, the read-only tree/finance trunk/leaf/
    single-fact reads and the finance/leaf_code searches. Sources are checked
    only through the ledger's existing supports links and coordinates — no
    original download and no real hash re-verification is claimed."""
    store = v4_job_generic_v2
    pack = load_pack('generic-v2')
    # Same test-scoped seams as the placement test: the draft binding register
    # accepts the draft pack for this one call only and is restored before the
    # chain; the packed-URI projection guard allows the draft pack for the
    # duration of the chain plus the read-only replay, then is restored before
    # the formal gate assertions. No production guard or default is changed.
    formal_pack = CompanyBindingStore._pack

    def draft_pack(name):
        return load_pack(name) if name == 'generic-v2' else formal_pack(name)

    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(draft_pack))
    store.binding.register(COMPANY, '临时草稿绑定公司', pack='generic-v2')
    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(formal_pack))

    from app.facts_tree import template_packs
    formal_released = template_packs.load_released_pack
    monkeypatch.setattr(template_packs, 'load_released_pack',
        lambda name='generic': load_pack(name) if name == 'generic-v2' else formal_released(name))
    model = FinanceV2PublishModel()
    result = run_full(store, model)
    publication = result['mapped_notes']['publication']
    assert result['done'] and publication['complete']
    by_note = {r['note_id']: r for r in store.db.execute(
        'SELECT * FROM md_note_publications WHERE company_id=? AND job_id=?', (COMPANY, JOB)).fetchall()}
    assert sorted(by_note) == ['n-0001', 'n-0002']
    assert by_note['n-0001']['leaf_code'] == 'L-finance-01'
    assert by_note['n-0002']['leaf_code'] == 'L-finance-02'
    fact_id_1 = by_note['n-0001']['fact_id']
    fact_id_2 = by_note['n-0002']['fact_id']
    # Rollup consumption was part of the same stage C completion.
    assert len(publication['md_consumption']['consumed']) == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_consumption').fetchone()[0] == 2

    # Read-only replay through TreeReadService: the draft seam is still open
    # here so the packed generic-v2 URIs resolve through the projection guard.
    with TreeReadService(store.memory.database, store.memory, company_id=COMPANY) as reader:
        tree = reader.read_tree(COMPANY)
        assert tree['node'] == 'tree' and tree['state'] == 'ready'
        assert tree['binding']['template_pack'] == 'generic-v2'
        assert tree['binding']['pack_digest'] == pack.manifest_sha256
        finance_summary = next(t for t in tree['trunks'] if t['code'] == 'finance')
        assert (finance_summary['state'], finance_summary['generated_leaves']) == ('ready', 2)
        assert all(t['state'] == 'not_generated' for t in tree['trunks'] if t['code'] != 'finance')
        # Rollup readback: the L0 brief carries both published facts with their
        # verbatim claimed status (never upgraded) and declared source name.
        for fact_id in (fact_id_1, fact_id_2):
            assert f'F{fact_id} 状态：claimed' in tree['brief']
        assert 'sample.docx' in tree['brief']

        trunk = reader.read_trunk(COMPANY, 'finance')
        assert trunk['node'] == 'trunk' and trunk['state'] == 'ready'
        assert len(trunk['leaves']) == 7  # generic-v2 finance trunk
        entries = {leaf['code']: leaf for leaf in trunk['leaves']}
        for code in ('L-finance-01', 'L-finance-02'):
            assert (entries[code]['state'], entries[code]['version']) == ('active', 1)

        leaf = reader.read_leaf(COMPANY, 'L-finance-01')
        assert leaf['node'] == 'leaf' and leaf['state'] == 'ready'
        assert leaf['version'] == 1 and leaf['leaf_state'] == 'active'
        assert leaf['leaf']['trunk'] == 'finance'
        assert leaf['index']['sources'] == [SHA]
        assert [fact['fact_id'] for fact in leaf['facts']] == [fact_id_1]
        leaf_fact = leaf['facts'][0]
        assert leaf_fact['status'] == 'claimed'
        assert leaf_fact['source']['file_hash'] == SHA
        assert leaf_fact['source']['original_name'] == 'sample.docx'

        fact = reader.read_fact(COMPANY, fact_id_2)
        assert fact['node'] == 'fact' and fact['code'] == f'F{fact_id_2}'
        assert fact['binding']['template_pack'] == 'generic-v2'
        assert fact['fact']['status'] == 'claimed'
        assert (fact['leaf']['trunk'], fact['leaf']['code']) == ('finance', 'L-finance-02')
        # 来源回放只到账本坐标：支持链接与笔记/行区间元数据，不下载原件、不重算哈希。
        span = json.loads(fact['fact']['source']['span'])
        assert span['reading_protocol'] == 4 and len(span['sources']) == 1
        witness = span['sources'][0]
        assert witness['note_id'] == 'n-0002' and witness['source_loc'] == LOC1
        assert witness['ranges'] and all(len(r) == 2 for r in witness['ranges'])

        uri_1 = leaf['uri']
        uri_2 = reader.read_leaf(COMPANY, 'L-finance-02')['uri']
        both = sorted([uri_1, uri_2])
        assert reader.search_leaves(COMPANY, '', filters={'trunk_code': 'finance'}) == both
        assert reader.search_leaves(COMPANY, '有9个') == both
        assert reader.search_leaves(COMPANY, '', filters={'leaf_code': 'L-finance-02'}) == [uri_2]
        assert reader.search_leaves(COMPANY, '', filters={'trunk_code': 'governance'}) == []

    # 来源注册表回放：每条已发布事实恰好一条 active supports 链接，
    # 坐标仍指回映射的 V4 笔记；没有回填链接。
    registry = SourceRegistry(store.memory.database, company_id=COMPANY)
    try:
        for fact_id, note_id in ((fact_id_1, 'n-0001'), (fact_id_2, 'n-0002')):
            links = registry.links_for_fact(fact_id)
            assert len(links) == 1 and links[0]['relation'] == 'supports'
            assert links[0]['status'] == 'active'
            evidences = registry.evidence_for_fact(fact_id)
            assert len(evidences) == 1
            coordinate = json.loads(evidences[0]['coordinate'])
            assert coordinate['note_id'] == note_id and coordinate['reading_protocol'] == 4
    finally:
        registry.close()

    # 草稿缝已关闭：正式发布包仍隐藏 generic-v2，正式绑定与运行时加载
    # 依旧拒绝，功能门默认值仍为关闭；注入的草稿绑定本身保持原样。
    monkeypatch.setattr(template_packs, 'load_released_pack', formal_released)
    assert 'generic-v2' not in available_packs()
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.register(COMPANY, '草稿门核验公司', pack='generic-v2')
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.bind_pack(COMPANY, 'generic-v2')
    with pytest.raises(ValueError, match='not released'):
        template_packs.load_released_pack('generic-v2')
    assert store.binding.get(COMPANY)['template_pack'] == 'generic-v2'
    monkeypatch.delenv('SAM_FACTS_TREE', raising=False)
    assert runpy.run_path(settings.__file__)['SAM_FACTS_TREE'] is False


def test_merged_same_fact_group_one_fact_all_witnesses(v4_job):
    store = v4_job
    model = MergePublishModel()
    result = merge_reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 2  # seed + one merged fact
    rows = store.db.execute(
        'SELECT * FROM md_note_publications WHERE company_id=? AND job_id=? ORDER BY note_id',
        (COMPANY, JOB)).fetchall()
    assert len(rows) == 2 and rows[0]['fact_id'] == rows[1]['fact_id']
    fact_id = rows[0]['fact_id']
    placement = store.db.execute(
        'SELECT * FROM fact_current_placement WHERE company_id=? AND fact_row_id=?',
        (COMPANY, fact_id)).fetchone()
    assert placement['leaf_code'] == rows[0]['leaf_code']
    assert store.db.execute(
        'SELECT count(*) FROM fact_revisions WHERE fact_row_id=?', (fact_id,)
    ).fetchone()[0] == 1
    assert store.db.execute(
        'SELECT count(*) FROM fact_placements WHERE fact_row_id=?', (fact_id,)
    ).fetchone()[0] == 1
    span = json.loads(store.db.execute('SELECT source_span FROM facts WHERE id=?', (fact_id,)).fetchone()[0])
    assert [w['note_id'] for w in span['sources']] == ['n-0001', 'n-0002']
    assert {w['source_loc'] for w in span['sources']} == {LOC0, MERGE_LOC1}
    # 决策13A：两名见证人/两个来源片段也不机械升级为 verified。
    fact = store.db.execute('SELECT status,confirm_mode FROM facts WHERE id=?', (fact_id,)).fetchone()
    assert fact['status'] == 'claimed' and fact['confirm_mode'] == 'auto'
    # Both member witnesses landed as evidence with active supports links.
    registry = SourceRegistry(store.memory.database, company_id=COMPANY)
    try:
        links = registry.links_for_fact(fact_id)
        assert len(links) == 2 and all(l['relation'] == 'supports' and l['status'] == 'active' for l in links)
        evidences = registry.evidence_for_fact(fact_id)
        assert len(evidences) == 2
        note_ids = {json.loads(e['coordinate']).get('note_id') for e in evidences}
        assert note_ids == {'n-0001', 'n-0002'}
        assert len(registry.distinct_source_ids_for_fact(fact_id)) == 1  # fragments of one file
    finally:
        registry.close()
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Group validation: explicit same_fact + identical fields, never host-inferred.
# ---------------------------------------------------------------------------

class GroupingModel(PublishModel):
    """First assignment groups both notes together; the corrected one splits."""

    def assign(self, request):
        if 'format_feedback' not in request:
            return dict(assignments=[dict(group_ref='grp-all', note_ids=['n-0001', 'n-0002'],
                leaf_code='L-bizov-07', reason='员工尝试合并两条笔记')])
        return super().assign(request)


def test_group_field_mismatch_rejected_then_corrected(v4_job):
    store = v4_job
    plans = {'1': dict(outcome='noted',
        creates=[dict(note_id='n-0002', candidate_index=0, status='verified',
            relations=[dict(rel='same_fact', to_note_id='n-0001', reason='员工声明同事实')],
            open_questions=[])],
        revisions=[], retain_context_windows=[], retention_reason='无需保留')}
    model = GroupingModel(plans=plans)
    result = run_full(store, model)
    assert result['done']
    publication = result['mapped_notes']['publication']
    assert publication['assignment_attempts'] == 1
    error = publication['assignment_rejected'][0]['error']
    assert 'different fact fields' in error and 'grp-all' in error
    # Corrected split published both notes as independent facts.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2


def test_group_without_same_fact_relation_rejected_then_corrected(v4_job):
    store = v4_job

    class GroupingMergeModel(MergePublishModel):
        relate = False

        def assign(self, request):
            if 'format_feedback' not in request:
                return super().assign(request)  # merged group without relations
            return PublishModel.assign(self, request)

    model = GroupingMergeModel()
    result = merge_reader(store, model).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    assert result['done']
    publication = result['mapped_notes']['publication']
    assert publication['assignment_attempts'] == 1
    assert 'same_fact relations' in publication['assignment_rejected'][0]['error']
    # Corrected: two independent one-note groups, two facts, nothing merged.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3


@pytest.mark.parametrize('bad, snippet', [
    ('duplicate', 'assigned twice'),
    ('unhandled', 'unhandled verified notes'),
    ('ghost', 'does not exist in this job'),
])
def test_bad_assignment_rejected_then_corrected(v4_job, bad, snippet):
    store = v4_job

    class BadOnceModel(PublishModel):
        def assign(self, request):
            if 'format_feedback' in request:
                return super().assign(request)
            if bad == 'duplicate':
                groups = [dict(group_ref='grp-a', note_ids=['n-0001'], leaf_code='L-bizov-07', reason='重复归叶'),
                          dict(group_ref='grp-b', note_ids=['n-0001', 'n-0002'], leaf_code='L-bizov-08', reason='重复归叶')]
            elif bad == 'unhandled':
                groups = [dict(group_ref='grp-a', note_ids=['n-0001'], leaf_code='L-bizov-07', reason='漏掉一条')]
            else:
                groups = [dict(group_ref='grp-a', note_ids=['n-0001', 'n-ghost'], leaf_code='L-bizov-07', reason='混入别任务笔记'),
                          dict(group_ref='grp-b', note_ids=['n-0002'], leaf_code='L-bizov-08', reason='正常')]
            return dict(assignments=groups)

    model = BadOnceModel()
    result = run_full(store, model)
    assert result['done']
    publication = result['mapped_notes']['publication']
    assert publication['assignment_attempts'] == 1
    assert snippet in publication['assignment_rejected'][0]['error']
    assert len(model.publish_requests) == 4  # bad assign + good assign + two leaf turns
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2


def test_assignment_correction_exhausted_blocks_then_explicit_resume(v4_job):
    store = v4_job

    class AlwaysBadModel(PublishModel):
        def assign(self, request):
            return dict(assignments=[dict(group_ref='grp-x', note_ids=['n-0001'],
                leaf_code='L-bizov-07', reason='永远漏掉一条笔记')])

    model = AlwaysBadModel()
    result = run_full(store, model)
    assert result['status'] == 'partial' and not result.get('done')
    state = load_state(store)
    assert state['mapped_notes']['publication']['assignment_blocked'] is True
    assert state['mapped_notes']['publication']['assignment_attempts'] == 2
    assert_formal_zero(store)
    # Ordinary resume re-asks the employee but the same bad employee blocks again;
    # an explicit resume with a corrected employee completes the chain.
    result2 = run_full(store, model)
    assert result2['status'] == 'partial'
    good = PublishModel()
    result3 = run_full(store, good, resume_unresolved=True)
    assert result3['done'] and result3['mapped_notes']['publication']['complete']
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Publication gate semantics: needs_check blocks; rejected never publishes.
# ---------------------------------------------------------------------------

class GateReviewModel(PublishModel):
    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'review':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            nid = request['note']['note_id']
            if nid == 'n-0001':
                return dict(note_id=nid, action='mark_needs_check', reason='员工保持待核实')
            return dict(note_id=nid, action='confirmed', reason='员工确认')
        return super().execute(**kwargs)


def test_needs_check_blocks_publish_gate_and_direct_publish_rejected(v4_job):
    store = v4_job
    model = GateReviewModel()
    result = run_full(store, model)
    progress = result['mapped_notes']
    assert result['done'] and progress['review_complete'] and not progress['awaiting_publication']
    assert progress['open_notes'] == 1
    assert model.publish_requests == []  # stage C never entered
    assert_formal_zero(store)
    # Defense in depth: even a tampered checkpoint cannot publish open notes;
    # the authoritative gate re-queries the durable note ledger.
    state = load_state(store)
    state['mapped_notes']['awaiting_publication'] = True
    state['mapped_notes']['open_notes'] = 0
    with pytest.raises(PublishRejected, match='needs_check'):
        direct_publish(store, PublishModel(), state, save=lambda: None)


class RejectingReviewModel(PublishModel):
    def execute(self, **kwargs):
        request = json.loads(kwargs['text'])
        if request.get('action') == 'review' and request['note']['note_id'] == 'n-0001':
            self.requests.append(deepcopy(request))
            self.skills.append(kwargs['skill_text'])
            return dict(note_id='n-0001', action='reject', reason='员工复核废弃该陈述')
        return super().execute(**kwargs)


def test_rejected_note_excluded_publishes_fine(v4_job):
    store = v4_job
    model = RejectingReviewModel()
    result = run_full(store, model)
    progress = result['mapped_notes']
    assert result['done'] and progress['awaiting_publication'] and progress['discarded_notes'] == 1
    rows = store.db.execute(
        'SELECT note_id FROM md_note_publications WHERE company_id=? AND job_id=?', (COMPANY, JOB)).fetchall()
    assert [r['note_id'] for r in rows] == ['n-0002']
    assert store.db.execute("SELECT status FROM md_read_notes WHERE note_id='n-0001'").fetchone()[0] == 'rejected'
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 2


def test_rejected_note_in_assignment_rejected_then_corrected(v4_job):
    store = v4_job

    class IncludeRejectedModel(RejectingReviewModel):
        def assign(self, request):
            if 'format_feedback' not in request:
                return dict(assignments=[
                    dict(group_ref='grp-1', note_ids=['n-0001'], leaf_code='L-bizov-07', reason='误含废弃笔记'),
                    dict(group_ref='grp-2', note_ids=['n-0002'], leaf_code='L-bizov-08', reason='正常归叶')])
            return super().assign(request)

    model = IncludeRejectedModel()
    result = run_full(store, model)
    assert result['done']
    publication = result['mapped_notes']['publication']
    assert publication['assignment_attempts'] == 1
    assert 'never publishes' in publication['assignment_rejected'][0]['error']
    rows = store.db.execute(
        'SELECT note_id FROM md_note_publications WHERE company_id=? AND job_id=?', (COMPANY, JOB)).fetchall()
    assert [r['note_id'] for r in rows] == ['n-0002']


# ---------------------------------------------------------------------------
# CAS, append-only retention, conflict todos, atomicity, correction loops.
# ---------------------------------------------------------------------------

def test_base_changed_rereads_and_resubmits(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model, limits=REVIEW_LIMITS)  # stop at stage B
    assert result['done'] and result['mapped_notes']['awaiting_publication']
    state = load_state(store)
    saves = {'count': 0}

    def hook_save():
        saves['count'] += 1
        kv_save(store, state)
        if saves['count'] == 3:
            # Assignment + base read + draft are persisted; a concurrent
            # publisher then advances the leaf before our commit.
            external_bump(store)

    direct_publish(store, model, state, save=hook_save)
    publication = state['mapped_notes']['publication']
    assert publication['complete']
    item = publication['items']['L-bizov-07']
    assert item['base_changes'] == 1 and item['status'] == 'submitted'
    compares = [r for r in model.publish_requests if r['operation'] == 'compare_submit'
                and r['target']['leaf_code'] == 'L-bizov-07']
    assert len(compares) == 2 and compares[1]['current']['version'] == 1
    # The resubmission retained the concurrent block; final version is 2.
    current = MDLeafPublisher(v4_job, reading_protocol=4).current(COMPANY, 'L-bizov-07')
    assert current['version'] == 2
    assert [b['block_id'] for b in current['blocks']] == ['ext-base', 'pub-grp-n-0001']


def test_existing_blocks_retained_and_conflict_todo(v4_job):
    store = v4_job
    external_bump(store)

    class ConflictModel(PublishModel):
        def submit_leaf(self, request):
            if request['target']['leaf_code'] != 'L-bizov-07':
                return super().submit_leaf(request)
            if 'correction_feedback' not in request:
                bad = super().submit_leaf(request)
                bad['blocks'][0]['text'] = '员工擅自改写既有块[^x]。'
                return bad
            good = super().submit_leaf(request)
            good['conflicts'] = [dict(old_block_id='ext-base',
                new_block=dict(block_id='fix-ext', text='更准陈述：有9个[^c]。',
                    evidence=[dict(ref='c', grant_id='0', file_hash=SHA, original_name='sample.docx',
                        source_loc=LOC0, start_line=1, end_line=1,
                        fact_refs=['grp-n-0001'], note_id='n-0001')]),
                reason='员工判断既有块表述与笔记冲突，留待办不裁决')]
            return good

    model = ConflictModel()
    result = run_full(store, model)
    assert result['done']
    item = result['mapped_notes']['publication']['items']['L-bizov-07']
    assert item['correction_round'] == 1 and len(item['rejected_drafts']) == 1
    assert 'existing content/index must be retained' in item['rejected_drafts'][0]['reason']
    todos = store.db.execute('SELECT * FROM md_leaf_todos').fetchall()
    assert len(todos) == 1 and todos[0]['leaf_code'] == 'L-bizov-07' and todos[0]['status'] == 'pending'
    payload = json.loads(todos[0]['payload'])
    assert payload['position'] == 'ext-base' and payload['old']['text'] == '外部基线陈述[^x]。'
    assert payload['new']['block_id'] == 'fix-ext'
    current = MDLeafPublisher(v4_job, reading_protocol=4).current(COMPANY, 'L-bizov-07')
    assert current['version'] == 2
    # Old block kept byte-identical; the conflict candidate never entered the leaf.
    assert [(b['block_id'], b['text']) for b in current['blocks']] == [
        ('ext-base', '外部基线陈述[^x]。'), ('pub-grp-n-0001', '技术描述可回查，有9个[^a]。')]


def test_bad_block_rolls_back_facts_sources_links_then_correction(v4_job):
    store = v4_job

    class UnionFixModel(PublishModel):
        def submit_leaf(self, request):
            draft = super().submit_leaf(request)
            if request['target']['leaf_code'] == 'L-bizov-07' and 'correction_feedback' not in request:
                draft['blocks'][-1]['text'] = '数值是10[^a]。'  # 10 is in no fact value or quote
            return draft

    model = UnionFixModel()
    result = run_full(store, model)
    assert result['done']
    item = result['mapped_notes']['publication']['items']['L-bizov-07']
    assert item['correction_round'] == 1 and len(item['rejected_drafts']) == 1
    # The rejected attempt committed nothing: no leaked fact mint, no registry
    # rows, no leaf writes, no publication rows beyond the successful retry.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM fact_revisions WHERE company_id=?',
                            (COMPANY,)).fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM fact_placements WHERE company_id=?',
                            (COMPANY,)).fetchone()[0] == 2
    assert registry_counts(store) == {'sources': 1, 'source_versions': 1,
                                      'source_evidence': 2, 'fact_evidence_links': 2}


def test_forged_evidence_correction_loop(v4_job):
    store = v4_job

    class ForgingModel(PublishModel):
        def submit_leaf(self, request):
            draft = super().submit_leaf(request)
            if request['target']['leaf_code'] != 'L-bizov-07':
                return draft
            feedback = request.get('correction_feedback')
            if feedback is None:
                draft['blocks'][-1]['evidence'][0]['note_id'] = 'n-0002'  # not a member witness
            elif 'member witness' in str(feedback.get('reason')):
                draft['blocks'][-1]['evidence'][0]['end_line'] = 99  # outside the read grant
            return draft

    model = ForgingModel()
    result = run_full(store, model)
    assert result['done']
    item = result['mapped_notes']['publication']['items']['L-bizov-07']
    assert item['correction_round'] == 2 and len(item['rejected_drafts']) == 2
    assert 'member witness' in item['rejected_drafts'][0]['reason']
    assert 'outside read grant' in item['rejected_drafts'][1]['reason']
    compares = [r for r in model.publish_requests if r['operation'] == 'compare_submit'
                and r['target']['leaf_code'] == 'L-bizov-07']
    assert len(compares) == 3
    assert 'correction_feedback' in compares[1] and 'correction_feedback' in compares[2]
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Crash/replay idempotency and hard rejections of second assignments.
# ---------------------------------------------------------------------------

def test_crash_after_commit_replay_zero_calls_no_duplicates(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model, limits=REVIEW_LIMITS)
    assert result['done'] and result['mapped_notes']['awaiting_publication']
    state = load_state(store)
    saves = {'count': 0}

    def crashing_save():
        saves['count'] += 1
        if saves['count'] == 4:
            raise RuntimeError('synthetic crash after commit')
        kv_save(store, state)

    with pytest.raises(RuntimeError, match='synthetic crash'):
        direct_publish(store, model, state, save=crashing_save)
    # The first leaf committed durably although the checkpoint save crashed.
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 1
    state2 = load_state(store)
    assert state2['mapped_notes']['publication']['items']['L-bizov-07']['status'] == 'assigned'
    before = len(model.requests)
    direct_publish(store, model, state2)
    publication = state2['mapped_notes']['publication']
    assert publication['complete']
    # The committed leaf replayed from durable receipts with zero model calls;
    # only the second leaf needed its compare turn.
    new_requests = model.requests[before:]
    assert len(new_requests) == 1
    assert new_requests[0]['operation'] == 'compare_submit'
    assert new_requests[0]['target']['leaf_code'] == 'L-bizov-08'
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2
    assert registry_counts(store) == {'sources': 1, 'source_versions': 1,
                                      'source_evidence': 2, 'fact_evidence_links': 2}


def test_second_assignment_of_published_note_hard_rejected(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model)
    assert result['done']
    # A tampered/redriven publication checkpoint must hit the durable
    # publication rows: second assignment and cross-leaf replay never write.
    state = load_state(store)
    state['mapped_notes']['publication'] = dict(assignments=None, assignment_attempts=0,
        assignment_rejected=[], items={}, order=[], cursor=0, complete=False)

    class CrossLeafModel(PublishModel):
        leaf_for = {'n-0001': 'L-bizov-08', 'n-0002': 'L-bizov-07'}

    with pytest.raises(ComparisonStopped, match='assignment correction exhausted'):
        direct_publish(store, CrossLeafModel(), state, save=lambda: None)
    publication = state['mapped_notes']['publication']
    assert publication['assignment_blocked'] is True
    assert 'already published' in publication['assignment_rejected'][0]['error']
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Unresolved leaves: partial, silent ordinary resume, explicit retry.
# ---------------------------------------------------------------------------

def test_unresolved_leaf_partial_ordinary_resume_silent_explicit_resume_retries(v4_job):
    store = v4_job

    class UnresolvedModel(PublishModel):
        unresolved = True

        def submit_leaf(self, request):
            if self.unresolved and request['target']['leaf_code'] == 'L-bizov-08':
                return dict(outcome='unresolved', reason='员工无法决定该叶内容，留待显式续跑')
            return super().submit_leaf(request)

    model = UnresolvedModel()
    result = run_full(store, model)
    assert result['status'] == 'partial' and not result.get('done')
    state = load_state(store)
    publication = state['mapped_notes']['publication']
    assert publication['items']['L-bizov-07']['status'] == 'submitted'
    assert publication['items']['L-bizov-08']['status'] == 'unresolved'
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 2  # seed + L-bizov-07
    # Ordinary resume: decided leaves and the unresolved item cost zero calls.
    before = len(model.requests)
    result2 = run_full(store, model)
    assert result2['status'] == 'partial' and len(model.requests) == before
    # Explicit resume_unresolved reopens only the unresolved leaf.
    model.unresolved = False
    result3 = run_full(store, model, resume_unresolved=True)
    assert result3['done'] and result3['mapped_notes']['publication']['complete']
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Durability guards, gate, and the frozen no-publish-budget behavior.
# ---------------------------------------------------------------------------

def test_publication_rows_append_only(v4_job):
    store = v4_job
    model = PublishModel()
    assert run_full(store, model)['done']
    with pytest.raises(Exception, match='append-only note publication audit'):
        store.db.execute("UPDATE md_note_publications SET leaf_code='L-x' WHERE note_id='n-0001'")
    store.db.rollback()
    with pytest.raises(Exception, match='append-only note publication audit'):
        store.db.execute("DELETE FROM md_note_publications WHERE note_id='n-0001'")
    store.db.rollback()


def test_gate_closed_constructor_raises(v4_job, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(RuntimeError, match='facts tree disabled'):
        NotesPublishService(v4_job.memory.database, v4_job.memory, dossier(),
            call=lambda action, payload: {}, save=lambda: None, original_name='sample.docx')


def test_no_publish_budget_completes_at_stage_b(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model, limits=REVIEW_LIMITS)
    progress = result['mapped_notes']
    # V1 behavior frozen: review completes, the gate is awaiting publication,
    # and without an explicit publish budget nothing is written or requested.
    assert result['done'] and progress['review_complete'] and progress['awaiting_publication']
    assert 'publication' not in progress
    assert model.publish_requests == []
    job = store.db.execute('SELECT status,l2_committed,message FROM parse_jobs WHERE job_id=?', (JOB,)).fetchone()
    assert job['status'] == 'done' and job['l2_committed'] == 0
    assert '发布待显式 publish 阶段预算' in job['message']
    assert_formal_zero(store)
    # A later run with the seventh budget resumes publication without replaying
    # reading or review calls.
    before = len(model.requests)
    result2 = run_full(store, model)
    assert result2['done'] and result2['mapped_notes']['publication']['complete']
    new_actions = [r['action'] for r in model.requests[before:]]
    assert new_actions == ['publish', 'publish', 'publish']
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2

def test_publish_rejects_missing_binding_identity_before_publication_state(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model, limits=REVIEW_LIMITS)
    assert result['done'] and result['mapped_notes']['review_complete']
    state = load_state(store)
    state['mapped_notes'].pop('binding_identity')
    kv_save(store, state)
    before = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (KV_KEY,)).fetchone()[0]
    rows_before = formal_table_count(store.db, 'md_note_publications')
    replay = PublishModel()
    with pytest.raises(PublishRejected, match='V4 notes checkpoint'):
        direct_publish(store, replay, state)
    after = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (KV_KEY,)).fetchone()[0]
    rows_after = formal_table_count(store.db, 'md_note_publications')
    assert after == before
    assert 'publication' not in state['mapped_notes']
    assert replay.requests == []
    assert rows_after == rows_before


def test_completed_publish_still_rejects_binding_identity_mismatch(v4_job):
    store = v4_job
    model = PublishModel()
    result = run_full(store, model)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    state = load_state(store)
    state['mapped_notes']['binding_identity']['binding_revision'] += 1
    kv_save(store, state)
    before = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (KV_KEY,)).fetchone()[0]
    rows_before = formal_table_count(store.db, 'md_note_publications')
    replay = PublishModel()
    with pytest.raises(PublishRejected, match='binding identity mismatch'):
        direct_publish(store, replay, state)
    after = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (KV_KEY,)).fetchone()[0]
    rows_after = formal_table_count(store.db, 'md_note_publications')
    assert after == before
    assert replay.requests == []
    assert rows_after == rows_before
