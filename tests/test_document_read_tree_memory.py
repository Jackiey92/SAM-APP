"""B3b new-company writes are tree-only; legacy closed golden stays untouched."""
import pytest

from app.document_read import service as reader_service
from app.memory.company_facts.l2_consolidation import ConsolidationService
from tests.test_document_read import task
from tests.test_document_read_leaves import open_task, LeafModel, service, run, CODES, revisions


def assert_tree_only(task, model):
    docs = task[2].docs
    assert not any('2b_L2' in uri or uri.rsplit('/', 1)[-1].startswith('F') for uri in docs)
    assert not any(uri in docs for uri in ('sam://facts/acc/brief.md', 'sam://facts/acc/brief.json',
                                         'sam://facts/acc/guide.md', 'sam://facts/acc/guide.json'))
    assert all('/leaves/' in uri or uri.endswith(('trunk-brief.md', 'trunk-guide.md',
                                                 'tree-brief.md', 'tree-guide.md')) for uri in docs)
    assert not any(action in ('brief', 'guide') for action in model.actions)
    facts = ConsolidationService(task[0]).list_facts(company_id='acc')
    assert len(facts) == 1 and facts[0]['value'] == '12.00' and facts[0]['status'] == 'verified'
    return facts


def test_open_skips_all_legacy_services_and_provider_rebuild(open_task, monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail('gate-open invoked legacy memory path')
    monkeypatch.setattr(reader_service.L2ProjectionService, 'publish', forbidden)
    monkeypatch.setattr(reader_service.L1BriefService, 'rebuild', forbidden)
    monkeypatch.setattr(reader_service.L0GuideService, 'rebuild', forbidden)
    monkeypatch.setattr(open_task[2], 'rebuild_projection', forbidden, raising=False)
    writes = []
    original = open_task[2].put
    def put(uri, content, **kwargs):
        assert kwargs.get('metadata', {}).get('layer') != '2b_L2'
        writes.append(uri)
        return original(uri, content, **kwargs)
    monkeypatch.setattr(open_task[2], 'put', put)
    model = LeafModel(open_task, codes=[CODES[0]])
    result = run(service(open_task, model), extract=1)
    assert result['done'] and result['leaves_done']
    assert result['brief_done'] is False and result['guide_done'] is False
    assert result['calls'] == dict(classify=1, attribute=1, extract=1, plan=1, leaf=1)
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    facts = assert_tree_only(open_task, model)
    from app.db.database import connect
    with connect(open_task[0]) as conn:
        assert conn.execute('SELECT fact_id FROM fact_leaf_links').fetchone()[0] == facts[0]['id']
    leaf = result['leaves_built'][0]['uri']
    expected = {leaf, 'sam://facts/acc/02-业务概览/trunk-brief.md',
                'sam://facts/acc/02-业务概览/trunk-guide.md',
                'sam://facts/acc/tree-brief.md', 'sam://facts/acc/tree-guide.md'}
    assert set(writes) == set(open_task[2].docs) == expected
    assert all(open_task[2].read(uri).strip() for uri in expected)


def test_partial_and_resume_never_write_legacy_outputs(open_task):
    model = LeafModel(open_task)
    result = run(service(open_task, model), extract=1, leaf=1)
    assert result['status'] == 'partial' and len(result['leaves_built']) == 1
    assert_tree_only(open_task, model)
    first = revisions(open_task)[0]
    result = run(service(open_task, model), extract=1)
    assert result['done'] and result['leaves_done'] and len(result['leaves_built']) == 2
    assert result['brief_done'] is False and result['guide_done'] is False
    assert result['calls']['extract'] == 1
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf', 'leaf']
    assert model.targets == CODES and revisions(open_task)[0] == first
    assert_tree_only(open_task, model)


def test_all_bad_leaves_keep_sqlite_facts_without_legacy_fallback(open_task):
    model = LeafModel(open_task, faults={code: 'coordinate' for code in CODES})
    result = run(service(open_task, model), extract=1)
    assert result['done'] and result['leaves_done'] and len(result['leaves_failed']) == 2
    assert not result['leaves_built'] and not open_task[2].docs
    assert result['brief_done'] is False and result['guide_done'] is False
    assert_tree_only(open_task, model)


def test_gate_open_does_not_clean_existing_legacy_documents(open_task):
    historic = {'sam://facts/acc/02-业务概览/F99.md': 'historic fact card bytes',
                'sam://facts/acc/brief.md': 'historic brief bytes',
                'sam://facts/acc/guide.md': 'historic guide bytes'}
    open_task[2].docs.update(historic)
    model = LeafModel(open_task, codes=[CODES[0]])
    result = run(service(open_task, model), extract=1)
    assert result['done'] and result['leaves_done']
    assert not result['brief_done'] and not result['guide_done']
    assert {uri:open_task[2].read(uri) for uri in historic} == historic
    assert model.actions == ['classify', 'attribute', 'extract', 'plan', 'leaf']
    assert ConsolidationService(open_task[0]).list_facts(company_id='acc')
