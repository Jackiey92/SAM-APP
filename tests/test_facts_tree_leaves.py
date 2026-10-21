"""B1 artifacts use only temporary ledgers and a temporary document port."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import sqlite3

import pytest
import yaml

from app import settings
from app.db.database import init_db
from app.db.schema import SCHEMA, LEAF_SCHEMA
from app.facts_tree.leaf_store import (LeafPublishService, LeafRejected, InMemoryQuoteResolver,
                                      NullQuoteResolver, slug)
from app.memory.initialize import initialize_memory
from app.memory import paths

SHA = 'a' * 64
OTHER = 'b' * 64
COMPANY = 'acc-b1'


class DocumentPort:
    """No vectors, networks, or production provider integration in B1 tests."""
    def __init__(self, root):
        self.memory_root = str(root)
        self.root = root
        self.writes = 0
        self.fail = False

    def path(self, uri):
        return self.root / hashlib.sha256(uri.encode()).hexdigest()

    def read(self, uri):
        return self.path(uri).read_bytes().decode('utf-8')

    def put(self, uri, content, *, metadata):
        self.writes += 1
        self.path(uri).write_text(content, encoding='utf-8')
        if self.fail:
            self.fail = False
            raise OSError('synthetic document failure')

    def delete(self, uri):
        self.path(uri).unlink(missing_ok=True)


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    database = tmp_path / 'ledger.db'
    init_db(database)
    root = tmp_path / 'docs'
    root.mkdir()
    port = DocumentPort(root)
    service = LeafPublishService(database, port, company_id=COMPANY)
    service.binding.register(COMPANY, '临时测试公司')
    service.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                       (SHA, 'sample.docx', 1, '/unused', '2026-10-14'))
    service.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '测试指标', '2025-01', '9', '个', SHA, 'bizov', '2026-10-14'))
    service.db.commit()
    yield service
    service.close()


def artifact(*, code='L-bizov-08', version=1, data=True, body='有9个。[^one]', history=None,
             status='active', **meta_changes):
    meta = dict(leaf_code=code, trunk='bizov', title='测试叶子', company_id=COMPANY,
                status=status, latest_period='2025-01', sources=[SHA], source_file_count=1,
                version=version, created_at='2026-10-14', updated_at='2026-10-14', owner_skill='fact-leaf')
    meta.update(meta_changes)
    table = '\n## 数据明细\n| fact_id | 指标 | 期间 | 数值 | 单位 | 来源 |\n|---|---|---|---|---|---|\n| F1 | 测试指标 | 2025-01 | 9 | 个 | [^one] |\n' if data else ''
    history = history or ''.join(f'| v{v} | 日期 | fact-leaf | sample | 更新 |\n' for v in range(1, version+1))
    return ('---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n'
            '# 测试叶子\n## 概览\n- 一句话定位：测试内容\n## 正文\n' + body + '\n' + table +
            '\n## 编辑历史\n| 版本 | 时间 | 技能 | 原件 | 摘要 |\n|---|---|---|---|---|\n' + history +
            f'\n[^one]: {SHA} docx:body/77/paragraph\n')


def publish(kernel, markdown=None, **kwargs):
    return kernel.publish_leaf(company_id=COMPANY, leaf_code=kwargs.pop('leaf_code', 'L-bizov-08'),
                               markdown=markdown or artifact(), source_files=kwargs.pop('source_files', [SHA]),
                               fact_ids=kwargs.pop('fact_ids', [1]), **kwargs)


def clean(kernel):
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert kernel.db.execute('SELECT count(*) FROM facts_tree_leaf_index').fetchone()[0] == 0
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 0
    assert list(kernel.memory.root.iterdir()) == []


def test_new_and_old_schema_facts_unchanged_and_links_created(tmp_path):
    for old in (False, True):
        database = tmp_path / f'{old}.db'
        db = sqlite3.connect(database)
        db.executescript(SCHEMA.replace(LEAF_SCHEMA, '') if old else SCHEMA)
        db.execute("INSERT INTO facts(entity,attribute,value,created_at) VALUES('x','x','9','now')")
        db.commit()
        before = db.execute('SELECT * FROM facts').fetchall()
        columns = db.execute('PRAGMA table_info(facts)').fetchall()
        initialize_memory(db)
        initialize_memory(db)
        assert db.execute('SELECT * FROM facts').fetchall() == before
        assert db.execute('PRAGMA table_info(facts)').fetchall() == columns
        assert 'leaf_code' not in {r[1] for r in columns}
        assert db.execute('SELECT count(*) FROM fact_leaf_links').fetchone() == (0,)
        assert {r[1] for r in db.execute('PRAGMA index_list(fact_leaf_links)')} >= {'idx_fll_leaf', 'idx_fll_file'}
        db.close()


def test_versions_append_only_unique_and_noop(kernel):
    first = publish(kernel)
    old = dict(kernel.db.execute('SELECT * FROM leaf_revisions').fetchone())
    writes = kernel.memory.writes
    assert publish(kernel) == {**first, 'changed': False}
    assert kernel.memory.writes == writes
    second = publish(kernel, artifact(version=2, title='改标题'))
    assert second['uri'] == first['uri']
    assert second['version'] == 2
    assert dict(kernel.db.execute('SELECT * FROM leaf_revisions WHERE version=1').fetchone()) == old
    with pytest.raises(sqlite3.IntegrityError):
        kernel.db.execute('INSERT INTO leaf_revisions SELECT NULL,company_id,leaf_code,version,content_sha,source_files,fact_ids,owner_skill,attribution_id,change_note,created_at FROM leaf_revisions WHERE version=1')
    kernel.db.rollback()
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2


@pytest.mark.parametrize('case', [
    'unknown', 'binding', 'trunk', 'company', 'missing-field', 'yaml', 'duplicate-yaml', 'owner',
    'version', 'boolean-version', 'sources', 'count', 'status', 'section', 'order', 'duplicate-section',
    'gap-heading', 's-no-data', 'dangling', 'duplicate-footnote', 'unused-footnote', 'paragraph',
    'fake-sha', 'fake-definition', 'undeclared-fact', 'missing-fact', 'cross-company', 'cross-trunk',
    'fact-conflict', 'fact-inferred', 'superseded', 'expired', 'bad-leaf-assignment', 'wrong-leaf-trunk',
    'body-number', 'body-unit', 'table-number', 'table-period', 'row-source', 'history',
    'sources-type', 'refs-type', 'source-foreign', 'quote-other-paragraph',
])
def test_invalid_artifacts_no_files_no_revisions(kernel, case):
    text = artifact()
    kwargs = {}
    if case == 'unknown': kwargs['leaf_code'] = 'L-bizov-99'
    elif case == 'binding':
        kernel.db.execute('DELETE FROM company_tree_meta'); kernel.db.commit()
    elif case == 'trunk': text = artifact(trunk='hr')
    elif case == 'company': text = artifact(company_id='acc-other')
    elif case == 'missing-field': text = text.replace('title: 测试叶子\n', '')
    elif case == 'yaml': text = text.replace('title: 测试叶子', 'title: [')
    elif case == 'duplicate-yaml': text = text.replace('title: 测试叶子', 'title: 测试叶子\ntitle: 重复')
    elif case == 'owner': text = artifact(owner_skill='other')
    elif case == 'version': text = artifact(version=2)
    elif case == 'boolean-version': text = artifact().replace('version: 1', 'version: true')
    elif case == 'sources': text = artifact(sources=[OTHER])
    elif case == 'count': text = artifact(source_file_count=2)
    elif case == 'status': text = artifact(status='conflict')
    elif case == 'section': text = text.replace('## 正文', '## 其他')
    elif case == 'order': text = text.replace('## 概览', '## temp').replace('## 正文', '## 概览').replace('## temp', '## 正文')
    elif case == 'duplicate-section': text += '\n## 正文\n'
    elif case == 'gap-heading': text += '\n## 待补\n'
    elif case == 's-no-data': text = artifact(data=False)
    elif case == 'dangling': text = text.replace('有9个。[^one]', '有9个。[^none]')
    elif case == 'duplicate-footnote': text += f'\n[^one]: {SHA} docx:body/77/paragraph\n'
    elif case == 'unused-footnote': text += f'\n[^unused]: {SHA} docx:body/77/paragraph\n'
    elif case == 'paragraph': text = artifact(body='无脚注\n\n有9个。[^one]')
    elif case == 'fake-sha': kwargs['source_files'] = [OTHER]
    elif case == 'fake-definition': text = text.replace(f'[^one]: {SHA}', f'[^one]: {OTHER}')
    elif case == 'undeclared-fact': kwargs['fact_ids'] = []
    elif case == 'missing-fact': kwargs['fact_ids'] = [999]
    elif case in ('bad-leaf-assignment', 'wrong-leaf-trunk'):
        code = 'L-bizov-99' if case == 'bad-leaf-assignment' else 'L-hr-01'
        kernel.db.execute('INSERT INTO fact_leaf_links(company_id,fact_id,trunk,leaf_code,source_file,created_at) VALUES(?,?,?,?,?,?)',
                          (COMPANY, 1, 'bizov', code, SHA, 'now'))
        kernel.db.commit()
    elif case in ('cross-company', 'cross-trunk', 'fact-conflict', 'fact-inferred', 'superseded', 'expired'):
        column, value = {'cross-company': ('company_id', 'acc-other'), 'cross-trunk': ('module_key', 'hr'),
            'fact-conflict': ('status', 'conflict'), 'fact-inferred': ('status', 'inferred'),
            'superseded': ('superseded_by', 1), 'expired': ('valid_to', 'now')}[case]
        kernel.db.execute(f'UPDATE facts SET {column}=? WHERE id=1', (value,)); kernel.db.commit()
    elif case == 'body-number': text = artifact(body='有10个。[^one]')
    elif case == 'body-unit': text = artifact(body='有9元。[^one]')
    elif case == 'table-number': text = text.replace('| 9 |', '| 10 |')
    elif case == 'table-period': text = text.replace('| 2025-01 |', '| 2024-01 |')
    elif case == 'row-source': text = text.replace('| [^one] |', '| 无 |')
    elif case == 'history': text = artifact(history='| v2 | 日期 | 技能 | 原件 | 更新 |\n')
    elif case == 'sources-type': kwargs['source_files'] = [1]
    elif case == 'refs-type': kwargs['fact_ids'] = [True]
    elif case == 'source-foreign':
        kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)', (OTHER, 'other.docx', 1, '/unused', 'now'))
        kernel.db.commit(); kwargs['source_files'] = [OTHER]
    elif case == 'quote-other-paragraph':
        kernel.quote_resolver = InMemoryQuoteResolver({(SHA, 'docx:body/77/paragraph'): '有10个。'})
        text = artifact(body='有10个。[^two]\n\n无数字。[^one]') + f'\n[^two]: {SHA} docx:body/88/paragraph\n'
    with pytest.raises(LeafRejected): publish(kernel, text, **kwargs)
    if case in ('bad-leaf-assignment', 'wrong-leaf-trunk'):
        assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
        assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 1
        assert list(kernel.memory.root.iterdir()) == []
    else:
        clean(kernel)


def test_narrative_without_data_and_fact_union(kernel):
    assert publish(kernel, artifact(code='L-bizov-07', data=False), leaf_code='L-bizov-07')['version'] == 1


@pytest.mark.parametrize('resolver,allowed', [(None, False), ('miss', False), ('hit', True)])
def test_quote_and_null_resolver_paths(kernel, resolver, allowed):
    kernel.quote_resolver = NullQuoteResolver() if resolver is None else InMemoryQuoteResolver({
        (SHA, 'docx:body/77/paragraph'): '有10个。' if resolver == 'hit' else '有11个。'})
    text = artifact(code='L-bizov-07', data=False, body='有10个。[^one]')
    if allowed:
        assert publish(kernel, text, leaf_code='L-bizov-07')['changed']
    else:
        with pytest.raises(LeafRejected): publish(kernel, text, leaf_code='L-bizov-07')
        clean(kernel)


def test_history_rewrite_rejected_preserves_previous(kernel):
    first = publish(kernel)
    before = kernel.memory.read(first['uri'])
    with pytest.raises(LeafRejected):
        publish(kernel, artifact(version=2, history='| v1 | 日期 | 技能 | 被改写 | 更新 |\n| v2 | 日期 | 技能 | 原件 | 更新 |\n'))
    assert kernel.memory.read(first['uri']) == before
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_publish_failure_rolls_back_first_and_update(kernel):
    kernel.memory.fail = True
    with pytest.raises(OSError): publish(kernel)
    clean(kernel)
    first = publish(kernel)
    kernel.memory.fail = True
    with pytest.raises(OSError): publish(kernel, artifact(version=2))
    assert kernel.memory.read(first['uri']) == artifact()
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_off_gate_no_side_effects(kernel, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(LeafRejected, match='disabled'): publish(kernel)
    clean(kernel)


def test_claimed_fact_publishes_and_verified_path_retained(kernel):
    # Decision 13A: a claimed fact passes leaf publication under the same
    # company/trunk/source/coordinate guards; the verified path is unchanged.
    kernel.db.execute("UPDATE facts SET status='claimed' WHERE id=1"); kernel.db.commit()
    first = publish(kernel)
    assert first['changed']
    kernel.db.execute("UPDATE facts SET status='verified' WHERE id=1"); kernel.db.commit()
    assert publish(kernel, artifact(version=2))['version'] == 2
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 2


def test_noop_revalidates_fact_authority(kernel):
    first = publish(kernel)
    kernel.db.execute("UPDATE facts SET status='inferred'"); kernel.db.commit()
    with pytest.raises(LeafRejected): publish(kernel)
    assert kernel.memory.read(first['uri']) == artifact()
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_uri_helpers_and_stable_slug():
    assert slug('稳定 标题/与_内容') == '稳定-标题-与-内容'
    assert paths.conflict_uri(COMPANY, 'conflict-bizov-test-job') == f'sam://facts/{COMPANY}/待办与冲突/conflict-bizov-test-job.md'
    assert paths.tree_brief_uri(COMPANY).endswith('/tree-brief.md')
    assert paths.tree_guide_uri(COMPANY).endswith('/tree-guide.md')
    with pytest.raises(ValueError): paths.conflict_uri(COMPANY, '../bad')


def test_original_filename_footnote_and_gap_word_in_prose(kernel):
    text = artifact(body='未出现待补段落，有9个。[^one]').replace(f'[^one]: {SHA}', '[^one]: sample.docx')
    assert publish(kernel, text)['changed']


def test_concurrent_duplicate_is_idempotent(kernel):
    db_path = Path(kernel.db.execute('PRAGMA database_list').fetchone()[2])
    def run(_):
        service = LeafPublishService(db_path, kernel.memory, company_id=COMPANY)
        try: return publish(service)
        finally: service.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(r['changed'] for r in results) == [False, True]
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_default_process_off_and_disabled_constructor_does_not_create_db(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != 'SAM_FACTS_TREE'}
    result = subprocess.run([sys.executable, '-c', 'from app import settings; print(settings.SAM_FACTS_TREE)'],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout == 'False\n'
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    db_path = tmp_path / 'never-created.db'
    with pytest.raises(LeafRejected): LeafPublishService(db_path, DocumentPort(tmp_path))
    assert not db_path.exists()


def test_disabled_existing_provider_whitelist_and_projection_bytes(tmp_path, monkeypatch):
    from tests.memory_setup import migrated_memory
    from tests.test_facts_tree_binding import Embedding
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    memory = migrated_memory(tmp_path / 'old.db', embedding=Embedding(), company_id=COMPANY)
    try:
        documents = {paths.facts_uri(COMPANY, None, 'brief.md'): '旧简报',
                     paths.facts_uri(COMPANY, None, 'guide.md'): '旧导览'}
        memory.publish_documents(documents, metadata={'company_id': COMPANY})
        before = [tuple(r) for r in memory.db.execute('SELECT * FROM m1_documents ORDER BY uri')]
        chunks = [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks ORDER BY chunk_id')]
        for uri in (paths.leaf_uri(COMPANY, '02-业务概览', 'L-bizov-08-测试'),
                    paths.trunk_doc_uri(COMPANY, '02-业务概览', 'trunk-brief.md'),
                    paths.tree_brief_uri(COMPANY), paths.conflict_uri(COMPANY, 'conflict-bizov-test-job')):
            with pytest.raises((ValueError, PermissionError)):
                memory.publish_documents({uri: '不得写入'}, metadata={'company_id': COMPANY})
        assert [tuple(r) for r in memory.db.execute('SELECT * FROM m1_documents ORDER BY uri')] == before
        assert [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks ORDER BY chunk_id')] == chunks
        assert {uri: memory.read(uri) for uri in documents} == documents
    finally:
        memory.close()


def test_commit_failure_restores_document_and_revision(kernel):
    import sqlite3
    class CommitFailure:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, key): return getattr(self.connection, key)
        def commit(self): raise sqlite3.OperationalError('synthetic commit failure')
    kernel.db = CommitFailure(kernel.db)
    with pytest.raises(sqlite3.OperationalError, match='commit failure'): publish(kernel)
    clean(kernel)


def test_stable_uri_survives_template_title_change(kernel, monkeypatch):
    from dataclasses import replace
    from app.facts_tree import leaf_store
    first = publish(kernel)
    loader = leaf_store.load_pack
    def retitled(name):
        pack = loader(name)
        return replace(pack, leaves=tuple(replace(leaf, title='模板改名') if leaf.code == 'L-bizov-08' else leaf for leaf in pack.leaves))
    monkeypatch.setattr(leaf_store, 'load_pack', retitled)
    assert publish(kernel, artifact(version=2))['uri'] == first['uri']


@pytest.mark.parametrize('case', ['duplicate', 'extra-version', 'malformed-table', 'edited-previous'])
def test_history_integrity_rejected(kernel, case):
    if case == 'edited-previous':
        result = publish(kernel)
        kernel.memory.put(result['uri'], artifact().replace('| sample |', '| 改写 |'), metadata={})
        text = artifact(version=2)
    elif case == 'duplicate':
        text = artifact(history='| v1 | 日期 | fact-leaf | sample | 更新 |\n' * 2)
    elif case == 'extra-version':
        text = artifact(history='| v1 | 日期 | fact-leaf | sample | 更新 |\n| v2 | 日期 | fact-leaf | sample | 更新 |\n')
    else: text = artifact().replace('|---|---|---|---|---|', 'not a table')
    with pytest.raises(LeafRejected): publish(kernel, text)
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == (1 if case == 'edited-previous' else 0)


def test_period_tokens_after_prose_space_are_guarded(kernel):
    text = artifact(body='截至 2025-01，有9个。[^one]')
    assert publish(kernel, text)['changed']
    with pytest.raises(LeafRejected):
        publish(kernel, artifact(version=2, body='截至 2024-01，有9个。[^one]'))


def test_history_whitespace_normalization_preserves_rows(kernel):
    publish(kernel)
    text = artifact(version=2).replace('| v1 | 日期 | fact-leaf | sample | 更新 |', '|  v1  | 日期 | fact-leaf  | sample | 更新 |')
    assert publish(kernel, text)['version'] == 2


def test_fenced_heading_is_not_a_required_section(kernel):
    text = artifact().replace('## 正文', '```markdown\n## 正文\n```')
    with pytest.raises(LeafRejected): publish(kernel, text)
    clean(kernel)


def test_crlf_markdown_keeps_published_bytes(kernel):
    text = artifact().replace('\n', '\r\n')
    result = publish(kernel, text)
    # The parsing view is normalized; publication is never rewritten.
    assert kernel.memory.path(result['uri']).read_bytes() == text.encode()


def test_mapping_upsert_noop_and_one_fact_can_link_multiple_leaves(kernel):
    first = publish(kernel, job_id='job-first')
    initial = dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone())
    assert {k: initial[k] for k in ('company_id', 'fact_id', 'trunk', 'leaf_code', 'source_file', 'job_id')} == {
        'company_id': COMPANY, 'fact_id': 1, 'trunk': 'bizov', 'leaf_code': 'L-bizov-08', 'source_file': SHA, 'job_id': 'job-first'}
    assert not publish(kernel, job_id='job-ignored')['changed']
    assert dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone()) == initial
    publish(kernel, artifact(version=2), job_id='job-second')
    updated = dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone())
    assert updated == {**initial, 'job_id': 'job-second'}
    publish(kernel, artifact(code='L-bizov-07', data=False), leaf_code='L-bizov-07')
    rows = kernel.db.execute('SELECT * FROM fact_leaf_links ORDER BY id').fetchall()
    assert len(rows) == 2 and {row['leaf_code'] for row in rows} == {'L-bizov-07', 'L-bizov-08'}
    with pytest.raises(sqlite3.IntegrityError):
        kernel.db.execute('INSERT INTO fact_leaf_links SELECT NULL,company_id,fact_id,trunk,leaf_code,source_file,job_id,created_at FROM fact_leaf_links WHERE id=?', (initial['id'],))
    kernel.db.rollback()
    assert kernel.memory.read(first['uri']) == artifact(version=2)


@pytest.mark.parametrize('field,value', [('trunk', 'hr'), ('source_file', OTHER)])
def test_corrupt_mapping_rejected_without_new_projection(kernel, field, value):
    kernel.db.execute('INSERT INTO fact_leaf_links(company_id,fact_id,trunk,leaf_code,source_file,created_at) VALUES(?,?,?,?,?,?)',
                      (COMPANY, 1, 'bizov', 'L-bizov-08', SHA, 'now'))
    kernel.db.execute(f'UPDATE fact_leaf_links SET {field}=?', (value,))
    kernel.db.commit()
    before = [tuple(r) for r in kernel.db.execute('SELECT * FROM fact_leaf_links')]
    with pytest.raises(LeafRejected, match='assignment'): publish(kernel)
    assert [tuple(r) for r in kernel.db.execute('SELECT * FROM fact_leaf_links')] == before
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert list(kernel.memory.root.iterdir()) == []


def test_mapping_insert_failure_rolls_back_revision_and_index(kernel):
    kernel.db.executescript("""CREATE TRIGGER reject_link BEFORE INSERT ON fact_leaf_links
        BEGIN SELECT RAISE(ABORT, 'synthetic mapping failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match='mapping failure'): publish(kernel)
    clean(kernel)


def test_failed_update_restores_existing_mapping_job(kernel):
    first = publish(kernel, job_id='job-original')
    before = dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone())
    kernel.memory.fail = True
    with pytest.raises(OSError): publish(kernel, artifact(version=2), job_id='job-failed')
    assert dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone()) == before
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
    assert kernel.memory.read(first['uri']) == artifact()


def test_disabled_migration_preserves_atomic_card_and_module_fact_sources_bytes(tmp_path, monkeypatch):
    import inspect
    import json
    from app.db.database import connect
    from app.memory.provider import LocalVectorMemoryProvider
    from app.memory.company_facts.l2_consolidation import ConsolidationService
    from app.memory.company_facts.module_projection import ModuleProjectionService
    from app.memory.company_facts.paths import module_document_uri
    from app.ports import LocalMemoryProvider
    from tests.test_module_incremental_projection import Employee

    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    database = tmp_path / 'legacy.db'
    init_db(database)
    db = connect(database)
    # Model the pre-B1 store: no B1 tables, unchanged old fact rows/column order.
    db.executescript('DROP TABLE fact_leaf_links; DROP TABLE leaf_revisions; DROP TABLE facts_tree_leaf_index;')
    db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
               (SHA, 'sample.docx', 1, '/unused', 'now'))
    db.execute('INSERT INTO facts(company_id,entity,attribute,value,unit,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (COMPANY, '临时主体', '测试指标', '9', '个', SHA, 'bizov', 'now'))
    db.commit()
    columns_before = [tuple(row) for row in db.execute('PRAGMA table_info(facts)')]
    rows_before = [tuple(row) for row in db.execute('SELECT * FROM facts')]
    facts = ConsolidationService(database)
    memory = LocalMemoryProvider(tmp_path / 'old-memory')
    projection = ModuleProjectionService(database, memory, worker=Employee(), token_budget=10000)
    assert not projection.update(company_id=COMPANY, verified_fact_ids=[1], full=True)['unresolved']
    module_uri = module_document_uri(COMPANY, 'bizov', 'brief.json')
    module_before = memory.read(module_uri).encode()
    fact_sources_before = json.dumps(json.loads(module_before)['fact_sources'], ensure_ascii=False, sort_keys=True).encode()
    atom_before = LocalVectorMemoryProvider._fact_document(facts.list_verified_facts(company_id=COMPANY)[0])['content'].encode()

    initialize_memory(db)
    db.commit()
    assert 'ALTER TABLE facts' not in inspect.getsource(initialize_memory)
    assert [tuple(row) for row in db.execute('PRAGMA table_info(facts)')] == columns_before
    assert 'leaf_code' not in {row[1] for row in columns_before}
    assert [tuple(row) for row in db.execute('SELECT * FROM facts')] == rows_before
    assert db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 0
    assert not projection.update(company_id=COMPANY, verified_fact_ids=[1], full=True)['unresolved']
    module_after = memory.read(module_uri).encode()
    atom_after = LocalVectorMemoryProvider._fact_document(facts.list_verified_facts(company_id=COMPANY)[0])['content'].encode()
    assert atom_before == atom_after
    assert module_before == module_after
    assert fact_sources_before == json.dumps(json.loads(module_after)['fact_sources'], ensure_ascii=False, sort_keys=True).encode()
    db.close()
