"""Closure 3 mutations of the real full-family fixture (appendix B §4)."""
from copy import deepcopy
from hashlib import sha256

import pytest

from app.facts_tree import git_format
from app.facts_tree.record_schema import (
    CLOSURE_VALIDATORS, RecordSchemaClosureError, validate_record_schema,
    validate_record_schema_closure,
)
from app.facts_tree.record_schema._primitives import _target_index
from app.facts_tree.record_schema.link_placement import LINEAGE_TYPES
from tests.facts_tree_full_fixture import DIGEST, SCOPE, full_tree


def record(tree, family):
    return next((path, payload) for path, payload in tree.items()
                if payload["record_type"] == family)


def ref(tree, family):
    path, _ = record(tree, family)
    ids = tuple(git_format._match_template(family, path).values())
    result = {"record_type": family, "stable_id": ids[0]}
    if len(ids) == 2:
        result["revision"] = ids[1]
    return result


def parts(field):
    return field.replace("[", ".").replace("]", "").split(".")


def get(payload, field):
    for key in parts(field):
        payload = payload[int(key) if isinstance(payload, list) else key]
    return payload


def put(payload, field, value):
    keys = parts(field)
    for key in keys[:-1]:
        payload = payload[int(key) if isinstance(payload, list) else key]
    key = int(keys[-1]) if isinstance(payload, list) else keys[-1]
    payload[key] = deepcopy(value)


def sync_issue_event(tree):
    # The full fixture's broker event hashes its issue output. Preserve closure
    # 1's event-after check when mutating that output to isolate closure 3.
    _, issue = record(tree, "issue_event")
    _, event = record(tree, "event")
    event["after"] = [sha256(git_format.dumps_record(issue)).hexdigest()]


def validate(tree):
    return validate_record_schema_closure(tree, **SCOPE)


def validate_semantics(tree):
    # Isolate closure 3 diagnostics: closure 2 also checks predecessor types.
    # Full-tree positives still use the public entry and all registered closures.
    ordered = {path: tree[path] for path in sorted(tree)}
    for path, payload in ordered.items():
        validate_record_schema(path, payload, **SCOPE)
    index = _target_index(ordered)
    CLOSURE_VALIDATORS[1](ordered, index)
    CLOSURE_VALIDATORS[3](ordered, index)
    return None


def rejected(tree, family, path, code, field):
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_semantics(tree)
    assert (caught.value.family, caught.value.path, caught.value.code, caught.value.field) == (
        family, path, code, field)


def link_branch(tree, subtype):
    path, payload = record(tree, "link_revision")
    if subtype == "fact_lineage":
        payload["link"] = {"subtype": subtype, "edges": [
            {"parent_ref": ref(tree, "fact_revision"),
             "child_ref": ref(tree, "fact_revision"), "lineage_type": "correction"}
            for _ in range(2)]}
    elif subtype == "source_dependency":
        payload["link"] = {"subtype": subtype,
                           "source_version_ref": ref(tree, "source_version"),
                           "basis_status": "unknown", "dependency_ref": None}
    return path, payload


def test_full_tree_registered_public_entry_and_purity():
    assert {1, 3, 5} <= set(CLOSURE_VALIDATORS)
    tree = full_tree()
    before = deepcopy(tree)
    assert validate(tree) is None
    assert validate(dict(reversed(list(tree.items())))) is None
    assert tree == before


def test_empty_tree():
    assert validate({}) is None


LOCATED_FIELDS = (
    "value_unit", "qualifiers.period", "qualifiers.currency", "qualifiers.scope",
    "qualifiers.basis", "qualifiers.claimant", "qualifiers.entity", "valid_from", "valid_to",
)


@pytest.mark.parametrize("field", LOCATED_FIELDS)
@pytest.mark.parametrize("status", ("known", "not_stated", "not_applicable", "ambiguous"))
def test_all_located_fields_accept_four_states(field, status):
    tree = full_tree()
    _, fact = record(tree, "fact_revision")
    get(fact, field).update(status=status, text="原文说明",
                            value=0 if status == "known" else None)
    assert validate(tree) is None


@pytest.mark.parametrize("field", LOCATED_FIELDS)
@pytest.mark.parametrize("missing", ("text", "value"))
def test_known_requires_text_and_value(field, missing):
    tree = full_tree()
    path, fact = record(tree, "fact_revision")
    get(fact, field).update(status="known", text="原文", value=False)
    get(fact, field)[missing] = None
    rejected(tree, "fact_revision", path, "located_text_binding", f"{field}.{missing}")


@pytest.mark.parametrize("field", LOCATED_FIELDS)
@pytest.mark.parametrize("status", ("not_stated", "not_applicable", "ambiguous"))
def test_nonknown_cannot_carry_value(field, status):
    tree = full_tree()
    path, fact = record(tree, "fact_revision")
    get(fact, field).update(status=status, text=None, value=0)
    rejected(tree, "fact_revision", path, "located_text_binding", field + ".value")


@pytest.mark.parametrize("value", (0, False, "", [], {}, "全公司", "2026", "CNY"))
def test_known_accepts_nonnull_json_without_keyword_rules(value):
    tree = full_tree()
    _, fact = record(tree, "fact_revision")
    fact["qualifiers"]["scope"].update(status="known", text="全公司", value=value)
    assert validate(tree) is None


@pytest.mark.parametrize("field,value,code", (
    ("text", "", "nonempty_string"), ("text", 1, "nonempty_string"),
    ("status", "invented", "enum_value"), ("status", None, "enum_value"),
))
def test_located_shape_and_enum_still_belong_to_schema(field, value, code):
    tree = full_tree()
    path, fact = record(tree, "fact_revision")
    fact["value_unit"][field] = value
    rejected(tree, "fact_revision", path, code, "value_unit." + field)


def test_arbitrary_json_is_not_inferred_to_be_a_located_text():
    tree = full_tree()
    _, fact = record(tree, "fact_revision")
    fact["canonical_value"] = {"status": "known", "text": None, "value": None,
                               "evidence_refs": [], "reason": None}
    assert validate(tree) is None


@pytest.mark.parametrize("missing", ("conversion_rule_version", "conversion_input_digest"))
def test_conversion_pair_is_still_enforced_by_schema(missing):
    tree = full_tree()
    path, fact = record(tree, "fact_revision")
    fact.update(conversion_rule_version="conversion-1", conversion_input_digest=DIGEST)
    fact[missing] = None
    rejected(tree, "fact_revision", path, "require_together", "$." + missing)


def test_conversion_pair_can_be_present_together():
    tree = full_tree()
    _, fact = record(tree, "fact_revision")
    fact.update(conversion_rule_version="conversion-1", conversion_input_digest=DIGEST)
    assert validate(tree) is None


@pytest.mark.parametrize("digest", (None, "A" * 64, "a" * 63, "g" * 64))
def test_quote_reference_requires_lowercase_sha256(digest):
    tree = full_tree()
    path, evidence = record(tree, "evidence")
    evidence["quote_digest"] = digest
    rejected(tree, "evidence", path, "sha256_digest", "quote_digest")


@pytest.mark.parametrize("digest", (None, DIGEST))
def test_no_quote_reference_does_not_require_or_forbid_digest(digest):
    tree = full_tree()
    _, evidence = record(tree, "evidence")
    evidence.update(quote_excerpt_ref=None, quote_digest=digest)
    assert validate(tree) is None


@pytest.mark.parametrize("original,mapped", ((DIGEST, None), (None, DIGEST), (DIGEST, DIGEST)))
def test_evidence_accepts_either_hash(original, mapped):
    tree = full_tree()
    _, evidence = record(tree, "evidence")
    evidence.update(original_hash=original, mapped_fingerprint=mapped)
    assert validate(tree) is None


def test_evidence_requires_one_hash_even_with_legacy_fingerprint():
    tree = full_tree()
    path, evidence = record(tree, "evidence")
    evidence.update(original_hash=None, mapped_fingerprint=None,
                    legacy_normalized_fingerprint=DIGEST, legacy_fingerprint_algorithm="sha256")
    rejected(tree, "evidence", path, "require_any", "original_hash")


@pytest.mark.parametrize("lineage_type", sorted(LINEAGE_TYPES))
def test_lineage_child_cardinality_positive(lineage_type):
    tree = full_tree()
    _, link = link_branch(tree, "fact_lineage")
    edge = link["link"]["edges"][1]
    edge.update(lineage_type=lineage_type,
                child_ref=None if lineage_type == "retract" else ref(tree, "fact_revision"))
    assert validate(tree) is None


@pytest.mark.parametrize("lineage_type", sorted(LINEAGE_TYPES))
def test_lineage_child_cardinality_negative_at_second_edge(lineage_type):
    tree = full_tree()
    path, link = link_branch(tree, "fact_lineage")
    edge = link["link"]["edges"][1]
    edge.update(lineage_type=lineage_type,
                child_ref=ref(tree, "fact_revision") if lineage_type == "retract" else None)
    rejected(tree, "link_revision", path, "lineage_child_ref",
             "link.fact_lineage.edges[1].child_ref")


@pytest.mark.parametrize("mode,field", (("statement", None), ("field", "qualifiers.period")))
def test_fact_evidence_field_binding_positive(mode, field):
    tree = full_tree()
    _, link = record(tree, "link_revision")
    link["link"].update(statement_or_field=mode, field=field)
    assert validate(tree) is None


@pytest.mark.parametrize("mode,field,code", (
    ("field", None, "nonempty_string"), ("statement", "value_unit", "statement_field_binding"),
))
def test_fact_evidence_field_binding_negative(mode, field, code):
    tree = full_tree()
    path, link = record(tree, "link_revision")
    link["link"].update(statement_or_field=mode, field=field)
    rejected(tree, "link_revision", path, code, "link.fact_evidence.field")


# Independent transcription of the nineteen appendix B §4.3 whitelist rows:
# family, actual payload path, target family, optional link branch, diagnostic.
ENDPOINTS = (
    ("entity_revision", "predecessor_ref", "entity_revision", None, "predecessor_ref"),
    ("entity_revision", "disambiguation_issue_ref", "issue_event", None, "disambiguation_issue_ref"),
    ("fact_revision", "entity_ref", "entity_revision", None, "entity_ref"),
    ("fact_revision", "predecessor_ref", "fact_revision", None, "predecessor_ref"),
    ("source_version", "prior_version_ref", "source_version", None, "prior_version_ref"),
    ("source_lifecycle", "predecessor_ref", "source_lifecycle", None, "predecessor_ref"),
    ("source_lifecycle", "version_scope.version_ref", "source_version", None, "version_scope.version_ref"),
    ("evidence", "source_version_ref", "source_version", None, "source_version_ref"),
    ("link_revision", "predecessor_ref", "link_revision", None, "predecessor_ref"),
    ("link_revision", "superseded_by", "link_revision", None, "superseded_by"),
    ("link_revision", "link.fact_ref", "fact_revision", None, "link.fact_evidence.fact_ref"),
    ("link_revision", "link.edges[1].parent_ref", "fact_revision", "fact_lineage", "link.fact_lineage.edges[1].parent_ref"),
    ("link_revision", "link.edges[1].child_ref", "fact_revision", "fact_lineage", "link.fact_lineage.edges[1].child_ref"),
    ("link_revision", "link.source_version_ref", "source_version", "source_dependency", "link.source_dependency.source_version_ref"),
    ("placement_revision", "fact_ref", "fact_revision", None, "fact_ref"),
    ("placement_revision", "predecessor_ref", "placement_revision", None, "predecessor_ref"),
    ("issue_event", "predecessor_event_ref", "issue_event", None, "predecessor_event_ref"),
    ("proposal_event", "predecessor_event_ref", "proposal_event", None, "predecessor_event_ref"),
    ("proposal_event", "execution_operation_ref", "operation", None, "execution_operation_ref"),
)


@pytest.mark.parametrize("family,field,target,subtype,diagnostic", ENDPOINTS)
@pytest.mark.parametrize("wrong_type", (False, True))
def test_every_whitelisted_endpoint_type(family, field, target, subtype, diagnostic, wrong_type):
    tree = full_tree()
    if subtype:
        link_branch(tree, subtype)
    path, payload = record(tree, family)
    wrong_family = ("evidence" if target == "entity_revision" else
                    "issue_event" if target == "operation" else "entity_revision")
    put(payload, field, ref(tree, wrong_family if wrong_type else target))
    sync_issue_event(tree)
    if wrong_type:
        rejected(tree, family, path, "ref_endpoint_type", diagnostic)
    else:
        # A self predecessor has the right endpoint type; chain legality is a
        # separate closure 2 concern, outside this directed type assertion.
        assert validate_semantics(tree) is None


# Bare refs deliberately point to other families. Result shapes retain their
# pre-existing terminal/nonterminal schema restrictions, independently of §4.3.
POLYMORPHIC = (
    ("entity_revision", "aliases[0].source_refs", True, "entity_revision"),
    ("entity_revision", "lineage.target_refs", True, "evidence"),
    ("fact_revision", "primary_evidence_refs", True, "entity_revision"),
    ("fact_revision", "field_evidence[0].evidence_refs", True, "entity_revision"),
    ("fact_revision", "qualification_basis_refs", True, "entity_revision"),
    ("fact_revision", "confirmation_basis_refs", True, "entity_revision"),
    ("fact_revision", "qualifiers.period.evidence_refs", True, "entity_revision"),
    ("source_version", "mapped_object_ref", False, "entity_revision"),
    ("evidence", "quote_excerpt_ref", False, "entity_revision"),
    ("link_revision", "evidence_refs", True, "entity_revision"),
    ("link_revision", "link.dependency_ref", False, "entity_revision"),
    ("placement_revision", "migration_ref", False, "entity_revision"),
    ("issue_event", "candidate_refs", True, "entity_revision"),
    ("issue_event", "fact_refs", True, "entity_revision"),
    ("issue_event", "conflict_evidence_refs", True, "entity_revision"),
    ("issue_event", "disposition_result.result_refs", True, "evidence"),
    ("issue_event", "affected_refs", True, "entity_revision"),
    ("proposal_event", "input_impact.input_refs", True, "entity_revision"),
    ("proposal_event", "input_impact.affected_refs", True, "entity_revision"),
    ("proposal_event", "disposition_result.result_refs", True, "issue_event"),
    ("proposal_event", "affected_refs", True, "entity_revision"),
)


@pytest.mark.parametrize("family,field,array,target", POLYMORPHIC)
def test_bare_refs_are_not_given_endpoint_types(family, field, array, target):
    tree = full_tree()
    _, payload = record(tree, family)
    if field.startswith("aliases"):
        payload["aliases"] = [{"alias": "别名", "kind": None, "source_refs": [], "basis": None}]
    elif field.startswith("lineage"):
        payload["lineage"] = {"kind": "merge", "target_refs": []}
    elif field.startswith("field_evidence"):
        payload["field_evidence"] = [{"field": "value_unit", "evidence_refs": [],
                                       "mapped_from": None, "mapping_basis": None}]
    elif field == "link.dependency_ref":
        link_branch(tree, "source_dependency")
    target_ref = ref(tree, target)
    put(payload, field, [target_ref] if array else target_ref)
    sync_issue_event(tree)
    assert validate(tree) is None


def test_unresolved_bare_ref_still_belongs_to_closure_one():
    tree = full_tree()
    path, fact = record(tree, "fact_revision")
    fact["primary_evidence_refs"] = [{"record_type": "entity_revision",
                                     "stable_id": "absent", "revision": 1}]
    rejected(tree, "fact_revision", path, "unresolved_typed_ref", "$.primary_evidence_refs[0]")


@pytest.mark.parametrize("status", ("pending_removal", "removed"))
def test_historical_evidence_survives_source_exit(status):
    tree = full_tree()
    _, lifecycle = record(tree, "source_lifecycle")
    lifecycle["status"] = status
    assert validate(tree) is None
