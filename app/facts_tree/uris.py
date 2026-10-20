"""Pack-scoped FactsTree document addresses.

The frozen generic pack keeps its existing projection addresses. New packs use
an explicit company/pack/version namespace so equal leaf codes cannot collide
across independently versioned trees.
"""
import re

from ..memory import paths


def leaf_slug(title):
    value = '-'.join(filter(None, re.split(r'[^\w]+', title.replace('_', '-'))))
    return paths._segment(value)


def _base(company_id, pack):
    company = paths.identifier(company_id)
    name = paths.identifier(pack.pack)
    if type(pack.version) is not int or pack.version < 1:
        raise ValueError("template pack version must be a positive integer")
    return f"{paths.FACTS_ROOT}co/{company}/tree/{name}/{pack.version}"


def leaf_uri(company_id, pack, leaf):
    if pack.leaf_for(leaf.code) != leaf or pack.trunk_for(leaf.trunk) is None:
        raise ValueError("leaf is outside the supplied template pack")
    name = f"{paths.identifier(leaf.code)}-{leaf_slug(leaf.title)}.md"
    if pack.pack == "generic":
        return paths.leaf_uri(company_id, pack.trunk_directory(leaf.trunk), name)
    return f"{_base(company_id, pack)}/leaves/{paths._segment(name)}"


def trunk_doc_uri(company_id, pack, trunk_code, document):
    if document not in {"trunk-brief.md", "trunk-guide.md"}:
        raise ValueError("unknown trunk document")
    trunk = pack.trunk_for(trunk_code)
    if trunk is None:
        raise ValueError("unknown trunk code")
    if pack.pack == "generic":
        return paths.trunk_doc_uri(company_id, trunk["directory"], document)
    return f"{_base(company_id, pack)}/trunks/{paths.identifier(trunk_code)}/{document}"


def tree_doc_uri(company_id, pack, document):
    if document not in {"tree-brief.md", "tree-guide.md"}:
        raise ValueError("unknown tree document")
    if pack.pack == "generic":
        return paths.tree_brief_uri(company_id) if document == "tree-brief.md" else paths.tree_guide_uri(company_id)
    return f"{_base(company_id, pack)}/{document}"


def tree_prefix(company_id):
    return f"{paths.FACTS_ROOT}co/{paths.identifier(company_id)}/tree/"
