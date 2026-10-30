"""Appendix B v1.1 (7928e55), §5: active facts need a primary placement.

Only rule 4.1 adds a rejection. Source exit preserves historical evidence
(4.4); support counting, template matching and external objects are deferred.
"""
from ._primitives import _fail, git_format
from ._registry import register_closure
from .entity_fact import FACT_CURRENT_STATUSES


_ACTIVE_STATUS = next(status for status in FACT_CURRENT_STATUSES if status == "active")


@register_closure(4)
def _closure_4(ordered, index):
    for path, payload in ordered.items():
        if git_format.canonical_record_type(path) != "fact_current":
            continue
        if payload["status"] != _ACTIVE_STATUS:
            continue
        fact_uid = payload["fact_uid"]
        current_path = git_format.record_path("placement_current", fact_uid=fact_uid)
        current = ordered.get(current_path)
        if current is None:
            _fail("fact_current", path, "active_fact_without_primary_placement", "fact_uid")
        revision_path = git_format.record_path(
            "placement_revision", fact_uid=fact_uid, revision=current["revision"])
        if revision_path not in ordered:
            _fail("fact_current", path, "active_fact_without_primary_placement", "fact_uid")
