"""Synthetic acc-gudou M2.5 dictionary and employee contract regressions."""
import sqlite3

import pytest

from app.db import connect, init_db
from app.db.schema import SCHEMA, DOC_TYPES
from app.module_taxonomy import MODULES, MODULE_KEYS
from app.classifier import ClassificationService
from app.storage import ArchiveFileStore


def test_legacy_dictionary_migration_preserves_tags_and_is_idempotent(tmp_path):
    db = tmp_path / 'acc-gudou.db'
    old_codes = ('company_overview', 'finance', 'legal', 'technology_product',
                 'customer_market', 'team_equity', 'other', 'sales', 'hr', 'marketing')
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA.replace(
            'CREATE TABLE IF NOT EXISTS doc_types (',
            'CREATE TABLE IF NOT EXISTS doc_types (parent_module TEXT REFERENCES modules(code),'))
        for index, code in enumerate(old_codes):
            conn.execute('INSERT INTO modules VALUES (?,?,?)', (code, 'acc 旧字典', index))
            conn.execute('INSERT INTO doc_types(code,name,parent_module,sort_order) VALUES (?,?,?,?)',
                         (f'acc-tag-{index}', 'acc 历史标签', code, index))
            conn.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) '
                         'VALUES (?,?,1,?,?)', (f'acc-{index}', '谷斗合成.txt', 'acc-fixture', 'now'))
            conn.execute('INSERT INTO file_classifications(file_hash,company_id,module,doc_type,created_at) '
                         'VALUES (?,?,?,?,?)', (f'acc-{index}', 'acc-gudou', code, f'acc-tag-{index}', 'now'))
    init_db(db)
    with connect(db) as conn:
        rows = [tuple(row) for row in conn.execute('SELECT module,doc_type,needs_review FROM file_classifications ORDER BY id')]
        assert rows == [(None, f'acc-tag-{i}', 1) for i in range(len(old_codes))]
        assert {row['code'] for row in conn.execute('SELECT code FROM modules')} == MODULE_KEYS
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        # Same-spelling legal in an upgraded database is a NEW employee choice.
        conn.execute("UPDATE file_classifications SET module='legal',needs_review=0 WHERE id=1")
        conn.commit()
        before = [tuple(r) for r in conn.execute('SELECT * FROM file_classifications ORDER BY id')]
    init_db(db)
    init_db(db)
    with connect(db) as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM file_classifications ORDER BY id')] == before
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_doc_type_is_orthogonal_even_without_a_module(tmp_path):
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou', original_name='谷斗合成.txt')
    service = ClassificationService(db)
    for module in (None, 'hr', 'legal', 'sales'):
        row = service.reclassify(file_hash=source.file_hash, company_id='acc-gudou',
                                 module=module, doc_type='financial_statement')
        assert row['module'] == module and row['doc_type'] == 'financial_statement'
    with pytest.raises(ValueError, match='unknown doc_type'):
        service.reclassify(file_hash=source.file_hash, company_id='acc-gudou', module='legal', doc_type='bad')


def test_fresh_dictionary_initialization_is_idempotent(tmp_path):
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    init_db(db)
    with connect(db) as conn:
        assert [tuple(r) for r in conn.execute('SELECT code,name,sort_order FROM modules ORDER BY sort_order')] == [
            (m.key, m.directory, m.order) for m in MODULES]
        assert [tuple(r) for r in conn.execute('SELECT code,name,sort_order FROM doc_types ORDER BY code')] == sorted(DOC_TYPES)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


class Employee:
    def __init__(self, result):
        self.result = result

    def classify_document(self, **_):
        return self.result


@pytest.mark.parametrize('module', [m.key for m in MODULES])
def test_employee_ten_modules_are_accepted_and_retrievable(tmp_path, module):
    from app.classifier.semantic_folders import classify
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou-contract', original_name='谷斗合成合同.txt')
    employee = Employee({'module': module, 'doc_type': 'contract'})
    service = ClassificationService(db, employee=employee)
    row = service.classify_parsed(file_hash=source.file_hash, company_id='acc-gudou', manifest={})
    assert row['module'] == module and row['doc_type'] == 'contract'
    assert len(service.list_current(company_id='acc-gudou', module=module)) == 1
    assert classify(text='谷斗合成合同', employee=employee) == next(m.directory for m in MODULES if m.key == module)


@pytest.mark.parametrize('module', ['company_overview', 'finance', 'technology_product',
                                  'customer_market', 'team_equity', 'other', 'marketing',
                                  'module_5', ' fin', 'LEGAL', '', None, ['legal']])
def test_invalid_or_missing_employee_module_never_guessed(tmp_path, module):
    from app.classifier.semantic_folders import classify
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou-finance', original_name='谷斗财务报表.xlsx')
    employee = Employee({'module': module, 'doc_type': 'financial_statement'})
    service = ClassificationService(db, employee=employee)
    row = service.classify_parsed(file_hash=source.file_hash, company_id='acc-gudou',
                                 manifest={'pages': [{'text_items': [{'text': '谷斗合成财务收入利润资产负债'}]}]})
    assert row['module'] is None and row['needs_review'] == 1
    assert row['doc_type'] == 'financial_statement'
    assert classify(text='财务收入利润资产负债', employee=employee) == 'unclassified'
    if isinstance(module, str) and module:
        with pytest.raises(ValueError, match='unknown module'):
            service.reclassify(file_hash=source.file_hash, company_id='acc-gudou', module=module, doc_type=None)


@pytest.mark.parametrize('tag', ['contract', 'patent', 'litigation', 'compliance', 'intellectual_property'])
def test_legal_employee_file_tags_are_orthogonal(tmp_path, tag):
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou-legal', original_name='谷斗合成.txt')
    service = ClassificationService(db, employee=Employee({'module': 'legal', 'doc_type': tag}))
    row = service.classify_parsed(file_hash=source.file_hash, company_id='acc-gudou', manifest={})
    assert row['module'] == 'legal' and row['doc_type'] == tag


def test_filename_and_skill_compatibility_never_classify_without_employee(tmp_path):
    import runpy
    from app.classifier.file_classifier import FileClassifier
    from app.classifier.semantic_folders import classify
    from webapp.classify import classify as web_classify
    entrypoint = runpy.run_path('skills/document-classifier/classifier.py')['classify']
    assert entrypoint('利润 资产负债 现金流 合同 专利') == 'unclassified'
    assert classify(text='财务报表') == 'unclassified'
    assert web_classify('财务报表.xlsx').module is None
    assert web_classify('合同.pdf', employee=Employee({'module': 'finance'})).module is None
    assert FileClassifier.guess('财务报表.xlsx').module is None
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou', original_name='谷斗财务报表.xlsx')
    service = ClassificationService(db)
    row = service.classify_name_fallback(file_hash=source.file_hash, company_id='acc-gudou', original_name=source.original_name)
    assert row['module'] is None and row['needs_review'] == 1


def test_hidden_modules_are_ui_only_and_knowledge_keeps_files(tmp_path, monkeypatch):
    from app.module_taxonomy import module_definitions
    from app.knowledge import KnowledgeService
    from app.ports import LocalMemoryProvider
    from tests.test_frontend_pages import _webapp_module
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    store = ArchiveFileStore(tmp_path / 'objects', db)
    service = ClassificationService(db)
    for module in MODULES:
        source = store.put_bytes(module.key.encode(), original_name=f'谷斗合成-{module.key}.txt')
        service.reclassify(file_hash=source.file_hash, company_id='acc-gudou', module=module.key, doc_type='contract')
    memory = LocalMemoryProvider(tmp_path / 'memory')
    knowledge = KnowledgeService(db, memory, objects_path=tmp_path / 'objects').read(company_id='acc-gudou')
    assert knowledge['modules'] == module_definitions()
    assert {f['category'] for f in knowledge['evidence']['files']} == MODULE_KEYS
    assert len(service.list_current(company_id='acc-gudou')) == 10
    assert [m['code'] for m in module_definitions(include_hidden=False)] == ['info', 'bizov', 'hr', 'mkt', 'legal']
    web = _webapp_module()
    monkeypatch.setattr(web, 'MAIN_DB', db)
    html = web.app.test_client().get('/files?company_id=acc-gudou').get_data(as_text=True)
    import re
    select = html.split('<select id="file-module-filter">')[1].split('</select>')[0]
    options = re.findall(r'<option value="([^"]*)">', select)
    assert options == ['', 'info', 'bizov', 'hr', 'mkt', 'legal', '__none__']
    assert '10-法务合规' in html
    # Hidden files remain in the all-files view and API (only entrypoints hide).
    for key in ('prod', 'purch', 'sales', 'fin', 'fund'):
        assert f'data-module="{key}"' in html
        assert web.app.test_client().get(f'/api/files/classifications?company_id=acc-gudou&module={key}').json['files']


def test_prototype_module_navigation_uses_shared_hidden_dictionary():
    import json
    import subprocess
    from pathlib import Path
    from app.module_taxonomy import module_definitions
    source = Path('prototype/startup-ai-manager.html').read_text()
    functions = source[source.index('  function moduleDictionary(data){'):source.index('  function renderTree(')]
    data = {'modules': module_definitions(), 'evidence': {'files': [
        {'category': 'fin'}, {'category': 'legal'}, {'category': None}]}}
    script = '''const window={SAM_CONFIG:{}};
function kObject(v){return v&&typeof v==='object'&&!Array.isArray(v)?v:{};}
function kList(v){return Array.isArray(v)?v:[];}
function fileCategory(f){return f.category===undefined||f.category===null?null:String(f.category);}
''' + functions + '\nconst data=' + json.dumps(data) + ''';
const categories=driveCategories(data);
console.log(JSON.stringify({categories,files:data.evidence.files.length}));
'''
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    # Hidden branches are filtered straight from the shared module dictionary:
    # they never enter the navigation even when they own files.
    assert [c['category'] for c in value['categories']] == ['info', 'bizov', 'hr', 'mkt', 'legal', None]
    assert {m['code'] for m in data['modules'] if m['hidden']} == {'prod', 'purch', 'sales', 'fin', 'fund'}
    assert value['files'] == 3  # hidden-module files still exist; only the navigation hides them
