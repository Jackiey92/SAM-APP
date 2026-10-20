"""The sole URI construction/validation boundary for the new SAM store.

The session namespace is reserved only; it is never accessible by M1.
"""
from dataclasses import dataclass

FACTS_ROOT = "sam://facts/"
ORIGINAL_ROOT = "sam://original/"
KV_ROOT = "sam://kv/"
KV_DOMAINS = frozenset({"cap_table", "derivation", "conversations"})
SESSION_ROOT = "sam://session/"


def identifier(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or not all(c.isalnum() or c == "-" for c in value)):
        raise ValueError("expected a nonempty semantic identifier (letters/digits/hyphens)")
    return value


def _segment(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or value.startswith(".")
            or not all(c.isalnum() or c in "-." for c in value)):
        raise ValueError("unsafe URI segment; underscores/escapes/traversal are forbidden")
    return value


@dataclass(frozen=True)
class Address:
    uri: str
    area: str
    company_id: str
    module_key: str
    parts: tuple[str, ...]


def parse(uri: str, *, company_id: str | None = None) -> Address:
    if not isinstance(uri, str) or len(uri) > 2048:
        raise ValueError("invalid memory URI")
    if uri.startswith(FACTS_ROOT):
        area, tail = "facts", uri[len(FACTS_ROOT):]
    elif uri.startswith(ORIGINAL_ROOT):
        area, tail = "original", uri[len(ORIGINAL_ROOT):]
    elif uri.startswith(KV_ROOT):
        parts = uri[len(KV_ROOT):].removesuffix("/").split("/")
        if len(parts) < 3 or parts[0] not in {"co", "rt"}:
            raise ValueError("KV requires co/company/domain or rt/session/task scope")
        identifier(parts[1])
        if parts[0] == "co":
            if parts[2] not in KV_DOMAINS:
                raise ValueError("unknown company KV domain")
            if company_id is None:
                raise ValueError("company KV requires injected host scope")
            if parts[1] != identifier(company_id):
                raise ValueError("URI company differs from injected host scope")
            company = parts[1]
        else:
            identifier(parts[2])
            company = ""
        for part in parts[3:]:
            # KV retains existing JSON filenames, including thread_summary.
            if (not part or len(part) > 200 or part.startswith(".")
                    or not all(c.isalnum() or c in "-._" for c in part)):
                raise ValueError("unsafe KV URI segment")
        return Address(uri.removesuffix("/"), "kv", company, "", tuple(parts))
    else:
        raise ValueError("only facts/original/kv are accessible; session is reserved")
    tail = tail.removesuffix("/")
    parts = tail.split("/")
    if area == "facts" and len(parts) >= 3 and parts[0] == "co" and parts[2] == "tree":
        company = identifier(parts[1])
        for part in parts[2:]:
            _segment(part)
        if company_id is not None and company != identifier(company_id):
            raise ValueError("URI company differs from injected host scope")
        return Address(uri.removesuffix("/"), area, company, "", tuple(parts))
    company = identifier(parts[0])
    for part in parts[1:]:
        _segment(part)
    if company_id is not None and company != identifier(company_id):
        raise ValueError("URI company differs from injected host scope")
    module = parts[1] if len(parts) > 2 or (len(parts) == 2 and "." not in parts[1]) else ""
    return Address(uri.removesuffix("/"), area, company, module, tuple(parts))


def facts_uri(company_id: str, module_key: str | None = None, *names: str) -> str:
    parts = [identifier(company_id)]
    if module_key is not None:
        parts.append(identifier(module_key))
    parts.extend(_segment(name) for name in names)
    return FACTS_ROOT + "/".join(parts)


def original_uri(company_id: str, *names: str) -> str:
    return ORIGINAL_ROOT + "/".join([identifier(company_id), *(_segment(n) for n in names)])


def fact_uri(company_id: str, module_key: str, fact_key: str) -> str:
    return facts_uri(company_id, module_key, "F" + identifier(fact_key) + ".md")


def company_kv_uri(company_id: str, domain: str, *names: str) -> str:
    uri = KV_ROOT + "co/" + identifier(company_id) + "/" + domain
    if names:
        uri += "/" + "/".join(names)
    return parse(uri, company_id=company_id).uri


def runtime_kv_uri(session_id: str, task_id: str, *names: str) -> str:
    uri = KV_ROOT + "rt/" + identifier(session_id) + "/" + identifier(task_id)
    if names:
        uri += "/" + "/".join(names)
    return parse(uri).uri


def _trunk_directory(directory: str) -> str:
    from ..module_tree import MODULES
    if not isinstance(directory, str) or directory not in {m.directory for m in MODULES}:
        raise ValueError("unknown trunk directory")
    return _segment(directory)


def leaf_uri(company_id: str, directory: str, file_name: str) -> str:
    file_name = _segment(file_name)
    if not file_name.endswith(".md"):
        file_name = _segment(file_name + ".md")
    return FACTS_ROOT + "/".join((identifier(company_id), _trunk_directory(directory),
                                  "leaves", file_name))


def tree_meta_uri(company_id: str) -> str:
    return facts_uri(company_id, None, "tree.meta")


def trunk_doc_uri(company_id: str, directory: str, doc: str) -> str:
    if not isinstance(doc, str) or doc not in {"trunk-brief.md", "trunk-guide.md"}:
        raise ValueError("unknown trunk document")
    return FACTS_ROOT + "/".join((identifier(company_id), _trunk_directory(directory), doc))


def conflict_uri(company_id: str, file_name: str) -> str:
    from ..module_tree import TODO_DIRECTORY
    file_name = _segment(file_name)
    if not file_name.endswith(".md"):
        file_name = _segment(file_name + ".md")
    return facts_uri(company_id, None, TODO_DIRECTORY, file_name)


def tree_brief_uri(company_id: str) -> str:
    return facts_uri(company_id, None, "tree-brief.md")


def tree_guide_uri(company_id: str) -> str:
    return facts_uri(company_id, None, "tree-guide.md")
