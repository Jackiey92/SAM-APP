"""Shared facts-tree branches: module keys and trunk directory names."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Module:
    key: str
    directory: str
    order: int
    hidden: bool = False

MODULES = (
    Module("info", "01-企业基本信息", 1),
    Module("bizov", "02-业务概览", 2),
    Module("hr", "03-人员管理", 3),
    Module("prod", "04-生产", 4, True),
    Module("purch", "05-采购", 5, True),
    Module("sales", "06-销售", 6, True),
    Module("mkt", "07-市场", 7),
    Module("fin", "08-财务", 8, True),
    Module("fund", "09-融资", 9, True),
    Module("legal", "10-法务合规", 10),
)
MODULE_KEYS = frozenset(m.key for m in MODULES)
TODO_DIRECTORY = "待办与冲突"
UNCLASSIFIED = "待归类"


def validated_module(value: object) -> str | None:
    """Exact employee keys only. Unknown/empty input stays unclassified."""
    return value if isinstance(value, str) and value in MODULE_KEYS else None


def projection_module(value: object) -> str:
    return validated_module(value) or UNCLASSIFIED


def module_definitions(*, include_hidden: bool = True) -> list[dict]:
    """UI/storage dictionary structure only; never classifies a document."""
    return [{"code": m.key, "name": m.directory, "sort_order": m.order, "hidden": m.hidden}
            for m in MODULES if include_hidden or not m.hidden]
