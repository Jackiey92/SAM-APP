"""Pure approval ownership/restatement closure; no current authorization.

Number 6 remains reserved for external object closure. This independent
closure consumes existing event.changed_refs -> operation ownership only.
Structural acceptance grants no rights, including for consumed/revoked refs.
"""
from ._primitives import _fail, _ref_key, git_format
from ._registry import register_closure


def _key(ref):
    return ref["installation_id"], ref["company_id"], ref["approval_id"]


@register_closure(7)
def validate_approval_ownership(tree, index):
    operations, owners, targets = {}, {}, {}
    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        if family == "operation":
            operations[payload["operation_id"]] = payload
            for ref in payload["approval_use_refs"]:
                key = _key(ref)
                if key in owners and owners[key] != payload["operation_id"]:
                    _fail("operation", path, "approval_use_owner_conflict", "approval_use_refs")
                owners[key] = payload["operation_id"]
        elif family == "event":
            for ref in payload["changed_refs"]:
                key = _ref_key(ref, "event", path, "changed_refs")
                targets.setdefault(key, set()).add(payload["operation_id"])
    for path, payload in tree.items():
        refs = payload.get("approval_use_refs", [])
        if not refs:
            continue
        family = git_format.canonical_record_type(path)
        if family in {"operation", "event"}:
            bound = {payload["operation_id"]}
        elif family in {"placement_revision", "source_lifecycle"}:
            stable = payload["fact_uid"] if family == "placement_revision" else payload["source_id"]
            bound = targets.get((family, stable, payload["revision"]), set())
        else:
            continue
        for ref in refs:
            operation = operations.get(ref["operation_id"])
            if operation is None or bound != {ref["operation_id"]}:
                _fail(family, path, "approval_use_operation_binding", "approval_use_refs")
            declared = {_key(item): item for item in operation["approval_use_refs"]}
            if declared.get(_key(ref)) != ref:
                _fail(family, path, "approval_use_restatement", "approval_use_refs")
    # A tree's installation anchor, when physically present, is authoritative
    # structural context; it is never inferred from company_id.
    meta = tree.get(git_format.record_path("repository_meta"), {})
    if "installation_id" in meta:
        for path, payload in tree.items():
            family = git_format.canonical_record_type(path)
            if family not in {"operation", "event", "placement_revision", "source_lifecycle"}:
                continue
            for ref in payload.get("approval_use_refs", []):
                if ref["installation_id"] != meta["installation_id"]:
                    _fail(family, path, "approval_use_scope", "approval_use_refs")
