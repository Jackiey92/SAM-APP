"""Appendix A v1.1 §§0, 5–7, 13: independent source/evidence contracts."""
from copy import deepcopy
from hashlib import sha256
from inspect import Parameter, signature

import pytest

from app.facts_tree import git_format, record_schema


COMMON = {"schema_version", "format_version", "record_type", "repo_id", "company_id"}
SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
DIGEST = "a" * 64
ACTOR = {"schema_version": 1, "audit_ref_type": "user_actor",
         "installation_id": "install-test", "company_id": "company-test",
         "principal_id": "owner-test", "member_revision": 1,
         "session_ref": "session-audit-id"}
REF = {"record_type": "source_version", "stable_id": "unresolved-source",
       "revision": "opaque-version"}

# Written from the frozen appendix, independently of the plugin field specs.
EXAMPLES = {
    "source_version": {
        "source_id": "source-1", "version_id": "v-1", "seq": 1,
        "kind": "an-unregistered-kind", "origin_zone": "zone", "origin_key": "key",
        "display_name": "原始来源名称", "locator": "source://original",
        "content_fingerprint": DIGEST, "fingerprint_algorithm": "algorithm",
        "file_hash": DIGEST, "source_description_ref": "description",
        "mapped_object_ref": REF, "adapter_rule_version": "adapter",
        "mapping_rule_version": "mapping", "prior_version_ref": REF,
        "captured_at": 0, "actor": ACTOR,
    },
    "source_lifecycle": {
        "source_id": "source-1", "revision": 1, "predecessor_ref": REF,
        "source_description_ref": "description", "status": "active",
        "version_scope": {"version_ref": REF, "scope_note": "scope"},
        "reason": "reason", "actor": ACTOR, "approval_use_refs": [], "event_time": 0,
    },
    "evidence": {
        "evidence_id": "output-1", "source_version_ref": REF,
        "original_hash": DIGEST, "mapped_fingerprint": DIGEST, "source_loc": "loc",
        "attribution_index": "index", "coordinate": "原始行号",
        "quote_excerpt_ref": REF, "quote_digest": DIGEST,
        "legacy_normalized_fingerprint": DIGEST,
        "legacy_fingerprint_algorithm": "legacy-algorithm", "usage": "usage",
        "read_audit_ref": "opaque-audit-reference",
    },
}
IDENTITIES = {"source_version": ("source_id", "version_id"),
              "source_lifecycle": ("source_id", "revision"),
              "evidence": ("evidence_id",)}
NULLABLE = {
    "source_version": {
        "origin_zone", "origin_key", "locator", "content_fingerprint",
        "fingerprint_algorithm", "file_hash", "source_description_ref",
        "mapped_object_ref", "adapter_rule_version", "mapping_rule_version",
        "prior_version_ref",
    },
    "source_lifecycle": {"predecessor_ref", "source_description_ref"},
    "evidence": {
        "original_hash", "mapped_fingerprint", "source_loc", "attribution_index",
        "coordinate", "quote_excerpt_ref", "quote_digest",
        "legacy_normalized_fingerprint", "legacy_fingerprint_algorithm", "read_audit_ref",
    },
}
TOP_FIELDS = [(family, field) for family, fields in EXAMPLES.items()
              for field in sorted(COMMON | fields.keys())]
DIGEST_FIELDS = [("source_version", field) for field in
                 ("content_fingerprint", "file_hash")]
DIGEST_FIELDS += [("evidence", field) for field in
                  ("original_hash", "mapped_fingerprint", "quote_digest",
                   "legacy_normalized_fingerprint")]


def sample(family, **ids):
    payload = record_schema.get_sample_builder(family)(**ids)
    path = git_format.record_path(family, **{k: payload[k] for k in IDENTITIES[family]})
    return path, payload


def validate(path, payload):
    return record_schema.validate_record_schema(path, payload, **SCOPE)


def reject(path, payload, code=None, field=None):
    with pytest.raises(record_schema.RecordSchemaClosureError) as caught:
        validate(path, payload)
    error = caught.value
    assert error.family == git_format.canonical_record_type(path)
    assert error.path == path
    if code is not None:
        assert error.code == code
    if field is not None:
        assert error.field == field
    return error


@pytest.mark.parametrize("family", EXAMPLES)
def test_discovered_builder_exact_frozen_fields_and_valid_payload(family):
    path, payload = sample(family)
    assert set(payload) == record_schema.SCHEMA_FIELDS[family] == COMMON | EXAMPLES[family].keys()
    assert {k: payload[k] for k in SCOPE} == SCOPE
    assert validate(path, payload) is None
    before = deepcopy(payload)
    assert record_schema.loads_schema_record(
        git_format.dumps_record(payload), path=path, **SCOPE) == payload
    assert payload == before
    parameters = signature(record_schema.get_sample_builder(family)).parameters
    assert set(parameters) == set(IDENTITIES[family])
    assert all(p.kind is Parameter.KEYWORD_ONLY and p.default is not Parameter.empty
               for p in parameters.values())


@pytest.mark.parametrize("family,ids", [
    ("source_version", {"source_id": "来源/二", "version_id": "0007"}),
    ("source_version", {"source_id": "来源/二", "version_id": "版本/零"}),
    ("source_lifecycle", {"source_id": "来源/二", "revision": 7}),
    ("evidence", {"evidence_id": "证据/二"}),
])
def test_builder_identity_overrides_and_opaque_version_ids(family, ids):
    path, payload = sample(family, **ids)
    assert {k: payload[k] for k in ids} == ids
    assert validate(path, payload) is None


@pytest.mark.parametrize("family", EXAMPLES)
def test_builder_is_fresh_and_rejects_business_arguments(family):
    builder = record_schema.get_sample_builder(family)
    first, second = builder(), builder()
    assert first == second and first is not second
    for field, value in first.items():
        if isinstance(value, dict):
            value["mutation"] = True
        elif isinstance(value, list):
            value.append("mutation")
    first["company_id"] = "changed"
    assert builder() == second
    with pytest.raises(TypeError):
        builder(company_id="foreign")
    with pytest.raises(TypeError):
        builder("positional-id")


@pytest.mark.parametrize("family", EXAMPLES)
def test_populated_payload_accepts_unresolved_refs_without_mutation(family):
    path, payload = sample(family)
    payload.update(deepcopy(EXAMPLES[family]))
    before = deepcopy(payload)
    assert validate(path, payload) is None
    assert payload == before


@pytest.mark.parametrize("family,field", TOP_FIELDS)
def test_every_top_level_key_is_required_even_when_nullable(family, field):
    path, payload = sample(family)
    del payload[field]
    reject(path, payload)


@pytest.mark.parametrize("family", EXAMPLES)
@pytest.mark.parametrize("field", ["unknown", "kind_meta"])
def test_unknown_top_level_keys_are_rejected(family, field):
    path, payload = sample(family)
    payload[field] = {}
    reject(path, payload, "unknown_field", "$")


@pytest.mark.parametrize("family,field", TOP_FIELDS)
def test_every_top_level_field_rejects_wrong_type(family, field):
    path, payload = sample(family)
    payload[field] = None if field == "approval_use_refs" else []
    reject(path, payload)


@pytest.mark.parametrize("family,field", [
    (family, field) for family in EXAMPLES
    for field in sorted((COMMON | EXAMPLES[family].keys()) - NULLABLE[family])
])
@pytest.mark.parametrize("value", [None, ""])
def test_required_fields_reject_null_and_empty(family, field, value):
    path, payload = sample(family)
    payload[field] = value
    reject(path, payload)


@pytest.mark.parametrize("family,field", [
    (family, field) for family in EXAMPLES for field in sorted(NULLABLE[family])
])
def test_nullable_does_not_allow_empty_strings(family, field):
    path, payload = sample(family)
    payload[field] = ""
    reject(path, payload)


@pytest.mark.parametrize("family,field", [
    (family, field) for family, fields in IDENTITIES.items() for field in fields
])
def test_path_identity_mismatch(family, field):
    path, payload = sample(family)
    payload[field] = 2 if field == "revision" else "different-id"
    reject(path, payload, "path_identity", field)


@pytest.mark.parametrize("family", EXAMPLES)
@pytest.mark.parametrize("field", ["schema_version", "format_version"])
@pytest.mark.parametrize("value", [True, False, 0, -1, "1", 2])
def test_common_versions_are_exact_and_supported(family, field, value):
    path, payload = sample(family)
    payload[field] = value
    reject(path, payload, "unsupported_version" if value == 2 else "positive_integer", field)


@pytest.mark.parametrize("family", EXAMPLES)
@pytest.mark.parametrize("field", SCOPE)
def test_scope_mismatch(family, field):
    path, payload = sample(family)
    payload[field] = "foreign"
    reject(path, payload, "scope_mismatch", field)


@pytest.mark.parametrize("family,field", [("source_version", "seq"),
                                          ("source_lifecycle", "revision")])
@pytest.mark.parametrize("value", [True, False, 0, -1, "1"])
def test_seq_and_revision_require_positive_builtin_integers(family, field, value):
    path, payload = sample(family)
    payload[field] = value
    reject(path, payload, "path_identity" if field == "revision" else "positive_integer", field)


@pytest.mark.parametrize("value", [7, True])
def test_version_id_is_an_opaque_string_not_a_number(value):
    path, payload = sample("source_version")
    payload["version_id"] = value
    reject(path, payload, "path_identity", "version_id")


@pytest.mark.parametrize("family,field", [("source_version", "captured_at"),
                                          ("source_lifecycle", "event_time")])
@pytest.mark.parametrize("value", [True, False, -1, "0"])
def test_timestamp_rejects_negative_or_non_integer(family, field, value):
    path, payload = sample(family)
    payload[field] = value
    reject(path, payload, "timestamp", field)


@pytest.mark.parametrize("family,field", DIGEST_FIELDS)
@pytest.mark.parametrize("value", ["A" * 64, "g" * 64, "a" * 63, "a" * 65, True, {}, ""])
def test_digests_require_64_lowercase_hex(family, field, value):
    path, payload = sample(family)
    payload[field] = value
    reject(path, payload, "sha256_digest", field)


@pytest.mark.parametrize("family,a,b", [
    ("source_version", "fingerprint_algorithm", "content_fingerprint"),
    ("evidence", "legacy_fingerprint_algorithm", "legacy_normalized_fingerprint"),
])
@pytest.mark.parametrize("present", [0, 1])
def test_fingerprint_pairs_must_be_non_null_together(family, a, b, present):
    path, payload = sample(family)
    payload[(a, b)[present]] = EXAMPLES[family][(a, b)[present]]
    reject(path, payload, "require_together", "$." + (b, a)[present])


@pytest.mark.parametrize("value", [None, "原文定位", {"kind": "file", "uri": "file:x", "digest": None},
                                   {"kind": "file", "uri": "file:x", "digest": DIGEST}])
def test_locator_accepts_only_nullable_string_or_exact_object(value):
    path, payload = sample("source_version")
    payload["locator"] = value
    assert validate(path, payload) is None


@pytest.mark.parametrize("value,code,field", [
    (3, "object_type", "locator"), (True, "object_type", "locator"),
    ([], "object_type", "locator"), ("", "nonempty_string", "locator"),
    ({"kind": "file", "digest": None}, "missing_field", "locator.uri"),
    ({"uri": "file:x", "digest": None}, "missing_field", "locator.kind"),
    ({"kind": "file", "uri": "file:x"}, "missing_field", "locator.digest"),
    ({"kind": "file", "uri": "file:x", "digest": None, "extra": 1}, "unknown_field", "locator.extra"),
    ({"kind": "", "uri": "file:x", "digest": None}, "nonempty_string", "locator.kind"),
    ({"kind": "file", "uri": "", "digest": None}, "nonempty_string", "locator.uri"),
    ({"kind": "file", "uri": "file:x", "digest": "bad"}, "sha256_digest", "locator.digest"),
])
def test_locator_rejects_third_shapes_and_unclosed_objects(value, code, field):
    path, payload = sample("source_version")
    payload["locator"] = value
    reject(path, payload, code, field)


@pytest.mark.parametrize("value", [None, "index", 1, 100])
def test_attribution_accepts_nullable_string_or_positive_integer(value):
    path, payload = sample("evidence")
    payload["attribution_index"] = value
    assert validate(path, payload) is None


@pytest.mark.parametrize("value", [True, False, 0, -1, [], {}, ""])
def test_attribution_rejects_bool_nonpositive_and_other_shapes(value):
    path, payload = sample("evidence")
    payload["attribution_index"] = value
    reject(path, payload)


@pytest.mark.parametrize("value", [None, "stable-read-audit-id"])
def test_read_audit_is_an_optional_opaque_string(value):
    path, payload = sample("evidence")
    payload["read_audit_ref"] = value
    assert validate(path, payload) is None


@pytest.mark.parametrize("value", [ACTOR, {}, [], True, 1, ""])
def test_read_audit_ref_rejects_objects_and_other_non_strings(value):
    path, payload = sample("evidence")
    payload["read_audit_ref"] = value
    reject(path, payload, "nonempty_string", "read_audit_ref")


@pytest.mark.parametrize("value", ["active", "pending_removal", "removed"])
def test_lifecycle_status_frozen_values(value):
    path, payload = sample("source_lifecycle")
    payload["status"] = value
    assert validate(path, payload) is None


@pytest.mark.parametrize("value", ["inactive", "ACTIVE", "", None, True, {}])
def test_lifecycle_rejects_unknown_status(value):
    path, payload = sample("source_lifecycle")
    payload["status"] = value
    reject(path, payload, "enum_value", "status")


@pytest.mark.parametrize("value,code", [([{}], "unsupported_approval_audit_ref"),
                                       (["opaque"], "unsupported_approval_audit_ref"),
                                       (None, "array_type"), ({}, "array_type")])
def test_approval_audit_refs_fail_closed(value, code):
    path, payload = sample("source_lifecycle")
    payload["approval_use_refs"] = value
    reject(path, payload, code, "approval_use_refs")


@pytest.mark.parametrize("family", ["source_version", "source_lifecycle"])
@pytest.mark.parametrize("value", [None, {}, {**ACTOR, "secret": "forbidden"},
                                   {**ACTOR, "member_revision": True}])
def test_actor_reuses_host_closed_shape(family, value):
    path, payload = sample(family)
    payload["actor"] = value
    reject(path, payload, "actor_audit_ref", "actor")


REF_FIELDS = [("source_version", "mapped_object_ref"), ("source_version", "prior_version_ref"),
              ("source_lifecycle", "predecessor_ref"), ("source_lifecycle", "version_scope"),
              ("evidence", "source_version_ref"), ("evidence", "quote_excerpt_ref")]


@pytest.mark.parametrize("family,field", REF_FIELDS)
@pytest.mark.parametrize("value", [
    {}, "ref", [], {"record_type": "unknown", "stable_id": "id"},
    {"record_type": "evidence"}, {"stable_id": "id"},
    {"record_type": "evidence", "stable_id": ""},
    {"record_type": "evidence", "stable_id": "id", "extra": 1},
    {"record_type": "source_version", "stable_id": "source", "revision": 0},
    {"record_type": "source_version", "stable_id": "source", "revision": True},
    {"record_type": "source_version", "stable_id": "source", "revision": "HEAD"},
    {"record_type": "evidence", "stable_id": "latest"},
])
def test_all_ref_positions_validate_shape_only_and_fail_closed(family, field, value):
    path, payload = sample(family)
    if field == "version_scope":
        payload[field]["version_ref"] = value
    else:
        payload[field] = value
    reject(path, payload)


@pytest.mark.parametrize("value,code,field", [
    (None, "object_type", "version_scope"),
    ({"version_ref": REF}, "missing_field", "version_scope.scope_note"),
    ({"scope_note": None}, "missing_field", "version_scope.version_ref"),
    ({"version_ref": REF, "scope_note": None, "extra": 1}, "unknown_field", "version_scope.extra"),
    ({"version_ref": REF, "scope_note": {}}, "nonempty_string", "version_scope.scope_note"),
])
def test_version_scope_is_exact(value, code, field):
    path, payload = sample("source_lifecycle")
    payload["version_scope"] = value
    reject(path, payload, code, field)


def test_hash_availability_and_quote_binding_are_deferred():
    path, payload = sample("evidence")
    assert payload["original_hash"] is payload["mapped_fingerprint"] is None
    payload["quote_excerpt_ref"] = deepcopy(REF)
    assert payload["quote_digest"] is None
    assert validate(path, payload) is None


def test_evidence_builder_self_reference_in_r1_terminal_tree():
    from tests.test_facts_tree_record_schema_closure import synthetic_tree

    tree, paths = synthetic_tree("evidence", True)
    path, payload = sample("evidence")
    assert path == paths[4] == "evidence/output-1.json"
    assert payload["source_version_ref"] == {"record_type": "evidence", "stable_id": "output-1"}
    tree[path] = payload
    tree[paths[3]]["after"] = [sha256(git_format.dumps_record(payload)).hexdigest()]
    operation = tree[paths[1]]
    result = operation["disposition_result"][0]
    assert result["terminal"] is True
    assert result["result_refs"] == [payload["source_version_ref"]]
    assert operation["formal_fact_refs"] == []
    assert not any(r["record_type"] == "fact_revision" for r in tree.values())
    assert record_schema.validate_record_schema_closure(tree, **SCOPE) is None
