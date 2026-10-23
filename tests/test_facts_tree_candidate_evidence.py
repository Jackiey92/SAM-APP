"""Synthetic host read-window/attribution binding checks.

All grants, attributions, qualifier credentials and candidates below
are synthetic fixtures built in this file; no real company data,
dossiers or services are touched. The fixtures mimic the
MDWindows.grant() shape (grant_id, company_id, mapped_fingerprint,
file_hash, source_loc, ranges, lines) plus per-grant persisted
attribution ranges in the real host shape of
app/document_read/service.py prepared['ranges']: a list of
{'entity': str, 'start_line': int, 'end_line': int} dicts.
"""
from copy import deepcopy

from app.facts_tree.candidate_evidence import (
    EvidenceBindingError,
    normalize_whitespace,
    validate_candidate_evidence,
)

COMPANY = "acc-evidence-bind"
FILE_HASH = "sha256:" + "a" * 64
FINGERPRINT = "fp-" + "b" * 32

LINES = {
    "1": "# 增资协议",
    "2": "甲公司与乙公司签订增资协议。",
    "3": "本轮融资总额为人民币100万元。",
    "4": "付款安排另行约定。",
    "5": "本轮融资总额为人民币100万元。",
}


def ranges_for(lines):
    """Group contiguous line numbers into [start, end] pairs."""
    numbers = sorted(int(n) for n in lines)
    ranges = []
    for number in numbers:
        if ranges and ranges[-1][1] == number - 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def make_grant(grant_id="w0", lines=None, source_loc="md:#s1",
               company=COMPANY, file_hash=FILE_HASH,
               fingerprint=FINGERPRINT, ranges=None):
    lines = LINES if lines is None else lines
    return {
        "grant_id": grant_id,
        "company_id": company,
        "mapped_fingerprint": fingerprint,
        "file_hash": file_hash,
        "original_name": "agreement.md",
        "source_loc": source_loc,
        "ranges": ranges_for(lines) if ranges is None else ranges,
        "lines": dict(lines),
    }


def make_attribution(start=3, end=4, entity="甲公司"):
    return {"entity": entity, "start_line": start, "end_line": end}


def make_evidence(ref="e1", grant_id="w0", index=0, quote=None,
                  start=3, end=3, source_loc="md:#s1"):
    return {
        "evidence_ref": ref,
        "read_grant_id": grant_id,
        "source_loc": source_loc,
        "attribution_index": index,
        "quote": LINES["3"] if quote is None else quote,
        "start_line": start,
        "end_line": end,
    }


def make_candidate(evidence, statement_refs=("e1",), extra_bindings=None):
    bindings = {key: [] for key in (
        "statement", "entity", "attribute", "value_raw", "period",
        "unit", "currency", "scope", "basis", "claimant",
        "statement_kind", "polarity")}
    bindings["statement"] = list(statement_refs)
    if extra_bindings:
        bindings.update(extra_bindings)
    return {
        "local_id": "c1",
        "statement": "本轮融资总额为人民币100万元。",
        "evidence": evidence,
        "field_bindings": bindings,
    }


def call(candidate, grants=None, attributions=None, **overrides):
    params = dict(
        company_id=COMPANY,
        file_hash=FILE_HASH,
        mapped_fingerprint=FINGERPRINT,
        current_source_loc="md:#s1",
        grants=[make_grant()] if grants is None else grants,
        attributions={"w0": [make_attribution()]} if attributions is None
        else attributions,
    )
    params.update(overrides)
    return validate_candidate_evidence(candidate, **params)


def codes(errors):
    return [e.code for e in errors]


def test_valid_primary_evidence_passes():
    candidate = make_candidate([make_evidence()])
    assert call(candidate) == []


def test_quote_with_surrounding_whitespace_passes_normalization():
    quote = "  本轮融资总额为人民币100万元。\n "
    candidate = make_candidate([make_evidence(quote=quote)])
    assert call(candidate) == []
    assert normalize_whitespace(quote) == LINES["3"]


def test_quote_with_extra_inner_whitespace_passes_normalization():
    # Extra spaces the original never had: whitespace is deleted for
    # matching only; the candidate quote itself is never rewritten.
    quote = "本轮融资总额为 人民币 100万元 。"
    candidate = make_candidate([make_evidence(quote=quote)])
    assert call(candidate) == []
    assert normalize_whitespace(quote) == LINES["3"]


def test_multiline_quote_passes_and_is_checked_per_line():
    quote = "本轮融资总额为人民币100万元。\n付款安排另行约定。"
    candidate = make_candidate([make_evidence(quote=quote, start=3, end=4)])
    assert call(candidate) == []


def test_unknown_grant_id_rejected():
    candidate = make_candidate([make_evidence(grant_id="w9")])
    assert codes(call(candidate)) == ["unknown_grant_id"]


def test_duplicate_grant_id_rejected():
    grants = [make_grant(), make_grant()]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["duplicate_grant_id"]


def test_company_mismatch_rejected():
    grants = [make_grant(company="acc-other")]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["company_mismatch"]


def test_file_mismatch_rejected():
    grants = [make_grant(file_hash="sha256:" + "c" * 64)]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["file_mismatch"]


def test_mapping_mismatch_rejected():
    grants = [make_grant(fingerprint="fp-" + "d" * 32)]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["mapping_mismatch"]


def test_source_loc_mismatch_rejected():
    candidate = make_candidate([make_evidence(source_loc="md:#s2")])
    assert codes(call(candidate)) == ["source_loc_mismatch"]


def reread_grants():
    """Current window w0 plus a same-file/same-mapping re-read window."""
    return [make_grant("w0"), make_grant("w1", source_loc="md:#s2")]


def test_statement_evidence_from_reread_window_rejected():
    # The re-read window is genuinely host-displayed: same file_hash,
    # same mapped_fingerprint, its own persisted attribution. It still
    # cannot carry the primary statement evidence.
    attributions = {"w0": [make_attribution()],
                    "w1": [make_attribution()]}
    candidate = make_candidate([
        make_evidence(grant_id="w1", source_loc="md:#s2")])
    errors = call(candidate, grants=reread_grants(),
                  attributions=attributions)
    assert codes(errors) == ["statement_not_current_window"]
    assert errors[0].path == "$.evidence[0].read_grant_id"


def test_shared_ref_on_reread_window_rejected_when_statement_cites_it():
    # Citing the same re-read evidence from a non-statement field as
    # well does not weaken the statement rule: statement wins.
    attributions = {"w0": [make_attribution()],
                    "w1": [make_attribution()]}
    candidate = make_candidate(
        [make_evidence(grant_id="w1", source_loc="md:#s2")],
        extra_bindings={"entity": ["e1"]})
    assert codes(call(candidate, grants=reread_grants(),
                      attributions=attributions)) == [
        "statement_not_current_window"]


def test_reread_window_evidence_allowed_for_qualifier_fields():
    # The identical re-read window passes when its evidence only backs
    # a non-statement qualifier field with a trusted credential.
    qualifier = make_evidence(ref="e2", grant_id="w1",
                              source_loc="md:#s2", index=None,
                              quote="# 增资协议", start=1, end=1)
    candidate = make_candidate([make_evidence(), qualifier],
                               extra_bindings={"entity": ["e2"]})
    qualifiers = {"e2": {"qualifier_kind": "header",
                         "read_grant_id": "w1",
                         "start_line": 1, "end_line": 1}}
    assert call(candidate, grants=reread_grants(),
                qualifiers=qualifiers) == []


def test_missing_or_invalid_current_source_loc_fails_closed():
    # Without the host's trusted current source_loc the statement rule
    # cannot run; it fails closed instead of being skipped silently.
    candidate = make_candidate([make_evidence()])
    for bad in (None, "", 7):
        errors = call(candidate, current_source_loc=bad)
        assert codes(errors) == ["current_source_loc_invalid"]
        assert errors[0].path == "$.host.current_source_loc"


def test_line_outside_grant_rejected():
    lines = {k: v for k, v in LINES.items() if k != "3"}
    grants = [make_grant(lines=lines)]
    candidate = make_candidate([make_evidence()])
    result = codes(call(candidate, grants=grants))
    # The same invalid candidate also leaves the default attribution
    # range [3,4] outside the shrunken grant; that independent
    # rejection is allowed, the expected rule code must be present.
    assert "line_not_granted" in result
    assert set(result) <= {"line_not_granted",
                           "attribution_range_outside_grant"}


def test_range_spanning_authorization_gap_rejected():
    lines = {k: v for k, v in LINES.items() if k != "4"}
    grants = [make_grant(lines=lines)]
    candidate = make_candidate([make_evidence(
        quote="本轮融资总额为人民币100万元。\n本轮融资总额为人民币100万元。",
        start=3, end=5)])
    result = codes(call(candidate, grants=grants))
    assert "line_not_granted" in result
    assert set(result) <= {"line_not_granted",
                           "attribution_range_outside_grant"}


def test_grant_ranges_lines_misalignment_rejected():
    # ranges claim line 6 while the lines dict ends at 5.
    grants = [make_grant(ranges=[[1, 6]])]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["grant_lines_mismatch"]


def test_grant_lines_without_range_cover_rejected():
    # lines holds line 6 but ranges stop at 5.
    lines = dict(LINES)
    lines["6"] = "附则另行规定。"
    grants = [make_grant(lines=lines, ranges=[[1, 5]])]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["grant_lines_mismatch"]


def test_grant_empty_ranges_rejected():
    grants = [make_grant(ranges=[])]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["grant_range_invalid"]


def test_grant_overlapping_or_duplicate_ranges_rejected():
    for bad in ([[1, 3], [3, 5]], [[1, 2], [1, 2]]):
        grants = [make_grant(ranges=[list(r) for r in bad])]
        candidate = make_candidate([make_evidence()])
        assert codes(call(candidate, grants=grants)) == [
            "grant_range_invalid"]


def test_grant_illegal_range_entry_rejected():
    for bad in ([[4, 2]], [["3", 4]], [[True, 4]]):
        grants = [make_grant(ranges=[list(r) for r in bad])]
        candidate = make_candidate([make_evidence()])
        assert codes(call(candidate, grants=grants)) == [
            "grant_range_invalid"]


def test_grant_non_canonical_line_key_rejected():
    # "03" is numeric but not the canonical decimal string of 3.
    lines = dict(LINES)
    lines["03"] = lines.pop("3")
    grants = [make_grant(lines=lines, ranges=[[1, 5]])]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["grant_malformed"]


def test_grant_unicode_digit_line_key_rejected():
    # "²".isdigit() is true but int("²") raises ValueError; the
    # validator must report grant_malformed instead of raising.
    for bad_key in ("²", "①", "٣"):
        lines = dict(LINES)
        lines[bad_key] = lines.pop("3")
        grants = [make_grant(lines=lines, ranges=[[1, 5]])]
        candidate = make_candidate([make_evidence()])
        assert codes(call(candidate, grants=grants)) == ["grant_malformed"]


def test_grant_non_string_line_text_rejected():
    lines = dict(LINES)
    lines["3"] = None
    grants = [make_grant(lines=lines)]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == ["grant_malformed"]


def test_huge_grant_range_rejected_without_materializing():
    # A corrupt host range endpoint near 1e12 must fail closed
    # arithmetically, never via set(range(...)).
    grants = [make_grant(ranges=[[1, 10**12]])]
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, grants=grants)) == [
        "grant_lines_mismatch"]


def test_huge_candidate_coordinates_rejected_without_materializing():
    candidate = make_candidate([make_evidence(start=1, end=10**12)])
    result = codes(call(candidate))
    assert "line_not_granted" in result
    assert set(result) <= {"line_not_granted"}


def test_huge_attribution_range_rejected_without_materializing():
    attributions = {"w0": [make_attribution(1, 10**12)]}
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, attributions=attributions)) == [
        "attribution_range_outside_grant"]


def test_forged_quote_rejected():
    candidate = make_candidate([make_evidence(quote="本轮融资总额为人民币120万元。")])
    assert codes(call(candidate)) == ["quote_mismatch"]


def test_duplicate_quote_disambiguated_by_precise_range():
    # Lines 3 and 5 hold the same text; the exact second range works.
    candidate = make_candidate([make_evidence(start=5, end=5)],
                               statement_refs=("e1",))
    errors = call(candidate, attributions={"w0": [make_attribution(5, 5)]})
    assert errors == []


def test_duplicate_quote_with_wrong_range_rejected():
    # Same text on lines 3 and 5, but the declared span 3-5 matches
    # neither occurrence exactly: never default to the first one. The
    # attribution range covers 3-5 so only quote locating is exercised.
    candidate = make_candidate([make_evidence(start=3, end=5)],
                               statement_refs=("e1",))
    assert codes(call(candidate, attributions={
        "w0": [make_attribution(3, 5)]})) == ["quote_line_range_mismatch"]


def test_quote_ambiguous_when_range_cannot_disambiguate():
    lines = dict(LINES)
    lines["6"] = "重复 重复"
    grants = [make_grant(lines=lines)]
    candidate = make_candidate([make_evidence(
        quote="重复", start=6, end=6)], statement_refs=("e1",))
    errors = call(candidate, grants=grants,
                  attributions={"w0": [make_attribution(6, 6)]})
    assert codes(errors) == ["quote_ambiguous"]


def test_quote_line_range_mismatch_when_bounds_do_not_match():
    # The attribution range covers 2-3 so only the quote span check
    # can fire; the intent of this case is quote location only.
    candidate = make_candidate([make_evidence(
        quote="本轮融资总额为人民币100万元。", start=2, end=3)])
    assert codes(call(candidate, attributions={
        "w0": [make_attribution(2, 3)]})) == ["quote_line_range_mismatch"]


def test_attribution_index_bool_rejected():
    candidate = make_candidate([make_evidence(index=True)])
    assert codes(call(candidate)) == ["bool_as_int"]


def test_attribution_index_negative_rejected():
    candidate = make_candidate([make_evidence(index=-1)])
    assert codes(call(candidate)) == ["integer_range"]


def test_attribution_index_out_of_range_rejected():
    candidate = make_candidate([make_evidence(index=1)])
    assert codes(call(candidate)) == ["attribution_index_out_of_range"]


def test_attribution_index_cannot_borrow_other_grants_list():
    grants = [make_grant("w0"), make_grant("w1", source_loc="md:#s1")]
    attributions = {"w0": [make_attribution(3, 4), make_attribution(2, 2)],
                    "w1": [make_attribution(3, 3)]}
    # Index 1 exists only in w0's list; the evidence cites w1.
    candidate = make_candidate([make_evidence(grant_id="w1", index=1)])
    assert codes(call(candidate, grants=grants,
                      attributions=attributions)) == [
        "attribution_index_out_of_range"]


def test_attribution_index_without_persisted_ranges_rejected():
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, attributions={})) == [
        "missing_attribution_ranges"]


def test_dict_shaped_persisted_attribution_entry_passes():
    # The real host receipt shape {'entity','start_line','end_line'}
    # is indexed directly.
    attributions = {"w0": [{"entity": "乙公司", "start_line": 3,
                            "end_line": 3}]}
    candidate = make_candidate([make_evidence()])
    assert call(candidate, attributions=attributions) == []


def test_legacy_pair_attribution_entry_rejected():
    attributions = {"w0": [[3, 4]]}
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, attributions=attributions)) == [
        "attribution_range_invalid"]


def test_attribution_range_outside_grant_rejected():
    attributions = {"w0": [make_attribution(3, 9)]}
    candidate = make_candidate([make_evidence()])
    assert codes(call(candidate, attributions=attributions)) == [
        "attribution_range_outside_grant"]


def test_evidence_coordinates_outside_attribution_range_rejected():
    candidate = make_candidate([make_evidence(start=2, end=3)],
                               statement_refs=("e1",))
    # Quote spans lines 2-3 but the attribution range covers only 3-4.
    candidate["evidence"][0]["quote"] = (
        "甲公司与乙公司签订增资协议。\n本轮融资总额为人民币100万元。")
    assert codes(call(candidate)) == ["attribution_range_exceeded"]


def test_statement_evidence_null_attribution_rejected():
    candidate = make_candidate([make_evidence(index=None)])
    assert codes(call(candidate)) == ["missing_attribution_index"]


def test_field_qualifier_null_attribution_without_credential_rejected():
    # A non-statement field binding alone never proves the
    # header/footnote semantic category; without a trusted host
    # qualifier_kind credential the rejection is stable.
    qualifier = make_evidence(ref="e2", index=None, quote="# 增资协议",
                              start=1, end=1)
    candidate = make_candidate(
        [make_evidence(), qualifier],
        extra_bindings={"entity": ["e2"]})
    assert codes(call(candidate)) == ["missing_qualifier_kind"]


def test_field_qualifier_with_valid_synthetic_credential_passes():
    qualifier = make_evidence(ref="e2", index=None, quote="# 增资协议",
                              start=1, end=1)
    candidate = make_candidate(
        [make_evidence(), qualifier],
        extra_bindings={"entity": ["e2"]})
    qualifiers = {"e2": {"qualifier_kind": "header", "read_grant_id": "w0",
                         "start_line": 1, "end_line": 1}}
    assert call(candidate, qualifiers=qualifiers) == []


def test_field_qualifier_credential_binding_mismatch_rejected():
    qualifier = make_evidence(ref="e2", index=None, quote="# 增资协议",
                              start=1, end=1)
    candidate = make_candidate(
        [make_evidence(), qualifier],
        extra_bindings={"entity": ["e2"]})
    qualifiers = {"e2": {"qualifier_kind": "footnote",
                         "read_grant_id": "w0",
                         "start_line": 1, "end_line": 2}}
    assert codes(call(candidate, qualifiers=qualifiers)) == [
        "qualifier_binding_mismatch"]


def test_null_attribution_without_field_binding_rejected():
    orphan = make_evidence(ref="e2", index=None, quote="# 增资协议",
                           start=1, end=1)
    candidate = make_candidate([make_evidence(), orphan])
    assert codes(call(candidate)) == ["null_attribution_unbound"]


def test_inputs_are_not_mutated():
    qualifier = make_evidence(ref="e2", index=None, quote="# 增资协议",
                              start=1, end=1)
    candidate = make_candidate(
        [make_evidence(), qualifier],
        extra_bindings={"entity": ["e2"]})
    grants = [make_grant()]
    attributions = {"w0": [make_attribution()]}
    qualifiers = {"e2": {"qualifier_kind": "header", "read_grant_id": "w0",
                         "start_line": 1, "end_line": 1}}
    snapshot = (deepcopy(candidate), deepcopy(grants),
                deepcopy(attributions), deepcopy(qualifiers))
    assert call(candidate, grants=grants, attributions=attributions,
                qualifiers=qualifiers) == []
    assert (candidate, grants, attributions, qualifiers) == snapshot


def test_error_shape_is_structured():
    candidate = make_candidate([make_evidence(grant_id="w9")])
    (error,) = call(candidate)
    assert isinstance(error, EvidenceBindingError)
    data = error.as_dict()
    assert data["path"] == "$.evidence[0].read_grant_id"
    assert data["code"] == "unknown_grant_id"
    assert data["message"] and data["suggestion"]
