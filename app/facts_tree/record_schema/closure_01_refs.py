"""Closure 1 for the supplied in-memory tree."""
from hashlib import sha256

from ._primitives import (
    _fail, _ref_key, bindings_current, git_format,
)
from ._registry import register_closure


@register_closure(1)
def _resolve_refs(tree, index):
    def walk(value, family, path, field):
        if isinstance(value, dict):
            if "record_type" in value and "stable_id" in value:
                key = _ref_key(value, family, path, field)
                if key not in index:
                    _fail(family, path, "unresolved_typed_ref", field)
            else:
                for name, item in value.items():
                    walk(item, family, path, field + "." + name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, family, path, f"{field}[{i}]")

    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        walk(payload, family, path, "$")
        if family == "event" and payload["event_type"] not in bindings_current.BINDING_EVENT_TYPES:
            for i, (ref, after) in enumerate(zip(payload["changed_refs"], payload["after"])):
                key = _ref_key(ref, family, path, f"changed_refs[{i}]")
                target = tree[index[key]]
                expected = (ref["revision"] if type(ref.get("revision")) is int else
                            sha256(git_format.dumps_record(target)).hexdigest())
                if type(after) is not type(expected) or after != expected:
                    _fail(family, path, "event_after_mismatch", f"after[{i}]")
