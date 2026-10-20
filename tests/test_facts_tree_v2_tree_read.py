"""generic-v2 draft pack read-only tree catalog via TreeReadService (split A).

The draft pack is never released: the binding is constructed only inside this
test's scope through a briefly patched CompanyBindingStore._pack, restored
before any read; afterwards the test explicitly asserts the restored store
still hides and refuses the draft pack.
"""
import pytest

from app import settings
from app.db.database import init_db
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.leaf_store import LeafPublishService
from app.facts_tree.template_packs import available_packs, load_pack
from app.facts_tree.tree_read import TreeReadService
from tests.test_facts_tree_leaves import DocumentPort

COMPANY = 'acc-v2-read'
PACK = load_pack('generic-v2')

# Approved P1 draft: nine business trunks, sixty leaves (2026-10-16 审批).
EXPECTED_TRUNKS = [  # (code, order, directory, leaf count)
    ('governance', 1, '01-公司主体与治理', 5),
    ('team', 2, '02-团队与组织', 7),
    ('market', 3, '03-市场与客户需求', 7),
    ('product', 4, '04-产品与技术', 8),
    ('business', 5, '05-商业模式与获客机制', 5),
    ('customers', 6, '06-客户与商业进展', 7),
    ('operations', 7, '07-交付运营与供应', 8),
    ('finance', 8, '08-财务与现金', 7),
    ('funding', 9, '09-融资与资本计划', 6),
]
FINANCE_01_TITLE = '收入、成本与利润'
FINANCE_01_SCOPE = ('经营总额、利润/毛利、普通经营收支口径、按产品或客户的收入构成；'
                    '排除/交叉：研发/人员明细归L-finance-05、资本支出指标归06、税务指标归07；'
                    '原值只引用不重复')


def _draft_pack(pack):
    if pack not in ('generic', 'generic-v2'):
        raise ValueError('segment A supports only generic')
    return load_pack(pack)


def test_generic_v2_tree_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    database = tmp_path / 'ledger.db'
    init_db(database)
    root = tmp_path / 'docs'
    root.mkdir()
    port = DocumentPort(root)
    # Draft binding: allowed only inside this block, restored before any read.
    original_pack = CompanyBindingStore._pack
    CompanyBindingStore._pack = staticmethod(_draft_pack)
    try:
        service = LeafPublishService(str(database), port, company_id=COMPANY)
        service.binding.register(COMPANY, '临时测试公司', pack='generic-v2')
        service.close()
    finally:
        CompanyBindingStore._pack = staticmethod(original_pack)

    with TreeReadService(str(database), port, company_id=COMPANY) as reader:
        tree = reader.read_tree(COMPANY)
        assert tree['node'] == 'tree' and tree['state'] == 'empty'
        assert tree['brief'] is None and tree['guide'] is None
        assert tree['binding'] == {'template_pack': 'generic-v2', 'pack_version': 1,
                                   'binding_revision': 1, 'pack_digest': PACK.manifest_sha256,
                                   'digest_source': 'verified-manifest'}
        assert [(t['code'], t['order'], t['directory'], t['leaves'])
                for t in tree['trunks']] == EXPECTED_TRUNKS
        assert all(t['state'] == 'not_generated' and t['generated_leaves'] == 0
                   for t in tree['trunks'])
        assert sum(t['leaves'] for t in tree['trunks']) == 60
        total = 0
        for code, _, _, count in EXPECTED_TRUNKS:
            trunk = reader.read_trunk(COMPANY, code)
            assert trunk['state'] == 'not_generated'
            assert trunk['brief'] is None and trunk['guide'] is None
            assert len(trunk['leaves']) == count
            total += len(trunk['leaves'])
            assert all(leaf['state'] == 'not_generated' and leaf['version'] is None
                       and leaf['trunk'] == code for leaf in trunk['leaves'])
        assert total == 60
        finance = reader.read_trunk(COMPANY, 'finance')
        entry = next(leaf for leaf in finance['leaves'] if leaf['code'] == 'L-finance-01')
        assert entry == {'code': 'L-finance-01', 'trunk': 'finance',
                         'title': FINANCE_01_TITLE, 'shape': None,
                         'scope': FINANCE_01_SCOPE,
                         'state': 'not_generated', 'version': None}
        leaf = reader.read_leaf(COMPANY, 'L-finance-01')
        assert leaf['node'] == 'leaf' and leaf['state'] == 'not_generated'
        assert leaf['leaf'] == {'code': 'L-finance-01', 'trunk': 'finance',
                                'title': FINANCE_01_TITLE, 'shape': None,
                                'scope': FINANCE_01_SCOPE}
        assert (leaf['uri'], leaf['version'], leaf['leaf_state'],
                leaf['index'], leaf['content']) == (None, None, None, None, None)

    # Restored state: the draft pack stays hidden and unbindable.
    assert 'generic' in available_packs()
    assert 'generic-v2' not in available_packs()
    store = CompanyBindingStore(str(tmp_path / 'rejected.db'))
    try:
        with pytest.raises(ValueError, match='only generic'):
            store.register('acc-v2-new', '新公司', pack='generic-v2')
        store.register('acc-v2-new', '新公司')  # released pack still binds
        with pytest.raises(ValueError, match='only generic'):
            store.bind_pack('acc-v2-new', 'generic-v2')
    finally:
        store.close()
