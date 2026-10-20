"""Storage-only visibility, cloud whitelist and binding regression tests."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest
from flask import Flask

from app import settings, cloud_export, cloud_home
from app.db.database import init_db
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.leaf_build import LeafBuildService
from app.facts_tree.leaf_store import LeafPublishService, LeafRejected
from app.facts_tree.quote_sources import DossierQuoteResolver
from app.facts_tree.visibility import active_trunks, visible_modules_for_company
from app.module_tree import module_definitions, MODULES
from app.ports import LocalMemoryProvider
from tests.test_facts_tree_leaves import SHA, artifact

COMPANY = 'acc-b3a'
LOC = 'docx:body/77/paragraph'
STATIC_GOLDEN = (Path(__file__).parent/'fixtures/visible_modules_b2b2.json').read_text().strip()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    db = tmp_path / 'ledger.db'
    init_db(db)
    memory = LocalMemoryProvider(tmp_path / 'documents')
    store = LeafPublishService(db, memory, company_id=COMPANY)
    store.binding.register(COMPANY, COMPANY)
    store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (SHA, 'sample.docx', 1, '/unused', 'now'))
    store.db.execute('INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     ('job', COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    yield db, memory, store
    store.close()


def build(tree, code):
    _, _, store = tree
    resolver = DossierQuoteResolver({SHA: {'markdown': '测试描述。', 'document_map': {'elements': [
        dict(source_loc=LOC, start_line=1, end_line=1)]}}})
    payload = dict(leaf_code=code, markdown='## 概览\n测试描述。[^one]\n## 正文\n测试描述。[^one]\n\n[^one]: sample.docx → '+LOC,
                   fact_ids=[], source_files=[SHA], note='首建')
    return LeafBuildService(store, quote_resolver=resolver).build_leaf(company_id=COMPANY, leaf_code=code, employee_payload=payload)


def active(tree, company=COMPANY):
    return active_trunks(db_path=tree[0], memory=tree[1], company_id=company)


def visible(tree, company=COMPANY):
    return visible_modules_for_company(db_path=tree[0], memory=tree[1], company_id=company)


def archive(tree, entry):
    text = tree[1].read(entry['uri']).replace('status: active', 'status: archived').replace('version: 1', 'version: 2')
    text = text.replace('\n\n[^one]:', '\n| v2 | now | fact-leaf | sample | archive |\n\n[^one]:')
    tree[2].publish_leaf(company_id=COMPANY, leaf_code=entry['code'], markdown=text, source_files=[SHA], fact_ids=[])


def test_empty_and_unbound_visibility_all_ten_hidden(tree):
    assert active(tree) == [] and active(tree, 'acc-unbound') == []
    for company in (COMPANY, 'acc-unbound'):
        modules = visible(tree, company)
        assert len(modules) == 10 and all(m['hidden'] for m in modules)
        assert [m['code'] for m in modules] == [m.key for m in MODULES]
        assert not any(m['code'] in ('待办与冲突', '待归类') for m in modules)


def test_active_hidden_branches_and_dynamic_archive(tree):
    first = build(tree, 'L-prod-01')
    assert active(tree) == ['prod']
    modules = visible(tree)
    assert [m['code'] for m in modules if not m['hidden']] == ['prod']
    assert next(m for m in modules if m['code'] == 'prod')['hidden'] is False
    second = build(tree, 'L-sales-03')
    assert active(tree) == ['prod', 'sales']
    archive(tree, dict(first, code='L-prod-01'))
    assert active(tree) == ['sales']
    archive(tree, dict(second, code='L-sales-03'))
    assert active(tree) == []


def test_empty_archived_and_conflict_excluded(tree):
    entry = build(tree, 'L-bizov-07')
    archive(tree, dict(entry, code='L-bizov-07'))
    empty = artifact(code='L-bizov-09', data=False, body='', company_id=COMPANY).replace('- 一句话定位：测试内容', '').replace(f'[^one]: {SHA} '+LOC, '')
    tree[2].publish_leaf(company_id=COMPANY, leaf_code='L-bizov-09', markdown=empty, source_files=[SHA], fact_ids=[])
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False,
                    target_trunk='bizov', target_leaf='L-bizov-07', metric='test', company_id=COMPANY)
    text = text[:text.index('# 测试叶子')] + f'## 候选\n测试描述。[^one]\n\n[^one]: {SHA} {LOC}\n'
    tree[2].publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov',
        target_leaf='L-bizov-07', metric='test', job_id='job', markdown=text, source_files=[SHA], fact_ids=[])
    assert active(tree) == []
    with pytest.raises(PermissionError, match='locked or nonempty'):
        tree[2].binding.bind_pack(COMPANY, 'generic')


def test_readonly_select_and_all_writes_rejected_even_noop(tree):
    entry = build(tree, 'L-bizov-07')
    before = '\n'.join(tree[2].db.iterdump())
    store = LeafPublishService.open_readonly(tree[0], tree[1], company_id=COMPANY)
    try:
        assert store._leaf(COMPANY, 'L-bizov-07')[0].trunk == 'bizov'
        assert store.binding.get(COMPANY)['template_pack'] == 'generic'
        assert store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            store.db.execute('CREATE TABLE forbidden(x)')
        for write in [lambda: store.binding.register('acc-new', 'acc-new'),
                      lambda: store.binding.bind_pack(COMPANY, 'generic'),
                      lambda: store.binding.mark_locked(COMPANY),
                      lambda: store.publish_leaf(company_id=COMPANY, leaf_code='L-bizov-07', markdown=tree[1].read(entry['uri']), source_files=[SHA], fact_ids=[]),
                      lambda: store.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job', markdown='', source_files=[SHA], fact_ids=[])]:
            with pytest.raises(PermissionError, match='read-only'):
                write()
    finally:
        store.close()
    assert '\n'.join(tree[2].db.iterdump()) == before


def test_missing_tables_readonly_does_not_create_ddl(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    db = tmp_path / 'empty.db'
    sqlite3.connect(db).close()
    before = db.read_bytes()
    memory = LocalMemoryProvider(tmp_path / 'docs')
    assert active_trunks(db_path=db, memory=memory, company_id=COMPANY) == []
    assert all(m['hidden'] for m in visible_modules_for_company(db_path=db, memory=memory, company_id=COMPANY))
    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []
    absent = tmp_path / 'not-created' / 'db.sqlite'
    assert active_trunks(db_path=absent, memory=memory, company_id=COMPANY) == []
    assert not absent.parent.exists()


def test_visibility_select_only_authorizer_and_zero_memory_writes(tree, monkeypatch):
    build(tree, 'L-bizov-07')
    from app.facts_tree import visibility
    original = LeafPublishService.open_readonly
    def opened(*args, **kwargs):
        store = original(*args, **kwargs)
        def authorize(action, *args):
            return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY
        store.db.set_authorizer(authorize)
        return store
    monkeypatch.setattr(visibility.LeafPublishService, 'open_readonly', opened)
    monkeypatch.setattr(tree[1], 'put', lambda *a, **kw: pytest.fail('visibility wrote memory'))
    assert active(tree) == ['bizov']


def test_visibility_retains_sha_guard(tree):
    entry = build(tree, 'L-bizov-07')
    tree[1].put(entry['uri'], 'tampered', metadata={})
    with pytest.raises(LeafRejected, match='differs from revision'):
        active(tree)


def test_register_projection_empty_rebind_no_empty_leaves(tree):
    binding = tree[2].binding
    row = binding.get(COMPANY)
    assert row['template_pack'] == 'generic' and row['pack_version'] == 1
    assert binding.read_tree_meta(COMPANY) == {k:v for k,v in row.items() if k != 'company_id'}
    with pytest.raises(ValueError, match='already registered'):
        binding.register(COMPANY, COMPANY)
    assert binding.bind_pack(COMPANY, 'generic') == row
    assert tree[2].db.execute('SELECT count(*) FROM facts_tree_leaf_index').fetchone()[0] == 0
    assert not any('/leaves/' in str(p) for p in tree[1].root.rglob('*'))


def test_leaf_without_atomic_facts_locks_binding(tree):
    build(tree, 'L-bizov-07')
    assert tree[2].db.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
    # The existing nonempty guard, not a new mark_locked side effect, blocks rebinding.
    assert not tree[2].binding.is_locked(COMPANY)
    with pytest.raises(PermissionError, match='locked or nonempty'):
        tree[2].binding.bind_pack(COMPANY, 'generic')


def test_closed_visibility_and_binding_reject_before_writes(tree, monkeypatch):
    before = '\n'.join(tree[2].db.iterdump())
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    for action in [lambda: active(tree), lambda: visible(tree),
                   lambda: tree[2].binding.register('acc-new', 'acc-new'),
                   lambda: tree[2].binding.bind_pack(COMPANY, 'generic')]:
        with pytest.raises(RuntimeError, match='disabled'): action()
    assert '\n'.join(tree[2].db.iterdump()) == before


def export(tree, monkeypatch, company=COMPANY):
    config = SimpleNamespace(cloud_readonly=False, main_db=tree[0], objects_dir=tree[0].parent/'objects')
    monkeypatch.setattr(cloud_export.RuntimeConfig, 'from_env', lambda: config)
    monkeypatch.setattr(cloud_export, 'memory_provider', lambda actual: tree[1])
    return cloud_export.build_home_snapshot(company_id=company)


def test_export_only_static_subset_order_and_hidden_never_expands(tree, monkeypatch):
    for code in ('L-bizov-07', 'L-info-03', 'L-prod-01'):
        build(tree, code)
    expected = [m for m in module_definitions(include_hidden=False) if m['code'] in ('info', 'bizov')]
    result = export(tree, monkeypatch)
    assert result['version'] == 1
    assert result['payload']['page_context']['visible_modules'] == expected


def test_export_empty_unbound_and_closed_golden(tree, monkeypatch):
    assert export(tree, monkeypatch)['payload']['page_context']['visible_modules'] == []
    assert export(tree, monkeypatch, 'acc-unbound')['payload']['page_context']['visible_modules'] == []
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    result = export(tree, monkeypatch)
    static = module_definitions(include_hidden=False)
    assert result['payload']['page_context']['visible_modules'] == static
    assert json.dumps(result['payload']['page_context']['visible_modules'], ensure_ascii=False) == STATIC_GOLDEN


def home_context(value):
    app = Flask(__name__)
    class Store:
        def read(self): return {'company_id': COMPANY, 'payload': {'page_context': value}}
    app.extensions['sam_snapshot_store'] = Store()
    with app.test_request_context():
        return cloud_home.page_context(app)['visible_modules']


def test_cloud_home_dirty_membership_only_local_display_and_order(monkeypatch):
    monkeypatch.delenv('SAM_COMPANY_ID', raising=False)
    items = [{'code':'legal', 'name':'<script>', 'sort_order':-1, 'hidden':True},
             {'code':'unknown'}, {'code':4}, 'info', {'code':'prod'}, {'code':'purch'},
             {'code':'info','name':'forged'}, {'code':'info'}, {'code':[]}]
    expected = [m for m in module_definitions(include_hidden=False) if m['code'] in ('info', 'legal')]
    assert home_context({'visible_modules': items}) == expected


@pytest.mark.parametrize('value', [None, {}, 'info', 1, []])
def test_cloud_home_nonlist_or_empty_narrows_to_empty(value, monkeypatch):
    monkeypatch.delenv('SAM_COMPANY_ID', raising=False)
    assert home_context({'visible_modules': value}) == []


def test_cloud_home_missing_and_closed_snapshot_golden(monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    monkeypatch.delenv('SAM_COMPANY_ID', raising=False)
    static = module_definitions(include_hidden=False)
    assert home_context({}) == static
    dirty = deepcopy(static)
    for item in dirty: item['name'] = 'forged'
    assert home_context({'visible_modules': dirty}) == static
    assert json.dumps(home_context({'visible_modules': static}), ensure_ascii=False) == STATIC_GOLDEN


def test_conflict_only_without_atomic_facts_blocks_rebind(tree):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False,
                    target_trunk='bizov', target_leaf='L-bizov-07', metric='test', company_id=COMPANY)
    text = text[:text.index('# 测试叶子')] + f'## 候选\n测试描述。[^one]\n\n[^one]: {SHA} {LOC}\n'
    tree[2].publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov',
        target_leaf='L-bizov-07', metric='test', job_id='job', markdown=text, source_files=[SHA], fact_ids=[])
    assert tree[2].db.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
    assert tree[2].db.execute('SELECT count(*) FROM facts_tree_leaf_index').fetchone()[0] == 1
    assert active(tree) == []
    with pytest.raises(PermissionError, match='locked or nonempty'):
        tree[2].binding.bind_pack(COMPANY, 'generic')


def test_archived_only_leaf_still_blocks_rebind(tree):
    entry = build(tree, 'L-bizov-07')
    archive(tree, dict(entry, code='L-bizov-07'))
    assert active(tree) == []
    assert tree[2].db.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
    with pytest.raises(PermissionError, match='locked or nonempty'):
        tree[2].binding.bind_pack(COMPANY, 'generic')


def test_binding_empty_check_missing_leaf_index_table_is_safe(tree):
    tree[2].db.execute('DROP TABLE facts_tree_leaf_index')
    tree[2].db.commit()
    assert tree[2].binding.bind_pack(COMPANY, 'generic')['template_pack'] == 'generic'


def test_readonly_binding_rejects_injected_writable_connection(tree):
    with pytest.raises(ValueError, match='database path'):
        CompanyBindingStore(tree[2].db, read_only=True)


def test_readonly_binding_path_directly_rejects_sql_write(tree):
    binding = CompanyBindingStore(tree[0], read_only=True)
    try:
        assert binding.get(COMPANY)['template_pack'] == 'generic'
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            binding.db.execute('DELETE FROM company_tree_meta')
    finally:
        binding.close()
