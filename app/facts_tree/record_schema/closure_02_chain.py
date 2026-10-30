"""Pure version/current chains, appendix B v1.1 (7928e55), section 3 only."""

from ._primitives import _fail, _ref_key, git_format
from ._registry import register_closure


_REVISIONS = frozenset({
    "entity_revision", "fact_revision", "link_revision",
    "placement_revision", "source_lifecycle",
})
_EVENTS = frozenset({"issue_event", "proposal_event"})
_CURRENTS = {
    "entity_current": "entity_revision",
    "fact_current": "fact_revision",
    "placement_current": "placement_revision",
}


def _chain(family, uid, records, ordered, index):
    field = ("prior_version_ref" if family == "source_version" else
             "predecessor_event_ref" if family in _EVENTS else "predecessor_ref")
    fork_code = ("source_version_chain_fork" if family == "source_version" else
                 "event_chain_fork" if family in _EVENTS else "revision_chain_fork")
    cycle_code = "event_chain_cycle" if family in _EVENTS else "revision_chain_cycle"
    predecessors = {}
    successors = {}
    roots = []
    for identity, (path, payload) in records.items():
        ref = payload[field]
        if (family == "fact_revision" and payload["revision_kind"] == "initial"
                and ref is not None):
            _fail(family, path, "initial_with_predecessor", field)
        if ref is None:
            roots.append(path)
            continue
        if ref["record_type"] != family:
            _fail(family, path, "predecessor_wrong_family", field)
        if ref["stable_id"] != uid:
            _fail(family, path, "predecessor_cross_identity", field)
        key = _ref_key(ref, family, path, field)
        if key not in index:
            # Closure 1 owns missing typed references, including predecessors.
            continue
        target = key[2]
        predecessors[identity] = target
        if target in successors:
            _fail(family, path, fork_code, field)
        successors[target] = identity

    # Diagnose cycles before their necessarily invalid integer/seq ordering.
    # Each node is visited once; a long imported history needs no recursion.
    finished = set()
    for identity in records:
        trail = set()
        cursor = identity
        while cursor in predecessors and cursor not in finished:
            if cursor in trail:
                _fail(family, records[cursor][0], cycle_code, field)
            trail.add(cursor)
            cursor = predecessors[cursor]
        finished.update(trail)

    for identity, target in predecessors.items():
        path, payload = records[identity]
        if family in _REVISIONS and target >= identity:
            _fail(family, path, "predecessor_revision_not_earlier", field)
        if family == "source_version":
            target_path = index[(family, uid, target)]
            if ordered[target_path]["seq"] >= payload["seq"]:
                _fail(family, path, "source_seq_not_earlier", field)

    if len(roots) > 1:
        if family == "source_lifecycle":
            _fail(family, roots[1], "lifecycle_double_tail", field)
        if family in _EVENTS:
            _fail(family, roots[1], "event_chain_fork", field)


@register_closure(2)
def _closure_2(ordered, index):
    groups = {}
    currents = []
    for path, payload in ordered.items():
        family = git_format.canonical_record_type(path)
        if family not in _REVISIONS | _EVENTS | {"source_version"} | _CURRENTS.keys():
            continue
        ids = tuple(git_format._match_template(family, path).values())
        uid = ids[0]
        if family in _CURRENTS:
            currents.append((family, uid, path, payload))
        else:
            groups.setdefault((family, uid), {})[ids[1]] = (path, payload)

    for (family, uid), records in groups.items():
        _chain(family, uid, records, ordered, index)
    for family, uid, path, payload in currents:
        revisions = groups.get((_CURRENTS[family], uid), {})
        revision = payload["revision"]
        if revision not in revisions:
            _fail(family, path, "current_dangling", "revision")
        if revision != max(revisions):
            _fail(family, path, "current_not_head", "revision")
