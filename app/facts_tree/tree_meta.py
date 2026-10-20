"""Controlled non-vector JSON metadata projection; callers own transactions."""
import json

from .. import settings
from ..memory.paths import identifier, tree_meta_uri

_FIELDS = frozenset({"display_name", "template_pack", "pack_version", "bound_at", "locked",
                     "pack_digest", "digest_source", "binding_revision"})
DIGEST_SOURCES = frozenset({"legacy-bootstrap", "verified-manifest"})


def _is_digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def require_enabled():
    if not settings.SAM_FACTS_TREE:
        raise RuntimeError("facts tree disabled")


def put_tree_meta(db, company_id, payload, *, scope=None):
    require_enabled()
    company_id = identifier(company_id)
    if scope is not None and company_id != identifier(scope):
        raise ValueError("company differs from injected host scope")
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("tree.meta requires only binding fields")
    if any(not isinstance(payload[k], str) or not payload[k].strip()
           for k in ("display_name", "template_pack", "bound_at")):
        raise ValueError("tree.meta text fields must be nonempty")
    identifier(payload["template_pack"])
    if type(payload["pack_version"]) is not int or payload["pack_version"] < 1:
        raise ValueError("tree.meta version must be a positive integer")
    if type(payload["locked"]) is not int or payload["locked"] not in (0, 1):
        raise ValueError("tree.meta locked must be 0/1")
    if not _is_digest(payload["pack_digest"]):
        raise ValueError("tree.meta pack_digest must be a sha256 hex digest")
    if payload["digest_source"] not in DIGEST_SOURCES:
        raise ValueError("tree.meta digest_source must name the digest baseline origin")
    if type(payload["binding_revision"]) is not int or payload["binding_revision"] < 1:
        raise ValueError("tree.meta binding_revision must be a positive integer")
    try:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("tree.meta must be JSON serializable") from exc
    db.execute("""INSERT INTO company_tree_meta_projection(company_id,uri,content)
        VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET
        uri=excluded.uri,content=excluded.content""", (company_id, tree_meta_uri(company_id), content))


def read_tree_meta(db, company_id, *, scope=None):
    company_id = identifier(company_id)
    if scope is not None and company_id != identifier(scope):
        raise ValueError("company differs from injected host scope")
    row = db.execute("SELECT content FROM company_tree_meta_projection WHERE company_id=?",
                     (company_id,)).fetchone()
    return json.loads(row[0]) if row is not None else None
