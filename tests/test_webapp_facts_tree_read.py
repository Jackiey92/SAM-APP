"""F-004-API-001 local route: host scope only, stable errors, no old-list fallback."""
import pytest

from app import settings
from app.facts_tree.rollup import FactsTreeRollup
from app.memory import paths
from tests.test_facts_tree_leaves import kernel, artifact, publish, COMPANY, SHA
from tests.test_facts_tree_tree_read import db_path, rolled


@pytest.fixture
def tree_web(kernel, monkeypatch):
    from webapp import app as web
    monkeypatch.setattr(web, 'MAIN_DB', db_path(kernel))
    monkeypatch.setitem(web.app.extensions, 'sam_memory_provider', kernel.memory)
    monkeypatch.setenv('SAM_COMPANY_ID', COMPANY)
    web._kernel = kernel
    return web


def test_disabled_gate_503(tree_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    result = tree_web.app.test_client().get('/api/facts/tree')
    assert result.status_code == 503
    assert result.get_json()['error'] == 'facts_tree_disabled'


def test_missing_host_scope_503(tree_web, monkeypatch):
    monkeypatch.delenv('SAM_COMPANY_ID')
    result = tree_web.app.test_client().get('/api/facts/tree')
    assert result.status_code == 503
    assert result.get_json()['error'] == 'company_scope_missing'


def test_company_id_rejected_400(tree_web):
    client = tree_web.app.test_client()
    for request in (lambda: client.get('/api/facts/tree?company_id=' + COMPANY),
                    lambda: client.get('/api/facts/tree?company_id=other'),
                    lambda: client.get('/api/facts/tree', data={'company_id': COMPANY}),
                    lambda: client.get('/api/facts/tree', json={'company_id': 'other'})):
        result = request()
        assert result.status_code == 400, result.data
        assert result.get_json()['error'] == 'company_id_rejected'


def test_invalid_parameters_400(tree_web):
    client = tree_web.app.test_client()
    for query in ('node=branch', 'node=trunk', 'node=leaf', 'node=trunk&code=',
                  'node=tree&code=bizov', 'code=bizov', 'format=json'):
        result = client.get('/api/facts/tree?' + query)
        assert result.status_code == 400, (query, result.data)
        assert result.get_json()['error'] == 'invalid_parameter'


def test_ready_tree_trunk_leaf_200(tree_web):
    rolled(tree_web._kernel)
    client = tree_web.app.test_client()
    tree = client.get('/api/facts/tree').get_json()
    assert tree['state'] == 'ready' and tree['node'] == 'tree'
    assert tree['company_id'] == COMPANY and tree['binding']['template_pack'] == 'generic'
    assert '有9个' in tree['brief']
    trunk = client.get('/api/facts/tree?node=trunk&code=bizov').get_json()
    assert trunk['state'] == 'ready' and trunk['code'] == 'bizov'
    assert next(l for l in trunk['leaves'] if l['code'] == 'L-bizov-08')['state'] == 'active'
    leaf = client.get('/api/facts/tree?node=leaf&code=L-bizov-08').get_json()
    assert leaf['state'] == 'ready' and leaf['version'] == 1
    assert leaf['content'] == artifact()


def test_unknown_code_404_and_unbound_200(tree_web, monkeypatch):
    client = tree_web.app.test_client()
    assert client.get('/api/facts/tree?node=trunk&code=nope').status_code == 404
    assert client.get('/api/facts/tree?node=leaf&code=L-hr-99').status_code == 404
    empty = client.get('/api/facts/tree').get_json()
    assert empty['state'] == 'empty'
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-unbound')
    result = client.get('/api/facts/tree')
    assert result.status_code == 200 and result.get_json()['state'] == 'unbound'


def test_stale_projection_409(tree_web):
    kernel = tree_web._kernel
    rolled(kernel)
    kernel.memory.put(paths.tree_brief_uri(COMPANY), '伪造结论', metadata={})
    result = tree_web.app.test_client().get('/api/facts/tree')
    assert result.status_code == 409
    assert result.get_json()['error'] == 'consistency_failed'


def test_no_fallback_to_flat_facts(tree_web):
    # A verified flat facts row never fabricates tree content.
    result = tree_web.app.test_client().get('/api/facts/tree').get_json()
    assert result['state'] == 'empty' and result['brief'] is None
    assert all(t['state'] == 'not_generated' for t in result['trunks'])
