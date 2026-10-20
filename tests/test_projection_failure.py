"""M3 upload terminal states with only synthetic acc-gudou evidence."""
import json
import time

import pytest

from app.db import connect, init_db
from app.employees import EmployeeRunner
from app.entities import EntityRosterService
from app.harness.staging import StagingStore
from app.memory.company_facts import L2CompleteFactService
from app.parse_jobs import ParseJobManager
from app.ports import LocalMemoryProvider
from app.storage import ArchiveFileStore
from app.runtime_errors import EmployeeTransportError

COMPANY = 'acc-gudou'
ENTITY = '谷斗科技（上海）有限公司'
TEXT = ENTITY + '2024年度收入3.20亿元'


@pytest.mark.parametrize('failure', ['l1-exception', 'l1-rejected', 'l0-exception', 'l0-rejected', 'transport', 'queue'])
def test_projection_terminal_and_projection_only_retry(tmp_path, monkeypatch, failure):
    import webapp.app as web
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    stored = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'acc-gudou synthetic', original_name='acc-gudou.xlsx')
    staging_id = StagingStore(db).save_manifest({'file_hash': stored.file_hash, 'filename': stored.original_name,
        'format': 'xlsx', 'parse_summary': {'status': 'parsed'}, 'pages': [{'page_no': 1,
        'text_items': [{'text': TEXT, 'source_loc': {'locator': 'text/0'}}], 'tables': []}]})
    EntityRosterService(db).declare(company_id=COMPANY, entity_name=ENTITY)
    calls, broken = [], [True]
    class Adapter:
        def run_parse(self, *_a, **_kw):
            calls.append('parse')
            return staging_id
    class Classification:
        def classify_parsed(self, **_):
            calls.append('classify')
            return {'id': 1, '_created_for_parse': False, 'resource_folder': '财务'}
    class EntityEmployee:
        def attribute_block(self, **_):
            calls.append('entity')
            return {'label': 'self', 'subject': ENTITY, 'relation': None, 'reason': 'synthetic'}
    def run(*, skill, text, **_):
        calls.append(skill)
        if skill == 'finance-fact-extraction':
            return {'items': [{'metric': '收入', 'value': '3.20', 'unit': '亿元', 'entity': ENTITY,
                               'period': '2024', 'quote': TEXT, 'module_key': 'fin'}]}
        if skill == 'fact-brief':
            if broken[0] and failure == 'transport':
                raise EmployeeTransportError('fixture private /internal/path', retryable=False)
            if broken[0] and failure == 'l1-exception':
                raise RuntimeError('secret /internal/path')
            ref = json.loads(text)['facts'][0]['fact_id']
            value = '3.21' if broken[0] and failure == 'l1-rejected' else '3.20'
            return {'items': [{'topic': '合成', 'conclusion': f'{value}亿元{{f:{ref}}}', 'fact_ids': [ref]}]}
        if broken[0] and failure == 'l0-exception':
            raise RuntimeError('secret /internal/path')
        value = '3.21' if broken[0] and failure == 'l0-rejected' else '3.20'
        entry_id = json.loads(text)['l1']['items'][0]['entry_id']
        assert entry_id == 'fin-1'
        return {'items': [{'conclusion': f'{value}亿元{{l:{entry_id}}}', 'l1_entry_ids': [entry_id]}]}
    def queue(*_a, **_kw):
        calls.append('queue')
        if failure == 'queue':
            raise RuntimeError('follow-up unavailable')
    monkeypatch.setattr(web, 'MAIN_DB', db)
    monkeypatch.setattr(web, 'OBJECTS_DIR', tmp_path / 'objects')
    monkeypatch.setattr(web, '_PARSE_ADAPTER', Adapter())
    monkeypatch.setattr(web, "_review_parsed_document", lambda job, adapter, staging, staging_id, timeout, check_cancel: staging_id)
    monkeypatch.setattr(web, 'classification_service', lambda **_: Classification())
    monkeypatch.setattr(web, 'semantic_employee', lambda **_: EntityEmployee())
    monkeypatch.setattr(web, 'EmployeeRunner', lambda **kw: EmployeeRunner(runner=run, retries=0, **kw))
    monkeypatch.setitem(web.app.extensions, 'sam_memory_provider', LocalMemoryProvider(tmp_path / 'memory'))
    monkeypatch.setattr(web, 'queue_import_guide', queue)
    manager = ParseJobManager(db, web._parse_job_worker, workers=1)
    monkeypatch.setattr(web, '_PARSE_MANAGER', manager)
    def wait(job_id):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            with manager._lock:
                if job_id not in manager._events:
                    return manager.get(job_id)
            time.sleep(.01)
        pytest.fail('worker did not terminate')
    try:
        job = manager.create(company_id=COMPANY, file_hash=stored.file_hash, original_name=stored.original_name,
                             file_format='xlsx', harness_format='xlsx', size_bytes=1)
        terminal = wait(job['job_id'])
        assert terminal['l2_committed'] == 1
        before = L2CompleteFactService(db).list_facts(company_id=COMPANY)
        assert len(before) == 1
        assert before[0]['module_key'] == 'fin'
        assert json.loads(terminal['projection_fact_ids']) == [before[0]['id']]
        if failure == 'queue':
            assert terminal['status'] == 'done'
            assert 'queue' in calls
            return
        assert terminal['status'] == 'failed'
        assert terminal['error_kind'] == ('employee_transport' if failure == 'transport' else 'projection_failed')
        if failure != 'transport':
            assert '事实已入库' in terminal['message']
            assert 'secret' not in terminal['message'] and '/internal/path' not in terminal['message']
        api = web.app.test_client().get(f"/api/parse-jobs/{job['job_id']}?company_id={COMPANY}")
        assert api.status_code == 200
        assert api.get_json()['job']['status'] == 'failed'
        assert 'queue' not in calls
        if failure == 'l1-exception':
            # Durable routing survives a process/manager restart, not just
            # an in-memory event or the original job dictionary.
            manager.executor.shutdown(wait=True)
            manager.close()
            manager = ParseJobManager(db, web._parse_job_worker, workers=1)
            monkeypatch.setattr(web, '_PARSE_MANAGER', manager)
            assert manager.get(job['job_id'])['l2_committed'] == 1
        broken[0] = False
        manager.retry(job['job_id'])
        assert wait(job['job_id'])['status'] == 'done'
        assert L2CompleteFactService(db).list_facts(company_id=COMPANY) == before
        assert calls.count('parse') == calls.count('classify') == calls.count('entity') == 1
        assert calls.count('finance-fact-extraction') == 1
        with connect(db) as conn:
            assert conn.execute('SELECT COUNT(*) FROM fact_runs WHERE company_id=?', (COMPANY,)).fetchone()[0] == 1
    finally:
        manager.executor.shutdown(wait=True)
        manager.close()


def test_parse_marker_migration_and_restart(tmp_path):
    db = tmp_path / 'acc-gudou.db'
    init_db(db)
    with connect(db) as conn:
        conn.execute('ALTER TABLE parse_jobs DROP COLUMN l2_committed')
    init_db(db)
    with connect(db) as conn:
        assert 'l2_committed' in {r['name'] for r in conn.execute('PRAGMA table_info(parse_jobs)')}
