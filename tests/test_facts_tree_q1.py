"""Q1 syntax/empty/source contracts, offline workers and isolated ledgers."""
import pytest

from app.facts_tree import leaf_plan
from app.facts_tree.leaf_contract import parse_employee_leaf
from app.facts_tree.leaf_store import LeafRejected
from tests.test_facts_tree_leaves import kernel, artifact, publish, clean, COMPANY, SHA
from tests.test_facts_tree_plan import Worker, service
from tests.test_document_read import task, service as reader_service
from tests.test_document_read_leaves import open_task, LeafModel, CODES, run, stored, revisions


@pytest.mark.parametrize('body', ['有9个[^one]。', '痛点 [^one]。', '有9个[^one]！',
                                  '有9个.[^one]', '有9个[^one] 。 ”）'])
@pytest.mark.parametrize('conflict', [False, True])
def test_terminal_punctuation_ordinary_and_conflict(kernel, body, conflict):
    if not conflict:
        assert publish(kernel, artifact(body=body))['changed']
        return
    text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict',
                    data=False, target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
    text = text[:text.index('# 测试叶子')] + f'## 候选\n{body}\n\n[^one]: {SHA} docx:body/77/paragraph\n'
    assert kernel.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001',
        target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job',
        markdown=text, source_files=[SHA], fact_ids=[1])['changed']


@pytest.mark.parametrize('body', ['有9个[^one]。这是无出处结论', '主要任职人员如下：',
                                  '本叶子暂无法整理出确定的组织架构与职责分工事实。'])
@pytest.mark.parametrize('conflict', [False, True])
def test_uncited_tail_and_introduction_still_rejected(kernel, body, conflict):
    cited = '[^one]' in body
    definition = f'[^one]: {SHA} docx:body/77/paragraph\n'
    code = 'L-bizov-08' if cited else 'L-bizov-07'
    text = artifact(code=code, body=body, data=cited)
    if not cited:
        text = text.replace(definition, '')
    with pytest.raises(LeafRejected, match='terminal footnote'):
        if not conflict:
            publish(kernel, text, leaf_code=code, source_files=[SHA])
        else:
            text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict',
                            data=False, target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
            text = text[:text.index('# 测试叶子')] + f'## 候选\n{body}\n\n'
            if cited:
                text += definition
            kernel.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001',
                target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job',
                markdown=text, source_files=[SHA], fact_ids=[1])
    clean(kernel)


@pytest.mark.parametrize('conflict', [False, True])
def test_unused_footnote_definition_still_rejected(kernel, conflict):
    body = '主要任职人员如下：'
    text = artifact(code='L-bizov-07', body=body, data=False)
    with pytest.raises(LeafRejected, match='dangling or unused footnote'):
        if not conflict:
            publish(kernel, text, leaf_code='L-bizov-07', source_files=[SHA])
        else:
            text = artifact(code='conflict-bizov-test-0001', trunk='待办与冲突', status='conflict',
                            data=False, target_trunk='bizov', target_leaf='L-bizov-07', metric='test')
            text = text[:text.index('# 测试叶子')] + f'## 候选\n{body}\n\n[^one]: {SHA} docx:body/77/paragraph\n'
            kernel.publish_conflict(company_id=COMPANY, leaf_code='conflict-bizov-test-0001',
                target_trunk='bizov', target_leaf='L-bizov-07', metric='test', job_id='job',
                markdown=text, source_files=[SHA], fact_ids=[1])
    clean(kernel)


@pytest.mark.parametrize('name', ['fmt_C_info.docx', 'mt_C_info.docx', 'fo.docx', 'ph; fmt_C_info.docx'])
def test_declared_original_exact_name_and_diagnostic(kernel, name):
    kernel.db.execute('UPDATE source_files SET original_name=? WHERE file_hash=?', ('fmt_C_info.docx', SHA))
    kernel.db.commit()
    text = artifact().replace(f'{SHA} docx:', f'{name} → docx:')
    if name == 'fmt_C_info.docx':
        assert publish(kernel, text)['changed']
    else:
        with pytest.raises(LeafRejected, match='declared originals.*fmt_C_info.docx'):
            publish(kernel, text)
        clean(kernel)


@pytest.mark.parametrize('change', [{'empty': 'true'}, {'leaf_code': 1}, {'note': None},
                                   {'markdown': '占位'}, {'source_files': [SHA]}, {'fact_ids': [1]}])
def test_empty_contract_rejects_invalid_or_nonempty_payload(change):
    raw = dict(leaf_code='L-bizov-07', empty=True, note='无证据')
    raw.update(change)
    with pytest.raises(LeafRejected): parse_employee_leaf(raw)


def test_empty_leaf_wrapper_no_writes_or_rollups(kernel, monkeypatch):
    worker = Worker(plan=[{'leaves': [{'trunk': 'bizov', 'leaf_code': 'L-bizov-07'}]}],
                    leaves={'L-bizov-07': dict(leaf_code='L-bizov-07', empty=True, note='无证据')})
    def forbidden(**kwargs): raise AssertionError('empty must not roll up')
    monkeypatch.setattr(leaf_plan, 'rollup_trunk', forbidden)
    monkeypatch.setattr(leaf_plan, 'rollup_tree', forbidden)
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert result['empty'] == [dict(leaf_code='L-bizov-07', empty=True, note='无证据')]
    assert not result['built'] and not result['failed_leaves'] and not result['tree_rolled']
    assert worker.calls[-1]['declared_sources'] == [dict(file_hash=SHA, original_name='sample.docx')]
    clean(kernel)


def test_empty_target_mismatch_is_rejection(kernel):
    worker = Worker(plan=[{'leaves': [{'trunk': 'bizov', 'leaf_code': 'L-bizov-07'}]}],
                    leaves={'L-bizov-07': dict(leaf_code='L-info-12', empty=True, note='无证据')})
    result = service(kernel, worker).plan_and_build(company_id=COMPANY)
    assert not result['built'] and len(result['failed_leaves']) == 1
    assert 'empty' not in result
    clean(kernel)


def test_empty_checkpoint_resume_skips_leaf_and_builds_content(open_task, monkeypatch):
    model = LeafModel(open_task)
    real = model.execute
    def execute(**kwargs):
        result = real(**kwargs)
        if model.actions[-1] == 'leaf' and model.targets[-1] == CODES[0]:
            return dict(leaf_code=CODES[0], empty=True, note='无证据')
        return result
    model.execute = execute
    reader = reader_service(open_task, model)
    result = run(reader, leaf=1)
    assert result['status'] == 'partial'
    state = stored(open_task)
    assert state['leaves_empty'] == [dict(leaf_code=CODES[0], empty=True, note='无证据')]
    assert not state['leaves_built'] and not state['leaves_failed']
    assert not revisions(open_task) and not open_task[2].docs
    from app.db.database import connect
    with connect(open_task[0]) as db:
        for table in ('m1_documents', 'facts_tree_leaf_index'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    result = run(reader_service(open_task, model), leaf=2)
    assert result['done'] and len(result['leaves_built']) == 1 and not result['leaves_failed']
    assert len(result['leaves_empty']) == 1 and model.targets == CODES
    assert model.actions.count('plan') == 1
    request = next(r for r in model.requests if r['action'] == 'leaf')
    assert request['declared_sources'] == [dict(file_hash=open_task[1].file_hash, original_name='acc.docx')]


def test_punctuation_does_not_relax_numeric_union(kernel):
    with pytest.raises(LeafRejected):
        publish(kernel, artifact(body='有999个[^one]。'))
    clean(kernel)


def test_punctuation_does_not_relax_coordinate_gate(kernel):
    from tests.test_facts_tree_build import builder, payload
    data = payload(body='有9个[^one]。')
    data['markdown'] = data['markdown'].replace('docx:body/77/paragraph', 'docx:body/999/paragraph')
    with pytest.raises(LeafRejected):
        builder(kernel).build_leaf(company_id=COMPANY, leaf_code=data['leaf_code'], employee_payload=data)
    clean(kernel)


@pytest.mark.parametrize('extra', [{}, dict(markdown='', source_files=[], fact_ids=[])])
def test_empty_contract_accepts_missing_or_empty_fields(extra):
    assert parse_employee_leaf(dict(leaf_code='L-info-12', empty=True, note='无证据', **extra)) == dict(
        leaf_code='L-info-12', empty=True, note='无证据')
