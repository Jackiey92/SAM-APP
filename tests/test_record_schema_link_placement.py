"""Appendix A v1.1 §§0/8–10/13; no cross-record business assertions."""
from copy import deepcopy
from decimal import Decimal
from inspect import Parameter, signature

import pytest

from app.facts_tree import git_format, record_schema
from app.facts_tree.fact_identity import EVENT_RELATIONS
from app.facts_tree.source_registry import EVIDENCE_RELATIONS
from app.facts_tree.record_schema import link_placement as plugin


SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
COMMON = {"schema_version", "format_version", "record_type", "repo_id", "company_id"}
# Independent literal transcription of Appendix A, rather than plugin specs.
EXPECTED = {
    "link_revision": COMMON | {
        "link_id", "revision", "predecessor_ref", "link", "status", "evidence_refs",
        "actor", "reason", "event_time", "superseded_by",
    },
    "placement_revision": COMMON | {
        "fact_uid", "revision", "fact_ref", "predecessor_ref", "pack_path_id",
        "template_pack", "pack_version", "manifest_sha256", "binding_revision",
        "trunk_code", "leaf_code", "action", "group_key", "order_key", "actor",
        "reason", "migration_ref", "approval_use_refs", "event_time",
    },
    "placement_current": COMMON | {"fact_uid", "revision"},
}
FAMILIES = tuple(EXPECTED)
FACT_REF = {"record_type": "fact_revision", "stable_id": "fact-test", "revision": 1}
BRANCHES = {
    "fact_evidence": {
        "subtype": "fact_evidence", "role": "supports", "fact_ref": FACT_REF,
        "statement_or_field": "statement", "field": None,
    },
    "fact_lineage": {
        "subtype": "fact_lineage", "edges": [
            {"parent_ref": FACT_REF, "child_ref": None, "lineage_type": "retract"}],
    },
    "source_dependency": {
        "subtype": "source_dependency",
        "source_version_ref": {"record_type": "source_version", "stable_id": "src",
                               "revision": "version-test"},
        "basis_status": "unknown", "dependency_ref": None,
    },
}


def sample(family):
    return record_schema.get_sample_builder(family)()


def path_for(family, payload=None):
    payload = sample(family) if payload is None else payload
    id_name = "link_id" if family == "link_revision" else "fact_uid"
    ids = {id_name: payload[id_name]}
    if family != "placement_current":
        ids["revision"] = payload["revision"]
    return git_format.record_path(family, **ids)


def validate(payload, path=None):
    family = payload["record_type"]
    return record_schema.validate_record_schema(
        path or path_for(family), payload, **SCOPE)


def rejected(payload, code=None, path=None):
    original = deepcopy(payload)
    with pytest.raises(record_schema.RecordSchemaClosureError) as caught:
        validate(payload, path)
    if code is not None:
        assert caught.value.code == code
    assert caught.value.family in FAMILIES
    assert caught.value.field
    assert payload == original


@pytest.mark.parametrize("family", FAMILIES)
def test_registered_fields_sample_signature_and_canonical_roundtrip(family):
    builder = record_schema.get_sample_builder(family)
    payload = builder()
    assert record_schema.SCHEMA_FIELDS[family] == EXPECTED[family] == set(payload)
    assert {k: payload[k] for k in SCOPE} == SCOPE
    id_name = "link_id" if family == "link_revision" else "fact_uid"
    parameters = signature(builder).parameters
    assert set(parameters) == ({id_name} if family == "placement_current"
                               else {id_name, "revision"})
    assert all(p.kind == Parameter.KEYWORD_ONLY and p.default != Parameter.empty
               for p in parameters.values())
    original = deepcopy(payload)
    for object_format in ("sha1", "sha256"):
        assert record_schema.validate_record_schema(
            path_for(family), payload, **SCOPE, object_format=object_format) is None
    assert record_schema.loads_schema_record(
        git_format.dumps_record(payload), path=path_for(family), **SCOPE) == payload
    assert payload == original
    with pytest.raises(TypeError):
        builder(company_id="foreign")


@pytest.mark.parametrize("family", FAMILIES)
def test_builder_is_fresh_at_every_nested_level(family):
    first = sample(family)
    original = deepcopy(first)

    def mutate(value):
        if isinstance(value, dict):
            for item in tuple(value.values()):
                mutate(item)
            value["changed"] = True
        elif isinstance(value, list):
            for item in value:
                mutate(item)
            value.append("changed")

    mutate(first)
    assert sample(family) == original


@pytest.mark.parametrize("family", FAMILIES)
def test_builder_identity_override_and_self_reference(family):
    id_name = "link_id" if family == "link_revision" else "fact_uid"
    ids = {id_name: "custom/二"}
    if family != "placement_current":
        ids["revision"] = 7
    payload = record_schema.get_sample_builder(family)(**ids)
    assert validate(payload, path_for(family, payload)) is None
    if family == "link_revision":
        assert payload["link"]["subtype"] == "source_dependency"
        assert payload["link"]["basis_status"] == "unknown"
        ref = payload["link"]["source_version_ref"]
    elif family == "placement_revision":
        ref = payload["fact_ref"]
    else:
        assert payload["revision"] == 1
        return
    assert ref == {"record_type": family, "stable_id": "custom/二", "revision": 7}


@pytest.mark.parametrize("family,field", [
    (family, field) for family, fields in EXPECTED.items() for field in sorted(fields)
])
def test_every_top_level_key_is_required(family, field):
    payload = sample(family)
    path = path_for(family)
    del payload[field]
    # record_type is also the helper's selector, so call the public boundary.
    with pytest.raises(record_schema.RecordSchemaClosureError):
        record_schema.validate_record_schema(path, payload, **SCOPE)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("field", ["unknown", "pack_id", "binding", "binding_version",
                                    "placement_seq", "base_type"])
def test_unknown_and_sqlite_legacy_fields_rejected(family, field):
    payload = sample(family)
    payload[field] = "legacy"
    rejected(payload, "unknown_field")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("field", ["schema_version", "format_version", "revision"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, "1", None, Decimal("1"), 1.0])
def test_positive_integer_fields_refuse_coercion(family, field, bad):
    payload = sample(family)
    payload[field] = bad
    rejected(payload)


@pytest.mark.parametrize("field", ["pack_version", "binding_revision"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, "1", None, Decimal("1"), 1.0])
def test_placement_versions_are_positive_builtin_integers(field, bad):
    payload = sample("placement_revision")
    payload[field] = bad
    rejected(payload, "record_encoding" if type(bad) is float else "positive_integer")


@pytest.mark.parametrize("bad", ["", "a" * 63, "a" * 65, "g" * 64, "A" * 64,
                                 "0x" + "a" * 62, None, 64, True])
def test_manifest_is_exact_lowercase_sha256(bad):
    payload = sample("placement_revision")
    payload["manifest_sha256"] = bad
    rejected(payload, "sha256_digest")


TEXT_FIELDS = {
    "link_revision": ("link_id", "repo_id", "company_id", "reason"),
    "placement_revision": ("fact_uid", "repo_id", "company_id", "pack_path_id",
                           "template_pack", "trunk_code", "leaf_code", "group_key",
                           "order_key", "reason"),
    "placement_current": ("fact_uid", "repo_id", "company_id"),
}


@pytest.mark.parametrize("family,field", [
    (family, field) for family, fields in TEXT_FIELDS.items() for field in fields
])
@pytest.mark.parametrize("bad", ["", None, True, 1, [], {}])
def test_required_strings_and_ids(family, field, bad):
    payload = sample(family)
    payload[field] = bad
    rejected(payload)


@pytest.mark.parametrize("family", FAMILIES)
def test_path_and_scope_must_match(family):
    for field in ("link_id" if family == "link_revision" else "fact_uid",
                  "repo_id", "company_id", "record_type"):
        payload = sample(family)
        payload[field] = "foreign"
        with pytest.raises(record_schema.RecordSchemaClosureError):
            record_schema.validate_record_schema(path_for(family), payload, **SCOPE)
    if family != "placement_current":
        payload = sample(family)
        payload["revision"] = 2
        rejected(payload, "path_identity")


@pytest.mark.parametrize("family", FAMILIES[:2])
@pytest.mark.parametrize("bad", [None, {}, [], "actor", {"schema_version": 1}])
def test_actor_uses_host_shape(family, bad):
    payload = sample(family)
    payload["actor"] = bad
    rejected(payload, "actor_audit_ref")


@pytest.mark.parametrize("family", FAMILIES[:2])
@pytest.mark.parametrize("bad", [True, False, -1, "0", None, Decimal("0"), 0.0])
def test_event_time_is_nonnegative_builtin_unix_seconds(family, bad):
    payload = sample(family)
    payload["event_time"] = bad
    rejected(payload, "record_encoding" if type(bad) is float else "timestamp")


@pytest.mark.parametrize("subtype", tuple(BRANCHES))
def test_each_union_branch_passes_single_record_without_resolving_refs(subtype):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES[subtype])
    assert validate(payload) is None


@pytest.mark.parametrize("subtype,field", [
    (subtype, field) for subtype, branch in BRANCHES.items() for field in branch
])
def test_each_union_key_required(subtype, field):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES[subtype])
    del payload["link"][field]
    rejected(payload, "missing_field")


@pytest.mark.parametrize("subtype", tuple(BRANCHES))
@pytest.mark.parametrize("extra", ["unknown", "from", "to", "status"])
def test_union_branch_keys_are_closed(subtype, extra):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES[subtype])
    payload["link"][extra] = "extra"
    rejected(payload, "unknown_field")


@pytest.mark.parametrize("bad", ["unknown", "", None, True, [], {}])
def test_unknown_subtype_has_stable_error(bad):
    payload = sample("link_revision")
    payload["link"]["subtype"] = bad
    rejected(payload, "unknown_subtype")


@pytest.mark.parametrize("subtype,field,allowed", [
    ("fact_evidence", "role", ("supports", "contradicts", "references")),
    ("fact_evidence", "statement_or_field", ("statement", "field")),
    ("source_dependency", "basis_status", ("known", "unknown")),
])
def test_union_controlled_values(subtype, field, allowed):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES[subtype])
    for value in allowed:
        payload["link"][field] = value
        assert validate(payload) is None
    for value in ("illegal", "", None, True, [], {}):
        payload["link"][field] = value
        rejected(payload, "enum_value")


@pytest.mark.parametrize("family,field,allowed", [
    ("link_revision", "status", ("active", "superseded", "retracted")),
    ("placement_revision", "action", ("initial", "reassign")),
])
def test_top_level_controlled_values(family, field, allowed):
    payload = sample(family)
    for value in allowed:
        payload[field] = value
        assert validate(payload) is None
    for value in ("illegal", "", None, True, [], {}):
        payload[field] = value
        rejected(payload, "enum_value")


@pytest.mark.parametrize("lineage_type", ["correction", "split", "merge", "migration", "retract"])
@pytest.mark.parametrize("child_ref", [None, FACT_REF])
def test_lineage_does_not_enforce_cross_field_cardinality(lineage_type, child_ref):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_lineage"])
    payload["link"]["edges"][0].update(
        lineage_type=lineage_type, child_ref=deepcopy(child_ref))
    assert validate(payload) is None


@pytest.mark.parametrize("field,bad", [
    ("edges", None), ("edges", {}), ("edges", [None]),
    ("lineage_type", "supersedes"), ("lineage_type", None),
    ("lineage_type", []), ("parent_ref", None),
])
def test_lineage_bad_shapes_and_type(field, bad):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_lineage"])
    target = payload["link"] if field == "edges" else payload["link"]["edges"][0]
    target[field] = bad
    rejected(payload)


@pytest.mark.parametrize("field", ["parent_ref", "child_ref", "lineage_type"])
def test_lineage_edge_keys_required(field):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_lineage"])
    del payload["link"]["edges"][0][field]
    rejected(payload, "missing_field")


def test_lineage_edge_unknown_key_rejected_and_empty_array_allowed():
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_lineage"])
    payload["link"]["edges"][0]["extra"] = True
    rejected(payload, "unknown_field")
    payload["link"]["edges"] = []
    assert validate(payload) is None


REF_SLOTS = [
    ("link_revision", None, ("predecessor_ref",)),
    ("link_revision", None, ("superseded_by",)),
    ("link_revision", "fact_evidence", ("link", "fact_ref")),
    ("link_revision", "fact_lineage", ("link", "edges", 0, "parent_ref")),
    ("link_revision", "fact_lineage", ("link", "edges", 0, "child_ref")),
    ("link_revision", "source_dependency", ("link", "source_version_ref")),
    ("link_revision", "source_dependency", ("link", "dependency_ref")),
    ("placement_revision", None, ("fact_ref",)),
    ("placement_revision", None, ("predecessor_ref",)),
    ("placement_revision", None, ("migration_ref",)),
]
BAD_REFS = [
    {}, [], "fact-test", {"record_type": "fact_revision"},
    {"stable_id": "fact-test"}, {**FACT_REF, "extra": "x"},
    {**FACT_REF, "record_type": "unknown"}, {**FACT_REF, "record_type": []},
    {**FACT_REF, "stable_id": ""}, {**FACT_REF, "stable_id": "HEAD"},
    {**FACT_REF, "stable_id": "current"}, {**FACT_REF, "stable_id": "latest"},
    *[{**FACT_REF, "revision": bad} for bad in (True, 0, -1, None, "latest", [], {})],
]


@pytest.mark.parametrize("family,subtype,slot", REF_SLOTS)
@pytest.mark.parametrize("bad", BAD_REFS)
def test_all_single_ref_slots_reject_bad_shapes(family, subtype, slot, bad):
    payload = sample(family)
    if subtype:
        payload["link"] = deepcopy(BRANCHES[subtype])
    target = payload
    for key in slot[:-1]:
        target = target[key]
    target[slot[-1]] = deepcopy(bad)
    rejected(payload)


@pytest.mark.parametrize("family,subtype,slot", REF_SLOTS)
def test_single_refs_accept_shape_without_target_inference(family, subtype, slot):
    payload = sample(family)
    if subtype:
        payload["link"] = deepcopy(BRANCHES[subtype])
    target = payload
    for key in slot[:-1]:
        target = target[key]
    for ref in (FACT_REF, {"record_type": "evidence", "stable_id": "evidence-test"},
                {"record_type": "source_version", "stable_id": "src", "revision": "v1"}):
        target[slot[-1]] = deepcopy(ref)
        assert validate(payload) is None


@pytest.mark.parametrize("bad", [None, {}, (), [FACT_REF, FACT_REF], [{}]])
def test_evidence_refs_array_shape_and_uniqueness(bad):
    payload = sample("link_revision")
    payload["evidence_refs"] = bad
    rejected(payload)


def test_evidence_refs_accepts_empty_or_distinct_refs():
    payload = sample("link_revision")
    for refs in ([], [FACT_REF], [FACT_REF, {**FACT_REF, "revision": 2}]):
        payload["evidence_refs"] = deepcopy(refs)
        assert validate(payload) is None


@pytest.mark.parametrize("bad", [None, {}, (), ["approval"], [FACT_REF]])
def test_approval_refs_fail_closed_until_host_schema_published(bad):
    payload = sample("placement_revision")
    payload["approval_use_refs"] = bad
    rejected(payload)


@pytest.mark.parametrize("bad", ["", True, 1, [], {}])
def test_fact_evidence_optional_field_still_has_string_type(bad):
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_evidence"])
    payload["link"]["field"] = bad
    rejected(payload, "nonempty_string")


def test_field_binding_is_deferred_and_controlled_sets_reuse_authority():
    payload = sample("link_revision")
    payload["link"] = deepcopy(BRANCHES["fact_evidence"])
    payload["link"].update(statement_or_field="field", field=None)
    assert validate(payload) is None
    payload["link"]["field"] = "statement_raw"
    assert validate(payload) is None
    assert plugin.EVIDENCE_RELATIONS is EVIDENCE_RELATIONS
    assert plugin.EVENT_RELATIONS is EVENT_RELATIONS
    assert plugin.LINEAGE_TYPES == frozenset({"correction", "split", "merge", "migration", "retract"})
    assert plugin.PLACEMENT_ACTIONS == frozenset({"initial", "reassign"})


@pytest.mark.parametrize("family,subtype,slot", [
    entry for entry in REF_SLOTS if entry[2][-1] in {
        "predecessor_ref", "superseded_by", "child_ref", "dependency_ref", "migration_ref"}
])
def test_nullable_ref_keys_accept_explicit_null(family, subtype, slot):
    payload = sample(family)
    if subtype:
        payload["link"] = deepcopy(BRANCHES[subtype])
    target = payload
    for key in slot[:-1]:
        target = target[key]
    target[slot[-1]] = None
    assert validate(payload) is None


@pytest.mark.parametrize("bad", [None, [], "source_dependency", True])
def test_link_requires_object(bad):
    payload = sample("link_revision")
    payload["link"] = bad
    rejected(payload, "object_type")


@pytest.mark.parametrize("subtype", tuple(BRANCHES))
def test_keys_from_other_union_branches_are_unknown(subtype):
    payload = sample("link_revision")
    for other, branch in BRANCHES.items():
        if other == subtype:
            continue
        payload["link"] = deepcopy(BRANCHES[subtype])
        for key in branch.keys() - BRANCHES[subtype].keys():
            payload["link"][key] = deepcopy(branch[key])
        rejected(payload, "unknown_field")
