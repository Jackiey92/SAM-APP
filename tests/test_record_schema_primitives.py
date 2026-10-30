"""Shared schema mechanisms only; no new business-family registrations."""
from copy import deepcopy
from decimal import Decimal
from functools import partial

import pytest

from app.facts_tree.record_schema import CLOSURE_VALIDATORS, SCHEMA_VALIDATORS
from app.facts_tree.record_schema._registry import R1_CLOSURE_FAMILIES
from app.facts_tree.record_schema._primitives import (
    RecordSchemaClosureError, _actor, _array_of, _audit_refs, _bool, _enum,
    _int_plus, _int_positive, _legacy_block, _located_text, _object, _optional,
    _positive, _refs, _require_any, _require_together, _str, _tagged_union, _ts,
)
from app.host_audit_refs import validate_actor_audit_ref


class IntSubclass(int):
    pass


class StrSubclass(str):
    pass


def check(validator, value):
    return validator(value, "test-family", "test/path.json", "parent")


def reject(validator, value, code, field="parent"):
    with pytest.raises(RecordSchemaClosureError) as caught:
        check(validator, value)
    error = caught.value
    assert (error.family, error.path, error.code, error.field) == (
        "test-family", "test/path.json", code, field)
    assert str(error) == f"test-family:test/path.json:{code}:{field}"


@pytest.mark.parametrize("validator,value", [
    (_str, "x"), (_str, "原文 "), (_bool, True), (_bool, False),
    (_ts, 0), (_ts, 1793318400), (_int_plus, 1), (_int_positive, 2),
    (_enum(frozenset({"x", "y"})), "x"), (_enum(frozenset({1})), 1),
])
def test_scalar_acceptance(validator, value):
    assert check(validator, value) is None


@pytest.mark.parametrize("validator,code", [
    (_str, "nonempty_string"), (_bool, "boolean_type"),
    (_ts, "timestamp"), (_int_plus, "positive_integer"),
])
@pytest.mark.parametrize("value", [None, "", [], {}, 1.5, Decimal("1")])
def test_scalar_wrong_types_and_null(validator, code, value):
    reject(validator, value, code)


@pytest.mark.parametrize("validator,code,values", [
    (_str, "nonempty_string", [False, 1, StrSubclass("x")]),
    (_bool, "boolean_type", [0, 1, "true", IntSubclass(1)]),
    (_ts, "timestamp", [True, False, -1, "1", IntSubclass(1)]),
    (_int_plus, "positive_integer", [True, False, 0, -1, "1", IntSubclass(1)]),
])
def test_scalar_exact_builtin_contracts(validator, code, values):
    for value in values:
        reject(validator, value, code)
    assert _int_plus is _positive is _int_positive


@pytest.mark.parametrize("value", [None, "", "z", [], {}, True, 1, StrSubclass("x")])
def test_enum_unknown_or_unhashable(value):
    reject(_enum(frozenset({"x", "y"})), value, "enum_value")


@pytest.mark.parametrize("value", [True, Decimal("1"), IntSubclass(1)])
def test_enum_never_coerces_numeric_types(value):
    reject(_enum(frozenset({1})), value, "enum_value")


def test_enum_empty_set_and_optional_only_skip_none():
    reject(_enum(frozenset()), "x", "enum_value")
    seen = []

    def observer(value, family, path, field):
        seen.append((value, family, path, field))

    for value in (None, False, 0, "", []):
        assert check(_optional(observer), value) is None
    assert [item[0] for item in seen] == [False, 0, "", []]
    assert all(item[1:] == ("test-family", "test/path.json", "parent") for item in seen)
    assert check(_optional(_str), None) is None
    assert check(_optional(_str), "x") is None
    reject(_optional(_str), "", "nonempty_string")


@pytest.mark.parametrize("value", [{"a": "x"}, {"b": 0}, {"a": False}, {"a": []}])
def test_require_any_presence(value):
    assert check(_require_any(("a", "b")), value) is None


@pytest.mark.parametrize("value", [{}, {"a": None}, {"a": None, "b": None}])
def test_require_any_rejects_missing_and_null(value):
    reject(_require_any(("a", "b")), value, "require_any")


@pytest.mark.parametrize("value", [{}, {"a": None}, {"a": None, "b": None},
                                   {"a": 0, "b": False}])
def test_require_together_presence(value):
    assert check(_require_together("a", "b"), value) is None


@pytest.mark.parametrize("value,field", [
    ({"a": 0}, "parent.b"), ({"a": None, "b": "x"}, "parent.a"),
])
def test_require_together_reports_missing_partner(value, field):
    reject(_require_together("a", "b"), value, "require_together", field)


@pytest.mark.parametrize("validator", [_require_any(("a", "b")), _require_together("a", "b")])
@pytest.mark.parametrize("value", [None, [], "x"])
def test_presence_helpers_require_an_object(validator, value):
    reject(validator, value, "object_type")


NESTED = _object({"items": _array_of(_object({"name": _str, "enabled": _bool}))})


def test_object_array_composition_and_no_implicit_deduplication():
    value = {"items": [{"name": "x", "enabled": False}] * 2}
    before = deepcopy(value)
    assert check(NESTED, value) is None
    assert value == before
    assert check(NESTED, {"items": []}) is None
    assert check(_object({"a": _optional(_str)}), {"a": None}) is None
    reject(_object({"a": _optional(_str)}), {}, "missing_field", "parent.a")


@pytest.mark.parametrize("value,code,field", [
    (None, "object_type", "parent"),
    ({1: "x"}, "object_type", "parent"),
    ({}, "missing_field", "parent.items"),
    ({"items": [], "extra": 1}, "unknown_field", "parent.extra"),
    ({"items": None}, "array_type", "parent.items"),
    ({"items": [None]}, "object_type", "parent.items[0]"),
    ({"items": [{"enabled": False}]}, "missing_field", "parent.items[0].name"),
    ({"items": [{"name": "x", "enabled": 0}]}, "boolean_type", "parent.items[0].enabled"),
    ({"items": [{"name": "x", "enabled": True, "extra": 1}]},
     "unknown_field", "parent.items[0].extra"),
])
def test_nested_coordinates(value, code, field):
    reject(NESTED, value, code, field)


def test_specs_are_snapshotted():
    spec = {"a": _str}
    validator = _object(spec)
    spec["b"] = _bool
    assert check(validator, {"a": "x"}) is None
    reject(validator, {"a": "x", "b": True}, "unknown_field", "parent.b")


@pytest.mark.parametrize("value", [None, {}, (), "x"])
def test_array_of_requires_builtin_list(value):
    reject(_array_of(_str), value, "array_type")


def located(**updates):
    return {"status": "known", "text": None, "value": None,
            "evidence_refs": [], "reason": None, **updates}


@pytest.mark.parametrize("status", ["known", "not_stated", "not_applicable", "ambiguous"])
def test_located_text_binding_disabled_by_default(status):
    assert check(_located_text, located(status=status)) is None
    assert check(_located_text, located(status=status, text="原文", value=0)) is None


@pytest.mark.parametrize("value", [None, "规范值", 0, False, Decimal("1.20"), [1], {"x": 1}])
def test_located_text_canonical_value_types(value):
    assert check(_located_text, located(value=value)) is None


@pytest.mark.parametrize("updates,code,field", [
    ({"status": "unknown"}, "enum_value", "parent.status"),
    ({"status": []}, "enum_value", "parent.status"),
    ({"text": ""}, "nonempty_string", "parent.text"),
    ({"text": 1}, "nonempty_string", "parent.text"),
    ({"reason": ""}, "nonempty_string", "parent.reason"),
    ({"reason": False}, "nonempty_string", "parent.reason"),
    ({"value": 1.5}, "record_encoding", "parent.value"),
    ({"value": Decimal("NaN")}, "record_encoding", "parent.value"),
    ({"value": {"x": float("inf")}}, "record_encoding", "parent.value"),
    ({"value": "\ud800"}, "record_encoding", "parent.value"),
    ({"evidence_refs": None}, "array_type", "parent.evidence_refs"),
    ({"evidence_refs": [{}]}, "typed_ref_type", "parent.evidence_refs[0]"),
    ({"extra": 1}, "unknown_field", "parent.extra"),
])
def test_located_text_invalid_fields(updates, code, field):
    reject(_located_text, located(**updates), code, field)


@pytest.mark.parametrize("key", ["status", "text", "value", "evidence_refs", "reason"])
def test_located_text_all_keys_required(key):
    value = located()
    del value[key]
    reject(_located_text, value, "missing_field", f"parent.{key}")


def test_located_text_uses_refs_and_optional_typed_value_validator():
    ref = {"record_type": "evidence", "stable_id": "test-evidence"}
    assert check(_located_text, located(evidence_refs=[ref])) is None
    reject(_located_text, located(evidence_refs=[ref, ref]),
           "duplicate_ref", "parent.evidence_refs")
    typed = partial(_located_text, value_validator=_int_plus)
    assert check(typed, located(value=1)) is None
    reject(typed, located(value=True), "positive_integer", "parent.value")
    strict = partial(_located_text, require_known=True)
    assert check(strict, located(text="原文", value=False)) is None
    assert check(strict, located(status="not_stated")) is None
    reject(strict, located(), "located_text_binding", "parent.text")
    reject(strict, located(text="原文"), "located_text_binding", "parent.value")
    reject(strict, located(status="ambiguous", value=0),
           "located_text_binding", "parent.value")


UNION = _tagged_union("kind", {"x": {"items": _array_of(_str)}, "y": {"flag": _bool}})


@pytest.mark.parametrize("value", [{"kind": "x", "items": []}, {"kind": "y", "flag": False}])
def test_tagged_union_selects_exact_branch(value):
    assert check(UNION, value) is None


@pytest.mark.parametrize("value,code,field", [
    (None, "object_type", "parent"),
    ({1: "x"}, "object_type", "parent"),
    ({}, "missing_field", "parent.kind"),
    ({"kind": "z"}, "unknown_subtype", "parent.kind"),
    ({"kind": []}, "unknown_subtype", "parent.kind"),
    ({"kind": None}, "unknown_subtype", "parent.kind"),
    ({"kind": "x"}, "missing_field", "parent.items"),
    ({"kind": "x", "items": [], "flag": True}, "unknown_field", "parent.flag"),
    ({"kind": "x", "items": [None]}, "nonempty_string", "parent.items[0]"),
    ({"kind": "y", "flag": 1}, "boolean_type", "parent.flag"),
])
def test_tagged_union_fail_closed(value, code, field):
    reject(UNION, value, code, field)


def test_tagged_union_numeric_discriminator_cannot_be_bool():
    validator = _tagged_union("kind", {1: {}})
    assert check(validator, {"kind": 1}) is None
    reject(validator, {"kind": True}, "unknown_subtype", "parent.kind")


def actor(task=False):
    value = {"schema_version": 1, "audit_ref_type": "user_actor",
             "installation_id": "install-test", "company_id": "company-test",
             "principal_id": "principal-test", "member_revision": 1,
             "session_ref": "session-ref-test"}
    if task:
        del value["session_ref"]
        value.update(audit_ref_type="task_actor", task_id="task-test", run_id="run-test",
                     policy_id="policy-test", policy_revision=1)
    return value


@pytest.mark.parametrize("task", [False, True])
def test_actor_reuses_both_frozen_host_shapes(task):
    value = actor(task)
    before = deepcopy(value)
    assert validate_actor_audit_ref(value) is None
    assert check(_actor, value) is None
    assert value == before


@pytest.mark.parametrize("value", [None, [], {}, actor() | {"token": "never-echo"},
                                   actor() | {"member_revision": True},
                                   actor() | {"principal_id": "\ud800"},
                                   actor(True) | {"policy_revision": IntSubclass(1)}])
def test_actor_wraps_host_errors_without_payload(value):
    reject(_actor, value, "actor_audit_ref")
    with pytest.raises(RecordSchemaClosureError) as caught:
        check(_actor, value)
    assert caught.value.__context__ is None
    assert "never-echo" not in str(caught.value)


def test_audit_refs_empty_only_until_host_publishes_approval_schema():
    assert check(_audit_refs, []) is None
    for value in ([{}], [actor()], [{"approval_id": "not-a-published-shape"}]):
        reject(_audit_refs, value, "unsupported_approval_audit_ref")


@pytest.mark.parametrize("value", [None, {}, (), ""])
def test_audit_refs_array_type(value):
    reject(_audit_refs, value, "array_type")


LEGACY = _legacy_block(frozenset({"a", "b"}))


@pytest.mark.parametrize("value", [{}, {"a": None}, {"b": [False, 0, ""]},
                                   {"a": {"raw": Decimal("1.20")}, "b": "原文"}])
def test_legacy_keys_are_optional_and_values_canonical(value):
    assert check(LEGACY, value) is None


@pytest.mark.parametrize("value,code,field", [
    (None, "object_type", "parent"), ([], "object_type", "parent"),
    ({1: "x"}, "object_type", "parent"),
    ({"extra": 1}, "unknown_field", "parent.extra"),
    ({"a": 1.5}, "record_encoding", "parent.a"),
    ({"b": {"x": float("nan")}}, "record_encoding", "parent.b"),
])
def test_legacy_block_fail_closed(value, code, field):
    reject(LEGACY, value, code, field)


def test_legacy_empty_whitelist_is_closed():
    validator = _legacy_block(frozenset())
    assert check(validator, {}) is None
    reject(validator, {"a": None}, "unknown_field", "parent.a")


def test_composition_does_not_register_business_plugins_and_r1_coverage_is_fixed():
    assert set(R1_CLOSURE_FAMILIES) == {
        "input_candidate", "operation", "consumed_candidate", "event"}
    assert set(R1_CLOSURE_FAMILIES) <= set(SCHEMA_VALIDATORS)
    assert {1, 5} <= set(CLOSURE_VALIDATORS)
    validator = _object({"payload": UNION, "legacy": _optional(LEGACY), "refs": _refs})
    value = {"payload": {"kind": "y", "flag": False}, "legacy": None, "refs": []}
    before = deepcopy(value)
    assert check(validator, value) is None
    assert value == before
