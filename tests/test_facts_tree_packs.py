"""Structural template validation only; no business classification."""
from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
import yaml

from app import module_tree, module_taxonomy
from app.memory import taxonomy
from app.facts_tree import template_packs as packs


def test_module_shims_preserve_identity_and_values():
    for symbol in ("Module", "MODULES", "MODULE_KEYS", "TODO_DIRECTORY", "UNCLASSIFIED",
                   "validated_module", "projection_module", "module_definitions"):
        assert getattr(module_tree, symbol) is getattr(module_taxonomy, symbol)
        if hasattr(taxonomy, symbol):
            assert getattr(module_tree, symbol) is getattr(taxonomy, symbol)
    assert len(module_tree.MODULES) == 10
    assert module_tree.MODULE_KEYS == {"info", "bizov", "hr", "prod", "purch", "sales", "mkt", "fin", "fund", "legal"}
    assert [(m.key, m.directory, m.order, m.hidden) for m in module_tree.MODULES] == [
        ("info", "01-企业基本信息", 1, False), ("bizov", "02-业务概览", 2, False),
        ("hr", "03-人员管理", 3, False), ("prod", "04-生产", 4, True),
        ("purch", "05-采购", 5, True), ("sales", "06-销售", 6, True),
        ("mkt", "07-市场", 7, False), ("fin", "08-财务", 8, True),
        ("fund", "09-融资", 9, True), ("legal", "10-法务合规", 10, False)]
    assert len(module_tree.module_definitions(include_hidden=False)) == 5
    assert module_tree.validated_module("info") == "info"
    assert module_tree.validated_module("nope") is None
    assert module_tree.projection_module(None) == "待归类"
    assert module_tree.TODO_DIRECTORY == "待办与冲突"
    assert module_tree.UNCLASSIFIED == "待归类"


def test_generic_exact_counts_and_read_only():
    pack = packs.load_pack()
    assert (pack.pack, pack.version, pack.is_industry_pack) == ("generic", 1, False)
    assert len(pack.leaves) == len({l.code for l in pack.leaves}) == 98
    assert Counter(l.trunk for l in pack.leaves) == dict(zip(
        (m.key for m in module_tree.MODULES), (12, 15, 9, 9, 4, 18, 19, 2, 5, 5)))
    for trunk in module_tree.MODULE_KEYS:
        assert sorted(int(l.code.split("-")[-1]) for l in pack.leaves if l.trunk == trunk) == list(
            range(1, sum(l.trunk == trunk for l in pack.leaves) + 1))
    with pytest.raises(FrozenInstanceError):
        pack.leaves[0].shape = "x"
    with pytest.raises(TypeError):
        pack.trunks[0]["code"] = "other"
    assert packs.available_packs() == ["generic"]


@pytest.fixture
def raw_pack():
    return yaml.safe_load((packs.TEMPLATES_ROOT / "generic" / "leaves.yaml").read_text(encoding="utf-8"))


def write_pack(root, name, raw):
    path = root / name / "leaves.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


@pytest.mark.parametrize("case", [
    "gap", "unknown-leaf-trunk", "bad-shape", "duplicate-code", "new-trunk",
    "trunk-mismatch", "bad-number", "zero-number", "wrong-order", "duplicate-trunk",
    "missing-trunk", "bad-directory", "count", "bad-version", "boolean-version",
    "bad-industry-flag", "name-mismatch", "bad-leaf-type", "bad-leaves-type",
    "bad-trunks-type", "missing-title", "blank-title", "bad-scope", "bad-root",
    "bad-hidden", "extra-trunk-field", "extra-leaf-field", "generic-industry",
])
def test_bad_packs_raise_value_error(tmp_path, monkeypatch, raw_pack, case):
    raw = deepcopy(raw_pack)
    if case == "gap": raw["leaves"][0]["code"] = "L-info-13"
    elif case == "unknown-leaf-trunk": raw["leaves"][0]["code"] = "L-other-01"
    elif case == "bad-shape": raw["leaves"][0]["shape"] = "x"
    elif case == "duplicate-code": raw["leaves"][1] = deepcopy(raw["leaves"][0])
    elif case == "new-trunk": raw["trunks"][0]["code"] = "other"
    elif case == "trunk-mismatch": raw["leaves"][0]["trunk"] = "hr"
    elif case == "bad-number": raw["leaves"][0]["code"] = "L-info-1"
    elif case == "zero-number": raw["leaves"][0]["code"] = "L-info-00"
    elif case == "wrong-order": raw["trunks"][0]["order"] = 99
    elif case == "duplicate-trunk": raw["trunks"].append(deepcopy(raw["trunks"][0]))
    elif case == "missing-trunk": raw["trunks"].pop()
    elif case == "bad-directory": raw["trunks"][0]["directory"] = "../bad"
    elif case == "count": raw["leaves"].pop()
    elif case == "bad-version": raw["version"] = 0
    elif case == "boolean-version": raw["version"] = True
    elif case == "bad-industry-flag": raw["is_industry_pack"] = "false"
    elif case == "name-mismatch": raw["pack"] = "other"
    elif case == "bad-leaf-type": raw["leaves"][0] = None
    elif case == "bad-leaves-type": raw["leaves"] = {}
    elif case == "bad-trunks-type": raw["trunks"] = {}
    elif case == "missing-title": raw["leaves"][0].pop("title")
    elif case == "blank-title": raw["leaves"][0]["title"] = " "
    elif case == "bad-scope": raw["leaves"][0]["scope"] = None
    elif case == "bad-root": raw = []
    elif case == "bad-hidden": raw["trunks"][0]["built_in_hidden_default"] = 1
    elif case == "extra-trunk-field": raw["trunks"][0]["extra"] = []
    elif case == "extra-leaf-field": raw["leaves"][0]["extra"] = []
    elif case == "generic-industry": raw["is_industry_pack"] = True
    write_pack(tmp_path, "generic", raw)
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    with pytest.raises(ValueError, match="pack 'generic', code"):
        packs.validate_pack("generic")
    assert packs.available_packs() == []


def test_manifest_digest_is_stable_and_semantic(tmp_path, monkeypatch, raw_pack):
    digest = packs.load_pack().manifest_sha256
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)
    assert packs.load_pack().manifest_sha256 == digest
    # Identical semantic content under a different root yields the same digest.
    write_pack(tmp_path, "generic", deepcopy(raw_pack))
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    assert packs.load_pack().manifest_sha256 == digest
    # YAML mapping key order and comments are not part of the manifest.
    reordered = {"is_industry_pack": raw_pack["is_industry_pack"],
                 "leaves": raw_pack["leaves"], "version": raw_pack["version"],
                 "trunks": raw_pack["trunks"], "pack": raw_pack["pack"]}
    path = tmp_path / "generic" / "leaves.yaml"
    path.write_text("# 无关注释\n" + yaml.safe_dump(reordered, allow_unicode=True) + "\n# tail\n",
                    encoding="utf-8")
    assert packs.load_pack().manifest_sha256 == digest


def test_manifest_digest_changes_with_any_boundary_field(tmp_path, monkeypatch, raw_pack):
    write_pack(tmp_path, "generic", deepcopy(raw_pack))
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    base = packs.load_pack().manifest_sha256
    mutations = (
        lambda raw: raw["leaves"][0].update(scope=raw["leaves"][0]["scope"] + " "),
        lambda raw: raw["leaves"][0].update(title=raw["leaves"][0]["title"] + "x"),
        lambda raw: raw.update(version=raw["version"] + 1),
        lambda raw: raw["trunks"][0].update(directory="01-改名"),
    )
    for mutate in mutations:
        raw = deepcopy(raw_pack)
        mutate(raw)
        write_pack(tmp_path, "generic", raw)
        assert packs.load_pack().manifest_sha256 != base
    # is_industry_pack is part of the manifest (generic itself forbids True).
    raw = deepcopy(raw_pack)
    raw["pack"], raw["is_industry_pack"] = "x-pack", True
    write_pack(tmp_path, "x-pack", raw)
    industry_digest = packs.load_pack("x-pack").manifest_sha256
    raw["is_industry_pack"] = False
    write_pack(tmp_path, "x-pack", raw)
    assert packs.load_pack("x-pack").manifest_sha256 != industry_digest


@pytest.mark.parametrize("name", ["../generic", "", "no_such", "missing", None])
def test_pack_names_and_missing_files_fail(name):
    with pytest.raises(ValueError):
        packs.load_pack(name)


def test_registry_sorting_extension_and_duplicate_yaml(tmp_path, monkeypatch, raw_pack):
    for name in ("z-pack", "a-pack"):
        raw = deepcopy(raw_pack)
        raw["pack"], raw["is_industry_pack"] = name, True
        raw["trunks"][0]["directory"] = "01-自定义名字"
        raw["leaves"] = raw["leaves"][:2]
        write_pack(tmp_path, name, raw)
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    assert packs.available_packs() == ["a-pack", "z-pack"]
    assert len(packs.load_pack("a-pack").leaves) == 2
    (tmp_path / "a-pack" / "leaves.yaml").write_text("pack: a-pack\npack: a-pack\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML"):
        packs.load_pack("a-pack")
    assert packs.available_packs() == ["z-pack"]


# --- generic-v2 draft pack (P3-1): explicit-load only, never listed/bound ---

GENERIC_V1_DIGEST = "ac137c1bed980451455e932661b82d34e453d65d2513d74a7a009d033b4e76aa"
GENERIC_V2_TRUNKS = [
    ("governance", "01-公司主体与治理", 1), ("team", "02-团队与组织", 2),
    ("market", "03-市场与客户需求", 3), ("product", "04-产品与技术", 4),
    ("business", "05-商业模式与获客机制", 5), ("customers", "06-客户与商业进展", 6),
    ("operations", "07-交付运营与供应", 7), ("finance", "08-财务与现金", 8),
    ("funding", "09-融资与资本计划", 9)]
GENERIC_V2_COUNTS = {"governance": 5, "team": 7, "market": 7, "product": 8,
                     "business": 5, "customers": 7, "operations": 8, "finance": 7,
                     "funding": 6}
# Approved P1 §2 leaf titles; pins codes to names so neither drifts silently.
GENERIC_V2_TITLES = {
    "L-governance-01": "主体登记与关联主体", "L-governance-02": "股权、出资与股东关系",
    "L-governance-03": "控制权与治理安排", "L-governance-04": "股权权属限制与争议",
    "L-governance-05": "公司治理变更事件",
    "L-team-01": "创始人与核心成员档案", "L-team-02": "组织结构与职责",
    "L-team-03": "人员规模与结构", "L-team-04": "核心团队与关键岗位",
    "L-team-05": "人员变动与团队协作", "L-team-06": "激励与人才计划",
    "L-team-07": "劳动关系与用工义务",
    "L-market-01": "行业与市场范围", "L-market-02": "目标客群与应用场景",
    "L-market-03": "需求证据与客户洞察", "L-market-04": "市场规模与测算",
    "L-market-05": "市场驱动与周期", "L-market-06": "竞争者与替代方案",
    "L-market-07": "行业门槛与外部环境",
    "L-product-01": "产品与服务组合", "L-product-02": "产品能力与性能指标",
    "L-product-03": "产品路线与生命周期", "L-product-04": "技术架构与关键难点",
    "L-product-05": "技术来源与合作", "L-product-06": "研发项目与技术迭代",
    "L-product-07": "知识产权与技术资产", "L-product-08": "核心部件与自研边界",
    "L-business-01": "价值交付与商业模式", "L-business-02": "收费与定价政策",
    "L-business-03": "获客与销售机制", "L-business-04": "渠道与合作机制",
    "L-business-05": "客户验证与服务政策",
    "L-customers-01": "客户与账户清单", "L-customers-02": "线索、拓展与商机",
    "L-customers-03": "试用、验证与采用进展", "L-customers-04": "合同、订单与履约",
    "L-customers-05": "客户反馈与合作状态", "L-customers-06": "渠道与商业成果",
    "L-customers-07": "客户集中与商业依赖",
    "L-operations-01": "交付与服务流程", "L-operations-02": "产能、容量与利用",
    "L-operations-03": "质量控制、安全与运营合规", "L-operations-04": "设备、场地与运营资产",
    "L-operations-05": "供应商与采购供给", "L-operations-06": "供应与交付依赖",
    "L-operations-07": "运营系统与自动化", "L-operations-08": "运营扩张与资本项目",
    "L-finance-01": "收入、成本与利润", "L-finance-02": "现金、回款与应收应付",
    "L-finance-03": "资产、负债与权益", "L-finance-04": "预算与财务预测",
    "L-finance-05": "研发与人员投入金额", "L-finance-06": "资本支出与融资资金执行",
    "L-finance-07": "税务、补贴与财务优惠",
    "L-funding-01": "融资历史与交易", "L-funding-02": "债务与融资义务",
    "L-funding-03": "本轮融资目标与方案", "L-funding-04": "资金用途与里程碑",
    "L-funding-05": "投资协议与交割条件", "L-funding-06": "上市与退出准备"}


def test_generic_v1_manifest_digest_is_frozen():
    # generic-v1 is frozen (P2): its digest must never move.
    assert packs.load_pack("generic").manifest_sha256 == GENERIC_V1_DIGEST


def test_generic_v2_loads_approved_nine_trunks_and_sixty_leaves():
    pack = packs.load_pack("generic-v2")
    assert (pack.pack, pack.version, pack.is_industry_pack) == ("generic-v2", 1, False)
    assert [(t["code"], t["directory"], t["order"]) for t in pack.trunks] == GENERIC_V2_TRUNKS
    assert len(pack.leaves) == len({l.code for l in pack.leaves}) == 60
    assert Counter(l.trunk for l in pack.leaves) == GENERIC_V2_COUNTS
    for trunk, count in GENERIC_V2_COUNTS.items():
        assert sorted(l.code for l in pack.leaves if l.trunk == trunk) == [
            f"L-{trunk}-{n:02d}" for n in range(1, count + 1)]
    assert {l.code: l.title for l in pack.leaves} == GENERIC_V2_TITLES
    assert all(l.scope.strip() for l in pack.leaves)
    team07 = next(l for l in pack.leaves if l.code == "L-team-07")
    assert team07.scope == ("劳动合同、用工义务、争议和处理进展；排除/交叉："
                            "薪酬核算/支付归财务；人数与招聘归原团队叶")
    assert "获批后才进入模板" not in team07.scope
    # 2026-10-17 裁定: v2 leaves carry no n/s shape; v1 keeps its own.
    assert all(l.shape is None for l in pack.leaves)
    assert all(l.shape in {"n", "s"} for l in packs.load_pack("generic").leaves)
    # The v2 namespace never reuses the legacy ten trunk codes.
    assert {t["code"] for t in pack.trunks}.isdisjoint(module_tree.MODULE_KEYS)
    assert pack.trunk_for("governance")["directory"] == "01-公司主体与治理"
    assert pack.trunk_directory("finance") == "08-财务与现金"
    assert pack.leaf_for("L-finance-01").trunk == "finance"
    assert pack.trunk_for("legal") is None
    assert pack.leaf_for("L-legal-01") is None
    digest = pack.manifest_sha256
    assert isinstance(digest, str) and len(digest) == 64
    assert packs.load_pack("generic-v2").manifest_sha256 == digest


def test_generic_v2_stays_unlisted_while_generic_stays_available():
    assert packs.available_packs() == ["generic"]
    assert "generic-v2" not in packs.available_packs()


@pytest.fixture
def raw_v2_pack():
    return yaml.safe_load(
        (packs.TEMPLATES_ROOT / "generic-v2" / "leaves.yaml").read_text(encoding="utf-8"))


def test_generic_v2_manifest_digest_is_stable_and_semantic(tmp_path, monkeypatch, raw_v2_pack):
    digest = packs.load_pack("generic-v2").manifest_sha256
    write_pack(tmp_path, "generic-v2", deepcopy(raw_v2_pack))
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    assert packs.load_pack("generic-v2").manifest_sha256 == digest
    raw = deepcopy(raw_v2_pack)
    raw["leaves"][0]["scope"] += " "
    write_pack(tmp_path, "generic-v2", raw)
    assert packs.load_pack("generic-v2").manifest_sha256 != digest
    # An unreleased pack is never listed, even when it validates.
    assert packs.available_packs() == []


@pytest.mark.parametrize("case", [
    "shape-field", "missing-leaf-field", "extra-leaf-field", "gap", "duplicate-code",
    "trunk-mismatch", "unknown-leaf-trunk", "legacy-leaf-trunk", "bad-number",
    "missing-trunk", "duplicate-trunk", "order-gap", "extra-trunk-field",
    "bad-directory", "name-mismatch", "boolean-version", "blank-title",
])
def test_bad_generic_v2_packs_raise_value_error(tmp_path, monkeypatch, raw_v2_pack, case):
    raw = deepcopy(raw_v2_pack)
    if case == "shape-field": raw["leaves"][0]["shape"] = "n"
    elif case == "missing-leaf-field": raw["leaves"][0].pop("scope")
    elif case == "extra-leaf-field": raw["leaves"][0]["extra"] = 1
    elif case == "gap": raw["leaves"].pop(0)
    elif case == "duplicate-code": raw["leaves"][1] = deepcopy(raw["leaves"][0])
    elif case == "trunk-mismatch": raw["leaves"][0]["trunk"] = "team"
    elif case == "unknown-leaf-trunk": raw["leaves"][0]["code"] = "L-other-01"
    elif case == "legacy-leaf-trunk": raw["leaves"][0]["code"] = "L-info-01"
    elif case == "bad-number": raw["leaves"][0]["code"] = "L-governance-1"
    elif case == "missing-trunk": raw["trunks"].pop(0)
    elif case == "duplicate-trunk": raw["trunks"].append(deepcopy(raw["trunks"][0]))
    elif case == "order-gap": raw["trunks"][8]["order"] = 10
    elif case == "extra-trunk-field": raw["trunks"][0]["built_in_hidden_default"] = True
    elif case == "bad-directory": raw["trunks"][0]["directory"] = "../bad"
    elif case == "name-mismatch": raw["pack"] = "generic"
    elif case == "boolean-version": raw["version"] = True
    elif case == "blank-title": raw["leaves"][0]["title"] = " "
    write_pack(tmp_path, "generic-v2", raw)
    monkeypatch.setattr(packs, "TEMPLATES_ROOT", tmp_path)
    with pytest.raises(ValueError, match="pack 'generic-v2', code"):
        packs.validate_pack("generic-v2")
