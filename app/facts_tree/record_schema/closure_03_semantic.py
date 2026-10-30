"""Closure 3: appendix B v1.1 §4 (7928e55), appendix A v1.1 (303eed2).

The schema boundary owns shapes and controlled sets; closure 1 owns reference
resolution. Only the nineteen explicitly frozen endpoints have a target type.
"""
from ._primitives import (
    _digest, _fail, _ref_key, _require_any, _str, git_format,
)
from ._registry import register_closure


_LOCATED_TEXT_FIELDS = frozenset({"status", "text", "value", "evidence_refs", "reason"})
# All LocatedText-bearing roots in appendix A. Recurse within these exact
# fields, not arbitrary JSON such as canonical_value or frozen wrappers.
_LOCATED_ROOTS = {
    "fact_revision": ("value_unit", "qualifiers", "valid_from", "valid_to"),
}

# Fifteen endpoints, plus the four subtype-specific endpoints below. Dotted
# paths follow exact objects, never arbitrary ref/ref[] payloads.
_ENDPOINTS = {
    "entity_revision": (("predecessor_ref", "entity_revision"),
                        ("disambiguation_issue_ref", "issue_event")),
    "fact_revision": (("entity_ref", "entity_revision"),
                      ("predecessor_ref", "fact_revision")),
    "source_version": (("prior_version_ref", "source_version"),),
    "source_lifecycle": (("predecessor_ref", "source_lifecycle"),
                         ("version_scope.version_ref", "source_version")),
    "evidence": (("source_version_ref", "source_version"),),
    "link_revision": (("predecessor_ref", "link_revision"),
                      ("superseded_by", "link_revision")),
    "placement_revision": (("fact_ref", "fact_revision"),
                           ("predecessor_ref", "placement_revision")),
    "issue_event": (("predecessor_event_ref", "issue_event"),),
    "proposal_event": (("predecessor_event_ref", "proposal_event"),
                       ("execution_operation_ref", "operation")),
}


def _located_bindings(value, family, path, field):
    if isinstance(value, dict):
        if set(value) == _LOCATED_TEXT_FIELDS:
            # _located_text already checked all four statuses and text types.
            # Non-known text may retain an explanation; its value stays null.
            known = value["status"] == "known"
            if known and (type(value["text"]) is not str or not value["text"]):
                _fail(family, path, "located_text_binding", field + ".text")
            if known == (value["value"] is None):
                _fail(family, path, "located_text_binding", field + ".value")
            return
        for name, item in value.items():
            _located_bindings(item, family, path, f"{field}.{name}" if field else name)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _located_bindings(item, family, path, f"{field}[{i}]")


@register_closure(3)
def _closure_3(ordered, index):
    def endpoint(ref, expected, family, path, field):
        if ref is not None:
            key = _ref_key(ref, family, path, field)
            target = ordered[index[key]]
            if target.get("record_type") != expected:
                _fail(family, path, "ref_endpoint_type", field)

    for path, payload in ordered.items():
        family = git_format.canonical_record_type(path)
        for field in _LOCATED_ROOTS.get(family, ()):
            _located_bindings(payload[field], family, path, field)
        for field, expected in _ENDPOINTS.get(family, ()):
            ref = payload
            for name in field.split("."):
                ref = ref[name]
            endpoint(ref, expected, family, path, field)

        if family == "evidence":
            if payload["quote_excerpt_ref"] is not None:
                _digest(payload["quote_digest"], family, path, "quote_digest")
            _require_any(("original_hash", "mapped_fingerprint"))(
                payload, family, path, "original_hash")
        elif family == "link_revision":
            link = payload["link"]
            if link["subtype"] == "fact_evidence":
                endpoint(link["fact_ref"], "fact_revision", family, path,
                         "link.fact_evidence.fact_ref")
                if link["statement_or_field"] == "field":
                    _str(link["field"], family, path, "link.fact_evidence.field")
                elif link["field"] is not None:
                    _fail(family, path, "statement_field_binding", "link.fact_evidence.field")
            elif link["subtype"] == "fact_lineage":
                for i, edge in enumerate(link["edges"]):
                    field = f"link.fact_lineage.edges[{i}]"
                    if (edge["lineage_type"] == "retract") != (edge["child_ref"] is None):
                        _fail(family, path, "lineage_child_ref", field + ".child_ref")
                    endpoint(edge["parent_ref"], "fact_revision", family, path,
                             field + ".parent_ref")
                    endpoint(edge["child_ref"], "fact_revision", family, path,
                             field + ".child_ref")
            elif link["subtype"] == "source_dependency":
                endpoint(link["source_version_ref"], "source_version", family, path,
                         "link.source_dependency.source_version_ref")
