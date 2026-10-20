"""F-004-API-002 local route: verified leaf citations only, cabinet-held originals.

Synthetic companies and temporary ledgers only. Happy-path leaves go through
the real V1 publish chain; adversarial ledger states (foreign sources,
ambiguous names, V2 index lines) are planted directly because the publish
guards rightly refuse to create them.
"""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app import settings
from app.facts_tree.uris import leaf_uri as pack_leaf_uri
from app.storage.archive_paths import hash_relpath
from tests.test_facts_tree_leaves import kernel, artifact, publish, COMPANY, SHA
from tests.test_facts_tree_tree_read import attribute, db_path


@pytest.fixture
def source_web(kernel, monkeypatch, tmp_path):
    from webapp import app as web
    monkeypatch.setattr(web, 'MAIN_DB', db_path(kernel))
    monkeypatch.setitem(web.app.extensions, 'sam_memory_provider', kernel.memory)
    objects = tmp_path / 'objects'
    objects.mkdir()
    monkeypatch.setattr(web, 'OBJECTS_DIR', objects)
    monkeypatch.setenv('SAM_COMPANY_ID', COMPANY)
    web._kernel = kernel
    web._objects = objects
    return web


def seed_file(kernel, *, content=b'acc-original-bytes', name='real.docx', company=COMPANY):
    real = hashlib.sha256(content).hexdigest()
    kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                      (real, name, len(content), str(hash_relpath(real)), '2026-10-14'))
    kernel.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (company, '临时主体', '测试指标', '2025-01', '9', '个', real, 'bizov', '2026-10-14'))
    kernel.db.commit()
    return real


def write_blob(web, file_hash, data):
    path = web._objects / hash_relpath(file_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def publish_v1(kernel, *, foot, sources):
    markdown = artifact(sources=sorted(sources), source_file_count=len(sources))
    markdown = markdown.replace(f'[^one]: {SHA} docx:body/77/paragraph', foot)
    return publish(kernel, markdown=markdown, source_files=list(sources), fact_ids=[1])


def v2_artifact(*, code='L-bizov-08', ref='a', name='real.docx', sha, sources=None,
                loc='docx:body/77/paragraph', start=77, end=78, block='b1'):
    sources = sources or [sha]
    meta = dict(schema_version=2, blocks_sha='b' * 64, leaf_code=code, trunk='bizov',
                title='测试叶子', company_id=COMPANY, status='active', latest_period='2025-01',
                sources=sources, source_file_count=len(sources), version=1,
                created_at='2026-10-14', updated_at='2026-10-14', owner_skill='fact-leaf')
    line = (f'[^{ref}]: {name} → {loc} (lines {start}-{end}; block {block}; sha {sha}; facts 1)')
    return ('---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n'
            f'## 内容\n<!-- block:{block} -->\n有9个[^{ref}]。\n\n## 索引\n{line}\n')


def plant_leaf(kernel, markdown, *, code='L-bizov-08', sources, fact_ids=(1,)):
    """Direct ledger/projection planting for states the publish guards reject."""
    leaf, pack = kernel._leaf(COMPANY, code)
    uri = pack_leaf_uri(COMPANY, pack, leaf)
    sha = hashlib.sha256(markdown.encode()).hexdigest()
    kernel.db.execute('INSERT INTO facts_tree_leaf_index VALUES(?,?,?)', (COMPANY, code, uri))
    kernel.db.execute('''INSERT INTO leaf_revisions(company_id,leaf_code,version,content_sha,
        source_files,fact_ids,owner_skill,change_note,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (COMPANY, code, 1, sha, json.dumps(sorted(sources)), json.dumps(sorted(fact_ids)),
         'fact-leaf', 'planted', '2026-10-14'))
    kernel.db.commit()
    kernel.memory.put(uri, markdown, metadata={})
    return uri


def get_source(web, query='leaf_code=L-bizov-08&ref=one'):
    return web.app.test_client().get('/api/facts/tree/source?' + query)


def test_v1_hash_ref_available(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    write_blob(source_web, real, b'acc-original-bytes')
    publish_v1(kernel, foot=f'[^one]: {real} docx:body/77/paragraph', sources=[SHA, real])
    attribute(kernel)
    result = get_source(source_web)
    assert result.status_code == 200, result.data
    body = result.get_json()
    assert body['company_id'] == COMPANY
    # API-002 reports the bound pack's standard leaf title, not the body YAML's custom one.
    template_title = kernel._leaf(COMPANY, 'L-bizov-08')[0].title
    assert body['leaf'] == {'code': 'L-bizov-08', 'trunk': 'bizov', 'title': template_title,
                            'version': 1, 'state': 'active'}
    assert body['source'] == {'ref': 'one', 'file_hash': real, 'original_name': 'real.docx',
                              'locator': 'docx:body/77/paragraph', 'lines': None}
    assert body['original'] == {'state': 'available',
                                'download_url': f'/api/knowledge/sources/{real}/original'}


def test_v1_exact_name_ref_maps_to_unique_hash(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    write_blob(source_web, real, b'acc-original-bytes')
    publish_v1(kernel, foot='[^one]: real.docx docx:body/77/paragraph', sources=[SHA, real])
    attribute(kernel)
    body = get_source(source_web).get_json()
    assert body['source']['file_hash'] == real
    assert body['source']['original_name'] == 'real.docx'
    assert body['original']['state'] == 'available'


def test_v2_index_line_ref_with_lines(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    write_blob(source_web, real, b'acc-original-bytes')
    plant_leaf(kernel, v2_artifact(sha=real), sources=[real])
    attribute(kernel)
    result = get_source(source_web, 'leaf_code=L-bizov-08&ref=a')
    assert result.status_code == 200, result.data
    body = result.get_json()
    assert body['source'] == {'ref': 'a', 'file_hash': real, 'original_name': 'real.docx',
                              'locator': 'docx:body/77/paragraph', 'lines': [77, 78]}
    assert body['original']['state'] == 'available'


def test_unknown_leaf_and_ref_404(source_web):
    publish(source_web._kernel)
    attribute(source_web._kernel)
    client = source_web.app.test_client()
    result = client.get('/api/facts/tree/source?leaf_code=L-hr-99&ref=one')
    assert result.status_code == 404 and result.get_json()['error'] == 'unknown_leaf'
    result = client.get('/api/facts/tree/source?leaf_code=conflict-bizov-x-1&ref=one')
    assert result.status_code == 404 and result.get_json()['error'] == 'unknown_leaf'
    result = get_source(source_web, 'leaf_code=L-bizov-08&ref=two')
    assert result.status_code == 404 and result.get_json()['error'] == 'unknown_source'


def test_not_generated_leaf_explicit_state(source_web):
    body = get_source(source_web).get_json()
    assert body['state'] == 'not_generated' and body['code'] == 'L-bizov-08'
    assert 'source' not in body and 'original' not in body


def test_unbound_company_explicit_state(source_web, monkeypatch):
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-unbound')
    result = get_source(source_web)
    assert result.status_code == 200
    assert result.get_json()['state'] == 'unbound'


def test_company_id_rejected_400(source_web):
    client = source_web.app.test_client()
    base = '/api/facts/tree/source?leaf_code=L-bizov-08&ref=one'
    for request in (lambda: client.get(base + '&company_id=' + COMPANY),
                    lambda: client.get(base, data={'company_id': COMPANY}),
                    lambda: client.get(base, json={'company_id': 'other'})):
        result = request()
        assert result.status_code == 400, result.data
        assert result.get_json()['error'] == 'company_id_rejected'


def test_invalid_parameters_400(source_web):
    client = source_web.app.test_client()
    for query in ('leaf_code=L-bizov-08', 'ref=one', 'leaf_code=&ref=one',
                  'leaf_code=L-bizov-08&ref=', f'leaf_code=L-bizov-08&ref=one&file_hash={SHA}',
                  'leaf_code=L-bizov-08&ref=one&node=leaf'):
        result = client.get('/api/facts/tree/source?' + query)
        assert result.status_code == 400, (query, result.data)
        assert result.get_json()['error'] == 'invalid_parameter'


def test_disabled_gate_and_missing_host_scope_503(source_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    result = get_source(source_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_disabled'
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.delenv('SAM_COMPANY_ID')
    result = get_source(source_web)
    assert result.status_code == 503 and result.get_json()['error'] == 'company_scope_missing'


def test_cross_company_source_fails_closed_409(source_web):
    kernel = source_web._kernel
    foreign = seed_file(kernel, content=b'acc-foreign-bytes', name='foreign.docx', company='acc-other')
    write_blob(source_web, foreign, b'acc-foreign-bytes')
    plant_leaf(kernel, v2_artifact(sha=foreign, name='foreign.docx'), sources=[foreign])
    attribute(kernel)
    result = get_source(source_web, 'leaf_code=L-bizov-08&ref=a')
    assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'


def test_original_missing_and_integrity_failed_have_no_download_url(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    plant_leaf(kernel, v2_artifact(sha=real), sources=[real])
    attribute(kernel)
    body = get_source(source_web, 'leaf_code=L-bizov-08&ref=a').get_json()
    assert body['original'] == {'state': 'unavailable', 'reason': 'original_missing'}
    write_blob(source_web, real, b'acc-corrupted-bytes')
    body = get_source(source_web, 'leaf_code=L-bizov-08&ref=a').get_json()
    assert body['original'] == {'state': 'unavailable', 'reason': 'integrity_failed'}
    assert 'download_url' not in body['original']


def test_ambiguous_v1_name_is_never_guessed(source_web):
    kernel = source_web._kernel
    first, second = 'c' * 64, 'd' * 64
    for file_hash in (first, second):
        kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                          (file_hash, 'dup.docx', 1, 'unused', '2026-10-14'))
    kernel.db.commit()
    markdown = artifact(sources=[first, second], source_file_count=2).replace(
        f'[^one]: {SHA} docx:body/77/paragraph', '[^one]: dup.docx docx:body/77/paragraph')
    plant_leaf(kernel, markdown, sources=[first, second])
    attribute(kernel)
    body = get_source(source_web).get_json()
    assert body['source']['file_hash'] is None
    assert body['source']['original_name'] == 'dup.docx'
    assert body['original'] == {'state': 'unavailable', 'reason': 'source_unresolvable'}


def test_download_route_recomputes_hash_and_never_sends_corrupt_bytes(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    client = source_web.app.test_client()
    url = f'/api/knowledge/sources/{real}/original'
    assert client.get(url).status_code == 404  # blob not written yet
    write_blob(source_web, real, b'acc-corrupted-bytes')
    result = client.get(url)
    assert result.status_code == 409 and result.get_json()['error'] == 'integrity_failed'
    assert b'acc-corrupted-bytes' not in result.data
    write_blob(source_web, real, b'acc-original-bytes')
    result = client.get(url)
    assert result.status_code == 200 and result.data == b'acc-original-bytes'
    assert result.headers['X-Content-Type-Options'] == 'nosniff'
    assert result.headers['Cache-Control'] == 'no-store'


def test_response_carries_no_paths_or_mapping_internals(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    write_blob(source_web, real, b'acc-original-bytes')
    plant_leaf(kernel, v2_artifact(sha=real), sources=[real])
    attribute(kernel)
    result = get_source(source_web, 'leaf_code=L-bizov-08&ref=a')
    body = result.get_json()
    assert set(body) == {'company_id', 'leaf', 'source', 'original'}
    assert set(body['source']) == {'ref', 'file_hash', 'original_name', 'locator', 'lines'}
    text = result.get_data(as_text=True)
    for forbidden in ('storage_path', 'manifest', 'mapping', str(source_web._objects)):
        assert forbidden not in text


def test_source_lookup_and_download_are_read_only(source_web):
    kernel = source_web._kernel
    real = seed_file(kernel)
    write_blob(source_web, real, b'acc-original-bytes')
    publish_v1(kernel, foot=f'[^one]: {real} docx:body/77/paragraph', sources=[SHA, real])
    attribute(kernel)
    ledger = Path(db_path(kernel)).read_bytes()
    projection = {p.name: p.read_bytes() for p in kernel.memory.root.iterdir()}
    client = source_web.app.test_client()
    assert get_source(source_web).status_code == 200
    assert client.get(f'/api/knowledge/sources/{real}/original').status_code == 200
    assert Path(db_path(kernel)).read_bytes() == ledger
    assert {p.name: p.read_bytes() for p in kernel.memory.root.iterdir()} == projection
