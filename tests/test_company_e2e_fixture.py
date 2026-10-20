"""The named company fixture is parsed by the real local Docling bridge."""
import hashlib
from pathlib import Path

from openpyxl import load_workbook

from tests.test_document_ingest_no_suffix import _bridge_module

FIXTURE = Path(__file__).parent / 'fixtures/company/谷斗科技-经营与财务摘要.xlsx'
NAME = '谷斗科技（上海）有限公司'


def test_company_fixture_names_subject_and_has_cross_period_modules_and_noise():
    workbook = load_workbook(FIXTURE, read_only=True, data_only=True)
    assert len(workbook.worksheets) == 6
    contents = {sheet.title: ' '.join(str(cell) for row in sheet.values for cell in row if cell is not None)
                for sheet in workbook}
    assert all(NAME in body for title, body in contents.items() if title != '未归属附件')
    assert all(value in contents['两期财务'] for value in ('2024', '2025', '1200', '180', '1500', '240'))
    assert all(value in contents['客户与订单'] for value in ('星河制造', '青禾设备', '海川物流', '300', '200', '100'))
    assert all(value in contents['核心员工'] for value in ('林舟', '周宁', '陈禾'))
    assert all(value in contents['股权结构'] for value in ('60%', '25%', '15%'))
    assert 'TEST-GUDOU-ONLY' in contents['公司基本信息']
    assert '987.65' in contents['未归属附件'] and '432.10' in contents['未归属附件']
    assert NAME not in contents['未归属附件']
    workbook.close()


def test_real_docling_excel_parse_preserves_named_evidence_and_coordinates(monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    bridge = _bridge_module()
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    pages, summary = bridge._run_docling(FIXTURE, digest, 'xlsx', FIXTURE.name)
    assert summary['status'] == 'parsed' and summary['table_count'] == 6
    assert summary['table_row_count'] == 10
    assert len(pages) == 6
    for page in pages:
        for table in page['tables']:
            assert table['source_loc']['file_hash'] == digest
            assert table['source_loc']['locator'].startswith('docling:table/')
    assert all(NAME in str(page['tables']) for page in pages[:5])
    assert all(value in str(pages[-1]['tables']) for value in ('987.65', '432.10'))


def test_acceptance_numeric_guard_handles_digits_adjacent_to_chinese():
    from scripts.check_2b_e2e import numeric_literals
    assert numeric_literals('谷斗2025年度营业收入1500万元，股比60%，净利润240万元') == {'2025', '1500', '60', '240'}
