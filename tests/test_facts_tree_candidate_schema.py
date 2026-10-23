"""Pinned checks for the fact-candidate/1 pure structural schema.

Covers the approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f
sections 3.1-3.5, 4 and 7: strict object shapes, exact types and enums,
LocatedText state combinations, evidence and field-binding closure,
required unresolved entries and the initial budget limits. All inputs
are synthetic in-memory JSON objects; nothing touches disk, Git,
SQLite, a model or any service.
"""
import copy
import json
import re

import pytest

from app.facts_tree import candidate_schema
from app.facts_tree import git_format


def _evidence(ref="e1", **overrides):
    item = {
        "evidence_ref": ref,
        "read_grant_id": "grant-1",
        "source_loc": "loc-1",
        "attribution_index": 0,
        "quote": "公司2024年营收为100万元。",
        "start_line": 3,
        "end_line": 4,
    }
    item.update(overrides)
    return item


def _known(text, refs=("e1",)):
    return {
        "status": "known",
        "text": text,
        "evidence_refs": list(refs),
        "reason": None,
    }


def _unresolved(field_path, **overrides):
    entry = {
        "field_path": field_path,
        "reason": "材料未说明该字段",
        "needed_clarification": "需要上传含该信息的原文段落",
    }
    entry.update(overrides)
    return entry


def _candidate(**overrides):
    candidate = {
        "local_id": "c1",
        "statement": "公司2024年营收为100万元。",
        "entity": _known("公司"),
        "attribute": "年度营收",
        "value_type": "number",
        "value_raw": "100万元",
        "period": _known("2024年"),
        "unit": _known("万元"),
        "currency": _known("人民币"),
        "scope": _known("本公司"),
        "basis": _known("经审计口径"),
        "claimant": _known("年报"),
        "statement_kind": "state",
        "polarity": "affirmed",
        "evidence": [_evidence()],
        "field_bindings": {
            "statement": ["e1"],
            "entity": ["e1"],
            "attribute": ["e1"],
            "value_raw": ["e1"],
            "period": ["e1"],
            "unit": ["e1"],
            "currency": ["e1"],
            "scope": ["e1"],
            "basis": ["e1"],
            "claimant": ["e1"],
            "statement_kind": ["e1"],
            "polarity": ["e1"],
        },
        "unresolved": [],
    }
    candidate.update(overrides)
    return candidate


def _batch(items=None, **overrides):
    if items is None:
        items = [_candidate()]
    batch = {
        "schema_version": "fact-candidate/1",
        "items": items,
        "complete": True,
        "remaining_source_locs": [],
        "empty_reason": None,
    }
    batch.update(overrides)
    return batch


def _codes(errors):
    return sorted(error.code for error in errors)


def _paths_with(errors, code):
    return [error.path for error in errors if error.code == code]


def _only_code(errors, code):
    assert errors, "expected validation errors"
    assert _codes(errors) == [code] * len(errors)


def test_public_contract_and_budgets_pinned():
    assert candidate_schema.SCHEMA_VERSION == "fact-candidate/1"
    assert candidate_schema.MAX_BATCH_ITEMS == 32
    assert candidate_schema.MAX_EVIDENCE_PER_CANDIDATE == 8
    assert candidate_schema.MAX_QUOTE_CHARS == 4000
    assert candidate_schema.MAX_ENTITY_CHARS == 256
    assert candidate_schema.MAX_ATTRIBUTE_CHARS == 256
    assert candidate_schema.MAX_STATEMENT_CHARS == 2000
    assert candidate_schema.MAX_RESPONSE_BYTES == 256 * 1024
    doc = candidate_schema.__doc__
    assert "initial suggested values" in doc
    assert "not the raw HTTP bytes" in doc
    assert "never performs them" in doc


def test_published_schema_is_json_compatible_and_strict():
    schema = candidate_schema.FACT_CANDIDATE_BATCH_SCHEMA
    assert json.loads(json.dumps(schema)) == schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version", "items", "complete", "remaining_source_locs",
        "empty_reason",
    }
    assert schema["properties"]["schema_version"]["const"] == \
        candidate_schema.SCHEMA_VERSION
    items = schema["properties"]["items"]
    assert items["maxItems"] == candidate_schema.MAX_BATCH_ITEMS
    candidate = items["items"]
    assert candidate["additionalProperties"] is False
    assert set(candidate["required"]) == {
        "local_id", "statement", "entity", "attribute", "value_type",
        "value_raw", "period", "unit", "currency", "scope", "basis",
        "claimant", "statement_kind", "polarity", "evidence",
        "field_bindings", "unresolved",
    }
    # P01 section 3.4: topic_hint is an optional string with no
    # non-empty rule and no separate length cap in the schema.
    assert candidate["properties"]["topic_hint"] == {"type": "string"}
    bindings = candidate["properties"]["field_bindings"]
    assert bindings["additionalProperties"] is False
    assert set(bindings["required"]) == set(
        candidate_schema.FIELD_BINDING_KEYS)
    evidence = candidate["properties"]["evidence"]
    assert evidence["minItems"] == 1
    assert evidence["maxItems"] == \
        candidate_schema.MAX_EVIDENCE_PER_CANDIDATE
    located = candidate["properties"]["entity"]
    assert located["additionalProperties"] is False
    assert set(located["required"]) == {
        "status", "text", "evidence_refs", "reason"}
    unresolved = candidate["properties"]["unresolved"]["items"]
    assert unresolved["additionalProperties"] is False
    assert set(unresolved["required"]) == {
        "field_path", "reason", "needed_clarification"}


def test_valid_complete_batch_passes():
    assert candidate_schema.validate_batch(_batch()) == []


def test_valid_partial_batch_passes():
    batch = _batch(complete=False, remaining_source_locs=["loc-1"])
    assert candidate_schema.validate_batch(batch) == []


def test_valid_empty_complete_batch_passes():
    batch = _batch(items=[], complete=True, remaining_source_locs=[],
                   empty_reason="本窗口仅为封面，无可提取陈述")
    assert candidate_schema.validate_batch(batch) == []


def test_batch_must_be_an_object():
    for payload in ([], None, "x", 3, True):
        errors = candidate_schema.validate_batch(payload)
        _only_code(errors, "type_object")
        assert errors[0].path == "$"


def test_batch_missing_and_unknown_keys():
    batch = _batch()
    del batch["complete"]
    batch["company_id"] = "co-1"
    errors = candidate_schema.validate_batch(batch)
    assert _codes(errors) == ["missing_field", "unknown_field"]
    assert "$.complete" in _paths_with(errors, "missing_field")
    assert "$.company_id" in _paths_with(errors, "unknown_field")


def test_schema_version_must_match_exactly():
    _only_code(
        candidate_schema.validate_batch(
            _batch(schema_version="Fact-Candidate/1")),
        "schema_version")
    _only_code(
        candidate_schema.validate_batch(_batch(schema_version=1)),
        "type_string")
    batch = _batch()
    del batch["schema_version"]
    _only_code(candidate_schema.validate_batch(batch), "missing_field")


def test_complete_must_be_a_real_boolean():
    for value in (1, 0, "true", None):
        errors = candidate_schema.validate_batch(_batch(complete=value))
        assert "type_boolean" in _codes(errors), value


def test_remaining_source_locs_combinations():
    errors = candidate_schema.validate_batch(
        _batch(complete=True, remaining_source_locs=["loc-1"]))
    _only_code(errors, "remaining_source_locs_state")

    errors = candidate_schema.validate_batch(
        _batch(complete=False, remaining_source_locs=[]))
    _only_code(errors, "remaining_source_locs_state")

    errors = candidate_schema.validate_batch(
        _batch(complete=False, remaining_source_locs=["a", "b"]))
    _only_code(errors, "remaining_source_locs_state")

    errors = candidate_schema.validate_batch(
        _batch(complete=False, remaining_source_locs=[""]))
    _only_code(errors, "empty_string")

    errors = candidate_schema.validate_batch(
        _batch(complete=False, remaining_source_locs=[7]))
    _only_code(errors, "type_string")

    errors = candidate_schema.validate_batch(
        _batch(complete=False, remaining_source_locs="loc-1"))
    _only_code(errors, "type_array")


def test_empty_reason_combinations():
    errors = candidate_schema.validate_batch(_batch(items=[]))
    _only_code(errors, "empty_reason_state")

    errors = candidate_schema.validate_batch(
        _batch(items=[], empty_reason=""))
    _only_code(errors, "empty_string")

    errors = candidate_schema.validate_batch(
        _batch(items=[], complete=False,
               remaining_source_locs=["loc-1"], empty_reason="x"))
    _only_code(errors, "empty_reason_state")

    errors = candidate_schema.validate_batch(_batch(empty_reason="x"))
    _only_code(errors, "empty_reason_state")

    errors = candidate_schema.validate_batch(_batch(empty_reason=5))
    _only_code(errors, "type_string")


def test_empty_reason_whitespace_only_is_rejected():
    # P01 section 3.3 requires a real explanation: a string of only
    # whitespace is as empty as "" and gets the same empty_string
    # error with path, rule code and correction advice.
    for reason in (" ", "   ", "\t\n ", "　 "):
        errors = candidate_schema.validate_batch(
            _batch(items=[], empty_reason=reason))
        _only_code(errors, "empty_string"), repr(reason)
        assert errors[0].path == "$.empty_reason"
        assert errors[0].message
        assert errors[0].suggestion


def test_empty_reason_with_real_text_passes_untouched():
    # Real explanation text is never trimmed or rewritten, even when
    # it carries surrounding whitespace.
    batch = _batch(items=[], empty_reason="  本窗口仅为封面，无可提取陈述  ")
    assert candidate_schema.validate_batch(batch) == []
    assert batch["empty_reason"] == "  本窗口仅为封面，无可提取陈述  "


def test_published_schema_empty_reason_constraint_matches_validator():
    branch = None
    for rule in candidate_schema.FACT_CANDIDATE_BATCH_SCHEMA["allOf"]:
        reason_schema = rule.get("then", {}).get(
            "properties", {}).get("empty_reason")
        if isinstance(reason_schema, dict) and \
                reason_schema.get("type") == "string":
            branch = reason_schema
    assert branch is not None, \
        "the empty completed batch branch must constrain empty_reason"
    assert branch.get("minLength") == 1
    pattern = re.compile(branch["pattern"])
    for reason in ("", " ", "   ", "\t\n ", "　 "):
        assert not pattern.search(reason), repr(reason)
        errors = candidate_schema.validate_batch(
            _batch(items=[], empty_reason=reason))
        _only_code(errors, "empty_string"), repr(reason)
    for reason in ("本窗口仅为封面，无可提取陈述", "  padded but real  "):
        assert pattern.search(reason), repr(reason)
        assert candidate_schema.validate_batch(
            _batch(items=[], empty_reason=reason)) == []


def test_items_shape_and_batch_size_budget():
    errors = candidate_schema.validate_batch(_batch(items={}))
    _only_code(errors, "type_array")

    errors = candidate_schema.validate_batch(_batch(items=["nope"]))
    _only_code(errors, "type_object")
    assert errors[0].path == "$.items[0]"

    full = _batch(items=[
        _candidate(local_id="c%d" % index) for index in range(32)
    ])
    assert candidate_schema.validate_batch(full) == []

    over = _batch(items=[
        _candidate(local_id="c%d" % index) for index in range(33)
    ])
    errors = candidate_schema.validate_batch(over)
    assert "array_too_long" in _codes(errors)
    assert "$.items" in _paths_with(errors, "array_too_long")


def test_local_id_rules():
    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(local_id="")]))
    assert "empty_string" in _codes(errors)

    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(local_id=5)]))
    assert "type_string" in _codes(errors)

    pair = _batch(items=[_candidate(local_id="dup"),
                         _candidate(local_id="dup")])
    errors = candidate_schema.validate_batch(pair)
    _only_code(errors, "duplicate_local_id")
    assert errors[0].path == "$.items[1].local_id"


def test_candidate_missing_required_keys():
    candidate = _candidate()
    del candidate["value_raw"]
    del candidate["polarity"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_field")
    assert "$.items[0].value_raw" in _paths_with(errors, "missing_field")
    assert "$.items[0].polarity" in _paths_with(errors, "missing_field")


def test_candidate_rejects_host_and_tree_fields():
    candidate = _candidate()
    candidate["module_key"] = "finance"
    candidate["leaf_code"] = "F-01"
    candidate["fact_uid"] = "fact-1"
    candidate["verified"] = True
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "unknown_field")
    assert {error.path for error in errors} == {
        "$.items[0].module_key", "$.items[0].leaf_code",
        "$.items[0].fact_uid", "$.items[0].verified",
    }


def test_statement_boundaries():
    ok = _batch(items=[_candidate(statement="x" * 2000)])
    assert candidate_schema.validate_batch(ok) == []

    over = _batch(items=[_candidate(statement="x" * 2001)])
    errors = candidate_schema.validate_batch(over)
    _only_code(errors, "string_too_long")

    empty = _batch(items=[_candidate(statement="")])
    errors = candidate_schema.validate_batch(empty)
    _only_code(errors, "empty_string")


def test_entity_and_attribute_boundaries():
    candidate = _candidate(attribute="a" * 256)
    candidate["entity"] = _known("e" * 256)
    assert candidate_schema.validate_batch(
        _batch(items=[candidate])) == []

    candidate = _candidate(attribute="a" * 257)
    candidate["entity"] = _known("e" * 257)
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "string_too_long")
    assert len(errors) == 2


def test_enums_are_exact_and_case_sensitive():
    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(value_type="Number")]))
    _only_code(errors, "enum_value")

    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(statement_kind="Event")]))
    _only_code(errors, "enum_value")

    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(polarity="AFFIRMED")]))
    _only_code(errors, "enum_value")

    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(value_type=1)]))
    _only_code(errors, "type_string")


def test_topic_hint_is_an_optional_string_without_emptiness_rule():
    # P01 section 3.4 fixes no non-empty rule for topic_hint; only the
    # type is checked here, and the response budget still bounds the
    # whole batch.
    assert candidate_schema.validate_batch(
        _batch(items=[_candidate(topic_hint="营收")])) == []
    assert candidate_schema.validate_batch(
        _batch(items=[_candidate(topic_hint="")])) == []

    errors = candidate_schema.validate_batch(
        _batch(items=[_candidate(topic_hint=3)]))
    _only_code(errors, "type_string")


def _candidate_with_located(field, located, unresolved=()):
    candidate = _candidate()
    candidate[field] = located
    candidate["field_bindings"][field] = list(
        located.get("evidence_refs") or [])
    candidate["unresolved"] = list(unresolved)
    return candidate


def test_located_text_object_shape():
    candidate = _candidate()
    candidate["period"] = "2024年"
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_object" in _codes(errors)

    candidate = _candidate()
    del candidate["period"]["reason"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].period.reason" in _paths_with(
        errors, "missing_field")

    candidate = _candidate()
    candidate["period"]["note"] = "额外说明"
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].period.note" in _paths_with(
        errors, "unknown_field")

    candidate = _candidate()
    candidate["period"]["status"] = "missing"
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].period.status" in _paths_with(
        errors, "enum_value")


def test_located_text_known_state_rules():
    located = _known("2024年")
    located["text"] = None
    candidate = _candidate_with_located("period", located)
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_string" in _codes(errors)

    candidate = _candidate_with_located("period", _known(""))
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "empty_string" in _codes(errors)

    located = _known("2024年")
    located["evidence_refs"] = []
    candidate = _candidate_with_located("period", located)
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)

    located = _known("2024年")
    located["reason"] = "多余的原因"
    candidate = _candidate_with_located("period", located)
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)


def test_located_text_not_stated_rules():
    located = {"status": "not_stated", "text": "2024年",
               "evidence_refs": [], "reason": "材料未给期间"}
    candidate = _candidate_with_located(
        "period", located, [_unresolved("period")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)

    located = {"status": "not_stated", "text": None,
               "evidence_refs": ["e1"], "reason": "材料未给期间"}
    candidate = _candidate_with_located("period", located)
    candidate["field_bindings"]["period"] = ["e1"]
    candidate["unresolved"] = [_unresolved("period")]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)

    located = {"status": "not_stated", "text": None,
               "evidence_refs": [], "reason": ""}
    candidate = _candidate_with_located(
        "period", located, [_unresolved("period")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "empty_string" in _codes(errors)

    located = {"status": "not_stated", "text": None,
               "evidence_refs": [], "reason": None}
    candidate = _candidate_with_located(
        "period", located, [_unresolved("period")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_string" in _codes(errors)


def test_located_text_not_applicable_needs_no_unresolved():
    located = {"status": "not_applicable", "text": None,
               "evidence_refs": [],
               "reason": "该陈述为营收总额，不涉及适用规则以外的单位"}
    candidate = _candidate_with_located("unit", located)
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []

    located = {"status": "not_applicable", "text": None,
               "evidence_refs": ["e1"], "reason": "同上"}
    candidate = _candidate_with_located("unit", located)
    candidate["field_bindings"]["unit"] = ["e1"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)


def test_located_text_ambiguous_rules():
    located = {"status": "ambiguous", "text": None,
               "evidence_refs": ["e1"], "reason": "两处表述不一致"}
    candidate = _candidate_with_located(
        "currency", located, [_unresolved("currency")])
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []

    located = {"status": "ambiguous", "text": "人民币",
               "evidence_refs": [], "reason": "猜一个方便值"}
    candidate = _candidate_with_located(
        "currency", located, [_unresolved("currency")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "located_text_state" in _codes(errors)


def test_all_four_located_text_states_in_one_valid_candidate():
    candidate = _candidate()
    candidate["period"] = {
        "status": "not_stated", "text": None, "evidence_refs": [],
        "reason": "材料未给期间",
    }
    candidate["unit"] = {
        "status": "not_applicable", "text": None, "evidence_refs": [],
        "reason": "纯文本陈述无单位",
    }
    candidate["currency"] = {
        "status": "ambiguous", "text": None, "evidence_refs": ["e1"],
        "reason": "前后两处币种表述不一致",
    }
    candidate["field_bindings"]["period"] = []
    candidate["field_bindings"]["unit"] = []
    candidate["field_bindings"]["currency"] = ["e1"]
    candidate["unresolved"] = [
        _unresolved("period"), _unresolved("currency"),
    ]
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []


def test_evidence_count_boundaries():
    candidate = _candidate(evidence=[
        _evidence(ref="e%d" % slot) for slot in range(1, 9)
    ])
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []

    candidate = _candidate(evidence=[
        _evidence(ref="e%d" % slot) for slot in range(1, 10)
    ])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "array_too_long" in _codes(errors)

    candidate = _candidate(evidence=[])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "array_too_short" in _codes(errors)


def test_evidence_item_shape():
    candidate = _candidate(evidence=["quote-only"])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_object" in _codes(errors)

    item = _evidence()
    del item["quote"]
    candidate = _candidate(evidence=[item])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].evidence[0].quote" in _paths_with(
        errors, "missing_field")

    item = _evidence()
    item["page"] = 3
    candidate = _candidate(evidence=[item])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].evidence[0].page" in _paths_with(
        errors, "unknown_field")


def test_evidence_ref_rules():
    candidate = _candidate(evidence=[_evidence(ref="")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "empty_string" in _codes(errors)

    candidate = _candidate(evidence=[_evidence(ref="e1"),
                                     _evidence(ref="e1")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "duplicate_evidence_ref" in _codes(errors)
    assert "$.items[0].evidence[1].evidence_ref" in _paths_with(
        errors, "duplicate_evidence_ref")


def test_read_grant_and_source_loc_are_non_empty_strings():
    candidate = _candidate(evidence=[
        _evidence(read_grant_id="", source_loc="loc-1")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "empty_string" in _codes(errors)

    candidate = _candidate(evidence=[
        _evidence(read_grant_id="grant-1", source_loc=None)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_string" in _codes(errors)


def test_attribution_index_rules():
    for value in (0, 3):
        candidate = _candidate(evidence=[
            _evidence(attribution_index=value)])
        assert candidate_schema.validate_batch(
            _batch(items=[candidate])) == [], value

    candidate = _candidate(evidence=[_evidence(attribution_index=-1)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "integer_range")

    candidate = _candidate(evidence=[_evidence(attribution_index=1.5)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "type_integer")

    candidate = _candidate(evidence=[_evidence(attribution_index="0")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "type_integer")

    candidate = _candidate(evidence=[_evidence(attribution_index=True)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "bool_as_int")


def test_primary_statement_evidence_rejects_null_attribution_index():
    candidate = _candidate(evidence=[_evidence(attribution_index=None)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_attribution_index")
    assert errors[0].path == \
        "$.items[0].evidence[0].attribution_index"

    # The rule follows the statement binding, not the array position.
    candidate = _candidate(evidence=[_evidence(ref="e1"),
                                     _evidence(ref="e2",
                                               attribution_index=None)])
    candidate["field_bindings"]["statement"] = ["e2"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_attribution_index")
    assert errors[0].path == \
        "$.items[0].evidence[1].attribution_index"


def test_null_attribution_index_stays_legal_off_the_statement_binding():
    # Unreferenced header/footnote-style evidence may keep null.
    candidate = _candidate(evidence=[
        _evidence(), _evidence(ref="e2", attribution_index=None)])
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []

    # A field qualifier cited only by a non-statement binding is the
    # P01 section 3.5 header/footnote case; null stays legal there.
    candidate = _candidate(evidence=[
        _evidence(), _evidence(ref="e2", attribution_index=None)])
    candidate["unit"] = _known("万元", refs=("e1", "e2"))
    candidate["field_bindings"]["unit"] = ["e1", "e2"]
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []


def test_quote_boundaries():
    candidate = _candidate(evidence=[_evidence(quote="")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "empty_string")

    candidate = _candidate(evidence=[_evidence(quote="x" * 4000)])
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []

    candidate = _candidate(evidence=[_evidence(quote="x" * 4001)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "string_too_long")


def test_line_number_rules():
    for start, end, code in (
            (0, 4, "integer_range"),
            (-2, 4, "integer_range"),
            (1.5, 4, "type_integer"),
            (3, "4", "type_integer"),
            (True, 4, "bool_as_int"),
            (5, 3, "line_order")):
        candidate = _candidate(evidence=[
            _evidence(start_line=start, end_line=end)])
        errors = candidate_schema.validate_batch(
            _batch(items=[candidate]))
        _only_code(errors, code), (start, end)

    candidate = _candidate(evidence=[_evidence(start_line=3,
                                               end_line=3)])
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []


def test_field_bindings_shape():
    candidate = _candidate()
    del candidate["field_bindings"]["basis"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].field_bindings.basis" in _paths_with(
        errors, "missing_field")

    candidate = _candidate()
    candidate["field_bindings"]["module_key"] = ["e1"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "$.items[0].field_bindings.module_key" in _paths_with(
        errors, "unknown_field")

    candidate = _candidate()
    candidate["field_bindings"]["statement"] = "e1"
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_array" in _codes(errors)

    candidate = _candidate()
    candidate["field_bindings"]["statement"] = [1]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_string" in _codes(errors)


def test_binding_and_located_refs_must_resolve():
    candidate = _candidate()
    candidate["field_bindings"]["statement"] = ["ghost"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "unresolvable_evidence_ref")
    assert errors[0].path == \
        "$.items[0].field_bindings.statement[0]"

    candidate = _candidate()
    candidate["period"] = _known("2024年", refs=("ghost",))
    candidate["field_bindings"]["period"] = ["ghost"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "unresolvable_evidence_ref")
    assert "$.items[0].period.evidence_refs[0]" in _paths_with(
        errors, "unresolvable_evidence_ref")


def test_binding_cannot_reference_another_candidates_evidence():
    first = _candidate(local_id="c1")
    second = _candidate(local_id="c2")
    second["evidence"] = [_evidence(ref="b1")]
    second["field_bindings"] = {
        key: ["b1"] for key in candidate_schema.FIELD_BINDING_KEYS
    }
    second["field_bindings"]["statement"] = ["e1"]
    for field in candidate_schema.LOCATED_TEXT_FIELDS:
        second[field] = _known(second[field]["text"], refs=("b1",))
    errors = candidate_schema.validate_batch(
        _batch(items=[first, second]))
    _only_code(errors, "unresolvable_evidence_ref")
    assert errors[0].path == \
        "$.items[1].field_bindings.statement[0]"


def test_binding_must_match_located_text_refs():
    candidate = _candidate(evidence=[_evidence(ref="e1"),
                                     _evidence(ref="e2")])
    candidate["field_bindings"]["entity"] = ["e2"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "binding_mismatch")
    assert errors[0].path == "$.items[0].field_bindings.entity"

    candidate = _candidate(evidence=[_evidence(ref="e1"),
                                     _evidence(ref="e2")])
    candidate["field_bindings"]["entity"] = ["e1", "e2"]
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "binding_mismatch")


def test_statement_needs_primary_evidence():
    candidate = _candidate()
    candidate["field_bindings"]["statement"] = []
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_primary_evidence")
    assert errors[0].path == "$.items[0].field_bindings.statement"


def test_direct_statement_fields_need_evidence():
    for key in ("attribute", "value_raw", "polarity"):
        candidate = _candidate()
        candidate["field_bindings"][key] = []
        errors = candidate_schema.validate_batch(
            _batch(items=[candidate]))
        _only_code(errors, "missing_field_evidence"), key


def test_statement_kind_binding_rules():
    candidate = _candidate(statement_kind="event")
    candidate["field_bindings"]["statement_kind"] = []
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_field_evidence")

    candidate = _candidate(statement_kind="unknown")
    candidate["field_bindings"]["statement_kind"] = []
    candidate["unresolved"] = [_unresolved("statement_kind")]
    assert candidate_schema.validate_batch(_batch(items=[candidate])) \
        == []


def test_missing_unresolved_for_gaps_and_unknown_kind():
    candidate = _candidate()
    candidate["period"] = {
        "status": "not_stated", "text": None, "evidence_refs": [],
        "reason": "材料未给期间",
    }
    candidate["field_bindings"]["period"] = []
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_unresolved")
    assert errors[0].path == "$.items[0].period"

    candidate = _candidate()
    candidate["currency"] = {
        "status": "ambiguous", "text": None, "evidence_refs": [],
        "reason": "两处不一致",
    }
    candidate["field_bindings"]["currency"] = []
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_unresolved")

    candidate = _candidate(statement_kind="unknown")
    candidate["field_bindings"]["statement_kind"] = []
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    _only_code(errors, "missing_unresolved")
    assert errors[0].path == "$.items[0].statement_kind"


def test_unresolved_entry_shape():
    entry = _unresolved("period")
    entry["extra"] = "x"
    candidate = _candidate(unresolved=[entry])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "unknown_field" in _codes(errors)

    entry = _unresolved("period")
    del entry["needed_clarification"]
    candidate = _candidate(unresolved=[entry])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "missing_field" in _codes(errors)

    candidate = _candidate(unresolved=[_unresolved("")])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "empty_string" in _codes(errors)

    candidate = _candidate(unresolved=[_unresolved("period",
                                                   reason=5)])
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_string" in _codes(errors)

    candidate = _candidate(unresolved="period")
    errors = candidate_schema.validate_batch(_batch(items=[candidate]))
    assert "type_array" in _codes(errors)


def _batch_at_canonical_size(target):
    items = []
    for index in range(12):
        candidate = _candidate(local_id="c%d" % (index + 1))
        candidate["evidence"] = [
            _evidence(ref="e%d" % slot, quote="x" * 1000)
            for slot in range(1, 9)
        ]
        items.append(candidate)
    batch = _batch(items=items)
    size = len(git_format.dumps_record(batch))
    assert size <= target, size
    gap = target - size
    # Every added ASCII quote character costs exactly one canonical
    # byte, so the record can be landed on the budget boundary.
    for candidate in batch["items"]:
        for evidence in candidate["evidence"]:
            room = candidate_schema.MAX_QUOTE_CHARS - \
                len(evidence["quote"])
            add = min(room, gap)
            evidence["quote"] += "x" * add
            gap -= add
    assert gap == 0, gap
    assert len(git_format.dumps_record(batch)) == target
    return batch


def test_response_budget_canonical_size_boundary():
    batch = _batch_at_canonical_size(candidate_schema.MAX_RESPONSE_BYTES)
    assert candidate_schema.validate_batch(batch) == []

    for candidate in batch["items"]:
        grown = False
        for evidence in candidate["evidence"]:
            if len(evidence["quote"]) < \
                    candidate_schema.MAX_QUOTE_CHARS:
                evidence["quote"] += "x"
                grown = True
                break
        if grown:
            break
    else:
        raise AssertionError("no quote room left for the +1 case")
    errors = candidate_schema.validate_batch(batch)
    _only_code(errors, "response_budget")
    assert errors[0].path == "$"


def test_validate_response_bytes_entry_point():
    limit = candidate_schema.MAX_RESPONSE_BYTES
    assert candidate_schema.validate_response_bytes(b"x" * limit) == []
    assert candidate_schema.validate_response_bytes("x" * limit) == []
    errors = candidate_schema.validate_response_bytes(b"x" * (limit + 1))
    _only_code(errors, "response_budget")
    errors = candidate_schema.validate_response_bytes("字" * limit)
    _only_code(errors, "response_budget")
    with pytest.raises(TypeError):
        candidate_schema.validate_response_bytes(3)


def test_validate_candidate_standalone_entry():
    assert candidate_schema.validate_candidate(_candidate()) == []
    candidate = _candidate(evidence=[_evidence(quote="")])
    errors = candidate_schema.validate_candidate(candidate)
    _only_code(errors, "empty_string")
    assert errors[0].path == "$.evidence[0].quote"


def test_validation_does_not_mutate_input():
    batch = _batch()
    batch["items"][0]["entity"]["evidence_refs"] = ["ghost"]
    batch["items"][0]["field_bindings"]["entity"] = ["ghost"]
    snapshot = copy.deepcopy(batch)
    errors = candidate_schema.validate_batch(batch)
    assert errors
    assert batch == snapshot

    valid = _batch()
    assert candidate_schema.validate_batch(valid) == []
    assert valid == _batch()


def test_errors_carry_path_code_message_and_suggestion():
    batch = _batch()
    batch["items"][0]["module_key"] = "finance"
    del batch["items"][0]["polarity"]
    errors = candidate_schema.validate_batch(batch)
    assert _codes(errors) == ["missing_field", "unknown_field"]
    by_code = {error.code: error for error in errors}
    assert by_code["unknown_field"].path == "$.items[0].module_key"
    assert by_code["missing_field"].path == "$.items[0].polarity"
    for error in errors:
        assert error.path.startswith("$")
        assert error.message
        assert error.suggestion
        assert error.as_dict() == {
            "path": error.path,
            "code": error.code,
            "message": error.message,
            "suggestion": error.suggestion,
        }
