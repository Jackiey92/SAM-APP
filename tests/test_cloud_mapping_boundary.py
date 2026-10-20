"""Cloud mapping boundary: mapping internals are never stored or served.

Ingest redacts payload.knowledge before the snapshot is persisted, and reads
project legacy stored documents per request without rewriting them. Synthetic
sentinels only; no OV, models or production data.
"""
import json

from tests.test_cloud_home import cloud_web, snapshot  # reused fixture/helper

MAPPING_BODY = 'acc-sentinel-mapping-body-7f3e'
PAGE_MAPPING = 'acc-sentinel-page-mapping-9c1d'
MANIFEST_URI = 'acc-sentinel://l2-manifest'
NESTED_URI = 'acc-sentinel://nested-l2-ref'
SENTINELS = (MAPPING_BODY, PAGE_MAPPING, MANIFEST_URI, NESTED_URI)
MAPPING_KEYS = {'mapping', 'manifest_uri', 'l2_manifest_uri'}


def knowledge_with_mapping():
    return {
        'evidence': {'files': [{
            'file_hash': 'abc',
            'original_name': '测试资料',
            'l0': {'content': 'acc-keep-l0', 'source_ref': {'file_hash': 'abc'}},
            'l2': {
                'manifest_uri': MANIFEST_URI,
                'mapping': MAPPING_BODY,
                'pages': [{'page': 1, 'mapping': PAGE_MAPPING,
                           'source_ref': {'file_hash': 'abc', 'page': 1,
                                          'l2_manifest_uri': MANIFEST_URI}}],
            },
        }]},
        'facts': {'groups': [{'entity': 'acc-keep-entity',
                              'facts': [{'value': 'acc-keep-fact',
                                         'source_ref': {'file_hash': 'abc',
                                                        'l2_manifest_uri': NESTED_URI}}]}]},
        'working_memory': {'items': [], 'warnings': []},
    }


def snapshot_with_mapping():
    data = snapshot()
    data['payload']['knowledge'] = knowledge_with_mapping()
    return data


def nested_keys(node) -> set:
    keys = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= nested_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= nested_keys(item)
    return keys


def assert_knowledge_redacted(knowledge):
    document = json.dumps(knowledge, ensure_ascii=False)
    for sentinel in SENTINELS:
        assert sentinel not in document
    assert nested_keys(knowledge).isdisjoint(MAPPING_KEYS)
    # Every non-mapping knowledge field survives the projection.
    file_entry = knowledge['evidence']['files'][0]
    assert file_entry['l0']['content'] == 'acc-keep-l0'
    assert file_entry['l0']['source_ref'] == {'file_hash': 'abc'}
    assert file_entry['l2']['pages'][0]['page'] == 1
    assert file_entry['l2']['pages'][0]['source_ref'] == {'file_hash': 'abc', 'page': 1}
    assert knowledge['facts']['groups'][0]['entity'] == 'acc-keep-entity'
    assert knowledge['facts']['groups'][0]['facts'][0]['value'] == 'acc-keep-fact'
    assert knowledge['facts']['groups'][0]['facts'][0]['source_ref'] == {'file_hash': 'abc'}
    assert knowledge['working_memory'] == {'items': [], 'warnings': []}


def test_ingest_stores_knowledge_without_mapping_internals(cloud_web):
    client = cloud_web.app.test_client()
    result = client.post('/ingest', json=snapshot_with_mapping(),
                         headers={'X-Ingest-Key': 'acc-ingest'})
    assert result.status_code == 200
    stored = cloud_web.app.extensions['sam_snapshot_store'].read()
    assert_knowledge_redacted(stored['payload']['knowledge'])
    # Sections outside knowledge are stored untouched.
    assert stored['payload']['page_context'] == snapshot()['payload']['page_context']
    assert stored['payload']['conversations'] == snapshot()['payload']['conversations']


def test_legacy_stored_snapshot_is_projected_without_rewrite(cloud_web):
    store = cloud_web.app.extensions['sam_snapshot_store']
    # A historical document persisted before ingest-side redaction existed.
    store.replace(snapshot_with_mapping())
    client = cloud_web.app.test_client()

    overview = client.get('/overview')
    assert overview.status_code == 200
    html = overview.get_data(as_text=True)
    for sentinel in SENTINELS:
        assert sentinel not in html

    response = client.get('/api/knowledge')
    assert response.status_code == 200
    assert_knowledge_redacted(response.get_json())

    # Read-time projection never rewrites the stored historical document.
    stored = store.read()
    l2 = stored['payload']['knowledge']['evidence']['files'][0]['l2']
    assert l2['mapping'] == MAPPING_BODY
    assert l2['manifest_uri'] == MANIFEST_URI
