"""Combined batch structure + host evidence binding checks.

All batches, grants, attributions and qualifier credentials below are
synthetic fixtures built in this file; no real company data, dossiers,
models or services are touched. The fixtures mimic the
MDWindows.grant() shape plus the persisted attribution shape of
app/document_read/service.py prepared['ranges'], exactly as the
candidate_evidence tests do, wrapped in structurally valid
fact-candidate/1 batches as pinned by the candidate_schema tests.

Host qualifier credentials are scoped per candidate: the batch-level
qualifiers argument is a nested local_id -> evidence_ref -> credential
mapping, because P01 section 3.5 makes an evidence_ref unique only
inside its own candidate. A flat evidence_ref map can never express
two candidates reusing the same ref for different windows.
"""
from copy import deepcopy

from app.facts_tree import candidate_evidence
from app.facts_tree import candidate_schema
from app.facts_tree.candidate_batch_evidence import (
    BatchEvidenceError,
    validate_candidate_batch_evidence,
)
from app.facts_tree.candidate_evidence import EvidenceBindingError
from app.facts_tree.candidate_schema import CandidateSchemaError

COMPANY = "acc-batch-evidence"
FILE_HASH = "sha256:" + "a" * 64
FINGERPRINT = "fp-" + "b" * 32
SOURCE_LOC = "md:#s1"

LINES = {
    "1": "# 年度报告",
    "2": "公司2024年营收为100万元。",
    "3": "付款安排另行约定。",
}


def _grant(grant_id="w0", **overrides):
    grant = {
        "grant_id": grant_id,
        "company_id": COMPANY,
        "mapped_fingerprint": FINGERPRINT,
        "file_hash": FILE_HASH,
        "source_loc": SOURCE_LOC,
        "ranges": [[1, 3]],
        "lines": dict(LINES),
    }
    grant.update(overrides)
    return grant


def _attribution(**overrides):
    entry = {"entity": "公司", "start_line": 2, "end_line": 3}
    entry.update(overrides)
    return entry


def _evidence(ref="e1", **overrides):
    item = {
        "evidence_ref": ref,
        "read_grant_id": "w0",
        "source_loc": SOURCE_LOC,
        "attribution_index": 0,
        "quote": LINES["2"],
        "start_line": 2,
        "end_line": 2,
    }
    item.update(overrides)
    return item


def _header_evidence():
    """Evidence citing only line 1 (the title) with null attribution."""
    return _evidence(ref="e2", attribution_index=None, quote=LINES["1"],
                     start_line=1, end_line=1)


def _qualifier():
    return {"qualifier_kind": "header", "read_grant_id": "w0",
            "start_line": 1, "end_line": 1}


def _known(text, refs=("e1",)):
    return {
        "status": "known",
        "text": text,
        "evidence_refs": list(refs),
        "reason": None,
    }


def _candidate(local_id="c1", evidence=None, entity_refs=("e1",),
               **overrides):
    if evidence is None:
        evidence = [_evidence()]
    bindings = {key: ["e1"] for key in (
        "statement", "entity", "attribute", "value_raw", "period",
        "unit", "currency", "scope", "basis", "claimant",
        "statement_kind", "polarity")}
    bindings["entity"] = list(entity_refs)
    candidate = {
        "local_id": local_id,
        "statement": "公司2024年营收为100万元。",
        "entity": _known("公司", entity_refs),
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
        "evidence": evidence,
        "field_bindings": bindings,
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


def _call(payload, **overrides):
    params = dict(
        company_id=COMPANY,
        file_hash=FILE_HASH,
        mapped_fingerprint=FINGERPRINT,
        current_source_loc=SOURCE_LOC,
        grants=[_grant()],
        attributions={"w0": [_attribution()]},
    )
    params.update(overrides)
    return validate_candidate_batch_evidence(payload, **params)


def _codes(errors):
    return [error.code for error in errors]


def _evidence_spy(monkeypatch):
    """Record calls to the evidence validator and return the call log.

    The real validator still runs, so returned errors stay genuine;
    tests assert on the log to prove the call happened or never did.
    """
    calls = []
    real = candidate_evidence.validate_candidate_evidence

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(candidate_evidence,
                        "validate_candidate_evidence", spy)
    return calls


def test_valid_complete_batch_passes():
    assert _call(_batch()) == []


def test_valid_incomplete_batch_passes_when_remaining_matches():
    payload = _batch(complete=False, remaining_source_locs=[SOURCE_LOC])
    assert _call(payload) == []


def test_valid_multi_candidate_batch_passes():
    payload = _batch(items=[_candidate("c1"), _candidate("c2")])
    assert _call(payload) == []


def test_empty_completed_batch_with_reason_passes():
    payload = _batch(items=[], empty_reason="标题窗口不含候选事实")
    assert _call(payload) == []


def test_incomplete_batch_with_wrong_remaining_loc_rejected():
    payload = _batch(complete=False, remaining_source_locs=["md:#s9"])
    errors = _call(payload)
    assert _codes(errors) == ["remaining_source_loc_mismatch"]
    error = errors[0]
    assert isinstance(error, BatchEvidenceError)
    assert error.path == "$.remaining_source_locs[0]"
    assert error.as_dict() == {
        "path": "$.remaining_source_locs[0]",
        "code": "remaining_source_loc_mismatch",
        "message": error.message,
        "suggestion": error.suggestion,
    }


def test_remaining_loc_comparison_is_exact():
    # A trailing space already differs; no normalization is applied.
    payload = _batch(complete=False,
                     remaining_source_locs=[SOURCE_LOC + " "])
    assert _codes(_call(payload)) == ["remaining_source_loc_mismatch"]


def test_incomplete_batch_combines_remaining_and_evidence_errors():
    payload = _batch(
        complete=False,
        remaining_source_locs=["md:#s9"],
        items=[_candidate(evidence=[_evidence(read_grant_id="w9")])],
    )
    errors = _call(payload)
    assert _codes(errors) == ["remaining_source_loc_mismatch",
                              "unknown_grant_id"]
    assert isinstance(errors[0], BatchEvidenceError)
    assert isinstance(errors[1], EvidenceBindingError)


def test_complete_batch_with_remaining_entries_is_schema_error():
    # The complete=true empty-array rule stays the schema's own rule;
    # this layer adds no code of its own for it.
    payload = _batch(complete=True, remaining_source_locs=[SOURCE_LOC])
    errors = _call(payload)
    assert _codes(errors) == ["remaining_source_locs_state"]
    assert isinstance(errors[0], CandidateSchemaError)


def test_empty_completed_batch_without_reason_is_schema_error():
    errors = _call(_batch(items=[]))
    assert _codes(errors) == ["empty_reason_state"]
    assert isinstance(errors[0], CandidateSchemaError)


def test_invalid_schema_batch_never_calls_evidence(monkeypatch):
    calls = _evidence_spy(monkeypatch)
    payload = _batch()
    payload["company_id"] = "acc-forged"  # unknown batch key
    payload["items"][0]["leaf_code"] = "F1"  # unknown candidate key
    errors = _call(payload)
    assert calls == []
    assert errors
    assert all(isinstance(error, CandidateSchemaError) for error in errors)
    assert "unknown_field" in _codes(errors)


def test_duplicate_local_id_stays_schema_error_and_skips_evidence(
        monkeypatch):
    calls = _evidence_spy(monkeypatch)
    payload = _batch(items=[_candidate("c1"), _candidate("c1")])
    errors = _call(payload)
    assert calls == []
    assert _codes(errors) == ["duplicate_local_id"]
    assert isinstance(errors[0], CandidateSchemaError)


def test_duplicate_evidence_ref_stays_schema_error_and_skips_evidence(
        monkeypatch):
    calls = _evidence_spy(monkeypatch)
    payload = _batch(items=[_candidate(evidence=[_evidence(),
                                                 _evidence()])])
    errors = _call(payload)
    assert calls == []
    assert "duplicate_evidence_ref" in _codes(errors)
    assert all(isinstance(error, CandidateSchemaError) for error in errors)


def test_each_candidate_is_bound_with_batch_rooted_paths(monkeypatch):
    calls = _evidence_spy(monkeypatch)
    payload = _batch(items=[_candidate("c1"), _candidate("c2")])
    assert _call(payload) == []
    assert len(calls) == 2
    assert [kwargs["path"] for _, kwargs in calls] == [
        "$.items[0]", "$.items[1]"]


def test_unknown_grant_error_points_inside_the_batch():
    payload = _batch(items=[
        _candidate("c1"),
        _candidate("c2", evidence=[_evidence(read_grant_id="w9")]),
    ])
    errors = _call(payload)
    assert _codes(errors) == ["unknown_grant_id"]
    assert isinstance(errors[0], EvidenceBindingError)
    assert errors[0].path == "$.items[1].evidence[0].read_grant_id"


def test_wrong_grant_company_delegated_unchanged():
    errors = _call(_batch(), grants=[_grant(company_id="acc-other")])
    assert _codes(errors) == ["company_mismatch"]
    assert isinstance(errors[0], EvidenceBindingError)


def test_quote_mismatch_delegated_unchanged():
    payload = _batch(items=[_candidate(evidence=[
        _evidence(quote="材料中不存在的一句。")])])
    errors = _call(payload)
    assert _codes(errors) == ["quote_mismatch"]
    assert isinstance(errors[0], EvidenceBindingError)


def test_attribution_index_out_of_range_delegated_unchanged():
    payload = _batch(items=[_candidate(evidence=[
        _evidence(attribution_index=7)])])
    errors = _call(payload)
    assert _codes(errors) == ["attribution_index_out_of_range"]
    assert isinstance(errors[0], EvidenceBindingError)


def test_null_attribution_qualifier_credential_passed_through():
    payload = _batch(items=[_candidate(
        evidence=[_evidence(), _header_evidence()],
        entity_refs=("e1", "e2"))])
    assert _call(payload, qualifiers={"c1": {"e2": _qualifier()}}) == []


def test_null_attribution_without_qualifier_fails_closed():
    payload = _batch(items=[_candidate(
        evidence=[_evidence(), _header_evidence()],
        entity_refs=("e1", "e2"))])
    errors = _call(payload)
    assert _codes(errors) == ["missing_qualifier_kind"]
    assert isinstance(errors[0], EvidenceBindingError)


def _reread_grants():
    """Current window w0 plus a same-file/same-mapping re-read window."""
    return [_grant(), _grant("w1", source_loc="md:#s2")]


def _reread_header_evidence():
    """Line-1 evidence citing the re-read window with null attribution."""
    return _evidence(ref="e2", read_grant_id="w1", source_loc="md:#s2",
                     attribution_index=None, quote=LINES["1"],
                     start_line=1, end_line=1)


def test_current_source_loc_forwarded_to_evidence_binding(monkeypatch):
    calls = _evidence_spy(monkeypatch)
    assert _call(_batch()) == []
    assert [kwargs["current_source_loc"] for _, kwargs in calls] == [
        SOURCE_LOC]


def test_statement_evidence_from_reread_window_rejected():
    # The host really displayed the re-read window (same file_hash and
    # mapped_fingerprint, its own attribution), but the statement
    # evidence must still come from the current read window.
    attributions = {"w0": [_attribution()], "w1": [_attribution()]}
    payload = _batch(items=[_candidate(evidence=[
        _evidence(read_grant_id="w1", source_loc="md:#s2")])])
    errors = _call(payload, grants=_reread_grants(),
                   attributions=attributions)
    assert _codes(errors) == ["statement_not_current_window"]
    assert isinstance(errors[0], EvidenceBindingError)
    assert errors[0].path == "$.items[0].evidence[0].read_grant_id"


def test_reread_window_qualifier_only_passes():
    # The identical re-read window is legal when its evidence backs
    # only a non-statement qualifier field with a trusted credential.
    payload = _batch(items=[_candidate(
        evidence=[_evidence(), _reread_header_evidence()],
        entity_refs=("e1", "e2"))])
    qualifiers = {"c1": {"e2": {"qualifier_kind": "header",
                                "read_grant_id": "w1",
                                "start_line": 1, "end_line": 1}}}
    assert _call(payload, grants=_reread_grants(),
                 qualifiers=qualifiers) == []


def _reread_qualifier():
    """Trusted header credential bound to the re-read window w1."""
    return {"qualifier_kind": "header", "read_grant_id": "w1",
            "start_line": 1, "end_line": 1}


def _two_window_qualifier_batch():
    """Two candidates reusing evidence_ref e2 for different windows.

    c1's e2 is the current window w0 header; c2's e2 is the same-file
    re-read window w1 header. Per P01 section 3.5 the ref is unique
    only inside its own candidate, so both uses are legal and each
    needs its own trusted credential.
    """
    return _batch(items=[
        _candidate("c1", evidence=[_evidence(), _header_evidence()],
                   entity_refs=("e1", "e2")),
        _candidate("c2", evidence=[_evidence(), _reread_header_evidence()],
                   entity_refs=("e1", "e2")),
    ])


def test_shared_evidence_ref_passes_with_per_candidate_credentials():
    # The reproduced collision: both candidates use evidence_ref "e2"
    # for header evidence in different windows; the nested scopes let
    # each bind its own legal credential.
    qualifiers = {"c1": {"e2": _qualifier()},
                  "c2": {"e2": _reread_qualifier()}}
    assert _call(_two_window_qualifier_batch(), grants=_reread_grants(),
                 qualifiers=qualifiers) == []


def test_each_candidate_receives_only_its_own_qualifier_scope(
        monkeypatch):
    calls = _evidence_spy(monkeypatch)
    scoped = {"c1": {"e2": _qualifier()}, "c2": {"e2": _reread_qualifier()}}
    assert _call(_two_window_qualifier_batch(), grants=_reread_grants(),
                 qualifiers=scoped) == []
    assert len(calls) == 2
    assert [kwargs["qualifiers"] for _, kwargs in calls] == [
        {"e2": _qualifier()}, {"e2": _reread_qualifier()}]


def test_credential_under_wrong_candidate_fails_closed():
    swapped = {"c1": {"e2": _reread_qualifier()},
               "c2": {"e2": _qualifier()}}
    errors = _call(_two_window_qualifier_batch(),
                   grants=_reread_grants(), qualifiers=swapped)
    assert _codes(errors) == ["qualifier_binding_mismatch",
                              "qualifier_binding_mismatch"]
    assert [error.path for error in errors] == [
        "$.items[0].evidence[1].attribution_index",
        "$.items[1].evidence[1].attribution_index"]
    assert all(isinstance(error, EvidenceBindingError) for error in errors)


def test_candidate_cannot_borrow_another_candidates_credential():
    # Only c2's scope is provided; c1 must fail closed even though the
    # other candidate's credential uses the same evidence_ref.
    errors = _call(_two_window_qualifier_batch(), grants=_reread_grants(),
                   qualifiers={"c2": {"e2": _reread_qualifier()}})
    assert _codes(errors) == ["missing_qualifier_kind"]
    assert errors[0].path == "$.items[0].evidence[1].attribution_index"


def test_flat_qualifiers_shape_never_binds_across_candidates():
    # The old flat shape selects no candidate scope, so every
    # null-attribution evidence fails closed instead of silently
    # binding whichever credential the single flat slot happened to
    # hold.
    errors = _call(_two_window_qualifier_batch(), grants=_reread_grants(),
                   qualifiers={"e2": _qualifier()})
    assert _codes(errors) == ["missing_qualifier_kind",
                              "missing_qualifier_kind"]
    assert [error.path for error in errors] == [
        "$.items[0].evidence[1].attribution_index",
        "$.items[1].evidence[1].attribution_index"]


def test_host_context_rejects_non_string_company_id():
    errors = _call(_batch(), company_id=123)
    assert _codes(errors) == ["host_context_invalid"]
    assert isinstance(errors[0], BatchEvidenceError)
    assert errors[0].path == "$.host.company_id"


def test_host_context_rejects_empty_file_hash():
    errors = _call(_batch(), file_hash="")
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.file_hash"


def test_host_context_rejects_none_mapped_fingerprint():
    errors = _call(_batch(), mapped_fingerprint=None)
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.mapped_fingerprint"


def test_host_context_rejects_empty_current_source_loc():
    errors = _call(_batch(), current_source_loc="")
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.current_source_loc"


def test_host_context_rejects_non_list_grants():
    errors = _call(_batch(), grants="w0")
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.grants"


def test_host_context_rejects_non_dict_grant_entries():
    errors = _call(_batch(), grants=[_grant(), "w1"])
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.grants"


def test_host_context_rejects_non_dict_attributions():
    errors = _call(_batch(), attributions=[("w0", [_attribution()])])
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.attributions"


def test_host_context_rejects_non_dict_qualifiers():
    errors = _call(_batch(), qualifiers=["e2"])
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.qualifiers"


def test_host_context_rejects_non_dict_qualifier_scope():
    errors = _call(_batch(), qualifiers={"c1": ["e2"]})
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.qualifiers"


def test_host_context_rejects_empty_qualifier_scope_key():
    errors = _call(_batch(), qualifiers={"": {"e2": _qualifier()}})
    assert _codes(errors) == ["host_context_invalid"]
    assert errors[0].path == "$.host.qualifiers"


def test_host_context_reports_each_invalid_parameter():
    errors = _call(_batch(), company_id="", file_hash=None, grants="w0")
    assert _codes(errors) == ["host_context_invalid"] * 3
    assert [error.path for error in errors] == [
        "$.host.company_id", "$.host.file_hash", "$.host.grants"]


def test_host_context_failure_precedes_schema_validation(monkeypatch):
    calls = []
    real = candidate_schema.validate_batch

    def spy(payload):
        calls.append(payload)
        return real(payload)

    monkeypatch.setattr(candidate_schema, "validate_batch", spy)
    payload = _batch()
    payload["forged"] = True
    errors = _call(payload, company_id="")
    assert calls == []
    assert _codes(errors) == ["host_context_invalid"]


def test_inputs_are_never_mutated():
    payload = _batch(complete=False, remaining_source_locs=[SOURCE_LOC])
    bad_quote = _batch(items=[_candidate(evidence=[
        _evidence(quote="材料中不存在的一句。")])])
    grants = [_grant()]
    attributions = {"w0": [_attribution()]}
    qualifiers = {"c1": {"e2": _qualifier()}}
    snapshot = deepcopy((payload, bad_quote, grants, attributions,
                         qualifiers))
    _call(payload, grants=grants, attributions=attributions,
          qualifiers=qualifiers)
    _call(bad_quote, grants=grants, attributions=attributions,
          qualifiers=qualifiers)
    assert (payload, bad_quote, grants, attributions, qualifiers) == \
        snapshot
