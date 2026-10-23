"""B2a uses task grants, fake employee artifacts and temporary databases only."""
import importlib
import sqlite3

import pytest

from app import settings
from app.facts_tree.leaf_build import LeafBuildService
from app.facts_tree.leaf_contract import parse_employee_leaf
from app.facts_tree.leaf_store import LeafRejected, parse_markdown, history_rows
from app.facts_tree.quote_sources import DossierQuoteResolver
from tests.test_facts_tree_leaves import kernel, SHA, OTHER, COMPANY, clean, artifact

LOC = 'docx:body/77/paragraph'
TABLE = 'docx:body/16/table'


def documents():
    return {SHA: {'markdown': '纳米级涂膜技术，提升产品精度 ^[loc docx:body/77/paragraph]\n有9个，另有10个。\n| 测试 | 值 |\n| 参数 | 9 |',
                 'document_map': {'elements': [
                     dict(kind='paragraph', source_loc=LOC, start_line=1, end_line=2),
                     dict(kind='table', source_loc=TABLE, start_line=3, end_line=4)]}}}


def payload(code='L-bizov-08', body='有9个。[^one]'):
    data = ('\n## 数据明细\n| fact_id | 指标 | 期间 | 数值 | 单位 | 来源 |\n'
            '|---|---|---|---|---|---|\n| F1 | 测试指标 | 2025-01 | 9 | 个 | [^one] |') if code.endswith('08') else ''
    return dict(leaf_code=code, markdown='## 概览\n概述。[^one]\n## 正文\n' + body + data +
                '\n\n[^one]: sample.docx → ' + LOC, source_files=[SHA], fact_ids=[1], note='首建')


def builder(kernel):
    return LeafBuildService(kernel, quote_resolver=DossierQuoteResolver(documents()))


def build(kernel, data=None, **kwargs):
    data = data or payload()
    return builder(kernel).build_leaf(company_id=COMPANY, leaf_code=data['leaf_code'], employee_payload=data, **kwargs)


def test_resolver_exact_blocks_and_grant_snapshot():
    granted = documents()
    resolver = DossierQuoteResolver(granted)
    expected = granted[SHA]['markdown'].splitlines()
    granted.clear()
    assert resolver.resolve(file_hash=SHA, loc=LOC) == '\n'.join(expected[:2])
    assert '纳米级涂膜技术，提升产品精度' in resolver.resolve(file_hash=SHA, loc=LOC)
    assert resolver.resolve(file_hash=SHA, loc=TABLE) == '\n'.join(expected[2:])
    assert resolver.resolve(file_hash=OTHER, loc=LOC) is None
    assert resolver.resolve(file_hash=SHA, loc='missing') is None
    for capability in ('read', 'range', 'sam_file_get'):
        assert not hasattr(resolver, capability)


@pytest.mark.parametrize('start,end', [(0, 2), (1, 100), (3, 2), (True, 2)])
def test_resolver_out_of_bounds_returns_none(start, end):
    granted = documents()
    granted[SHA]['document_map']['elements'][0].update(start_line=start, end_line=end)
    assert DossierQuoteResolver(granted).resolve(file_hash=SHA, loc=LOC) is None


def test_contract_valid_copies_lists():
    raw = payload()
    parsed = parse_employee_leaf(raw)
    assert parsed == raw
    parsed['fact_ids'].append(2)
    assert raw['fact_ids'] == [1]


@pytest.mark.parametrize('field', ['leaf_code', 'markdown', 'source_files', 'fact_ids', 'note'])
def test_contract_missing(field):
    raw = payload(); del raw[field]
    with pytest.raises(LeafRejected): parse_employee_leaf(raw)


@pytest.mark.parametrize('field,value', [('leaf_code', 1), ('markdown', ''), ('markdown', '  '),
    ('markdown', None), ('source_files', 'sha'), ('source_files', [1]), ('fact_ids', '1'),
    ('fact_ids', ['1']), ('fact_ids', [True]), ('fact_ids', [1.0]), ('note', None)])
def test_contract_bad_shapes(field, value):
    raw = payload(); raw[field] = value
    with pytest.raises(LeafRejected): parse_employee_leaf(raw)


@pytest.mark.parametrize('raw', [[], None, '```json\n{}\n```'])
def test_contract_requires_object(raw):
    with pytest.raises(LeafRejected): parse_employee_leaf(raw)


def test_build_success_noop_update_and_history(kernel):
    result = build(kernel, job_id='job-first')
    assert result['version'] == 1 and result['changed']
    assert result['uri'].startswith(f'sam://facts/{COMPANY}/02-业务概览/leaves/')
    meta, _, _, sections = parse_markdown(kernel.memory.read(result['uri']))
    assert meta['title'] == kernel._leaf(COMPANY, 'L-bizov-08')[0].title
    assert meta['created_at'] == meta['updated_at']
    assert meta['latest_period'] == '2025-01' and meta['status'] == 'active'
    assert meta['sources'] == [SHA] and meta['source_file_count'] == 1
    assert len(history_rows(sections['编辑历史'])) == 1
    link = dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone())
    assert (link['fact_id'], link['trunk'], link['source_file'], link['job_id']) == (1, 'bizov', SHA, 'job-first')
    assert result['updated_at'] == kernel.db.execute('SELECT created_at FROM leaf_revisions').fetchone()[0]
    writes = kernel.memory.writes
    again = build(kernel, job_id='ignored')
    assert again == {**result, 'changed': False}
    assert kernel.memory.writes == writes
    data = payload(); data['note'] = '更新 | 摘要\n第二行'
    data['markdown'] = data['markdown'].replace('有9个。', '现有9个。')
    second = build(kernel, data, job_id='job-second')
    assert second['version'] == 2 and second['uri'] == result['uri']
    _, _, _, sections = parse_markdown(kernel.memory.read(second['uri']))
    assert len(history_rows(sections['编辑历史'])) == 2
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 1


@pytest.mark.parametrize('case', ['missing-loc', 'unauthorized', 'number', 'trunk', 'foreign-source',
                                 'dangling', 'target', 'frontmatter', 'history', 'missing-data'])
def test_build_rejections_leave_no_residue(kernel, case):
    data = payload()
    if case == 'missing-loc': data['markdown'] = data['markdown'].replace(LOC, 'docx:body/999/paragraph')
    elif case == 'unauthorized':
        service = LeafBuildService(kernel, quote_resolver=DossierQuoteResolver({}))
        with pytest.raises(LeafRejected): service.build_leaf(company_id=COMPANY, leaf_code=data['leaf_code'], employee_payload=data)
        clean(kernel); return
    elif case == 'number': data['markdown'] = data['markdown'].replace('有9个。', '有999个。')
    elif case == 'trunk': kernel.db.execute("UPDATE facts SET module_key='hr'"); kernel.db.commit()
    elif case == 'foreign-source': data['source_files'].append(OTHER)
    elif case == 'dangling': data['markdown'] = data['markdown'].replace('[^one]:', '[^other]:')
    elif case == 'target': data['leaf_code'] = 'unknown'
    elif case == 'frontmatter': data['markdown'] = '---\nx: y\n---\n' + data['markdown']
    elif case == 'history': data['markdown'] += '\n## 编辑历史\n伪造'
    elif case == 'missing-data': data['markdown'] = data['markdown'].replace('## 数据明细', '## 其他')
    with pytest.raises(LeafRejected): build(kernel, data)
    clean(kernel)


def test_narrative_description_coordinate_and_quote_union(kernel):
    data = payload('L-bizov-07', '纳米级涂膜技术，提升产品精度，另有10个。[^one]')
    assert build(kernel, data)['changed']
    data['markdown'] = data['markdown'].replace(LOC, 'docx:body/999/paragraph')
    with pytest.raises(LeafRejected, match='coordinate'): build(kernel, data)


def test_description_without_numbers_still_rejects_missing_coordinate(kernel):
    data = payload('L-bizov-07', '纳米级涂膜技术，提升产品精度。[^one]')
    data['fact_ids'] = []
    data['markdown'] = data['markdown'].replace(LOC, 'docx:body/999/paragraph')
    with pytest.raises(LeafRejected, match='coordinate'): build(kernel, data)
    clean(kernel)


def test_unspecified_period(kernel):
    data = payload('L-bizov-07', '叙述。[^one]'); data['fact_ids'] = []
    result = build(kernel, data)
    assert parse_markdown(kernel.memory.read(result['uri']))[0]['latest_period'] == 'unspecified'


def test_conflict_thin_wrapper_and_coordinate_gate(kernel):
    kwargs = dict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov',
                  target_leaf='L-bizov-08', metric='test', job_id='job-conflict',
                  markdown=artifact(code='conflict-bizov-test-0001', status='conflict', trunk='待办与冲突',
                                    target_trunk='bizov', target_leaf='L-bizov-08', metric='test').replace('测试内容', '测试内容。[^one]'),
                  source_files=[SHA], fact_ids=[1])
    result = builder(kernel).build_conflict(**kwargs)
    assert result['uri'] == f'sam://facts/{COMPANY}/待办与冲突/conflict-bizov-test-job-conflict.md'
    meta = parse_markdown(kernel.memory.read(result['uri']))[0]
    assert (meta['target_trunk'], meta['target_leaf'], meta['metric']) == ('bizov', 'L-bizov-08', 'test')
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 0
    kwargs['markdown'] = kwargs['markdown'].replace(LOC, 'docx:body/999/paragraph')
    with pytest.raises(LeafRejected): builder(kernel).build_conflict(**kwargs)


@pytest.mark.parametrize('stage', ['links', 'document', 'revoked'])
def test_publish_failure_rolls_back(kernel, monkeypatch, stage):
    if stage == 'links':
        kernel.db.executescript("CREATE TRIGGER reject_link BEFORE INSERT ON fact_leaf_links BEGIN SELECT RAISE(ABORT, 'mapping failure'); END;")
        error = sqlite3.IntegrityError
    elif stage == 'document': kernel.memory.fail = True; error = OSError
    else:
        original = kernel.publish_leaf
        def revoked(**kwargs):
            kernel.db.execute("UPDATE facts SET status='inferred'"); kernel.db.commit()
            return original(**kwargs)
        monkeypatch.setattr(kernel, 'publish_leaf', revoked)
        error = LeafRejected
    with pytest.raises(error): build(kernel)
    clean(kernel)


def test_disabled_import_and_build_zero_writes(kernel, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    before = '\n'.join(kernel.db.iterdump())
    writes = kernel.memory.writes
    for name in ('quote_sources', 'leaf_contract', 'leaf_build'):
        importlib.reload(importlib.import_module('app.facts_tree.' + name))
    with pytest.raises(RuntimeError, match='disabled'): build(kernel)
    with pytest.raises(RuntimeError, match='disabled'): builder(kernel).build_conflict()
    assert '\n'.join(kernel.db.iterdump()) == before
    assert kernel.memory.writes == writes
    clean(kernel)


def test_existing_foreign_company_source_rejected(kernel):
    kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                      (OTHER, 'foreign.docx', 1, '/unused', 'now'))
    kernel.db.execute('INSERT INTO facts(company_id,entity,attribute,value,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?)',
                      ('acc-other', '主体', '指标', '9', OTHER, 'bizov', 'now'))
    kernel.db.commit()
    data = payload(); data['source_files'].append(OTHER)
    with pytest.raises(LeafRejected, match='outside company'): build(kernel, data)
    clean(kernel)


def test_ambiguous_original_name_rejected(kernel):
    kernel.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                      (OTHER, 'sample.docx', 1, '/unused', 'now'))
    kernel.db.execute('INSERT INTO facts(company_id,entity,attribute,value,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?)',
                      (COMPANY, '主体', '指标', '9', OTHER, 'bizov', 'now'))
    kernel.db.commit()
    data = payload(); data['source_files'].append(OTHER)
    with pytest.raises(LeafRejected, match='ambiguous'): build(kernel, data)
    clean(kernel)


def test_latest_period_mechanical_max_and_first_created_at(kernel):
    kernel.db.execute('INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                      (COMPANY, '主体', '指标', '2026-01', '9', '个', SHA, 'bizov', 'now'))
    kernel.db.commit()
    data = payload('L-bizov-07'); data['fact_ids'] = [2, 1]
    result = build(kernel, data)
    first_created = kernel.db.execute('SELECT created_at FROM leaf_revisions ORDER BY version LIMIT 1').fetchone()[0]
    assert parse_markdown(kernel.memory.read(result['uri']))[0]['latest_period'] == '2026-01'
    data['note'] = '再次更新'
    second = build(kernel, data)
    assert parse_markdown(kernel.memory.read(second['uri']))[0]['created_at'] == first_created


def test_data_table_coordinate_is_checked_even_without_prose_use(kernel):
    data = payload()
    data['markdown'] = data['markdown'].replace('个 | [^one]', '个 | [^table]')
    data['markdown'] += '\n[^table]: sample.docx → ' + TABLE
    assert build(kernel, data)['changed']
    data['markdown'] = data['markdown'].replace(TABLE, 'docx:body/999/table')
    with pytest.raises(LeafRejected, match='coordinate'): build(kernel, data)


def test_noop_revalidates_authority(kernel):
    build(kernel)
    # Decision 13-A: claimed publishes; the revoked-authority gate is still
    # exercised with inferred, which stays unreadable/unpublishable.
    kernel.db.execute("UPDATE facts SET status='inferred'"); kernel.db.commit()
    with pytest.raises(LeafRejected): build(kernel)
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_failed_update_preserves_document_history_and_link(kernel):
    first = build(kernel, job_id='original')
    before = kernel.memory.read(first['uri'])
    link = dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone())
    data = payload(); data['note'] = '更新'
    kernel.memory.fail = True
    with pytest.raises(OSError): build(kernel, data, job_id='failed')
    assert kernel.memory.read(first['uri']) == before
    assert dict(kernel.db.execute('SELECT * FROM fact_leaf_links').fetchone()) == link
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_concurrent_host_assembly_is_idempotent(kernel):
    from concurrent.futures import ThreadPoolExecutor
    from app.facts_tree.leaf_store import LeafPublishService
    database = kernel.db.execute('PRAGMA database_list').fetchone()[2]
    def run(_):
        store = LeafPublishService(database, kernel.memory, company_id=COMPANY)
        try:
            return build(store)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(r['changed'] for r in results) == [False, True]
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 1
