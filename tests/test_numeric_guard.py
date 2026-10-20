"""M3 synthetic acc-gudou only; no employee/network calls."""
import json

import pytest

from app.memory.company_facts.numeric_guard import validate, strip_marks, tokens
from app.memory.company_facts.l1_brief import L1BriefService
from app.memory.company_facts.l0_guide import L0GuideService
from app.memory.company_facts.paths import brief_json_uri, brief_md_uri, guide_json_uri, guide_md_uri
from app.ports import LocalMemoryProvider

COMPANY = 'acc-gudou'
CARDS = {'12': {'value': '3.2', 'unit': '亿元', 'period': '2024', 'entity': COMPANY},
         '18': {'value': '12.5', 'unit': '%', 'period': '2023', 'entity': COMPANY}}


@pytest.mark.parametrize('text,reason', [
    ('3.20亿元{f:12}', None), ('３．２０ 億元{f:12}', None),
    ('3.20亿元', 'missing-source'), ('3.20亿元{f:99}', 'invalid-source'),
    ('3.20亿元{l:12}', 'invalid-source'), ('3.21亿元{f:12}', 'number-not-in-source'),
    ('99亿元{f:12}', 'number-not-in-source'), ('3.20万元{f:12}', 'unit-mismatch'),
    ('12.50{f:18}', 'unit-mismatch'), ('12.50%{f:18}', None),
    ('2025年{f:12}3.2亿元{f:12}', 'period-mismatch'),
    ('2023年{f:18}3.2亿元{f:12}', 'period-mismatch'),
    ('2024年{f:12}3.2亿元{f:12}', None),
    ('合计15.7亿元{f:12,18}', 'number-not-in-source'),
    ('增长率4%{f:18}', 'number-not-in-source'),
    ('12.5%{f:12}', 'number-not-in-source'), ('纯文字结论', None),
])
def test_mechanical_rules(text, reason):
    verified, errors = validate(text, CARDS, kind='f')
    if reason:
        assert reason in {e['reason'] for e in errors}
        assert all(e['start'] < e['end'] for e in errors)
    else:
        assert errors == []
        assert len(verified) == len(list(tokens(text)))


@pytest.mark.parametrize('text', ['1,234.50元{f:12}', '１，２３４．５０ 元{f:12}'])
def test_thousands_fullwidth_decimal(text):
    assert validate(text, {'12': {'value': '1234.5', 'unit': '元', 'period': ''}}, kind='f')[1] == []


def test_table_and_link_label_atomic_tokens():
    text = '| [3.20亿元{f:12}](sam://acc-gudou/F999) | 12.50%{f:18} |'
    assert [t['number'] for t in tokens(text)] == ['3.2', '12.5']
    assert validate(text, CARDS, kind='f')[1] == []
    assert '{' not in strip_marks(text)
    assert '(sam://acc-gudou/F999)' in strip_marks(text)


def test_arbitrary_literal_unit_not_semantic_mapping():
    source = {'12': {'value': '3.2', 'unit': '公里', 'period': ''}}
    assert validate('3.20公里{f:12}', source, kind='f')[1] == []
    assert validate('3200米{f:12}', source, kind='f')[1]


@pytest.mark.parametrize('text,reason', [
    ('3.20亿元{l:brief-1}', None), ('3.2亿元{l:brief-2}', 'number-not-in-source'),
    ('3.2亿元{l:brief-99}', 'invalid-source'), ('纯文字导览', None),
    ('3.2万元{l:brief-1}', 'unit-mismatch'),
    ('2023年{l:brief-2}3.2亿元{l:brief-1}', 'period-mismatch'),
])
def test_l0_exact_entry_not_union(text, reason):
    first, _ = validate('2024年{f:12}3.20亿元{f:12}', CARDS, kind='f')
    second, _ = validate('2023年{f:18}12.50%{f:18}', CARDS, kind='f')
    _, errors = validate(text, {'brief-1': first, 'brief-2': second}, kind='l')
    assert (errors[0]['reason'] if errors else None) == reason


class Worker:
    def __init__(self, items):
        self.items = items
    def run(self, **_):
        return self.items


class Facts:
    def list_verified_facts(self, **kwargs):
        assert kwargs['company_id'] == COMPANY
        return [{**card, 'id': int(ref), 'source_file': 'synthetic', 'source_page': 1,
                 'source_span': 'synthetic:0'} for ref, card in CARDS.items()]


def l1(memory, text):
    service = L1BriefService('/tmp/unused-acc-gudou.db', memory,
        worker=Worker([{'topic': '合成', 'conclusion': text, 'fact_ids': [12]}]))
    service.facts = Facts()
    return service.rebuild(company_id=COMPANY)


def test_l1_rejection_retains_pair_no_partial_write(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    before = l1(memory, '3.20亿元{f:12}')
    snapshot = [memory.read(uri) for uri in (brief_json_uri(COMPANY), brief_md_uri(COMPANY))]
    assert '{f:12}' in snapshot[0] and '{' not in snapshot[1]
    rejected = l1(memory, '12.50%{f:18}')
    assert rejected['status'] == 'rejected'
    assert rejected['unresolved'][0]['status'] == 'unresolved'
    assert rejected['unresolved'][0]['reason'] == 'invalid-source'
    assert [memory.read(uri) for uri in (brief_json_uri(COMPANY), brief_md_uri(COMPANY))] == snapshot
    assert json.loads(snapshot[0]) == before


def test_l0_rejection_retains_pair_and_reads_only_l1(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    l1(memory, '3.20亿元{f:12}')
    original_read = memory.read
    reads = []
    def read(uri):
        reads.append(uri)
        assert uri in {brief_json_uri(COMPANY), guide_json_uri(COMPANY), guide_md_uri(COMPANY)}
        return original_read(uri)
    memory.read = read
    def rebuild(text):
        return L0GuideService(memory, worker=Worker([
            {'conclusion': text, 'l1_entry_ids': ['brief-1']}])).rebuild(company_id=COMPANY)
    assert rebuild('3.2亿元{l:brief-1}')['items']
    snapshot = [original_read(uri) for uri in (guide_json_uri(COMPANY), guide_md_uri(COMPANY))]
    assert '{l:brief-1}' in snapshot[0] and '{' not in snapshot[1]
    assert '#brief-1)' in snapshot[1]
    assert rebuild('12.5%{l:brief-1}')['status'] == 'rejected'
    assert [original_read(uri) for uri in (guide_json_uri(COMPANY), guide_md_uri(COMPANY))] == snapshot
    assert reads


def test_legacy_unverified_l1_numbers_not_trusted(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    memory.put(brief_json_uri(COMPANY), json.dumps({'company_id': COMPANY, 'items': [
        {'entry_id': 'brief-1', 'conclusion': '3.2亿元', 'fact_ids': [12]}]}))
    result = L0GuideService(memory, worker=Worker([{'conclusion': '3.2亿元{l:brief-1}',
                    'l1_entry_ids': ['brief-1']}])).rebuild(company_id=COMPANY)
    assert result['status'] == 'rejected'


@pytest.mark.parametrize('text', ['３ ． ２０ 億元{f:12}', '3.2 0亿元{f:12}'])
def test_internal_whitespace_character_normalization(text):
    assert validate(text, CARDS, kind='f')[1] == []


def test_large_decimal_never_rounded():
    value = '123456789012345678901234567890.12345000'
    assert validate(value + '元{f:12}', {'12': {'value': value[:-3], 'unit': '元', 'period': ''}}, kind='f')[1] == []
    assert validate(value + '元{f:12}', {'12': {'value': value.replace('5', '6'), 'unit': '元', 'period': ''}}, kind='f')[1]


@pytest.mark.parametrize('period', ['2024Q1', '2024-01-02', '2024年第一季度'])
def test_normative_period_is_one_token_and_exact(period):
    sources = {'12': {'value': '3.2', 'unit': '亿元', 'period': period}}
    text = period + '{f:12}收入3.2亿元{f:12}'
    verified, errors = validate(text, sources, kind='f')
    assert errors == []
    assert len(verified) == 2
    assert validate(text, {'12': {**sources['12'], 'period': '2023'}}, kind='f')[1][0]['reason'] == 'period-mismatch'
    assert validate(period + '{l:brief-1}3.2亿元{l:brief-1}', {'brief-1': verified}, kind='l')[1] == []


def test_orphan_or_out_of_scope_mark_cannot_bypass_validation():
    assert validate('纯文字{f:99}', CARDS, kind='f')[1][0]['reason'] == 'invalid-source'


def test_money_four_digits_is_not_a_year():
    assert validate('2024元{f:12}', {'12': {'value': '2024', 'unit': '元', 'period': '2023'}}, kind='f')[1] == []


def test_mark_mask_does_not_merge_adjacent_bare_numbers():
    sources = {'12': {'value': '3', 'unit': '', 'period': ''},
               '18': {'value': '4', 'unit': '', 'period': ''}}
    assert validate('3{f:12} 4{f:18}', sources, kind='f')[1] == []


def test_l0_whole_revision_rejects_even_after_budget_prefix(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    l1(memory, '3.20亿元{f:12}')
    service = L0GuideService(memory, token_budget=1, worker=Worker([
        {'conclusion': '过长纯文', 'l1_entry_ids': ['brief-1']},
        {'conclusion': '3.21亿元{l:brief-1}', 'l1_entry_ids': ['brief-1']}]))
    assert service.rebuild(company_id=COMPANY)['status'] == 'rejected'


def test_reject_first_revision_creates_no_projection_files(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    assert l1(memory, '3.21亿元{f:12}')['status'] == 'rejected'
    assert memory.query(prefix='sam://') == []


def test_l0_two_entries_are_not_a_numeric_union(tmp_path):
    memory = LocalMemoryProvider(tmp_path)
    first, _ = validate('3.20亿元{f:12}', CARDS, kind='f')
    second, _ = validate('12.50%{f:18}', CARDS, kind='f')
    memory.put(brief_json_uri(COMPANY), json.dumps({'company_id': COMPANY, 'items': [
        {'entry_id': 'brief-1', 'verified_numbers': first},
        {'entry_id': 'brief-2', 'verified_numbers': second}]}))
    result = L0GuideService(memory, worker=Worker([{'conclusion': '3.20亿元{l:brief-2}',
        'l1_entry_ids': ['brief-1', 'brief-2']}])).rebuild(company_id=COMPANY)
    assert result['status'] == 'rejected'
    assert result['unresolved'][0]['reason'] == 'number-not-in-source'


def test_multiple_sources_all_must_match_not_union():
    same = {**CARDS, '19': dict(CARDS['12'])}
    assert validate('3.2亿元{f:12,19}', same, kind='f')[1] == []
    assert validate('3.2亿元{f:12,18}', same, kind='f')[1]


def test_wrong_period_cannot_hide_inside_longer_numeric_period():
    assert validate('2024年{f:12}', {'12': {'value': '3.2', 'unit': '亿元', 'period': '12024'}}, kind='f')[1]


@pytest.mark.parametrize('text', ['.50亿元{f:12}', '．５０億元{f:12}'])
def test_leading_decimal_cannot_be_mistaken_for_integer(text):
    assert validate(text, {'12': {'value': '0.5', 'unit': '亿元', 'period': ''}}, kind='f')[1] == []
    assert validate(text, {'12': {'value': '50', 'unit': '亿元', 'period': ''}}, kind='f')[1][0]['reason'] == 'number-not-in-source'
