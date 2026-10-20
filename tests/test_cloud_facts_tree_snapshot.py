"""Dedicated FactsTree snapshot channel; the homepage snapshot never serves it.

Synthetic envelopes only; no OV, models or production data.
"""
from app import settings
from tests.test_cloud_home import cloud_web, facts_tree_section, snapshot  # reused fixture/helpers


def facts_tree_envelope(company='acc-cloud', section=None):
    return {"version": 1, "company_id": company, "pushed_at": 2.5,
            "payload": {"facts_tree": facts_tree_section(company) if section is None else section}}


def test_facts_tree_ingest_writes_only_dedicated_store(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    headers = {'X-Ingest-Key': 'acc-ingest'}
    result = client.post('/ingest/facts-tree', json=facts_tree_envelope(), headers=headers)
    assert result.status_code == 200 and result.get_json() == {'ok': True}
    stored = cloud_web.app.extensions['sam_facts_tree_snapshot_store'].read()
    assert stored['payload']['facts_tree']['tree']['brief'] == '树简报原文'
    assert cloud_web.app.extensions['sam_snapshot_store'].read() is None  # homepage store untouched
    tree = client.get('/api/facts/tree').get_json()
    assert tree['brief'] == '树简报原文'


def test_facts_tree_ingest_auth(cloud_web, monkeypatch):
    client = cloud_web.app.test_client()
    assert client.post('/ingest/facts-tree', json=facts_tree_envelope()).status_code == 401
    wrong = {'X-Ingest-Key': 'acc-wrong-key'}
    assert client.post('/ingest/facts-tree', json=facts_tree_envelope(), headers=wrong).status_code == 401
    monkeypatch.delenv('SAM_CLOUD_INGEST_KEY')
    headers = {'X-Ingest-Key': 'acc-ingest'}
    assert client.post('/ingest/facts-tree', json=facts_tree_envelope(), headers=headers).status_code == 503
    assert cloud_web.app.extensions['sam_facts_tree_snapshot_store'].read() is None


def test_facts_tree_ingest_rejects_bad_scope_and_structure(cloud_web):
    client = cloud_web.app.test_client()
    headers = {'X-Ingest-Key': 'acc-ingest'}
    # Envelope company must match the host scope.
    result = client.post('/ingest/facts-tree', json=facts_tree_envelope(company='acc-other'),
                         headers=headers)
    assert result.status_code == 400
    # The payload must hold exactly the facts_tree section.
    extra = facts_tree_envelope()
    extra['payload']['knowledge'] = {}
    assert client.post('/ingest/facts-tree', json=extra, headers=headers).status_code == 400
    empty = facts_tree_envelope()
    empty['payload'] = {}
    assert client.post('/ingest/facts-tree', json=empty, headers=headers).status_code == 400
    # The section company must match the envelope company.
    mismatched = facts_tree_envelope(section={'company_id': 'acc-other', 'state': 'unbound'})
    assert client.post('/ingest/facts-tree', json=mismatched, headers=headers).status_code == 400
    # The section must be an object carrying its own company_id.
    not_object = facts_tree_envelope(section=['acc-cloud'])
    assert client.post('/ingest/facts-tree', json=not_object, headers=headers).status_code == 400
    # Envelope and transport validation still apply.
    bad_version = {**facts_tree_envelope(), 'version': 2}
    assert client.post('/ingest/facts-tree', json=bad_version, headers=headers).status_code == 400
    assert client.post('/ingest/facts-tree', data=b'{bad', headers=headers).status_code == 400
    assert client.post('/ingest/facts-tree', data=b'{"pushed_at":NaN}', headers=headers).status_code == 400
    assert cloud_web.app.extensions['sam_facts_tree_snapshot_store'].read() is None


def test_api_reads_dedicated_store_never_homepage_fallback(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    forged = snapshot()
    forged['payload']['facts_tree'] = facts_tree_section()
    forged['payload']['facts_tree']['tree']['brief'] = '伪造综合快照树简报'
    cloud_web.app.extensions['sam_snapshot_store'].replace(forged)
    client = cloud_web.app.test_client()
    result = client.get('/api/facts/tree')
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    # With a dedicated snapshot, its content wins and the forged one is ignored.
    headers = {'X-Ingest-Key': 'acc-ingest'}
    assert client.post('/ingest/facts-tree', json=facts_tree_envelope(), headers=headers).status_code == 200
    tree = client.get('/api/facts/tree').get_json()
    assert tree['brief'] == '树简报原文' and '伪造' not in tree['brief']
    # Homepage snapshot reads keep working independently.
    assert client.get('/api/knowledge').status_code == 200
    assert client.get('/overview').status_code == 200


def test_gate_closed_hides_existing_dedicated_snapshot(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    headers = {'X-Ingest-Key': 'acc-ingest'}
    client = cloud_web.app.test_client()
    assert client.post('/ingest/facts-tree', json=facts_tree_envelope(), headers=headers).status_code == 200
    for query in ('', '?node=trunk&code=bizov', '?node=leaf&code=L-bizov-08'):
        result = client.get('/api/facts/tree' + query)
        assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_disabled'


def test_ingest_rejects_missing_or_broken_placement_marker(cloud_web, monkeypatch):
    """DEC-020 A: only locally placement-validated ready sections are ingested."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    headers = {'X-Ingest-Key': 'acc-ingest'}
    store = cloud_web.app.extensions['sam_facts_tree_snapshot_store']

    def post(section):
        return client.post('/ingest/facts-tree', json=facts_tree_envelope(section=section),
                           headers=headers)
    missing = facts_tree_section()
    del missing['placement_validation']
    assert post(missing).status_code == 400
    old_contract = facts_tree_section()
    old_contract['placement_validation']['contract_version'] = 0
    assert post(old_contract).status_code == 400
    bad_digest = facts_tree_section()
    bad_digest['placement_validation']['snapshot_sha256'] = '0' * 64
    assert post(bad_digest).status_code == 400
    foreign = facts_tree_section()
    foreign['placement_validation']['company_id'] = 'acc-other'
    assert post(foreign).status_code == 400
    rebound = facts_tree_section()
    rebound['placement_validation']['binding_revision'] = 99
    assert post(rebound).status_code == 400
    tampered = facts_tree_section()
    tampered['tree']['brief'] = '篡改后的树简报'  # marker no longer matches content
    assert post(tampered).status_code == 400
    assert store.read() is None  # nothing was persisted
    # A valid marker still ingests; the unbound state never needs one.
    assert post(facts_tree_section()).status_code == 200
    assert post({'company_id': 'acc-cloud', 'state': 'unbound'}).status_code == 200


def test_read_rejects_snapshot_without_valid_marker(cloud_web, monkeypatch):
    """Old or damaged persisted snapshots fail closed and are never served."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    store = cloud_web.app.extensions['sam_facts_tree_snapshot_store']
    client = cloud_web.app.test_client()
    section = facts_tree_section()
    del section['placement_validation']  # persisted before the contract existed
    store.replace(facts_tree_envelope(section=section))
    for query in ('', '?node=trunk&code=bizov', '?node=leaf&code=L-bizov-08'):
        result = client.get('/api/facts/tree' + query)
        assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'
    damaged = facts_tree_section()
    damaged['leaves']['L-bizov-08']['content'] = '篡改后的叶正文'
    store.replace(facts_tree_envelope(section=damaged))
    result = client.get('/api/facts/tree')
    assert result.status_code == 409 and result.get_json()['error'] == 'consistency_failed'
    # Replacing the damaged snapshot with a valid one recovers the reads.
    store.replace(facts_tree_envelope())
    assert client.get('/api/facts/tree').get_json()['brief'] == '树简报原文'


def test_homepage_ingest_unaffected_by_dedicated_channel(cloud_web, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    headers = {'X-Ingest-Key': 'acc-ingest'}
    data = snapshot()
    data['payload']['facts_tree'] = facts_tree_section()  # stays stored but never read back
    assert client.post('/ingest', json=data, headers=headers).status_code == 200
    assert cloud_web.app.extensions['sam_facts_tree_snapshot_store'].read() is None
    result = client.get('/api/facts/tree')
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    assert client.get('/api/knowledge').status_code == 200


def test_ingest_rejects_unbound_section_with_tree_content(cloud_web, monkeypatch):
    """DEC-020 A: unbound carries no facts-tree content; anything more is refused."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    headers = {'X-Ingest-Key': 'acc-ingest'}
    store = cloud_web.app.extensions['sam_facts_tree_snapshot_store']

    def post(section):
        return client.post('/ingest/facts-tree', json=facts_tree_envelope(section=section),
                           headers=headers)

    base = {'company_id': 'acc-cloud', 'state': 'unbound'}
    for key in ('tree', 'trunks', 'leaves'):
        assert post({**base, key: {}}).status_code == 400, key
    assert post({**base, 'placement_validation': {}}).status_code == 400  # any extra key
    assert post({'company_id': 'acc-cloud', 'state': 'unbound', 'brief': '伪造'}).status_code == 400
    assert store.read() is None  # nothing was persisted
    # The legal snapshot() unbound shape still ingests and is served as unbound.
    assert post(base).status_code == 200
    assert store.read()['payload']['facts_tree'] == base
    assert client.get('/api/facts/tree').get_json()['state'] == 'unbound'
