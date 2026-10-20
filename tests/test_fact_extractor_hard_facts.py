import pytest

from app.employees.worker import EmployeeRunner
from app.memory.company_facts.l2_fact_verifier import verify_candidates


def _employee_facts(text, items):
    runner = EmployeeRunner(runner=lambda **_: {"candidates": items})
    candidates = runner.run(skill="finance-fact-extraction", text=text, company_id="acme")
    return verify_candidates(candidates, [{
        "content": text, "file_hash": "f" * 64, "source_page": 1,
        "source_span": "fixture", "subject": "acme",
    }], company_id="acme")


# These are scripted employee conclusions, not an application alias table.
# The verifier preserves arbitrary employee labels and never extracts on its own.
CASES = (
    ("客户", "客户数量：12", "12", ("前五大客户", "甲科技有限公司"), "客户满意度：98%"),
    ("供应商", "供应商数量为30", "30", ("主要供应商", "乙材料有限公司"), "供应商质量评分：95"),
    ("在手订单", "在手订单金额：8,000", "8,000", ("现有订单", "25份"), "订单正在处理中：8"),
    ("员工人数", "员工人数：260", "260", ("人员总数", "300"), "员工满意度：90%"),
    ("重大合同", "重大合同金额：5000", "5000", ("重大合同", "丙公司采购框架协议"), "合同审批天数：3"),
    ("产能", "设计产能：10000", "10000", ("现有产能", "1.2万吨/年"), "产能规划正在讨论：2025"),
    ("研发投入", "研发投入金额：600", "600", ("研发支出", "720万元"), "研发投入计划尚未确定：2025"),
    ("毛利率", "销售毛利率：35.2%", "35.2%", ("综合毛利率", "36%"), "毛利金额：352"),
    ("发明专利", "发明专利数量：18", "18", ("发明专利数", "20项"), "专利申请日期：2025"),
    ("重大诉讼", "重大诉讼案件数：2", "2", ("重大仲裁", "某采购争议"), "诉讼风险提示：2"),
)


@pytest.mark.parametrize("metric,positive,expected,alias_row,negative", CASES)
def test_employee_chosen_metric_and_value_are_not_reclassified(
    metric, positive, expected, alias_row, negative
):
    positive_facts = _employee_facts(positive, [{"metric": metric, "value": expected, "quote": positive}])
    assert [(fact.metric, fact.value) for fact in positive_facts] == [
        (metric, expected)
    ]

    alias_facts = _employee_facts(str(alias_row), [{"metric": metric, "value": alias_row[1], "quote": str(alias_row)}])
    assert [(fact.metric, fact.value) for fact in alias_facts] == [(metric, alias_row[1])]

    assert _employee_facts(negative, []) == []


@pytest.mark.parametrize(
    "label,value,metric",
    (
        ("新增订单", "35份", "新签订单"),
        ("订单总额", "900万元", "订单金额"),
        ("研发人员数", "45", "研发人员"),
        ("研发人员比例", "20%", "研发人员占比"),
        ("中标金额", "1200万元", "中标"),
        ("合同总额", "1500万元", "合同"),
        ("实际产量", "8000吨", "产量"),
        ("销售量", "7600吨", "销量"),
        ("研发经费", "300万元", "研发费用"),
        ("研发费率", "8%", "研发费用率"),
        ("销售净利率", "12%", "净利率"),
        ("授权专利", "36项", "专利"),
        ("软著数量", "15项", "软件著作权"),
        ("风险事项", "原材料价格波动", "风险"),
        ("对外担保余额", "100万元", "对外担保"),
        ("抵押及质押", "机器设备", "抵押质押"),
    ),
)
def test_arbitrary_employee_labels_survive_source_verification(label, value, metric):
    facts = _employee_facts(f"{label}: {value}", [{"metric": metric, "value": value, "quote": f"{label}: {value}"}])
    assert [(fact.metric, fact.value) for fact in facts] == [(metric, value)]


@pytest.mark.parametrize(
    "text",
    (
        "新签订单预计明年增长：20",
        "研发人员正在招聘：10",
        "研发费用预算尚未获批：300",
        "净利润同比增长率：12%",
        "软件著作权申请日期：2025",
        "对外担保制度版本：3",
        "抵押质押流程共分：4步",
    ),
)
def test_empty_employee_decision_has_no_code_side_fallback(text):
    assert _employee_facts(text, []) == []
