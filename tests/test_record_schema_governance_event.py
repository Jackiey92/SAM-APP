"""Appendix A v1.1 (303eed2) §§0/11/12/13; synthetic single-record checks."""
from copy import deepcopy
from decimal import Decimal
from inspect import Parameter, signature

import pytest

from app.facts_tree import git_format, record_schema
from app.facts_tree.record_schema import _primitives, governance_event


SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
COMMON = {"schema_version", "format_version", "record_type", "repo_id", "company_id"}
# Literal, independent transcription of the frozen tables, including nullable keys.
FIELDS = {
    "issue_event": COMMON | {
        "issue_uid", "event_id", "ordinal", "predecessor_event_ref", "issue_type",
        "status", "candidate_refs", "fact_refs", "conflict_evidence_refs",
        "disposition_result", "basis", "affected_refs", "actor", "event_time",
    },
    "proposal_event": COMMON | {
        "proposal_uid", "event_id", "ordinal", "predecessor_event_ref", "proposal_type",
        "input_impact", "status", "disposition_result", "execution_operation_ref",
        "basis", "affected_refs", "actor", "event_time",
    },
}
STATUSES = {
    "issue_event": ("open", "resolved", "dismissed", "reopened"),
    "proposal_event": ("open", "approved", "rejected", "executed", "cancelled"),
}
IDENTITY = {"issue_event": "issue_uid", "proposal_event": "proposal_uid"}


def typed(family="fact_revision", stable_id="fact-1", revision=1):
    return {"record_type": family, "stable_id": stable_id, "revision": revision}


def sample(family, **ids):
    return record_schema.get_sample_builder(family)(**ids)


def path_for(payload):
    family = payload["record_type"]
    return git_format.record_path(family, **{
        IDENTITY[family]: payload[IDENTITY[family]], "event_id": payload["event_id"],
    })


def validate(payload, *, path=None):
    return record_schema.validate_record_schema(path or path_for(payload), payload, **SCOPE)


def reject(payload, *, field, code=None, path=None):
    with pytest.raises(record_schema.RecordSchemaClosureError) as caught:
        validate(payload, path=path)
    error = caught.value
    assert error.family == payload["record_type"]
    assert error.path == (path or path_for(payload))
    assert error.field == field
    if code is not None:
        assert error.code == code


@pytest.fixture(params=tuple(FIELDS))
def family(request):
    return request.param


def test_sample_has_exact_frozen_fields_and_closes_on_itself(family):
    payload = sample(family)
    before = deepcopy(payload)
    assert set(payload) == FIELDS[family] == record_schema.SCHEMA_FIELDS[family]
    assert payload[IDENTITY[family]] == "output-1"
    assert payload["event_id"] == "output-event"
    assert payload["repo_id"] == "repo-test" and payload["company_id"] == "company-test"
    assert payload["ordinal"] == 1 and payload["status"] == "open"
    assert payload["disposition_result"] == {
        "terminal": False, "result_refs": [typed(family, "output-1", "output-event")],
    }
    assert validate(payload) is None
    assert record_schema.validate_record_schema_closure(
        {path_for(payload): payload}, **SCOPE) is None
    assert payload == before
    encoded = git_format.dumps_record(payload)
    assert record_schema.loads_schema_record(encoded, path=path_for(payload), **SCOPE) == payload


def test_builder_returns_fresh_nested_values(family):
    first, second = sample(family), sample(family)
    assert first == second and first is not second
    first["actor"]["principal_id"] = "changed"
    first["disposition_result"]["result_refs"][0]["stable_id"] = "changed"
    first["affected_refs"].append(typed())
    if family == "issue_event":
        for field in ("candidate_refs", "fact_refs", "conflict_evidence_refs"):
            first[field].append(typed())
    else:
        first["input_impact"]["input_refs"].append(typed())
        first["input_impact"]["affected_refs"].append(typed())
    assert second == sample(family)


def test_builder_only_accepts_keyword_path_identities(family):
    builder = record_schema.get_sample_builder(family)
    params = signature(builder).parameters
    assert tuple(params) == (IDENTITY[family], "event_id")
    assert all(p.kind == Parameter.KEYWORD_ONLY for p in params.values())
    assert [p.default for p in params.values()] == ["output-1", "output-event"]
    with pytest.raises(TypeError):
        builder("other")
    with pytest.raises(TypeError):
        builder(status="resolved")
    payload = builder(**{IDENTITY[family]: "对象/二", "event_id": "opaque/event-二"})
    assert payload["disposition_result"]["result_refs"] == [
        typed(family, "对象/二", "opaque/event-二")]
    assert record_schema.validate_record_schema_closure({path_for(payload): payload}, **SCOPE) is None


@pytest.mark.parametrize("kind,field", [
    (kind, field) for kind, fields in FIELDS.items() for field in sorted(fields)
])
def test_every_field_is_required(kind, field):
    payload = sample(kind)
    path = path_for(payload)
    del payload[field]
    with pytest.raises(record_schema.RecordSchemaClosureError):
        validate(payload, path=path)


def test_unknown_top_level_field_is_rejected(family):
    payload = sample(family)
    payload["action"] = "invented"
    reject(payload, field="$", code="unknown_field")


@pytest.mark.parametrize("field", ["event_id", "identity"])
def test_path_identity_mismatch(family, field):
    payload = sample(family)
    path = path_for(payload)
    field = IDENTITY[family] if field == "identity" else field
    payload[field] = "different"
    reject(payload, path=path, field=field, code="path_identity")


@pytest.mark.parametrize("field,value,code", [
    ("ordinal", True, "positive_integer"), ("ordinal", False, "positive_integer"),
    ("ordinal", 0, "positive_integer"), ("ordinal", -1, "positive_integer"),
    ("ordinal", "1", "positive_integer"), ("ordinal", Decimal("1"), "positive_integer"),
    ("event_time", -1, "timestamp"), ("event_time", True, "timestamp"),
    ("event_time", "0", "timestamp"), ("event_time", None, "timestamp"),
    ("event_time", Decimal("0"), "timestamp"),
    ("basis", "", "nonempty_string"), ("basis", 1, "nonempty_string"),
    ("actor", {}, "actor_audit_ref"), ("actor", None, "actor_audit_ref"),
    ("affected_refs", None, "array_type"),
])
def test_common_field_types(family, field, value, code):
    payload = sample(family)
    payload[field] = value
    reject(payload, field=field, code=code)


@pytest.mark.parametrize("value", ["", None, True, 1, [], {}])
def test_business_type_requires_nonempty_string(family, value):
    payload = sample(family)
    field = "issue_type" if family == "issue_event" else "proposal_type"
    payload[field] = value
    reject(payload, field=field, code="nonempty_string")


def test_status_and_type_do_not_decide_business_outcomes(family):
    for status in STATUSES[family]:
        payload = sample(family)
        payload["status"] = status
        payload["issue_type" if family == "issue_event" else "proposal_type"] = "自由类型/未枚举"
        payload["basis"] = "synthetic basis"
        payload["event_time"] = 1
        assert validate(payload) is None
    expected = governance_event.ISSUE_STATUSES if family == "issue_event" else governance_event.PROPOSAL_STATUSES
    assert type(expected) is frozenset and expected == frozenset(STATUSES[family])


@pytest.mark.parametrize("status", ["unknown", "OPEN", "", None, True, 1, [], {}])
def test_invalid_status(family, status):
    payload = sample(family)
    payload["status"] = status
    reject(payload, field="status", code="enum_value")


@pytest.mark.parametrize("terminal,refs", [
    (False, [typed("issue_event", "issue-1", "evt-1")]),
    (False, [typed("proposal_event", "proposal-1", "evt-1")]),
    (True, [typed()]),
    (True, [typed("issue_event", "issue-1", "evt-1")]),
])
def test_both_builtin_terminal_shapes_use_r1_result_rules(family, terminal, refs):
    payload = sample(family)
    payload["disposition_result"] = {"terminal": terminal, "result_refs": refs}
    assert validate(payload) is None
    assert governance_event._result is _primitives._result


@pytest.mark.parametrize("value,code,field", [
    (None, "object_type", "disposition_result"),
    ([], "object_type", "disposition_result"),
    ({"terminal": False}, "missing_field", "disposition_result"),
    ({"terminal": False, "result_refs": [], "extra": 1}, "unknown_field", "disposition_result"),
    ({"terminal": False, "result_refs": []}, "missing_result_ref", "disposition_result"),
    ({"terminal": True, "result_refs": []}, "missing_result_ref", "disposition_result"),
    ({"terminal": 1, "result_refs": [typed()]}, "terminal_boolean", "disposition_result"),
    ({"terminal": 0, "result_refs": [typed()]}, "terminal_boolean", "disposition_result"),
    ({"terminal": "false", "result_refs": [typed()]}, "terminal_boolean", "disposition_result"),
    ({"terminal": None, "result_refs": [typed()]}, "terminal_boolean", "disposition_result"),
    ({"terminal": False, "result_refs": [typed()]}, "nonterminal_result_type", "disposition_result"),
    ({"terminal": False, "result_refs": [typed("issue_event"), typed()]},
     "nonterminal_result_type", "disposition_result"),
    ({"terminal": False, "result_refs": [typed("issue_event"), typed("proposal_event")]},
     "multiple_primary_result_types", "disposition_result"),
    ({"terminal": True, "result_refs": [typed("proposal_event")]},
     "terminal_result_type", "disposition_result"),
    ({"terminal": True, "result_refs": [typed("issue_event"), typed()]},
     "conflict_fact_output", "disposition_result"),
    ({"terminal": False, "result_refs": None}, "array_type", "disposition_result.result_refs"),
])
def test_invalid_result_shapes_and_types(family, value, code, field):
    payload = sample(family)
    payload["disposition_result"] = deepcopy(value)
    reject(payload, field=field, code=code)


REF_FIELDS = [
    ("issue_event", ("predecessor_event_ref",), False),
    ("issue_event", ("candidate_refs",), True),
    ("issue_event", ("fact_refs",), True),
    ("issue_event", ("conflict_evidence_refs",), True),
    ("issue_event", ("affected_refs",), True),
    ("issue_event", ("disposition_result", "result_refs"), True),
    ("proposal_event", ("predecessor_event_ref",), False),
    ("proposal_event", ("execution_operation_ref",), False),
    ("proposal_event", ("affected_refs",), True),
    ("proposal_event", ("input_impact", "input_refs"), True),
    ("proposal_event", ("input_impact", "affected_refs"), True),
    ("proposal_event", ("disposition_result", "result_refs"), True),
]


@pytest.mark.parametrize("kind,keys,is_array", REF_FIELDS)
@pytest.mark.parametrize("bad_ref", [
    {"record_type": "issue_event", "revision": "evt-1"},
    {"record_type": "issue_event", "stable_id": "issue-1", "revision": "HEAD"},
    "issue-1",
])
def test_bad_typed_refs_in_every_reference_field(kind, keys, is_array, bad_ref):
    payload = sample(kind)
    target = payload
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = [deepcopy(bad_ref)] if is_array else deepcopy(bad_ref)
    with pytest.raises(record_schema.RecordSchemaClosureError):
        validate(payload)


@pytest.mark.parametrize("kind,keys,is_array", REF_FIELDS)
def test_ref_shapes_do_not_require_an_existing_target(kind, keys, is_array):
    payload = sample(kind)
    target = payload
    for key in keys[:-1]:
        target = target[key]
    ref = typed(kind, "missing-target", "opaque-event")
    if keys == ("execution_operation_ref",):
        ref = {"record_type": "operation", "stable_id": "missing-operation"}
    target[keys[-1]] = [ref] if is_array else ref
    assert validate(payload) is None


@pytest.mark.parametrize("key", ["input_refs", "affected_refs", "note"])
def test_input_impact_requires_every_key(key):
    payload = sample("proposal_event")
    del payload["input_impact"][key]
    reject(payload, field=f"input_impact.{key}", code="missing_field")


def test_input_impact_rejects_unknown_key():
    payload = sample("proposal_event")
    payload["input_impact"]["extra"] = None
    reject(payload, field="input_impact.extra", code="unknown_field")


@pytest.mark.parametrize("value", [None, [], "impact"])
def test_input_impact_requires_object(value):
    payload = sample("proposal_event")
    payload["input_impact"] = value
    reject(payload, field="input_impact", code="object_type")


@pytest.mark.parametrize("key,value,code", [
    ("input_refs", None, "array_type"), ("affected_refs", {}, "array_type"),
    ("note", "", "nonempty_string"), ("note", 1, "nonempty_string"),
])
def test_input_impact_child_types(key, value, code):
    payload = sample("proposal_event")
    payload["input_impact"][key] = value
    reject(payload, field=f"input_impact.{key}", code=code)


def test_input_impact_note_and_null_execution_ref_are_legal():
    payload = sample("proposal_event")
    payload["input_impact"]["note"] = "synthetic input impact"
    assert payload["execution_operation_ref"] is None
    assert validate(payload) is None
