"""F-001 员工回执 × 后端守卫契约矩阵（只固定契约，不改生产行为）。

历史坐标与缺口来自 docs/handoffs/facts-tree-segment-u-wsl-review.md：
原件窗口17的授权行并集是 [46,47]+[48,52]（表头两段+数据段），员工选 46..52
曾被旧接口按“必须落在单一 ranges 段”误拒；空归属 ranges=[] 曾被强制非空；
叶 no_content 曾被误当普通写稿。本矩阵把三类边界逐行钉死。

矩阵（员工回执 -> 后端守卫 -> 允许/拒绝的副作用）：

协议                        员工回执                后端守卫                                      允许的副作用                                         拒绝形态
--------------------------  ---------------------  -------------------------------------------  ---------------------------------------------------  ------------------------------------------------------------------
门开 mapped-MD（V3/V4 同构） attribute ranges=[]    service._prepare_md_window 空归属校验         接受 no_attribution；extract 与独立归叶照常进行；    缺 outcome / 空 reason / 错 outcome / ranges 非 list /
                            +outcome=no_attribution                                               空归属本身不授权任何事实                              reason>2000 -> 一次计费纠正，原件入 attribution_rejections
门关 旧整篇兼容（冻结契约）  attribute ranges=[]    service 旧整篇循环 'missing attribution       无：即使带齐 no_attribution 字段也按格式错误拒收     一次计费纠正后仍空 -> EmployeeTransportError，partial，
                            （无论带什么 outcome）   ranges'，整份 document 入请求，无窗口契约                                                           不进入 extract，不产生 mapped_md 检查点
门开 mapped-MD              plan 分配 [46,52]       grants.range_is_granted 逐行授权并集          接受跨 [46,47]+[48,52] 两段的整段选择；精确定 7 行    覆盖未授权行（45 / 53 / 缺口 50）-> LeafRejected
                            跨两个授权段            （真缺口/类型错/大跨度同样拒）                授权，绝不扩大到全文 1..45                            'allocation outside read window'，零 leaf 调用、零写入
门开 mapped-MD              leaf evidence 46..52   md_publish range_is_granted 证据行守卫        授权并集内行 -> 写修订与操作回执                      授权行有缺口 -> LeafRejected 'evidence line range outside
                                                                                                                                                     read grant'，相关表全部零写入
门开 mapped-MD              leaf outcome=no_content md_reader 空叶回执独立分流（非普通写稿）      changed=False 收据；叶版本/修订/操作不变；可幂等重放  空 reason / 夹带 blocks/conflicts/assignments / 叶码不符 /
                                                                                                                                                     reason>2000 -> rejected_drafts，correction 耗尽为 unresolved
门开 mapped-MD              plan outcome=no_content allocation_contract 窗口级空回执            记录窗口 no_content，零 leaf 调用                     缺 reason / retention 字段由 plan 格式拒收路径处理
门开 mapped-MD              空归属 + extract 事实   service._verify 实体范围/quote 守卫            无事实落账，原件进 feedback（invalid_candidate）      'fact lies outside employee attributed range'

注意：门开 mapped-MD 的“空 ranges 合法”只存在于窗口协议；门关旧整篇路径的
非空 ranges 契约是已批准保留的行为，两者不得合并成同一条空 ranges 规则。
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app import settings
from app.document_read.contracts import response_contract, response_contract_v4
from app.document_read.dossier import Dossier
from app.document_read.service import DirectReadService
from app.facts_tree.grants import range_is_granted
from app.facts_tree.leaf_store import LeafRejected
from app.facts_tree.md_publish import MDLeafPublisher
from app.facts_tree.md_reader import MDWindows
from app.facts_tree.quote_sources import DossierQuoteResolver
from tests.test_facts_tree_transactions import real_store  # noqa: F401  (复用真实临时库夹具)
from tests.test_facts_tree_leaves import COMPANY, SHA
from tests.test_document_read import ROOT

PHASES = ('classify', 'attribute', 'extract', 'plan', 'leaf')
LOC = 'docx:body/17/table'
EMPTY_ATTRIBUTION = dict(ranges=[], outcome='no_attribution',
                         reason='本窗只有填表提示，没有可明确归属的实体')


def table_dossier():
    """46..47 为表头段、48..52 为数据段的单映射窗口；1..45 行有真实内容但不映射。"""
    filler = [f'前文铺垫第{i}行，不属于映射表格窗口。' for i in range(1, 46)]
    table = ['| 项目 | 内容 |', '| --- | --- |'] + ['| 事项 | 无具体数值内容 |'] * 5
    d = Dossier(dict(markdown='\n'.join(filler + table),
        document_map=dict(elements=[dict(source_loc=LOC, start_line=46, end_line=52, kind='table')]),
        assets=[]), file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def make_legacy_dossier():
    d = Dossier(dict(markdown='文档标题\n待填写提示\n可回查的技术描述。',
        document_map=dict(elements=[dict(source_loc=f'docx:body/{i}/paragraph', start_line=i, end_line=i)
                                    for i in range(1, 4)]), assets=[]),
        file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def insert_job(store, job):
    store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (job, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()


def assigned(ranges):
    return dict(outcome='assigned',
                assignments=[dict(leaf_code='L-bizov-07', ranges=ranges, context_windows=[])],
                retain_context_windows=[], retention_reason='员工判断后续窗口仍需本表限定')


def plan_no_content():
    return dict(outcome='no_content', assignments=[], reason='员工判断本窗没有可归叶内容',
                retain_context_windows=[], retention_reason='员工判断无需为后续窗口保留原文限定')


def strings_recursive(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_recursive(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_recursive(item)


class Employee:
    """门开 mapped-MD 五相假员工；默认产出 46..52 跨段分配与普通写稿。"""

    def __init__(self, *, attribute=None, plan=None, leaf='block', leaf_bad=None,
                 extract=None, bad_attribute=None):
        self.attribute = attribute
        self.plan = plan
        self.leaf = leaf
        self.leaf_bad = leaf_bad
        self.extract = extract
        self.bad_attribute = bad_attribute
        self.requests = []

    def execute(self, **kwargs):
        q = json.loads(kwargs['text'])
        action = q.pop('action')
        self.requests.append((action, deepcopy(q)))
        if action == 'classify':
            return dict(classification=dict(description='表格窗口，含项目表头与五行事项'))
        if action == 'attribute':
            if self.bad_attribute is not None:
                bad, self.bad_attribute = self.bad_attribute, None
                return bad
            if self.attribute is not None:
                return self.attribute
            return dict(ranges=[dict(entity='资料主体', start_line=46, end_line=52)])
        if action == 'extract':
            return self.extract if self.extract is not None else dict(facts=[], complete=True)
        if action == 'plan':
            return self.plan if self.plan is not None else assigned([[46, 52]])
        assert action == 'leaf'
        if self.leaf == 'no_content':
            result = dict(leaf_code='L-bizov-07', outcome='no_content',
                          reason='员工判断本段没有可归该叶的内容')
            if self.leaf_bad is not None:
                result.update(self.leaf_bad)
            return result
        current, g = q['current'], q['grants'][0]
        return dict(leaf_code='L-bizov-07', base_version=current['version'], base_sha=current['content_sha'],
            blocks=current['blocks'] + [dict(block_id='narrative', text='本窗表格事项已核对，没有可写入叶的数字[^e]。',
                evidence=[dict(ref='e', grant_id=g['grant_id'], file_hash=SHA, original_name='sample.docx',
                               source_loc=LOC, start_line=46, end_line=52, fact_ids=[])])],
            conflicts=[], note='只归叶已读表格内容')


def run_reader(store, d, employee, *, job='contract-matrix', limits=None, **kwargs):
    reader = DirectReadService(store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d, memory=store.memory, model=employee)
    return reader, reader.run(company_id=COMPANY, job_id=job,
                              limits=limits or dict.fromkeys(PHASES, 4), **kwargs)


def only_record(state):
    records = state['mapped_md']['records']
    assert len(records) == 1
    return records[0]


# ── 协议分层：门开 mapped-MD 有空归属/空叶契约，V4 无 leaf 动作 ────────────────

def test_receipt_contracts_exist_only_on_gate_open_mapped_protocol():
    attr = response_contract('attribute')
    assert attr['empty_example']['ranges'] == []
    assert attr['empty_example']['outcome'] == 'no_attribution'
    assert attr['empty_example']['reason'].strip()
    assert response_contract('leaf')['no_content']['outcome'] == 'no_content'
    v4_attr = response_contract_v4('attribute')
    assert v4_attr['empty_example']['outcome'] == 'no_attribution'
    # V4 阅读协议没有 leaf 动作（归叶在独立 publish 阶段）；V1 更没有。
    with pytest.raises(ValueError):
        response_contract_v4('leaf')


def test_historical_window_grant_is_exactly_two_segment_union_46_47_48_52():
    d = table_dossier()
    windows = MDWindows(d)
    assert len(windows.windows) == 1
    window = windows.windows[0]
    assert window['ranges'] == [[46, 47], [48, 52]]
    grant = windows.grant(window, 'sample.docx')
    assert set(grant['lines']) == {str(i) for i in range(46, 53)}
    assert grant['complete_coordinate']
    # 1..45 行确有全文内容，但既不在窗口授权行内，也不在映射窗口内。
    assert windows.unmapped == list(range(1, 46))


# ── ① 空归属回执：门开合法/非法形态矩阵 ──────────────────────────────────────

def test_empty_attribution_accepted_and_still_reaches_extract_and_plan(real_store):
    d = table_dossier()
    insert_job(real_store, 'matrix-empty-legal')
    employee = Employee(attribute=EMPTY_ATTRIBUTION, plan=plan_no_content())
    state = run_reader(real_store, d, employee, job='matrix-empty-legal')[1]
    assert state['done']
    prep = only_record(state)['preparation']
    assert prep['ranges'] == []
    assert prep['attribution_decision'] == dict(outcome='no_attribution',
                                                reason=EMPTY_ATTRIBUTION['reason'])
    # 空归属不跳过 extract：窗口继续走到窗口级 no_content 归叶回执。
    assert [a for a, _ in employee.requests] == ['classify', 'attribute', 'extract', 'plan']
    assert only_record(state)['status'] == 'no_content'
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1  # 仅夹具基线
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


@pytest.mark.parametrize('bad,marker', [
    (dict(ranges=[]), 'empty ranges require outcome=no_attribution'),
    (dict(ranges=[], outcome='no_attribution', reason='   '), 'nonempty reason'),
    (dict(ranges=[], outcome='attributed', reason='员工误填'), 'no_attribution'),
    (dict(ranges={}), 'attribution ranges must be a list'),
    (dict(ranges=[], outcome='no_attribution', reason='x' * 2001), 'max 2000 characters'),
])
def test_illegal_empty_attribution_gets_one_charged_correction_then_accepted(real_store, bad, marker):
    d = table_dossier()
    insert_job(real_store, 'matrix-empty-shape')
    employee = Employee(attribute=EMPTY_ATTRIBUTION, plan=plan_no_content(), bad_attribute=bad)
    state = run_reader(real_store, d, employee, job='matrix-empty-shape')[1]
    assert state['done']
    prep = only_record(state)['preparation']
    assert len(prep['attribution_rejections']) == 1
    assert marker in prep['attribution_rejections'][0]['reason']
    assert prep['ranges'] == [] and prep['attribution_decision']['outcome'] == 'no_attribution'
    attempts = [q for a, q in employee.requests if a == 'attribute' and q['window']['grant_id'] == '0']
    assert len(attempts) == 2 and attempts[1]['format_feedback']['error']
    assert state['calls']['attribute'] == 2
    # 拒收期间没有任何事实或叶写入；纠正后窗口以窗口级 no_content 收尾。
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 0


def test_empty_attribution_cannot_authorize_any_fact(real_store):
    d = table_dossier()
    insert_job(real_store, 'matrix-empty-forged')
    forged = dict(facts=[dict(entity='资料主体', attribute='事项', period='unknown',
        value='无具体数值内容', unit='', value_type='string',
        quote='| 事项 | 无具体数值内容 |', source_loc=LOC)], complete=True)
    employee = Employee(attribute=EMPTY_ATTRIBUTION, extract=forged)
    state = run_reader(real_store, d, employee, job='matrix-empty-forged',
                       limits={**dict.fromkeys(PHASES, 4), 'extract': 1})[1]
    assert state['status'] == 'partial' and not state.get('done')
    prep = only_record(state)['preparation']
    assert prep['ranges'] == [] and not prep['complete'] and prep['feedback']
    assert 'attributed range' in prep['feedback'][0]['blocking']
    assert not state['fact_ids'] and not state['accepted']
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0


# ── ② 跨段授权：逐行并集 [46,47]+[48,52] 接受 46..52，缺行拒绝 ────────────────

def test_range_guard_union_at_original_window17_coordinates():
    lines = {str(i): f'第{i}行' for i in (*range(46, 48), *range(48, 53))}
    assert range_is_granted(lines, 46, 52)          # 跨两段连续并集
    assert range_is_granted(lines, 46, 47)
    assert range_is_granted(lines, 48, 52)
    missing_one = {k: v for k, v in lines.items() if k != '50'}
    assert not range_is_granted(missing_one, 46, 52)  # 并集有缺口不得放过
    assert not range_is_granted(lines, 45, 52)        # 未授权行在前
    assert not range_is_granted(lines, 46, 53)        # 未授权行在后
    for start, end in ((46, '52'), ('46', 52), (True, 52), (52, 46), (46, 10 ** 15)):
        assert not range_is_granted(lines, start, end)  # 类型错/倒序/大跨度，且不物化巨区间


def test_cross_segment_plan_allocation_accepted_with_exact_seven_lines_no_widening(real_store):
    d = table_dossier()
    insert_job(real_store, 'matrix-union-ok')
    employee = Employee()
    state = run_reader(real_store, d, employee, job='matrix-union-ok')[1]
    assert state['done'] and len(state['leaves_built']) == 1
    assignment = only_record(state)['assignments'][0]
    assert assignment['status'] == 'submitted'
    assert assignment['grant']['ranges'] == [[46, 52]]
    assert set(assignment['grant']['lines']) == {str(i) for i in range(46, 53)}
    # 授权不扩大到全文：1..45 的真实内容从未出现在任何员工请求里。
    assert not any('不属于映射表格窗口' in text for _, q in employee.requests
                   for text in strings_recursive(q))
    assert not any(d.document['markdown'] in text for _, q in employee.requests
                   for text in strings_recursive(q))
    leaf_requests = [q for a, q in employee.requests if a == 'leaf']
    assert len(leaf_requests) == 1 and leaf_requests[0]['grants'][0]['ranges'] == [[46, 52]]
    pub = MDLeafPublisher(real_store)
    current = pub.current(COMPANY, 'L-bizov-07')
    assert current['version'] == 1
    evidence = current['blocks'][0]['evidence'][0]
    assert (evidence['grant_id'], evidence['start_line'], evidence['end_line']) == ('0', 46, 52)
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM m1_documents WHERE fact_json IS NOT NULL').fetchone()[0] == 0


@pytest.mark.parametrize('selection', [[[46, 53]], [[45, 52]]])
def test_plan_allocation_over_unauthorized_line_rejected_before_leaf(real_store, selection):
    d = table_dossier()
    insert_job(real_store, 'matrix-union-bad')
    employee = Employee(plan=assigned(selection))
    with pytest.raises(LeafRejected, match='allocation outside read window'):
        run_reader(real_store, d, employee, job='matrix-union-bad')
    assert all(a != 'leaf' for a, _ in employee.requests)
    for table in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links',
                  'm1_documents', 'm1_chunks', 'md_leaf_operations', 'md_leaf_todos'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_leaf_evidence_spanning_union_gap_rejected_zero_side_effects(real_store):
    d = table_dossier()
    real_store.quote_resolver = DossierQuoteResolver({SHA: d.document})
    window = MDWindows(d).windows[0]
    grant = MDWindows(d).grant(window, 'sample.docx')
    grant['lines'].pop('50')  # 授权并集缺行：[46,47]+[48,49]+[51,52]
    grant['ranges'] = [[46, 47], [48, 49], [51, 52]]
    grant['complete_coordinate'] = False
    pub = MDLeafPublisher(real_store)
    current = pub.current(COMPANY, 'L-bizov-07')
    block = dict(block_id='narrative', text='本窗表格事项已核对[^e]。',
        evidence=[dict(ref='e', grant_id='0', file_hash=SHA, original_name='sample.docx',
                       source_loc=LOC, start_line=46, end_line=52, fact_ids=[])])
    with pytest.raises(LeafRejected, match='evidence line range outside read grant'):
        pub.submit(company=COMPANY, code='L-bizov-07', operation_id='union-gap',
                   base_version=current['version'], base_sha=current['content_sha'],
                   blocks=current['blocks'] + [block], conflicts=[], grants={'0': grant},
                   job_id='matrix-union-gap', note='员工提交')
    assert pub.current(COMPANY, 'L-bizov-07')['version'] == 0
    for table in ('leaf_revisions', 'facts_tree_leaf_index', 'fact_leaf_links',
                  'm1_documents', 'm1_chunks', 'md_leaf_operations', 'md_leaf_todos'):
        assert real_store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


# ── ③ 叶 no_content：独立无写入回执、版本不变、可重放；非法形态走拒收 ─────────

def test_leaf_no_content_receipt_writes_nothing_and_replay_makes_no_new_call(real_store):
    d = table_dossier()
    insert_job(real_store, 'matrix-leaf-empty')
    employee = Employee(leaf='no_content')
    reader, state = run_reader(real_store, d, employee, job='matrix-leaf-empty')
    assignment = only_record(state)['assignments'][0]
    assert assignment['status'] == 'no_content' and assignment['draft']['outcome'] == 'no_content'
    receipt = assignment['receipt']
    assert receipt['outcome'] == 'no_content' and receipt['changed'] is False and receipt['todos'] == 0
    assert receipt['grant_ids'] == ['0'] and receipt['reason']
    assert state['leaves_empty'] == [receipt] and not state['leaves_built'] and not state['leaves_failed']
    pub = MDLeafPublisher(real_store)
    assert pub.current(COMPANY, 'L-bizov-07')['version'] == 0
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 0
    leaf_calls = sum(a == 'leaf' for a, _ in employee.requests)
    assert leaf_calls == 1
    # 同 job 幂等重放：done 检查点直接短路，不重复调用员工，也不补写任何叶记录。
    state_again = run_reader(real_store, d, employee, job='matrix-leaf-empty')[1]
    assert state_again['done']
    assert sum(a == 'leaf' for a, _ in employee.requests) == leaf_calls
    assert state_again['leaves_empty'] == [receipt]
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 0
    assert pub.current(COMPANY, 'L-bizov-07')['version'] == 0


def test_plan_level_no_content_is_a_distinct_window_receipt_without_leaf_call(real_store):
    d = table_dossier()
    insert_job(real_store, 'matrix-plan-empty')
    employee = Employee(plan=plan_no_content())
    state = run_reader(real_store, d, employee, job='matrix-plan-empty')[1]
    assert state['done']
    record = only_record(state)
    assert record['status'] == 'no_content' and not record['assignments']
    assert [a for a, _ in employee.requests] == ['classify', 'attribute', 'extract', 'plan']
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    before = len(employee.requests)
    run_reader(real_store, d, employee, job='matrix-plan-empty')
    assert len(employee.requests) == before  # 重放不重新读窗、不重新归叶


@pytest.mark.parametrize('bad', [
    dict(reason=' '),
    dict(reason=[]),
    dict(reason='x' * 2001),
    dict(leaf_code='L-bizov-08'),
    dict(blocks=[{}]),
    dict(assignments=None),
])
def test_invalid_leaf_no_content_rejected_unresolved_with_zero_writes(real_store, bad):
    d = table_dossier()
    insert_job(real_store, 'matrix-leaf-empty-bad')
    employee = Employee(leaf='no_content', leaf_bad=bad)
    state = run_reader(real_store, d, employee, job='matrix-leaf-empty-bad',
                       correction_attempts=0)[1]
    assignment = only_record(state)['assignments'][0]
    assert assignment['status'] == 'unresolved'
    assert len(assignment['rejected_drafts']) == 1 and not state['leaves_empty']
    assert state['calls']['leaf'] == 1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
    assert real_store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 0


# ── 门关旧整篇兼容路径：空 ranges 契约冻结，不被门开规则同化 ──────────────────

def test_gate_closed_legacy_path_still_rejects_empty_attribution(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    d = make_legacy_dossier()
    insert_job(real_store, 'matrix-legacy-empty')

    class LegacyEmployee:
        def __init__(self):
            self.requests = []

        def execute(self, **kwargs):
            q = json.loads(kwargs['text'])
            action = q.pop('action')
            self.requests.append((action, q))
            if action == 'classify':
                return dict(classification=dict(description='旧整篇协议判断'))
            # 带齐门开协议字段也无效：旧整篇循环根本不识别 no_attribution。
            return dict(ranges=[], outcome='no_attribution', reason='员工判断无可归属实体')

    employee = LegacyEmployee()
    reader = DirectReadService(real_store.memory.database,
        config=SimpleNamespace(skill_root=ROOT / 'skills'), dossier=d,
        memory=real_store.memory, model=employee)
    state = reader.run(company_id=COMPANY, job_id='matrix-legacy-empty',
                       limits=dict(classify=2, attribute=2, extract=2))
    assert state['status'] == 'partial' and not state.get('done')
    assert state['stage_failure']['action'] == 'attribute'
    assert 'attribution format failed after one correction' in state['stage_failure']['detail']
    attribute_calls = [q for a, q in employee.requests if a == 'attribute']
    assert len(attribute_calls) == 2
    # 旧协议整份 document 随请求下发，且没有窗口 attribute_contract。
    assert attribute_calls[0]['document'] == d.document['markdown']
    assert 'attribute_contract' not in attribute_calls[0]
    assert attribute_calls[1]['format_feedback']['error'] == 'missing attribution ranges'
    assert state['calls'] == dict(classify=1, attribute=2, extract=0)
    assert 'mapped_md' not in state and state['ranges'] is None
    assert real_store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
    assert real_store.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == 0
