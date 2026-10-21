"""Mechanical rollups are opt-in and tested with temporary documents only."""
import json

import pytest

from app import settings
from app.facts_tree.rollup import FactsTreeRollup, validate_rollup
from app.facts_tree.leaf_store import LeafRejected
from app.memory import paths
from app.module_tree import MODULES
from tests.test_facts_tree_leaves import kernel, artifact, publish, COMPANY, SHA


def test_trunk_tree_copy_and_noop(kernel):
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    assert '有9个。[^L-bizov-08-one]' in kernel.memory.read(uri)
    assert '一句话定位：测试内容' in kernel.memory.read(uri)
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    assert kernel.memory.read(paths.tree_brief_uri(COMPANY)) == kernel.memory.read(uri)
    writes = kernel.memory.writes
    assert not rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert not rollup.rollup_tree(company_id=COMPANY)['changed']
    assert kernel.memory.writes == writes


def test_empty_company_and_trunk_no_files(kernel):
    rollup = FactsTreeRollup(kernel)
    assert not rollup.rollup_trunk(company_id=COMPANY, trunk='hr')['changed']
    assert not rollup.rollup_tree(company_id=COMPANY)['changed']
    assert list(kernel.memory.root.iterdir()) == []


def test_archived_and_empty_leaves_excluded_and_stale_removed(kernel):
    publish(kernel)
    publish(kernel, artifact(code='L-bizov-07', data=False, status='archived', body='归档内容。[^one]'), leaf_code='L-bizov-07')
    publish(kernel, artifact(code='L-bizov-09', data=False, body='').replace('- 一句话定位：测试内容', '').replace(f'[^one]: {SHA} docx:body/77/paragraph', ''), leaf_code='L-bizov-09')
    rollup = FactsTreeRollup(kernel)
    rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    rollup.rollup_tree(company_id=COMPANY)
    assert '归档内容' not in kernel.memory.read(paths.tree_brief_uri(COMPANY))
    publish(kernel, artifact(version=2, status='archived'))
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    for uri in (paths.tree_brief_uri(COMPANY), paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')):
        with pytest.raises(FileNotFoundError): kernel.memory.read(uri)


def test_conflict_independent_iterable_and_excluded(kernel):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False, target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
    # Conflicts have no ordinary leaf section/history contract.
    text = text[:text.index('# 测试叶子')] + f'## 候选\n有9个。[^one]\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    kwargs = dict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job', source_files=[SHA], fact_ids=[1])
    result = kernel.publish_conflict(markdown=text, **kwargs)
    assert '/待办与冲突/conflict-bizov-test-job.md' in result['uri']
    assert not kernel.publish_conflict(markdown=text, **kwargs)['changed']
    assert kernel.publish_conflict(markdown=text.replace('version: 1', 'version: 2'), **kwargs)['version'] == 2
    assert kernel.db.execute('SELECT count(*) FROM fact_leaf_links').fetchone()[0] == 0
    assert not FactsTreeRollup(kernel).rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert not FactsTreeRollup(kernel).rollup_tree(company_id=COMPANY)['changed']


@pytest.mark.parametrize('case', ['fake-sha', 'number', 'status'])
def test_conflict_guards_no_half_product(kernel, case):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False, target_trunk='bizov', target_leaf='L-bizov-08', metric='test')
    text = text[:text.index('# 测试叶子')] + f'## 候选\n有9个。[^one]\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    if case == 'fake-sha': text = text.replace(f'[^one]: {SHA}', '[^one]: fake')
    elif case == 'number': text = text.replace('有9个', '有10个')
    else: text = text.replace('status: conflict', 'status: active')
    with pytest.raises(LeafRejected):
        kernel.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov', target_leaf='L-bizov-08', metric='test', job_id='job', markdown=text, source_files=[SHA], fact_ids=[1])
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert list(kernel.memory.root.iterdir()) == []


def test_rollup_numeric_rejection(kernel):
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    kernel.memory.put(uri, '有987654个。', metadata={})
    before = list(kernel.memory.root.iterdir())
    with pytest.raises(LeafRejected): rollup.rollup_tree(company_id=COMPANY)
    assert list(kernel.memory.root.iterdir()) == before
    with pytest.raises(LeafRejected): validate_rollup('有987654个。', [artifact()])


def test_rollup_off_and_unknown_trunk(kernel, monkeypatch):
    rollup = FactsTreeRollup(kernel)
    with pytest.raises(LeafRejected): rollup.rollup_trunk(company_id=COMPANY, trunk='nope')
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(LeafRejected): rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    with pytest.raises(LeafRejected): rollup.rollup_tree(company_id=COMPANY)
    assert list(kernel.memory.root.iterdir()) == []


def test_rollup_pair_failure_restores_both(kernel):
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    kernel.memory.fail = True
    with pytest.raises(OSError): rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    assert len(list(kernel.memory.root.iterdir())) == 1


def test_multiple_trunks_order_only_nonempty(kernel):
    kernel.db.execute("INSERT INTO facts(company_id,entity,attribute,value,unit,source_file,module_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (COMPANY, 'x', 'x', '9', '个', SHA, 'hr', 'now'))
    kernel.db.commit()
    publish(kernel)
    text = artifact(code='L-hr-04', trunk='hr', title='后续树枝').replace('| F1 |', '| F2 |').replace('| 2025-01 |', '| unspecified |')
    publish(kernel, text, leaf_code='L-hr-04', fact_ids=[2])
    rollup = FactsTreeRollup(kernel)
    for trunk in ('hr', 'bizov'): rollup.rollup_trunk(company_id=COMPANY, trunk=trunk)
    rollup.rollup_tree(company_id=COMPANY)
    brief = kernel.memory.read(paths.tree_brief_uri(COMPANY))
    assert brief.index('# 测试叶子') < brief.index('# 后续树枝')
    assert '无数据树枝' not in brief


@pytest.mark.parametrize('case', [
    'missing-target-trunk', 'missing-target-leaf', 'missing-metric', 'target-trunk',
    'target-leaf', 'metric', 'ordinary-code', 'code-prefix', 'seq', 'underscore',
    'archived', 'system-trunk', 'company', 'cross-trunk-fact', 'no-footnote',
])
def test_conflict_ruling_rejects_invalid_metadata(kernel, case):
    meta = dict(code='conflict-bizov-test-0001', trunk='待办与冲突', target_trunk='bizov',
                target_leaf='L-bizov-07', metric='test', status='conflict', data=False)
    kwargs = dict(company_id=COMPANY, leaf_code=meta['code'], target_trunk='bizov', target_leaf='L-bizov-07',
                  metric='test', job_id='job', source_files=[SHA], fact_ids=[1])
    if case == 'target-trunk': meta['target_trunk'] = 'hr'
    elif case == 'target-leaf': meta['target_leaf'] = 'L-bizov-08'
    elif case == 'metric': meta['metric'] = 'other'
    elif case == 'ordinary-code': meta['code'] = kwargs['leaf_code'] = 'L-bizov-07'
    elif case == 'code-prefix': meta['code'] = kwargs['leaf_code'] = 'conflict-hr-test-0001'
    elif case == 'seq': meta['code'] = kwargs['leaf_code'] = 'conflict-bizov-test-zero'
    elif case == 'underscore': meta['metric'] = kwargs['metric'] = 'test_bad'
    elif case == 'archived': meta['status'] = 'archived'
    elif case == 'system-trunk': meta['trunk'] = 'bizov'
    elif case == 'company': meta['company_id'] = 'acc-other'
    elif case == 'cross-trunk-fact':
        kernel.db.execute("UPDATE facts SET module_key='hr'"); kernel.db.commit()
    text = artifact(**meta)
    text = text[:text.index('# 测试叶子')] + f'## 候选\n有9个。[^one]\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    if case.startswith('missing-'):
        key = case.removeprefix('missing-').replace('-', '_')
        text = '\n'.join(line for line in text.split('\n') if not line.startswith(key + ':'))
    if case == 'no-footnote': text = text.replace('有9个。[^one]', '有9个。')
    with pytest.raises(LeafRejected): kernel.publish_conflict(markdown=text, **kwargs)
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert list(kernel.memory.root.iterdir()) == []


def test_tree_rejects_stale_trunk_prose_without_numbers(kernel):
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    kernel.memory.put(uri, '伪造的无数字结论', metadata={})
    with pytest.raises(LeafRejected, match='stale'): rollup.rollup_tree(company_id=COMPANY)
    with pytest.raises(FileNotFoundError): kernel.memory.read(paths.tree_brief_uri(COMPANY))


def test_multiple_leaf_footnotes_preserve_coordinates_and_are_namespaced(kernel):
    publish(kernel)
    second = artifact(code='L-bizov-07', data=False).replace('docx:body/77/paragraph', 'docx:body/88/paragraph')
    publish(kernel, second, leaf_code='L-bizov-07')
    rollup = FactsTreeRollup(kernel)
    rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    brief = kernel.memory.read(paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md'))
    assert f'[^L-bizov-07-one]: {SHA} docx:body/88/paragraph' in brief
    assert f'[^L-bizov-08-one]: {SHA} docx:body/77/paragraph' in brief


def test_data_only_active_leaf_is_not_empty(kernel):
    text = artifact(body='').replace('- 一句话定位：测试内容', '')
    publish(kernel, text)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']


def test_conflict_table_references_and_sources(kernel):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False,
                    target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
    text = text[:text.index('# 测试叶子')] + f"## 候选\n| fact_id | 数值 | 单位 | 来源 |\n|---|---|---|---|\n| F1 | 9 | 个 | [^one] |\n\n[^one]: {SHA} docx:body/77/paragraph\n"
    kwargs = dict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001', target_trunk='bizov', target_leaf='L-bizov-07',
                  metric='test', job_id='job', source_files=[SHA], fact_ids=[1])
    assert kernel.publish_conflict(markdown=text, **kwargs)['changed']
    for invalid in (text.replace('| F1 |', '| F2 |'), text.replace('| 9 |', '| 10 |'), text.replace('| [^one] |', '| 无 |')):
        with pytest.raises(LeafRejected): kernel.publish_conflict(markdown=invalid.replace('version: 1', 'version: 2'), **kwargs)
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1


def test_rollup_marks_claimed_and_verified_with_source_next_to_leaf(kernel):
    kernel.db.execute("UPDATE facts SET status='claimed', source_page=3, source_span='docx:body/77/paragraph' WHERE id=1")
    kernel.db.execute("INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (COMPANY, '临时主体', '另一指标', '2025-01', '9', '个', SHA, 'bizov', '2026-10-14'))
    kernel.db.commit()
    publish(kernel)  # L-bizov-08 cites fact 1 (claimed)
    publish(kernel, artifact(code='L-bizov-07', data=False), leaf_code='L-bizov-07', fact_ids=[2])  # cites fact 2 (verified)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    brief = kernel.memory.read(paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md'))
    guide = kernel.memory.read(paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-guide.md'))
    claimed = '[^L-bizov-08-f1]: F1 状态：claimed；来源：sample.docx；第3页；坐标：docx:body/77/paragraph'
    verified = '[^L-bizov-07-f2]: F2 状态：verified；来源：sample.docx'
    for text in (brief, guide):
        assert claimed in text and verified in text
    # Each marker sits directly under its own leaf entry: L-bizov-07's marker
    # precedes the next leaf heading; L-bizov-08's ends the document.
    assert verified + '\n\n# 测试叶子' in brief
    assert brief.endswith(claimed + '\n')
    # The tree brief inherits the markers; claimed is never shown as verified.
    assert kernel.memory.read(paths.tree_brief_uri(COMPANY)) == brief
    assert 'F1 状态：verified' not in brief
    assert '有9个。[^L-bizov-08-one]' in brief  # leaf prose untouched


def test_rollup_claimed_only_fact_is_not_upgraded(kernel):
    kernel.db.execute("UPDATE facts SET status='claimed' WHERE id=1")
    kernel.db.commit()
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    brief = kernel.memory.read(paths.tree_brief_uri(COMPANY))
    assert '[^L-bizov-08-f1]: F1 状态：claimed；来源：sample.docx' in brief
    assert 'verified' not in brief


def test_rollup_structured_span_shows_stable_locator_and_ranges(kernel):
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    span = json.dumps({'source_loc': 'docx:body/9/paragraph', 'quote': '有9个。', 'ranges': [[3, 5], [8, 9]]}, ensure_ascii=False)
    kernel.db.execute("UPDATE facts SET source_span=? WHERE id=1", (span,))
    kernel.db.commit()
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    brief = kernel.memory.read(uri)
    assert '坐标：docx:body/9/paragraph lines 3-5,8-9' in brief
    assert 'quote' not in brief  # witness payloads never leak into coordinates
    # A structured span without stable locator/ranges falls back to raw text.
    kernel.db.execute("UPDATE facts SET source_span=? WHERE id=1", ('{"quote":"注册资本100万"}',))
    kernel.db.commit()
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert '坐标：{"quote":"注册资本100万"}' in kernel.memory.read(uri)


def test_rollup_coordinates_do_not_weaken_numeric_guard(kernel):
    kernel.db.execute("UPDATE facts SET source_page=987654 WHERE id=1")  # absent from leaf text
    kernel.db.commit()
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    brief = kernel.memory.read(uri)
    assert '第987654页' in brief  # stored coordinate verbatim, never rewritten
    validate_rollup(brief, [artifact()])  # footnote lines stay outside guarded prose
    with pytest.raises(LeafRejected):
        validate_rollup(brief + '\n有987654个。', [artifact()])


@pytest.mark.parametrize('case', ['superseded', 'expired', 'inferred', 'missing-fact', 'missing-source'])
def test_rollup_rejects_unusable_fact_references(kernel, case):
    publish(kernel)
    rollup = FactsTreeRollup(kernel)
    rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    rollup.rollup_tree(company_id=COMPANY)
    uri = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
    before = kernel.memory.read(uri)
    if case == 'superseded':
        kernel.db.execute("UPDATE facts SET superseded_by=id WHERE id=1")
    elif case == 'expired':
        kernel.db.execute("UPDATE facts SET valid_to='2026-01-01' WHERE id=1")
    elif case == 'inferred':
        kernel.db.execute("UPDATE facts SET status='inferred' WHERE id=1")
    elif case == 'missing-fact':
        kernel.db.execute("DELETE FROM facts WHERE id=1")
    else:
        kernel.db.execute("UPDATE facts SET source_file=NULL WHERE id=1")
    kernel.db.commit()
    with pytest.raises(LeafRejected): rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')
    with pytest.raises(LeafRejected): rollup.rollup_tree(company_id=COMPANY)
    # Failed closed without a half product: the last good projection is untouched.
    assert kernel.memory.read(uri) == before


def test_conflict_address_collision_never_overwrites_previous(kernel):
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict', data=False,
                    target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
    text = text[:text.index('# 测试叶子')] + f'## 候选\n有9个。[^one]\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    kwargs = dict(company_id=COMPANY, target_trunk='bizov', target_leaf='L-bizov-07', metric='test',
                  job_id='job', source_files=[SHA], fact_ids=[1])
    result = kernel.publish_conflict(leaf_code='conflict-bizov-test-0001', markdown=text, **kwargs)
    with pytest.raises(LeafRejected, match='different leaf'):
        kernel.publish_conflict(leaf_code='conflict-bizov-test-0002',
                                markdown=text.replace('conflict-bizov-test-0001', 'conflict-bizov-test-0002'), **kwargs)
    assert kernel.memory.read(result['uri']) == text
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
    assert kernel.db.execute('SELECT count(*) FROM facts_tree_leaf_index').fetchone()[0] == 1
