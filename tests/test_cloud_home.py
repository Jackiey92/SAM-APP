"""Cloud transport/assembly contracts; no OV, models or production data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    return {"version": 1, "company_id": "acc-cloud", "pushed_at": 1.5, "payload": {
        "page_context": {"facts": [{"extra": "事实原文"}], "todos": [{"extra": "待办原文"}],
                         "files": [{"file_hash": "abc", "original_name": "测试资料"}],
                         "module_counts": {"new-module": 2}},
        "knowledge": {"evidence": {"files": [{"file_hash": "abc", "l2": {"mapping": "L2原文"}}]},
                      "facts": {"new-field": [False, None]}, "working_memory": {}},
        "overview": {"new-field": "业务原文"}, "dashboard": {},
        "conversations": [{"thread_id": "acc-thread", "turns": [{"text": "历史对话"}]}],
        "jobs": [], "future": {"opaque": [False, None, 0, "<script>alert(1)</script>"]}}}


def test_null_memory_has_all_protocol_methods(tmp_path):
    from app.null_memory import NullMemoryProvider, ReadOnlyMemoryError
    from app.ports import MemoryProvider
    memory = NullMemoryProvider()
    assert all(hasattr(memory, name) for name in MemoryProvider.__dict__ if not name.startswith('_'))
    assert memory.read('anything') == ''
    assert memory.query(prefix='anything') == []
    assert memory.search('anything') == []
    assert memory.get_fact('acc-cloud', 'x') is None
    assert memory.get_2b('acc-cloud', 'x') is None
    assert memory.list_facts('acc-cloud') == []
    writes = [lambda: memory.add_resource(str(tmp_path), parent='x'),
              lambda: memory.add_resource_to(str(tmp_path), 'x'),
              lambda: memory.ensure_directory('x'), lambda: memory.wait_for_resource('x'),
              lambda: memory.put('x', 'body'), lambda: memory.put_fact('acc-cloud', 'x', {}),
              lambda: memory.delete('x', recursive=True)]
    for write in writes:
        with pytest.raises(ReadOnlyMemoryError, match='只读'):
            write()
    assert list(tmp_path.iterdir()) == []


def test_local_config_no_longer_requires_transport_root():
    from app.runtime_config import RuntimeConfig
    config = RuntimeConfig.from_env(ROOT, env={})
    assert config.memory_root_uri == 'sam://facts'
    assert config.memory_provider == 'localvector'


def test_cloud_config_needs_neither_root_nor_node(monkeypatch, tmp_path):
    from app import runtime_config
    from app.providers import memory_provider
    from app.null_memory import NullMemoryProvider
    monkeypatch.setattr(runtime_config, '_select_node_bin', lambda *_: pytest.fail('must not probe Node'))
    config = runtime_config.RuntimeConfig.from_env(ROOT, env={
        'SAM_CLOUD_READONLY': '1', 'SAM_DATA_ROOT': str(tmp_path), 'SAM_PROFILE': 'cloud'})
    assert config.cloud_readonly
    assert config.data_root == tmp_path
    assert config.objects_dir == tmp_path / 'objects'
    assert config.main_db == tmp_path / 'app.db'
    assert isinstance(memory_provider(config, env={}), NullMemoryProvider)
    with pytest.raises(RuntimeError, match='SAM_DATA_ROOT'):
        runtime_config.RuntimeConfig.from_env(ROOT, env={'SAM_CLOUD_READONLY': '1'})


@pytest.mark.parametrize('cloud,success', [('1', True), ('0', True)])
def test_import_time_root_branch(cloud, success, tmp_path):
    import os
    env = dict(os.environ, SAM_CLOUD_READONLY=cloud, SAM_DATA_ROOT=str(tmp_path),
               SAM_MAIN_DB=str(tmp_path / "app.db"), SAM_APP_DB=str(tmp_path / "sales.db"),
               SAM_2A_ROOT=str(tmp_path / "2a"))
    env.pop('SAM_MEMORY_ROOT_URI', None)
    from app.db.migrate import main as migrate
    migrate(['--db', str(tmp_path / ('cloud-snapshot.db' if cloud == '1' else 'app.db'))] + (['--snapshot'] if cloud == '1' else []))
    result = subprocess.run([sys.executable, '-c', 'import app.memory_paths; import webapp.app'],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert success and result.returncode == 0, result.stderr
    if cloud == '1':
        assert not (tmp_path / 'app.db').exists()
    else:
        assert (tmp_path / 'app.db').exists()


def test_snapshot_atomic_roundtrip_and_replace(tmp_path):
    from app.cloud_snapshot import SnapshotStore, initialize_snapshot
    initialize_snapshot(tmp_path / 'snapshot.db')
    store = SnapshotStore(tmp_path / 'snapshot.db')
    assert store.read() is None
    original = snapshot()
    store.replace(original)
    assert store.read() == original
    original['payload']['future']['opaque'].append('not saved')
    assert store.read() != original
    second = snapshot()
    second['payload'] = {}
    store.replace(second)
    assert store.read() == second  # no merging of stale fields


@pytest.mark.parametrize('change', [ {'version': 0}, {'company_id': ''}, {'pushed_at': float('nan')},
                                    {'pushed_at': True}, {'payload': []}, {'version': True}])
def test_invalid_snapshot_preserves_old_data(tmp_path, change):
    from app.cloud_snapshot import SnapshotStore, initialize_snapshot
    initialize_snapshot(tmp_path / 'snapshot.db')
    store = SnapshotStore(tmp_path / 'snapshot.db')
    original = snapshot()
    store.replace(original)
    with pytest.raises(ValueError):
        store.replace({**original, **change})
    assert store.read() == original


def test_concurrent_snapshot_replacement(tmp_path):
    from app.cloud_snapshot import SnapshotStore, initialize_snapshot
    initialize_snapshot(tmp_path / 'snapshot.db')
    store = SnapshotStore(tmp_path / 'snapshot.db')
    values = [{**snapshot(), 'pushed_at': i} for i in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store.replace, values))
    assert store.read() in values


@pytest.fixture
def cloud_web(monkeypatch, tmp_path):
    from app.cloud_snapshot import initialize_snapshot
    initialize_snapshot(tmp_path / 'cloud-snapshot.db')
    initialize_snapshot(tmp_path / 'cloud-facts-tree-snapshot.db')
    monkeypatch.setenv('SAM_CLOUD_READONLY', '1')
    monkeypatch.setenv('SAM_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-cloud')
    monkeypatch.setenv('SAM_CLOUD_INGEST_KEY', 'acc-ingest')
    # This cloud fixture owns its data root; never inherit a local test ledger.
    for name in ('SAM_MEMORY_ROOT_URI', 'SAM_VIEW_USER', 'SAM_VIEW_PASS',
                 'SAM_MAIN_DB', 'SAM_APP_DB', 'SAM_OBJECTS_DIR'):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location('acc_cloud_web', ROOT / 'webapp/app.py')
    web = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, web)
    spec.loader.exec_module(web)
    web.app.config.update(TESTING=True)
    return web


@pytest.mark.parametrize('with_snapshot', [False, True])
def test_all_cloud_pages_and_read_apis(cloud_web, with_snapshot, monkeypatch):
    web = cloud_web
    if with_snapshot:
        web.app.extensions['sam_snapshot_store'].replace(snapshot())
    monkeypatch.setattr(web, 'consolidation_service', lambda: pytest.fail('no local ledger'))
    monkeypatch.setattr(web, 'classification_service', lambda: pytest.fail('no local ledger'))
    monkeypatch.setattr(web, 'business_overview_service', lambda: pytest.fail('no local ledger'))
    client = web.app.test_client()
    # The high-fidelity shell is rendered with host-owned cloud configuration.
    landing = client.get('/')
    assert landing.status_code == 200
    landing_html = landing.get_data(as_text=True)
    assert '企业经营智能体原型' in landing_html
    assert '<div class="screen active" id="overview">' in landing_html
    assert landing_html.count('<div class="screen active"') == 1
    assert '<div class="screen" id="bizov">' in landing_html
    assert '<button class="ov-item active" data-go="overview">' in landing_html
    for label in ('业务画像', '产品矩阵', '商业模式', '技术与知识产权'):
        assert f'<button class="sub-item" data-go="bizov"><span class="dot"></span>{label}</button>' in landing_html
    assert '<div class="crumb" id="crumb">企业综合</div>' in landing_html
    assert 'overview:"企业综合"' in landing_html
    # CHG-045 公司看板切片：只核对首页 overview 区块，隐藏旧原型页里的演示
    # 文字不作为首页功能证据。
    overview_html = landing_html.split('<div class="screen active" id="overview">', 1)[1]
    overview_html = overview_html.split('<div class="screen" id="facts">', 1)[0]
    # FactsTree 真实状态卡：壳内只有状态占位与入口，数据由前端异步读取。
    assert '<div class="card" id="factsBoard">' in overview_html
    assert '<h3>事实树</h3>' in overview_html
    assert '正在读取当前公司事实树状态…' in overview_html  # 空态/加载态占位
    assert 'id="factsBoardSub"' in overview_html
    assert 'id="factsState" role="status"' in overview_html
    assert 'id="factsBrief" hidden' in overview_html
    assert 'id="factsTrunks" hidden' in overview_html
    assert 'id="factsRetryBtn" type="button" hidden' in overview_html
    # 事实库入口与上传资料入口都在看板卡上；事实库入口在同一主页壳内切页，不跳出外壳。
    assert '<button class="drive-btn" id="factsTreeLink" type="button" data-go="facts">查看事实库</button>' in overview_html
    assert '<button class="drive-btn primary" id="factsUploadBtn" type="button">上传企业资料</button>' in overview_html
    # 旧版静态经营卡片字段不得出现在首页 overview 区块。
    for label in ('企业健康度', '营业收入（1–9月）', '净利润', '现金跑道',
                  '在手订单', '员工激励待办', '研发项目进展', '订单进展'):
        assert label not in overview_html
    # 失败态明文不回退演示数据。
    assert '未回退到演示数据' in landing_html
    # 侧栏“事实库”在同一主页壳内打开完整只读 FactsTree，位置在文件柜入口之前。
    assert '<button class="sys-item" type="button" data-go="facts">' in landing_html
    assert landing_html.index('data-go="facts"') < landing_html.index('id="importNavBtn"')
    assert 'facts:"事实库"' in landing_html
    # 事实库页面呈现整棵树：树/枝/叶导航、状态行、详情区与脚注来源回查，均复用
    # 只读渲染器 webapp/static/js/facts-tree.js（同 /facts 页共用的容器结构）。
    assert '<div class="screen" id="facts">' in landing_html
    facts_html = landing_html.split('<div class="screen" id="facts">', 1)[1]
    facts_html = facts_html.split('<div class="import-mask" id="knowledgeSourceMask"', 1)[0]
    for marker in ('id="facts-tree"', 'id="facts-tree-status"',
                   'id="facts-tree-nav"', 'id="facts-tree-detail"'):
        assert marker in facts_html
    assert '<script src="/static/js/facts-tree.js"></script>' in landing_html
    # 旧知识库平铺事实页已淘汰：旧导航、旧页面与按来源平铺的旧事实/股权链/工作
    # 记忆区块不得作为新版事实库的通过证据。
    assert 'data-go="knowledge"' not in landing_html
    assert 'id="knowledge"' not in landing_html
    for legacy in ('id="knowledgeFacts"', 'id="knowledgeEquity"', 'id="knowledgeRuntime"',
                   'id="confirmed-facts"', 'id="working-memory"',
                   'id="knowledgeTree"', 'id="knowledgeSelect"'):
        assert legacy not in landing_html
    prototype = client.get('/prototype')
    assert prototype.status_code == 200
    assert prototype.get_data(as_text=True) == landing_html
    for path in ['/bizov', '/overview', '/todos', '/facts', '/knowledge',
                 '/knowledge/sources/abc', '/files', '/files/abc', '/chat', '/inbox']:
        result = client.get(path)
        assert result.status_code == 200, (path, result.data)
        html = result.get_data(as_text=True)
        if path in ('/knowledge', '/knowledge/sources/abc'):
            assert html == landing_html
            assert 'id="knowledgeSourceMask"' in html
            continue
        assert 'data-upload-open' not in html
        assert 'id="upload-modal"' not in html
        assert '只读' in html
        assert 'type="file"' not in html
        if not with_snapshot:
            if path == '/facts':
                # FactsTree loads asynchronously: the shell renders server-side;
                # '暂无同步数据' only appears after facts_tree_unavailable.
                assert 'id="facts-tree-status"' in html
                assert 'facts-tree.js' in html
            else:
                assert '暂无同步数据' in html
    for path in ['/api/bizov', '/api/business-overview', '/api/dashboard',
                 '/api/dashboard/preferences', '/api/knowledge', '/api/facts', '/api/todos',
                 '/api/files/classifications', '/api/entity-roster', '/api/parse-jobs', '/api/chat/history']:
        assert client.get(path).status_code == 200, path
    assert not web.MAIN_DB.exists()
    with web.app.app_context():
        from app.knowledge import mapping_redacted_projection
        expected_knowledge = mapping_redacted_projection(snapshot()['payload']['knowledge'])
        assert web.knowledge_service().read(company_id='acc-cloud') == (
            expected_knowledge if with_snapshot else {})
    if with_snapshot:
        assert '历史对话' in client.get('/chat').get_data(as_text=True)
        served = client.get('/api/knowledge').get_json()
        assert 'mapping' not in served['evidence']['files'][0]['l2']  # mapping internals are redacted
        assert served['facts'] == snapshot()['payload']['knowledge']['facts']
        assert served['working_memory'] == {}
        assert 'new-field' in client.get('/bizov').get_data(as_text=True)
        assert 'new-module' in client.get('/overview').get_data(as_text=True)
        assert client.get('/api/facts?company_id=other').get_json()['company_id'] == 'acc-cloud'


def test_all_write_routes_blocked_centrally(cloud_web):
    client = cloud_web.app.test_client()
    for rule in cloud_web.app.url_map.iter_rules():
        if 'POST' in rule.methods and rule.endpoint not in ('cloud_ingest', 'cloud_ingest_facts_tree'):
            path = str(rule).replace('<int:todo_id>', '1').replace('<job_id>', 'x').replace('<card_id>', 'x')
            result = client.post(path, json={})
            assert result.status_code == 403, (path, result.data)
            assert result.get_json()['message'] == '云端为只读演示窗口，请在本地操作'
    for method in ['put', 'patch', 'delete']:
        assert getattr(client, method)('/api/facts').status_code == 403


def test_ingest_auth_limits_validation_and_replace(cloud_web, monkeypatch):
    client = cloud_web.app.test_client()
    assert client.post('/ingest', json=snapshot()).status_code == 401
    headers = {'X-Ingest-Key': 'acc-ingest'}
    assert client.post('/ingest', json=snapshot(), headers=headers).status_code == 200
    knowledge = snapshot()['payload']['knowledge']
    knowledge['evidence']['files'][0]['l2'].pop('mapping')  # ingest redacts mapping internals
    assert client.get('/api/knowledge').get_json() == knowledge
    assert client.post('/ingest', json={**snapshot(), 'company_id': 'other'}, headers=headers).status_code == 400
    assert client.post('/ingest', data=b'{bad', headers=headers).status_code == 400
    assert client.post('/ingest', data=b'{"pushed_at":NaN}', headers=headers).status_code == 400
    cloud_web.app.config['MAX_CONTENT_LENGTH'] = 8
    assert client.post('/ingest', data=b' ' * 9, headers=headers).status_code == 413
    monkeypatch.delenv('SAM_CLOUD_INGEST_KEY')
    assert client.post('/ingest', headers=headers).status_code == 503


def test_view_auth_is_global_and_separate_from_ingest(cloud_web, monkeypatch):
    monkeypatch.setenv('SAM_VIEW_USER', 'acc-view')
    monkeypatch.setenv('SAM_VIEW_PASS', '查看钥匙')
    client = cloud_web.app.test_client()
    for path in ['/', '/bizov', '/knowledge', '/files', '/chat', '/api/facts']:
        assert client.get(path).status_code == 401
        assert client.get(path, auth=('acc-view', '查看钥匙')).status_code in (200, 302)
    assert client.get('/healthz').status_code == 200
    assert client.post('/ingest', json=snapshot(), headers={'X-Ingest-Key': 'acc-ingest'}).status_code == 200
    monkeypatch.delenv('SAM_VIEW_PASS')
    assert client.get('/bizov', auth=('acc-view', '查看钥匙')).status_code == 401


def test_unknown_and_partial_fields_render_safely(cloud_web):
    data = snapshot()
    data['payload']['page_context'] = {'facts': [{}], 'todos': [{}], 'files': [{}]}
    cloud_web.app.extensions['sam_snapshot_store'].replace(data)
    client = cloud_web.app.test_client()
    for path in ['/overview', '/todos', '/facts', '/knowledge', '/files']:
        assert client.get(path).status_code == 200
    html = client.get('/overview').get_data(as_text=True)
    assert 'opaque' in html
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_local_snapshot_assembly_uses_scoped_existing_services(tmp_path, monkeypatch):
    from app import cloud_export
    from app.db.database import init_db
    from app.ports import LocalMemoryProvider
    from app.runtime_config import RuntimeConfig
    from app.memory.user_memory.l2_conversation import ConversationStore
    from app.thread_manager import ThreadManager
    from types import SimpleNamespace

    db = tmp_path / 'app.db'
    init_db(db)
    config = SimpleNamespace(cloud_readonly=False, main_db=db, objects_dir=tmp_path / 'objects')
    memory = LocalMemoryProvider(tmp_path / 'memory')
    ThreadManager(memory).create('acc-cloud', 'acc-thread', title='测试会话')
    ConversationStore(memory).append('acc-cloud', 'acc-thread', role='user', text='历史原文')
    ThreadManager(memory).create('acc-other', 'private', title='其他公司')
    monkeypatch.setattr(cloud_export, 'memory_provider', lambda actual: memory)
    monkeypatch.setattr(cloud_export.RuntimeConfig, 'from_env', lambda **_: config)
    result = cloud_export.build_home_snapshot(company_id='acc-cloud')
    assert result['company_id'] == 'acc-cloud'
    assert result['version'] == 1
    payload = result['payload']
    assert set(payload['page_context']) >= {'facts', 'todos', 'files', 'module_counts'}
    assert set(payload['knowledge']) >= {'evidence', 'facts', 'working_memory'}
    assert payload['conversations'][0]['turns'][0]['text'] == '历史原文'
    assert len(payload['conversations']) == 1
    assert not payload['jobs']
    config.cloud_readonly = True
    with pytest.raises(RuntimeError, match='local'):
        cloud_export.build_home_snapshot(company_id='acc-cloud')


def test_homepage_push_preserves_complete_envelope(monkeypatch, capsys):
    from scripts import sam_knowledge_push as push
    from tests.test_knowledge_push import cloud
    data = snapshot()
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-cloud')
    monkeypatch.setenv('SAM_CLOUD_INGEST_KEY', 'acc-home-key')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    monkeypatch.setattr(push, 'home_snapshot', lambda company_id: deepcopy(data))
    with cloud(monkeypatch) as requests:
        monkeypatch.setenv('SAM_CLOUD_URL', __import__('os').environ['SAM_KNOWLEDGE_CLOUD_URL'])
        assert push.main(['--homepage']) == 0
    sent = json.loads(requests[0][2])
    assert sent == data
    assert requests[0][1]['X-Ingest-Key'] == 'acc-home-key'
    assert 'acc-home-key' not in capsys.readouterr().out
    assert push.main(['--homepage', '--company-id', 'acc-other']) == 1


def test_payload_cannot_override_template_guard(cloud_web):
    value = snapshot()
    value['payload']['page_context'].update(read_only=False, title='untrusted', sections=[], snapshot={})
    cloud_web.app.extensions['sam_snapshot_store'].replace(value)
    result = cloud_web.app.test_client().get('/overview')
    assert result.status_code == 200
    assert b'data-upload-open' not in result.data


def test_snapshot_depth_limit_preserves_previous(tmp_path):
    from app.cloud_snapshot import SnapshotStore, initialize_snapshot
    initialize_snapshot(tmp_path / 'snapshot.db')
    store = SnapshotStore(tmp_path / 'snapshot.db')
    store.replace(snapshot())
    nested = {}
    for _ in range(40):
        nested = {'nested': nested}
    value = snapshot()
    value['payload']['future'] = nested
    with pytest.raises(ValueError, match='depth'):
        store.replace(value)
    assert store.read() == snapshot()


def test_private_cloud_responses_are_not_cached(cloud_web):
    response = cloud_web.app.test_client().get('/bizov')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert response.headers.get('Access-Control-Allow-Origin') != '*'


def test_future_endpoints_cannot_escape_cloud_boundary(cloud_web):
    from flask import render_template
    web = cloud_web
    def future_read():
        pytest.fail('future local GET must not run in cloud')
    def future_write():
        pytest.fail('future local write must not run in cloud')
    web.app.add_url_rule('/acc-future-read', 'acc_future_read', future_read)
    web.app.add_url_rule('/acc-future-write', 'acc_future_write', future_write, methods=['POST'])
    assert web.app.test_client().get('/acc-future-read').status_code == 404
    assert web.app.test_client().post('/acc-future-write').status_code == 403
    with web.app.test_request_context('/'):
        html = render_template('base.html', company_id='acc-cloud')
        assert 'data-upload-open' not in html


def test_cloud_launcher_serves_real_http_without_memory_root(tmp_path):
    import os
    import socket
    import time
    import urllib.error
    import urllib.request
    from contextlib import closing

    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, SAM_CLOUD_PYTHON=sys.executable, SAM_DATA_ROOT=str(tmp_path),
               PORT=str(port), HOST='127.0.0.1', NO_PROXY='127.0.0.1')
    for key in ('SAM_CLOUD_READONLY', 'SAM_MEMORY_ROOT_URI', 'SAM_VIEW_USER', 'SAM_VIEW_PASS'):
        env.pop(key, None)
    from app.cloud_snapshot import initialize_snapshot
    initialize_snapshot(tmp_path / 'cloud-snapshot.db')
    with (tmp_path / 'server.log').open('w+') as log:
        process = subprocess.Popen(['bash', str(ROOT / 'scripts/cloud_run.sh')], cwd='/tmp',
                                   env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=1) as response:
                        assert json.load(response) == {'ok': True, 'read_only': True}
                    break
                except urllib.error.URLError:
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(.05)
            else:
                pytest.fail('cloud launcher readiness timeout')
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/bizov', timeout=2) as response:
                assert '暂无同步数据' in response.read().decode()
            # cloud_run.sh initializes only the dedicated FactsTree store.
            assert (tmp_path / 'cloud-facts-tree-snapshot.db').exists()
            assert not (tmp_path / 'app.db').exists()
        finally:
            process.terminate()
            process.wait(timeout=5)


def test_homepage_transport_rejects_redirect_and_redacts_key(monkeypatch, capsys):
    from scripts import sam_knowledge_push as push
    from tests.test_knowledge_push import cloud
    import os
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-cloud')
    monkeypatch.setenv('SAM_CLOUD_INGEST_KEY', 'acc-secret-home-key')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    monkeypatch.setattr(push, 'home_snapshot', lambda company_id: snapshot())
    with cloud(monkeypatch) as destination:
        target = os.environ['SAM_KNOWLEDGE_CLOUD_URL'] + 'ingest'
        with cloud(monkeypatch, status=307, location=target,
                   response=b'acc-secret-home-key'):
            monkeypatch.setenv('SAM_CLOUD_URL', os.environ['SAM_KNOWLEDGE_CLOUD_URL'])
            assert push.main(['--homepage']) == 1
        assert not destination
    output = capsys.readouterr()
    assert 'HTTP 307' in output.err
    assert 'acc-secret-home-key' not in output.err


def test_changed_host_scope_does_not_display_previous_company(cloud_web, monkeypatch):
    cloud_web.app.extensions['sam_snapshot_store'].replace(snapshot())
    monkeypatch.setenv('SAM_COMPANY_ID', 'acc-new-company')
    client = cloud_web.app.test_client()
    assert client.get('/api/knowledge').get_json() == {}
    html = client.get('/overview').get_data(as_text=True)
    assert '事实原文' not in html
    assert '暂无同步数据' in html


def test_malformed_optional_collections_do_not_break_pages(cloud_web):
    value = snapshot()
    value['payload']['page_context'] = {'facts': 3, 'todos': False, 'files': 'opaque', 'module_counts': []}
    cloud_web.app.extensions['sam_snapshot_store'].replace(value)
    for path in ['/bizov', '/overview', '/facts', '/todos', '/files']:
        assert cloud_web.app.test_client().get(path).status_code == 200


def facts_tree_section(company='acc-cloud'):
    """A synthetic ready section carrying a valid placement contract marker."""
    from app.facts_tree.tree_read import attach_placement_validation
    binding = {'template_pack': 'generic', 'pack_version': 1, 'binding_revision': 1,
               'pack_digest': 'a' * 64, 'digest_source': 'verified-manifest'}
    section = {'company_id': company, 'state': 'ready',
               'tree': {'company_id': company, 'binding': binding,
                        'node': 'tree', 'code': None, 'state': 'ready', 'brief': '树简报原文'},
               'trunks': {'bizov': {'company_id': company, 'node': 'trunk', 'code': 'bizov',
                                    'state': 'ready', 'brief': '枝简报原文'}},
               'leaves': {'L-bizov-08': {'company_id': company, 'node': 'leaf', 'code': 'L-bizov-08',
                                         'state': 'ready', 'version': 1, 'content': '叶正文原文'}}}
    return attach_placement_validation(section)


def test_cloud_facts_tree_reads_snapshot_only(cloud_web, monkeypatch):
    from app import settings
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    data = snapshot()
    data['payload'] = {'facts_tree': facts_tree_section()}
    cloud_web.app.extensions['sam_facts_tree_snapshot_store'].replace(data)
    client = cloud_web.app.test_client()
    tree = client.get('/api/facts/tree').get_json()
    assert tree['state'] == 'ready' and tree['brief'] == '树简报原文'
    trunk = client.get('/api/facts/tree?node=trunk&code=bizov').get_json()
    assert trunk['brief'] == '枝简报原文' and trunk['code'] == 'bizov'
    leaf = client.get('/api/facts/tree?node=leaf&code=L-bizov-08').get_json()
    assert leaf['content'] == '叶正文原文' and leaf['version'] == 1
    assert client.get('/api/facts/tree?node=trunk&code=nope').status_code == 404
    assert client.get('/api/facts/tree?node=leaf&code=L-hr-01').status_code == 404
    assert client.get('/api/facts/tree?node=branch').status_code == 400
    assert client.get('/api/facts/tree?node=trunk').status_code == 400
    result = client.get('/api/facts/tree?company_id=other')
    assert result.status_code == 400 and result.get_json()['error'] == 'company_id_rejected'
    assert not cloud_web.MAIN_DB.exists()


def test_cloud_facts_tree_unavailable_without_matching_section(cloud_web, monkeypatch):
    from app import settings
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    client = cloud_web.app.test_client()
    assert client.get('/api/facts/tree').status_code == 503  # no dedicated snapshot at all
    # A homepage snapshot carrying facts_tree is never a fallback source.
    forged = snapshot()
    forged['payload']['facts_tree'] = facts_tree_section()
    cloud_web.app.extensions['sam_snapshot_store'].replace(forged)
    result = client.get('/api/facts/tree')
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    # A dedicated envelope without the section fails closed as well.
    cloud_web.app.extensions['sam_facts_tree_snapshot_store'].replace(snapshot())
    result = client.get('/api/facts/tree')
    assert result.status_code == 503 and result.get_json()['error'] == 'facts_tree_unavailable'
    foreign = snapshot()
    foreign['company_id'] = 'acc-other'
    foreign['payload'] = {'facts_tree': facts_tree_section(company='acc-other')}
    cloud_web.app.extensions['sam_facts_tree_snapshot_store'].replace(foreign)
    assert client.get('/api/facts/tree').status_code == 503  # cross-company snapshot never served
    monkeypatch.delenv('SAM_COMPANY_ID')
    result = client.get('/api/facts/tree')
    assert result.status_code == 503 and result.get_json()['error'] == 'company_scope_missing'


def test_cloud_facts_tree_unbound_state(cloud_web, monkeypatch):
    from app import settings
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    data = snapshot()
    data['payload'] = {'facts_tree': {'company_id': 'acc-cloud', 'state': 'unbound'}}
    cloud_web.app.extensions['sam_facts_tree_snapshot_store'].replace(data)
    client = cloud_web.app.test_client()
    for query in ('', '?node=trunk&code=bizov', '?node=leaf&code=L-bizov-08'):
        result = client.get('/api/facts/tree' + query)
        assert result.status_code == 200 and result.get_json()['state'] == 'unbound'


def test_local_snapshot_includes_facts_tree_documents_only(tmp_path, monkeypatch):
    from app import cloud_export, settings
    from app.db.database import init_db
    from app.facts_tree.leaf_store import LeafPublishService
    from app.facts_tree.rollup import FactsTreeRollup
    from app.memory import paths
    from app.module_tree import MODULES
    from app.ports import LocalMemoryProvider
    from types import SimpleNamespace
    from tests.test_facts_tree_leaves import artifact, SHA

    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    db = tmp_path / 'app.db'
    init_db(db)
    memory = LocalMemoryProvider(tmp_path / 'memory')
    service = LeafPublishService(db, memory, company_id='acc-cloud')
    service.binding.register('acc-cloud', '云端测试公司')
    service.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                       (SHA, 'sample.docx', 1, '/unused', '2026-10-14'))
    service.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', ('acc-cloud', '临时主体', '测试指标', '2025-01', '9', '个', SHA, 'bizov', '2026-10-14'))
    service.db.commit()
    markdown = artifact(company_id='acc-cloud')
    service.publish_leaf(company_id='acc-cloud', leaf_code='L-bizov-08', markdown=markdown,
                         source_files=[SHA], fact_ids=[1])
    from app.facts_tree.fact_identity import FactIdentityStore
    from app.facts_tree.fact_placements import FactPlacementStore
    FactIdentityStore(service.db).register_initial('acc-cloud', 1, '归属测试', '身份登记')
    FactPlacementStore(service.db, company_id='acc-cloud').place_initial(
        'acc-cloud', 1, 'bizov', 'L-bizov-08', '归属测试', '首次归属')
    rollup = FactsTreeRollup(service)
    rollup.rollup_trunk(company_id='acc-cloud', trunk='bizov')
    rollup.rollup_tree(company_id='acc-cloud')
    service.close()
    config = SimpleNamespace(cloud_readonly=False, main_db=db, objects_dir=tmp_path / 'objects')
    monkeypatch.setattr(cloud_export.RuntimeConfig, 'from_env', lambda **_: config)
    monkeypatch.setattr(cloud_export, 'memory_provider', lambda actual: memory)
    home = cloud_export.build_home_snapshot(company_id='acc-cloud')
    json.dumps(home, ensure_ascii=False)
    # The homepage snapshot keeps its existing sections but never embeds FactsTree.
    assert 'facts_tree' not in home['payload']
    assert set(home['payload']['page_context']) >= {'facts', 'todos', 'files', 'module_counts'}
    result = cloud_export.build_facts_tree_snapshot(company_id='acc-cloud')
    json.dumps(result, ensure_ascii=False)
    assert result['company_id'] == 'acc-cloud' and result['version'] == 1
    assert set(result['payload']) == {'facts_tree'}
    section = result['payload']['facts_tree']
    assert section['company_id'] == 'acc-cloud' and section['state'] == 'ready'
    assert section['tree']['brief'] == memory.read(paths.tree_brief_uri('acc-cloud'))
    assert section['trunks']['bizov']['brief'] == memory.read(
        paths.trunk_doc_uri('acc-cloud', MODULES[1].directory, 'trunk-brief.md'))
    assert section['leaves']['L-bizov-08']['content'] == markdown
    assert section['leaves']['L-bizov-01']['state'] == 'not_generated'
    assert '历史对话' not in json.dumps(section, ensure_ascii=False)  # no conversations/originals leak
    # DEC-020 A: the ready section carries the minimal placement contract marker.
    from app.facts_tree.tree_read import (PLACEMENT_CONTRACT_VERSION,
                                          verify_placement_validation)
    marker = section['placement_validation']
    binding = section['tree']['binding']
    assert marker == {'contract_version': PLACEMENT_CONTRACT_VERSION, 'company_id': 'acc-cloud',
                      'template_pack': binding['template_pack'], 'pack_version': binding['pack_version'],
                      'binding_revision': binding['binding_revision'], 'pack_digest': binding['pack_digest'],
                      'snapshot_sha256': marker['snapshot_sha256']}
    assert len(marker['snapshot_sha256']) == 64
    verify_placement_validation(section, 'acc-cloud')  # the marker recomputes cleanly
    # The ledger itself never leaves the local database.
    assert 'fact_uid' not in json.dumps(section, ensure_ascii=False)
    unbound = cloud_export.build_facts_tree_snapshot(company_id='acc-other')
    assert set(unbound['payload']) == {'facts_tree'}
    assert unbound['payload']['facts_tree'] == {'company_id': 'acc-other', 'state': 'unbound'}


@pytest.mark.parametrize('with_snapshot', [False, True])
def test_cloud_facts_page_uses_tree_template_without_flat_facts(cloud_web, with_snapshot, monkeypatch):
    """F-004 P03: /facts renders facts.html with or without a snapshot; the
    readonly gate stays and snapshot page_context.facts never reach the page."""
    import app.cloud_home as cloud_home
    web = cloud_web
    if with_snapshot:
        web.app.extensions['sam_snapshot_store'].replace(snapshot())
    monkeypatch.setattr(web, 'consolidation_service', lambda: pytest.fail('no local ledger'))
    rendered = []
    real_render = cloud_home.render_template
    def recording_render(template, **context):
        rendered.append((template, context))
        return real_render(template, **context)
    monkeypatch.setattr(cloud_home, 'render_template', recording_render)
    result = web.app.test_client().get('/facts')
    assert result.status_code == 200
    assert [name for name, _ in rendered] == ['facts.html']
    context = rendered[0][1]
    assert context['company_id'] == 'acc-cloud'
    assert 'facts' not in context  # old flat facts never reach the tree template
    assert context['todos'] == (snapshot()['payload']['page_context']['todos'] if with_snapshot else [])
    html = result.get_data(as_text=True)
    assert '事实原文' not in html  # snapshot flat facts stay out of the page
    assert 'data-upload-open' not in html
    assert 'id="upload-modal"' not in html
    assert not web.MAIN_DB.exists()
