"""Appendix A v1.1 §§0–4/13 contract tests, independent of R1 fixtures."""
from copy import deepcopy
from decimal import Decimal
import inspect

import pytest

from app.facts_tree import git_format
from app.facts_tree.record_schema import (
    RecordSchemaClosureError, SCHEMA_BUILDERS, SCHEMA_FIELDS, SCHEMA_VALIDATORS,
    loads_schema_record, validate_record_schema,
)


SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
COMMON = {"schema_version", "format_version", "record_type", "repo_id", "company_id"}
# Literal authority-side expectations, deliberately independent of plugin specs.
BUSINESS_FIELDS = {
    "entity_revision": {
        "entity_uid", "revision", "entity_kind", "raw_name", "canonical_name",
        "aliases", "status", "predecessor_ref", "lineage", "disambiguation_issue_ref",
        "actor", "reason", "event_time",
    },
    "entity_current": {"entity_uid", "revision", "status"},
    "fact_revision": {
        "fact_uid", "revision", "entity_ref", "statement_raw", "value_raw", "value_type",
        "canonical_value", "value_unit", "conversion_rule_version", "conversion_input_digest",
        "statement_kind", "polarity", "qualifiers", "primary_evidence_refs", "field_evidence",
        "unresolved", "qualification_basis_refs", "confirmation_basis_refs", "predecessor_ref",
        "valid_from", "valid_to", "revision_kind", "actor", "reason", "event_time",
    },
    "fact_current": {"fact_uid", "revision", "status"},
}
FAMILIES = tuple(BUSINESS_FIELDS)
PATH_IDS = {
    "entity_revision": {"entity_uid": "entity-1", "revision": 1},
    "entity_current": {"entity_uid": "entity-1"},
    "fact_revision": {"fact_uid": "output-1", "revision": 1},
    "fact_current": {"fact_uid": "output-1"},
}
QUALIFIERS = ("period", "currency", "scope", "basis", "claimant", "entity")
ENUMS = {
    ("entity_revision", "entity_kind"): ("company", "organization", "person", "other"),
    ("entity_revision", "status"): ("first_seen", "active", "merged", "retracted"),
    ("entity_current", "status"): ("first_seen", "active", "merged", "retracted"),
    ("fact_current", "status"): ("active", "superseded", "retracted"),
    ("fact_revision", "revision_kind"): ("correct", "supersede", "retract", "lineage", "initial"),
    ("fact_revision", "value_type"): ("number", "text", "boolean", "date", "range"),
    ("fact_revision", "statement_kind"): ("event", "state", "plan", "forecast", "opinion", "unknown"),
    ("fact_revision", "polarity"): ("affirmed", "negated", "uncertain"),
}


def sample(family):
    return SCHEMA_BUILDERS[family]()


def path_for(family):
    return git_format.record_path(family, **PATH_IDS[family])


def validate(family, payload):
    return validate_record_schema(path_for(family), payload, **SCOPE)


def reject(family, payload, code=None, field=None):
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate(family, payload)
    error = caught.value
    assert error.family == family
    assert error.path == path_for(family)
    if code is not None:
        assert error.code == code
    if field is not None:
        assert error.field == field
    return error


def typed():
    return {"record_type": "fact_revision", "stable_id": "output-1", "revision": 1}


def rich_sample(family):
    payload = sample(family)
    if family == "entity_revision":
        payload.update(
            aliases=[{"alias": "别名", "kind": None, "source_refs": [typed()], "basis": None}],
            lineage={"kind": "merge", "target_refs": [typed()]},
            predecessor_ref=typed(), disambiguation_issue_ref=typed())
    elif family == "fact_revision":
        payload.update(
            field_evidence=[{"field": "attribute", "evidence_refs": [typed()],
                             "mapped_from": None, "mapping_basis": None}],
            unresolved=[{"code": "unspecified", "coordinate": None, "note": None}])
    return payload


def at(payload, parts):
    for part in parts:
        payload = payload[part]
    return payload


def coordinate(parts):
    result = ""
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else ("." if result else "") + part
    return result


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_minimal_sample_and_canonical_roundtrip(family, object_format):
    payload = sample(family)
    original = deepcopy(payload)
    assert set(payload) == SCHEMA_FIELDS[family] == COMMON | BUSINESS_FIELDS[family]
    assert {key: payload[key] for key in SCOPE} == SCOPE
    assert validate_record_schema(path_for(family), payload, **SCOPE,
                                  object_format=object_format) is None
    assert loads_schema_record(git_format.dumps_record(payload), path=path_for(family),
                               **SCOPE, object_format=object_format) == payload
    assert payload == original


@pytest.mark.parametrize("family", FAMILIES)
def test_builder_only_accepts_keyword_path_identities_and_returns_fresh_payload(family):
    builder = SCHEMA_BUILDERS[family]
    parameters = inspect.signature(builder).parameters
    assert set(parameters) == set(PATH_IDS[family])
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY and p.default is not p.empty
               for p in parameters.values())
    for unexpected in ("repo_id", "company_id", "status"):
        with pytest.raises(TypeError):
            builder(**{unexpected: "foreign"})
    with pytest.raises(TypeError):
        builder("positional")
    first = builder()
    expected = deepcopy(first)
    for key in tuple(first):
        first[key] = "mutated"
    assert builder() == expected
    custom_ids = {key: 7 if key == "revision" else "路径/二" for key in PATH_IDS[family]}
    custom = builder(**custom_ids)
    path = git_format.record_path(family, **custom_ids)
    assert git_format._match_template(family, path) == custom_ids
    assert validate_record_schema(path, custom, **SCOPE) is None
    assert {key: custom[key] for key in custom_ids} == custom_ids


def test_fact_sample_self_reference_and_nested_objects_are_fresh():
    payload = sample("fact_revision")
    assert payload["entity_ref"] == typed()
    assert payload["predecessor_ref"] is None
    original = deepcopy(payload)
    for key in ("value_unit", "valid_from", "valid_to"):
        assert payload[key] == {"status": "not_stated", "text": None, "value": None,
                                "evidence_refs": [], "reason": None}
    payload["qualifiers"]["period"]["evidence_refs"].append(typed())
    assert all(payload["qualifiers"][key]["evidence_refs"] == [] for key in QUALIFIERS[1:])
    payload["actor"]["principal_id"] = "changed"
    payload["entity_ref"]["stable_id"] = "changed"
    assert sample("fact_revision") == original
    entity = sample("entity_revision")
    entity["aliases"].append({})
    entity["actor"]["principal_id"] = "changed"
    assert sample("entity_revision")["aliases"] == []
    assert sample("entity_revision")["actor"]["principal_id"] == "owner-test"


@pytest.mark.parametrize("family,field", [
    (family, field) for family in FAMILIES for field in sorted(COMMON | BUSINESS_FIELDS[family])
])
def test_every_key_is_required_even_when_nullable(family, field):
    payload = sample(family)
    del payload[field]
    reject(family, payload)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("field", ["unknown", "confidence", "verified"])
def test_top_level_unknown_fields_fail_closed(family, field):
    payload = sample(family)
    payload[field] = None
    reject(family, payload, "unknown_field")


@pytest.mark.parametrize("field", ["legacy", "row_id", "confirm_mode", "supersedes", "lineage"])
def test_fact_revision_has_no_legacy_or_invented_fields(field):
    payload = sample("fact_revision")
    payload[field] = {}
    reject("fact_revision", payload, "unknown_field")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("value", [True, False, 0, -1, "1", None])
def test_revision_is_a_positive_builtin_integer(family, value):
    payload = sample(family)
    payload["revision"] = value
    reject(family, payload)


@pytest.mark.parametrize("family,field", [
    (family, field) for family in FAMILIES for field in PATH_IDS[family]
])
def test_path_identities_must_equal_template_reverse_match(family, field):
    payload = sample(family)
    payload[field] = 2 if field == "revision" else "different-id"
    reject(family, payload, "path_identity", field)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 0), ("schema_version", 2),
    ("format_version", True), ("format_version", 0),
    ("format_version", git_format.FORMAT_VERSION + 1),
    ("record_type", "unknown"), ("record_type", None),
    ("repo_id", "foreign"), ("company_id", "foreign"),
    ("repo_id", ""), ("company_id", False),
])
def test_common_envelope_is_strict(family, field, value):
    payload = sample(family)
    payload[field] = value
    reject(family, payload, field=field)


@pytest.mark.parametrize("family,field", [
    ("entity_revision", "raw_name"), ("entity_revision", "canonical_name"),
    ("entity_revision", "reason"), ("fact_revision", "statement_raw"),
    ("fact_revision", "reason"),
])
@pytest.mark.parametrize("value", ["", None, True, 1, [], {}])
def test_required_nonempty_strings(family, field, value):
    payload = sample(family)
    payload[field] = value
    reject(family, payload, "nonempty_string", field)


@pytest.mark.parametrize("family,field,value", [
    (family, field, value) for (family, field), values in ENUMS.items() for value in values
])
def test_all_frozen_controlled_values_are_accepted(family, field, value):
    payload = sample(family)
    payload[field] = value
    assert validate(family, payload) is None


@pytest.mark.parametrize("family,field", tuple(ENUMS))
@pytest.mark.parametrize("value", ["unknown-token", "", None, True, 1, [], {}])
def test_controlled_sets_are_closed_and_typed(family, field, value):
    payload = sample(family)
    payload[field] = value
    reject(family, payload, "enum_value", field)


@pytest.mark.parametrize("family", ["entity_revision", "fact_revision"])
@pytest.mark.parametrize("value", [True, False, -1, "1", None, [], {}])
def test_timestamp_type_and_range(family, value):
    payload = sample(family)
    payload["event_time"] = value
    reject(family, payload, "timestamp", "event_time")


@pytest.mark.parametrize("family", ["entity_revision", "fact_revision"])
@pytest.mark.parametrize("change", [None, {}, {"token": "must-not-be-recorded"},
                                     {"member_revision": True}])
def test_actor_uses_host_shape_without_echoing_payload(family, change):
    payload = sample(family)
    if change is None or change == {}:
        payload["actor"] = change
    else:
        payload["actor"].update(change)
    error = reject(family, payload, "actor_audit_ref", "actor")
    assert "must-not-be-recorded" not in str(error)


@pytest.mark.parametrize("family", ["entity_revision", "fact_revision"])
def test_rich_nested_shapes_are_valid_without_resolution_or_inference(family):
    payload = rich_sample(family)
    before = deepcopy(payload)
    payload["event_time"] = 0
    before["event_time"] = 0
    assert validate(family, payload) is None
    assert payload == before
    assert "verified" not in payload


NESTED_OBJECTS = [
    ("entity_revision", ("aliases", 0), ("alias", "kind", "source_refs", "basis")),
    ("entity_revision", ("lineage",), ("kind", "target_refs")),
    ("fact_revision", ("field_evidence", 0), ("field", "evidence_refs", "mapped_from", "mapping_basis")),
    ("fact_revision", ("unresolved", 0), ("code", "coordinate", "note")),
    ("fact_revision", ("qualifiers",), QUALIFIERS),
] + [("fact_revision", parts, ("status", "text", "value", "evidence_refs", "reason"))
     for parts in [("value_unit",), ("valid_from",), ("valid_to",)]
     + [("qualifiers", key) for key in QUALIFIERS]]


@pytest.mark.parametrize("family,parts,field", [
    (family, parts, field) for family, parts, fields in NESTED_OBJECTS for field in fields
])
def test_nested_objects_require_all_keys(family, parts, field):
    payload = rich_sample(family)
    del at(payload, parts)[field]
    reject(family, payload, "missing_field", coordinate((*parts, field)))


@pytest.mark.parametrize("family,parts,fields", NESTED_OBJECTS)
def test_nested_objects_reject_unknown_keys_at_child_coordinate(family, parts, fields):
    payload = rich_sample(family)
    at(payload, parts)["extra"] = None
    reject(family, payload, "unknown_field", coordinate((*parts, "extra")))


@pytest.mark.parametrize("family,parts,fields", NESTED_OBJECTS)
@pytest.mark.parametrize("value", [None, [], "object", 1])
def test_nested_objects_reject_wrong_types(family, parts, fields, value):
    payload = rich_sample(family)
    at(payload, parts[:-1])[parts[-1]] = value
    # lineage alone is nullable by design.
    if parts == ("lineage",) and value is None:
        assert validate(family, payload) is None
    else:
        reject(family, payload, "object_type", coordinate(parts))


@pytest.mark.parametrize("kind", ["merge", "correct", "retract"])
def test_lineage_kind_values(kind):
    payload = rich_sample("entity_revision")
    payload["lineage"]["kind"] = kind
    assert validate("entity_revision", payload) is None


@pytest.mark.parametrize("value", ["initial", "correction", "", None, [], True])
def test_lineage_kind_is_closed(value):
    payload = rich_sample("entity_revision")
    payload["lineage"]["kind"] = value
    reject("entity_revision", payload, "enum_value", "lineage.kind")


NESTED_STRINGS = [
    ("entity_revision", ("aliases", 0, "alias"), False),
    ("entity_revision", ("aliases", 0, "kind"), True),
    ("entity_revision", ("aliases", 0, "basis"), True),
    ("fact_revision", ("field_evidence", 0, "field"), False),
    ("fact_revision", ("field_evidence", 0, "mapped_from"), True),
    ("fact_revision", ("field_evidence", 0, "mapping_basis"), True),
    ("fact_revision", ("unresolved", 0, "code"), False),
    ("fact_revision", ("unresolved", 0, "coordinate"), True),
    ("fact_revision", ("unresolved", 0, "note"), True),
    ("fact_revision", ("value_raw",), True),
]


@pytest.mark.parametrize("family,parts,nullable", NESTED_STRINGS)
@pytest.mark.parametrize("value", [None, "文本", "", 1, True, [], {}])
def test_nested_and_nullable_strings(family, parts, nullable, value):
    payload = rich_sample(family)
    at(payload, parts[:-1])[parts[-1]] = value
    if value == "文本" or (nullable and value is None):
        assert validate(family, payload) is None
    else:
        reject(family, payload, "nonempty_string", coordinate(parts))


LOCATED_PATHS = [(key,) for key in ("value_unit", "valid_from", "valid_to")]
LOCATED_PATHS += [("qualifiers", key) for key in QUALIFIERS]


@pytest.mark.parametrize("parts", LOCATED_PATHS)
@pytest.mark.parametrize("status", ["known", "not_stated", "not_applicable", "ambiguous"])
def test_located_text_four_states_do_not_enable_closure3_binding(parts, status):
    payload = sample("fact_revision")
    located = at(payload, parts)
    located["status"] = status
    # Even known/null and not_stated/non-null remain legal shapes at this layer.
    assert validate("fact_revision", payload) is None
    located.update(text="来源原文", value={"exact": Decimal("1.25")}, reason="定位依据")
    assert validate("fact_revision", payload) is None


@pytest.mark.parametrize("parts", LOCATED_PATHS)
@pytest.mark.parametrize("field,value,code", [
    ("status", "inferred", "enum_value"), ("status", None, "enum_value"),
    ("text", "", "nonempty_string"), ("text", True, "nonempty_string"),
    ("reason", [], "nonempty_string"), ("reason", "", "nonempty_string"),
    ("evidence_refs", None, "array_type"),
])
def test_located_text_member_types(parts, field, value, code):
    payload = sample("fact_revision")
    at(payload, parts)[field] = value
    reject("fact_revision", payload, code, coordinate((*parts, field)))


@pytest.mark.parametrize("value", [None, "原文", True, False, 0, -1, Decimal("1.25"),
                                   {}, {"nested": [None, Decimal("2.5"), True]}, []])
def test_canonical_value_accepts_exact_json_without_value_type_inference(value):
    payload = sample("fact_revision")
    payload["canonical_value"] = value
    assert validate("fact_revision", payload) is None


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), Decimal("NaN"),
                                   {"nested": float("inf")}, {1: "non-string-key"}, (1, 2)])
def test_canonical_value_rejects_noncanonical_json_with_local_diagnostic(value):
    payload = sample("fact_revision")
    payload["canonical_value"] = value
    # Direct plugin validation proves the reused JSON primitive keeps the field
    # coordinate; the public serializer may reject the whole record earlier.
    with pytest.raises(RecordSchemaClosureError) as caught:
        SCHEMA_VALIDATORS["fact_revision"](payload, "fact_revision", path_for("fact_revision"), 40)
    assert caught.value.code == "record_encoding"
    assert caught.value.field == "canonical_value"
    reject("fact_revision", payload, "record_encoding")


@pytest.mark.parametrize("rule,digest,missing", [
    (None, None, None), ("conversion-v1", "a" * 64, None),
    ("conversion-v1", None, "conversion_input_digest"),
    (None, "a" * 64, "conversion_rule_version"),
])
def test_conversion_fields_are_nonnull_together(rule, digest, missing):
    payload = sample("fact_revision")
    payload.update(conversion_rule_version=rule, conversion_input_digest=digest)
    if missing:
        reject("fact_revision", payload, "require_together", "$." + missing)
    else:
        assert validate("fact_revision", payload) is None


@pytest.mark.parametrize("field,value,code", [
    ("conversion_rule_version", "", "nonempty_string"),
    ("conversion_rule_version", True, "nonempty_string"),
    ("conversion_input_digest", "A" * 64, "sha256_digest"),
    ("conversion_input_digest", "a" * 63, "sha256_digest"),
    ("conversion_input_digest", 1, "sha256_digest"),
])
def test_conversion_member_types(field, value, code):
    payload = sample("fact_revision")
    payload.update(conversion_rule_version="conversion-v1", conversion_input_digest="a" * 64)
    payload[field] = value
    reject("fact_revision", payload, code, field)


SINGULAR_REFS = [
    ("entity_revision", ("predecessor_ref",)),
    ("entity_revision", ("disambiguation_issue_ref",)),
    ("fact_revision", ("entity_ref",)), ("fact_revision", ("predecessor_ref",)),
]
REF_ARRAYS = [
    ("entity_revision", ("aliases", 0, "source_refs")),
    ("entity_revision", ("lineage", "target_refs")),
    ("fact_revision", ("primary_evidence_refs",)),
    ("fact_revision", ("field_evidence", 0, "evidence_refs")),
    ("fact_revision", ("qualification_basis_refs",)),
    ("fact_revision", ("confirmation_basis_refs",)),
] + [("fact_revision", (*parts, "evidence_refs")) for parts in LOCATED_PATHS]
ALL_REFS = SINGULAR_REFS + [(family, (*parts, 0)) for family, parts in REF_ARRAYS]


def with_ref(family, parts, ref):
    payload = rich_sample(family)
    if parts[-1] == 0:
        at(payload, parts[:-2])[parts[-2]] = [ref]
    else:
        at(payload, parts[:-1])[parts[-1]] = ref
    return payload


@pytest.mark.parametrize("family,parts", ALL_REFS)
@pytest.mark.parametrize("ref", [
    {}, {"record_type": "fact_revision", "revision": 1},
    {"record_type": "outside", "stable_id": "x", "revision": 1},
    {"record_type": [], "stable_id": "x"},
    {"record_type": "fact_revision", "stable_id": ""},
    {"record_type": "fact_revision", "stable_id": "x", "revision": "HEAD"},
    {"record_type": "fact_revision", "stable_id": "current", "revision": 1},
    {"record_type": "fact_revision", "stable_id": "x", "revision": True},
    {"record_type": "fact_revision", "stable_id": "x", "revision": 0},
    {"record_type": "fact_revision", "stable_id": "x", "revision": -1},
    {"record_type": "fact_revision", "stable_id": "x", "revision": None},
    {"record_type": "fact_revision", "stable_id": "x", "extra": None},
    "ref", [],
])
def test_bad_typed_refs_in_every_reference_slot(family, parts, ref):
    error = reject(family, with_ref(family, parts, deepcopy(ref)))
    assert error.field.startswith(coordinate(parts))


@pytest.mark.parametrize("family,parts", ALL_REFS)
@pytest.mark.parametrize("ref", [
    {"record_type": "fact_revision", "stable_id": "unresolved-but-shape-valid", "revision": 9},
    {"record_type": "entity_revision", "stable_id": "unresolved"},
    {"record_type": "issue_event", "stable_id": "issue", "revision": "event-id"},
])
def test_reference_shapes_do_not_resolve_or_enforce_target_semantics(family, parts, ref):
    assert validate(family, with_ref(family, parts, deepcopy(ref))) is None


@pytest.mark.parametrize("family,parts", REF_ARRAYS)
def test_reference_arrays_reject_duplicates_and_allow_empty(family, parts):
    payload = rich_sample(family)
    at(payload, parts[:-1])[parts[-1]] = [typed(), typed()]
    reject(family, payload, "duplicate_ref", coordinate(parts))
    at(payload, parts[:-1])[parts[-1]] = []
    assert validate(family, payload) is None


@pytest.mark.parametrize("family,parts", REF_ARRAYS + [
    ("entity_revision", ("aliases",)), ("fact_revision", ("field_evidence",)),
    ("fact_revision", ("unresolved",)),
])
@pytest.mark.parametrize("value", [None, {}, "array", True])
def test_arrays_have_exact_list_type(family, parts, value):
    payload = rich_sample(family)
    at(payload, parts[:-1])[parts[-1]] = value
    reject(family, payload, "array_type", coordinate(parts))
