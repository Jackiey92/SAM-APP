"""Cloud F-004-API-002: citations resolve from the dedicated snapshot only.

Synthetic envelopes only; the snapshot channel carries no original binaries,
so every located original reports an explicit unavailable state. The homepage
snapshot is never a fallback and the local DB is never touched.
"""
from app import settings
from tests.test_cloud_home import cloud_web, snapshot  # reused fixture/helpers
from tests.test_facts_tree_leaves import SHA

V2_CONTENT = ('## 内容\n有9个[^a]。\n\n## 索引\n'
              f'[^a]: real.docx → docx:body/77/paragraph (lines 77-78; block b1; sha {SHA}; facts 1)\n')
V1_HASH_CONTENT = f'叶正文。[^one]\n\n[^one]: {SHA} docx:body/77/paragraph\n'
V1_NAME_CONTENT = '叶正文。[^one]\n\n[^one]: real.docx docx:body/77/paragraph\n'


def leaf_entry(content, *, company='acc-cloud', code='L-bizov-08', state='ready'):
    entry = {'company_id': company, 'binding': {'template_pack': 'generic'},
             'node': 'leaf', 'code': code, 'state': state,
             'leaf': {'code': code, 'trunk': 'bizov', 'title': '测试叶子',
                      'shape': 'n', 'scope': 'company'},
             'uri': None, 'version': 2, 'leaf_state': 'active',
             'index': {'leaf_code': code, 'trunk': 'bizov', 'title': '测试叶子',
                       'company_id': company, 'status': 'active', 'sources': [SHA],
                       'source_file_count': 1, 'version': 2, 'latest_period': '2025-01',
                       'created_at': '2026-10-14', 'updated_at': '2026-10-14',
                       'owner_skill': 'fact-leaf'},
             'content': content}
    if state != 'ready':
        entry.update(version=None, leaf_state=None, index=None, content=None)
    return entry


def section(company='acc-cloud', leaf=None):
    """A synthetic ready section carrying a valid placement contract marker."""
    from app.facts_tree.tree_read import attach_placement_validation
    binding = {'template_pack': 'generic', 'pack_version': 1, 'binding_revision': 1,
               'pack_digest': 'a' * 64, 'digest_source': 'verified-manifest'}
    sec = {'company_id': company, 'state': 'ready',
           'tree': {'company_id': company, 'binding': binding,
                    'node': 'tree', 'code': None, 'state': 'ready', 'brief': '树简报原文'},
           'trunks': {},
           'leaves': {} if leaf is None else {leaf['code']: leaf}}
    return attach_placement_validation(sec)


def store_section(cloud_web, sec, *, company='acc-cloud'):
    cloud_web.app.extensions['sam_facts_tree_snapshot_store'].replace(
        {'version': 1, 'company_id': company, 'pushed_at': 2.5,
         'payload': {'facts_tree': sec}})


def get_source(cloud_web, query='leaf_code=L-bizov-08&ref=a'):
    return cloud_web.app.test_client().get('/api/facts/tree/source?' + query)


def test_cloud_v2_ref_located_but_original_unavailable(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(V2_CONTENT)))
    result = get_source(cloud_web)
    assert result.status_code == 200, result.data
    body = result.get_json()
    assert body['company_id'] == 'acc-cloud'
    assert body['leaf'] == {'code': 'L-bizov-08', 'trunk': 'bizov', 'title': '测试叶子',
                            'version': 2, 'state': 'active'}
    assert body['source'] == {'ref': 'a', 'file_hash': SHA, 'original_name': 'real.docx',
                              'locator': 'docx:body/77/paragraph', 'lines': [77, 78]}
    assert body['original'] == {'state': 'unavailable', 'reason': 'cloud_snapshot'}
    assert 'download_url' not in body['original']
    assert not cloud_web.MAIN_DB.exists()


def test_cloud_v1_hash_and_name_refs(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(V1_HASH_CONTENT)))
    body = get_source(cloud_web, 'leaf_code=L-bizov-08&ref=one').get_json()
    assert body['source'] == {'ref': 'one', 'file_hash': SHA, 'original_name': None,
                              'locator': 'docx:body/77/paragraph', 'lines': None}
    assert body['original'] == {'state': 'unavailable', 'reason': 'cloud_snapshot'}
    # A name-form citation cannot be mapped without the local source table.
    store_section(cloud_web, section(leaf=leaf_entry(V1_NAME_CONTENT)))
    body = get_source(cloud_web, 'leaf_code=L-bizov-08&ref=one').get_json()
    assert body['source']['file_hash'] is None
    assert body['source']['original_name'] == 'real.docx'
    assert body['original'] == {'state': 'unavailable', 'reason': 'source_unresolvable'}


def test_cloud_unknown_leaf_and_ref_404(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(V2_CONTENT)))
    result = get_source(cloud_web, 'leaf_code=L-hr-99&ref=a')
    assert result.status_code == 404 and result.get_json()['error'] == 'unknown_leaf'
    result = get_source(cloud_web, 'leaf_code=L-bizov-08&ref=two')
    assert result.status_code == 404 and result.get_json()['error'] == 'unknown_source'


def test_cloud_not_generated_leaf_state(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(None, state='not_generated')))
    result = get_source(cloud_web)
    assert result.status_code == 200
    body = result.get_json()
    assert body['state'] == 'not_generated' and body['code'] == 'L-bizov-08'
    assert body['content'] is None


def test_cloud_unbound_section_state(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, {'company_id': 'acc-cloud', 'state': 'unbound'})
    result = get_source(cloud_web)
    assert result.status_code == 200
    assert result.get_json() == {'company_id': 'acc-cloud', 'binding': None, 'node': 'leaf',
                                 'code': 'L-bizov-08', 'state': 'unbound'}


def test_cloud_parameter_guards(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(V2_CONTENT)))
    client = cloud_web.app.test_client()
    base = '/api/facts/tree/source?leaf_code=L-bizov-08&ref=a'
    for request in (lambda: client.get(base + '&company_id=acc-cloud'),
                    lambda: client.get(base, data={'company_id': 'acc-cloud'}),
                    lambda: client.get(base, json={'company_id': 'other'})):
        result = request()
        assert result.status_code == 400 and result.get_json()['error'] == 'company_id_rejected'
    for query in ('leaf_code=L-bizov-08', 'ref=a', 'leaf_code=&ref=a',
                  f'leaf_code=L-bizov-08&ref=a&file_hash={SHA}',
                  'leaf_code=L-bizov-08&ref=a&node=leaf'):
        result = get_source(cloud_web, query)
        assert result.status_code == 400, (query, result.data)
        assert result.get_json()['error'] == 'invalid_parameter'


def test_cloud_gate_closed_and_missing_host_scope(cloud_web, monkeypatch):
    store_section(cloud_web, section(leaf=leaf_entry(V2_CONTENT)))
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    result = get_source(cloud_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_disabled'
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.delenv('SAM_COMPANY_ID')
    result = get_source(cloud_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'company_scope_missing'


def test_cloud_snapshot_missing_foreign_and_no_homepage_fallback(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    result = get_source(cloud_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    # A forged homepage snapshot is never a fallback source.
    forged = snapshot()
    forged['payload']['facts_tree'] = section(leaf=leaf_entry(V2_CONTENT))
    cloud_web.app.extensions['sam_snapshot_store'].replace(forged)
    result = get_source(cloud_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    assert client.get('/api/knowledge').status_code == 200  # homepage reads unaffected
    # A snapshot persisted under a different host scope fails closed too.
    store_section(cloud_web, section('acc-other', leaf_entry(V2_CONTENT, company='acc-other')),
                  company='acc-other')
    result = get_source(cloud_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    assert not cloud_web.MAIN_DB.exists()


def test_cloud_source_lookup_rejects_missing_or_tampered_marker(cloud_web, monkeypatch):
    """DEC-020 A: source resolution also requires the intact contract marker."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    section_missing = section(leaf=leaf_entry(V2_CONTENT))
    del section_missing['placement_validation']
    store_section(cloud_web, section_missing)
    result = get_source(cloud_web)
    assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'
    tampered = section(leaf=leaf_entry(V2_CONTENT))
    tampered['placement_validation']['snapshot_sha256'] = '0' * 64
    store_section(cloud_web, tampered)
    result = get_source(cloud_web)
    assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'
    assert not cloud_web.MAIN_DB.exists()


def test_cloud_ready_leaf_without_content_is_409(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    entry = leaf_entry(V2_CONTENT)
    entry['content'] = None
    store_section(cloud_web, section(leaf=entry))
    result = get_source(cloud_web)
    assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'


def test_cloud_response_carries_no_paths_or_mapping_internals(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store_section(cloud_web, section(leaf=leaf_entry(V2_CONTENT)))
    result = get_source(cloud_web)
    body = result.get_json()
    assert set(body) == {'company_id', 'leaf', 'source', 'original'}
    assert set(body['source']) == {'ref', 'file_hash', 'original_name', 'locator', 'lines'}
    text = result.get_data(as_text=True)
    for forbidden in ('storage_path', 'manifest', 'mapping', 'sam://'):
        assert forbidden not in text
