"""Company fact-memory addresses; root argument retained for adapter callers."""
from ..paths import facts_uri, fact_uri as card_uri
from ..taxonomy import projection_module

FACTS_ROOT = "sam://facts/"
L2_DIRECTORY = ""
L1_DIRECTORY = ""
L0_DIRECTORY = ""


def company_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return facts_uri(company_id)


def l2_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return company_uri(company_id)


def fact_uri(company_id: str, fact_id: int, *, root: str = FACTS_ROOT, module_key=None) -> str:
    if type(fact_id) is not int or fact_id < 1:
        raise ValueError("invalid fact id")
    return card_uri(company_id, projection_module(module_key), str(fact_id))


def brief_md_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return facts_uri(company_id, None, "brief.md")


def brief_json_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return facts_uri(company_id, None, "brief.json")


def guide_md_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return facts_uri(company_id, None, "guide.md")


def guide_json_uri(company_id: str, *, root: str = FACTS_ROOT) -> str:
    return facts_uri(company_id, None, "guide.json")


def module_document_uri(company_id: str, module_key: str, name: str) -> str:
    from ..taxonomy import MODULE_KEYS
    if module_key not in MODULE_KEYS:
        raise ValueError("unknown module key")
    if name not in {"brief.json", "brief.md", "guide.json", "guide.md"}:
        raise ValueError("unknown module document")
    return facts_uri(company_id, module_key, name)


def module_brief_json_uri(company_id, module_key):
    return module_document_uri(company_id, module_key, "brief.json")


def module_brief_md_uri(company_id, module_key):
    return module_document_uri(company_id, module_key, "brief.md")


def module_guide_json_uri(company_id, module_key):
    return module_document_uri(company_id, module_key, "guide.json")


def module_guide_md_uri(company_id, module_key):
    return module_document_uri(company_id, module_key, "guide.md")
