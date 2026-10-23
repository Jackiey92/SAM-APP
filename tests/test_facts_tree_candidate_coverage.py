"""Coverage-manifest builder checks for host-verified window records.

All expected windows and window records below are synthetic fixtures
built in this file; no real company data, dossiers, models, services or
persistence are touched. The fixtures mimic the host-generated ordered
window list and the host's persisted per-window summaries described in
F-001 P01 sections 3.3 and 4 (approved design baseline
36f9035555c7a016d0f51eb2a266611a36ab5e3f).
"""
from copy import deepcopy
import hashlib

import pytest

from app.facts_tree import git_format
from app.facts_tree import candidate_coverage
from app.facts_tree.candidate_coverage import build_coverage_manifest

FILE_HASH = "sha256:" + "a" * 64
FINGERPRINT = "fp-" + "b" * 32


def _expected(window_id_value, source_loc_value, **overrides):
    window = {
        "window_id": window_id_value,
        "source_loc": source_loc_value,
        "file_hash": FILE_HASH,
        "mapped_fingerprint": FINGERPRINT,
    }
    window.update(overrides)
    return window


def _revision(candidate_uid="cand-1", revision=1, **overrides):
    entry = {"candidate_uid": candidate_uid, "revision": revision}
    entry.update(overrides)
    return entry


def _exclusion(exclusion_id="ex-1", **overrides):
    entry = {
        "exclusion_id": exclusion_id,
        "reason": "pure boilerplate heading, no statement",
        "review_confirmed": True,
    }
    entry.update(overrides)
    return entry


def _record(window_id_value, source_loc_value, **overrides):
    record = {
        "window_id": window_id_value,
        "source_loc": source_loc_value,
        "file_hash": FILE_HASH,
        "mapped_fingerprint": FINGERPRINT,
        "status": "reviewed",
        "candidate_revisions": [_revision()],
        "pending_rejections": [],
        "exclusions": [],
        "unresolved": [],
    }
    record.update(overrides)
    return record


def _windows(*ids):
    return [_expected(window_id, "md:#s%d" % (index + 1))
            for index, window_id in enumerate(ids)]


def _records_for(expected, **per_window_overrides):
    records = []
    for window in expected:
        fields = {
            "file_hash": window["file_hash"],
            "mapped_fingerprint": window["mapped_fingerprint"],
        }
        fields.update(per_window_overrides.get(window["window_id"], {}))
        records.append(_record(
            window["window_id"], window["source_loc"], **fields))
    return records


def test_complete_coverage_all_windows_reviewed():
    expected = _windows("w0", "w1", "w2")
    records = _records_for(expected)
    result = build_coverage_manifest(expected, records)
    manifest = result["record"]
    assert manifest["coverage_complete"] is True
    assert manifest["record_type"] == "candidate_coverage_manifest"
    assert [window["window_id"] for window in manifest["windows"]] == [
        "w0", "w1", "w2"]
    for window, source in zip(manifest["windows"], expected):
        assert window["record_present"] is True
        assert window["status"] == "reviewed"
        assert window["gaps"] == []
        assert window["source_loc"] == source["source_loc"]
        assert window["file_hash"] == FILE_HASH
        assert window["mapped_fingerprint"] == FINGERPRINT


def test_missing_window_record_is_gap_not_exception():
    expected = _windows("w0", "w1", "w2")
    records = _records_for(expected)[:2]
    result = build_coverage_manifest(expected, records)
    manifest = result["record"]
    assert manifest["coverage_complete"] is False
    missing = manifest["windows"][2]
    assert missing["window_id"] == "w2"
    assert missing["record_present"] is False
    assert missing["status"] is None
    assert missing["candidate_revisions"] == []
    assert missing["exclusions"] == []
    assert missing["unresolved"] == []
    assert missing["gaps"] == [{"code": "missing_window_record"}]
    assert manifest["windows"][0]["gaps"] == []


def test_duplicate_expected_window_rejected():
    expected = [_expected("w0", "md:#s1"), _expected("w0", "md:#s2")]
    with pytest.raises(ValueError, match="duplicate expected window_id"):
        build_coverage_manifest(expected, [])


def test_duplicate_window_record_rejected():
    expected = _windows("w0")
    records = [_record("w0", "md:#s1"), _record("w0", "md:#s1")]
    with pytest.raises(ValueError, match="duplicate window record"):
        build_coverage_manifest(expected, records)


def test_unknown_window_record_rejected():
    expected = _windows("w0")
    records = [_record("w0", "md:#s1"), _record("wX", "md:#s9")]
    with pytest.raises(ValueError, match="unknown window_id"):
        build_coverage_manifest(expected, records)


@pytest.mark.parametrize("field,value", [
    ("source_loc", "md:#s2"),
    ("file_hash", "sha256:" + "c" * 64),
    ("mapped_fingerprint", "fp-" + "d" * 32),
])
def test_record_window_binding_mismatch_rejected(field, value):
    # the record is bound to the expected window's exact source-file
    # version: any of source_loc, file_hash or mapped_fingerprint
    # drifting from the expected window fails closed
    expected = _windows("w0")
    records = [_record("w0", "md:#s1", **{field: value})]
    with pytest.raises(ValueError, match="wrong-window"):
        build_coverage_manifest(expected, records)


def test_pending_status_is_locatable_gap():
    expected = _windows("w0")
    records = _records_for(expected, w0={"status": "pending"})
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is False
    assert window["record_present"] is True
    assert window["gaps"] == [
        {"code": "window_not_reviewed", "status": "pending"}]
    # the record content is still carried in full, not summarized away
    assert window["candidate_revisions"] == [_revision()]


def test_pending_rejections_are_locatable_gap():
    expected = _windows("w0")
    records = _records_for(
        expected, w0={"pending_rejections": ["rej-1", "rej-2"]})
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is False
    assert window["gaps"] == [{
        "code": "pending_rejections_open",
        "rejection_ids": ["rej-1", "rej-2"],
    }]


def test_unconfirmed_exclusion_is_locatable_gap():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "exclusions": [
            _exclusion("ex-ok"),
            _exclusion("ex-bad", review_confirmed=False),
        ],
    })
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is False
    assert window["gaps"] == [
        {"code": "exclusion_unconfirmed", "exclusion_id": "ex-bad"}]
    assert [entry["exclusion_id"] for entry in window["exclusions"]] == [
        "ex-ok", "ex-bad"]


def test_reviewed_window_without_any_content_is_gap():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "candidate_revisions": [],
        "exclusions": [],
        "unresolved": [],
    })
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is False
    assert window["gaps"] == [{"code": "reviewed_window_empty"}]


def test_unresolved_alone_never_proves_no_content():
    # P01 section 4 item 3: only an employee's explicit "no submittable
    # statement here" exclusion, confirmed by the full-text review,
    # completes an otherwise empty reviewed window; an unresolved
    # clarification item alone is a coverage gap, never proof of "no
    # content" — and the item itself is still preserved verbatim.
    expected = _windows("w0")
    unresolved = [{
        "field": "$.window",
        "reason": "heading-only window, unclear whether any statement",
        "needed": "员工确认本段是否构成可提交陈述",
    }]
    records = _records_for(expected, w0={
        "candidate_revisions": [],
        "exclusions": [],
        "unresolved": unresolved,
    })
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is False
    assert window["gaps"] == [{"code": "reviewed_window_empty"}]
    assert window["unresolved"] == unresolved


def test_confirmed_exclusion_counts_as_explicit_content():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "candidate_revisions": [],
        "exclusions": [_exclusion()],
        "unresolved": [],
    })
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is True
    assert window["gaps"] == []
    assert window["exclusions"] == [_exclusion()]


def test_unresolved_items_preserved_verbatim_without_gap():
    expected = _windows("w0")
    unresolved = [
        {"field": "$.items[0].period", "reason": "not_stated",
         "needed": "报告期间原文", "nested": {"refs": ["e1", "e2"]}},
        {"field": "$.items[0].currency", "reason": "ambiguous",
         "candidates": ["人民币", "美元"]},
    ]
    records = _records_for(expected, w0={"unresolved": unresolved})
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is True
    assert window["unresolved"] == unresolved
    assert window["unresolved"] is not unresolved


def test_candidate_revision_history_preserved_in_order():
    expected = _windows("w0")
    history = [
        _revision("cand-1", 1),
        _revision("cand-1", 2),
        _revision("cand-1", 3),
        _revision("cand-2", 1),
    ]
    records = _records_for(expected, w0={"candidate_revisions": history})
    result = build_coverage_manifest(expected, records)
    window = result["record"]["windows"][0]
    assert result["record"]["coverage_complete"] is True
    assert window["candidate_revisions"] == history


def test_bool_never_impersonates_revision():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "candidate_revisions": [_revision("cand-1", True)],
    })
    with pytest.raises(TypeError, match="revision must be a positive int"):
        build_coverage_manifest(expected, records)


@pytest.mark.parametrize("bad_revision", [0, -1, "1", 1.5, None])
def test_nonpositive_or_nonstr_int_revision_rejected(bad_revision):
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "candidate_revisions": [_revision("cand-1", bad_revision)],
    })
    with pytest.raises((TypeError, ValueError),
                       match="revision must be a positive int"):
        build_coverage_manifest(expected, records)


def test_expected_window_missing_and_unknown_keys_rejected():
    with pytest.raises(ValueError, match="missing required keys"):
        build_coverage_manifest(
            [{"window_id": "w0", "source_loc": "md:#s1",
              "file_hash": FILE_HASH}], [])
    with pytest.raises(ValueError, match="unknown keys"):
        build_coverage_manifest(
            [_expected("w0", "md:#s1", complete=True)], [])


def test_window_record_missing_and_unknown_keys_rejected():
    expected = _windows("w0")
    record = _record("w0", "md:#s1")
    del record["status"]
    with pytest.raises(ValueError, match="missing required keys"):
        build_coverage_manifest(expected, [record])
    record = _record("w0", "md:#s1", note="extra")
    with pytest.raises(ValueError, match="unknown keys"):
        build_coverage_manifest(expected, [record])


def test_employee_complete_field_is_not_an_input():
    # the batch-level self-report must never stand in as coverage input
    expected = _windows("w0")
    record = _record("w0", "md:#s1", complete=True)
    with pytest.raises(ValueError, match="unknown keys"):
        build_coverage_manifest(expected, [record])


@pytest.mark.parametrize("field", ["window_id", "source_loc",
                                   "file_hash", "mapped_fingerprint"])
def test_expected_window_requires_nonempty_strings(field):
    expected = [_expected("w0", "md:#s1", **{field: ""})]
    with pytest.raises(ValueError, match="non-empty string"):
        build_coverage_manifest(expected, [])
    expected = [_expected("w0", "md:#s1", **{field: 7})]
    with pytest.raises(TypeError, match="non-empty string"):
        build_coverage_manifest(expected, [])


def test_status_must_be_pending_or_reviewed():
    expected = _windows("w0")
    records = _records_for(expected, w0={"status": "done"})
    with pytest.raises(ValueError, match="must be one of"):
        build_coverage_manifest(expected, records)
    records = _records_for(expected, w0={"status": True})
    with pytest.raises(TypeError, match="must be one of"):
        build_coverage_manifest(expected, records)


def test_pending_rejection_ids_must_be_nonempty_strings():
    expected = _windows("w0")
    records = _records_for(expected, w0={"pending_rejections": [""]})
    with pytest.raises(ValueError, match="non-empty string"):
        build_coverage_manifest(expected, records)
    records = _records_for(expected, w0={"pending_rejections": [3]})
    with pytest.raises(TypeError, match="non-empty string"):
        build_coverage_manifest(expected, records)


def test_exclusion_requires_reason_and_bool_confirmation():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "exclusions": [_exclusion(reason="")],
    })
    with pytest.raises(ValueError, match="non-empty string"):
        build_coverage_manifest(expected, records)
    records = _records_for(expected, w0={
        "exclusions": [_exclusion(review_confirmed=1)],
    })
    with pytest.raises(TypeError, match="review_confirmed must be a bool"):
        build_coverage_manifest(expected, records)


def test_unresolved_items_must_be_json_objects():
    expected = _windows("w0")
    records = _records_for(expected, w0={"unresolved": ["not-an-object"]})
    with pytest.raises(TypeError, match="JSON object"):
        build_coverage_manifest(expected, records)


def test_duplicate_candidate_revision_rejected():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "candidate_revisions": [_revision("cand-1", 1),
                                _revision("cand-1", 1)],
    })
    with pytest.raises(ValueError, match="duplicate candidate revision"):
        build_coverage_manifest(expected, records)


def test_duplicate_pending_rejection_id_rejected():
    expected = _windows("w0")
    records = _records_for(
        expected, w0={"pending_rejections": ["rej-1", "rej-1"]})
    with pytest.raises(ValueError, match="duplicate pending rejection id"):
        build_coverage_manifest(expected, records)


def test_duplicate_exclusion_id_rejected():
    expected = _windows("w0")
    records = _records_for(expected, w0={
        "exclusions": [_exclusion("ex-1"), _exclusion("ex-1")],
    })
    with pytest.raises(ValueError, match="duplicate exclusion_id"):
        build_coverage_manifest(expected, records)


def test_non_string_mapping_keys_rejected_before_key_comparison():
    # a non-string key is a structural TypeError naming the location,
    # never a byproduct of key-set difference or sorting
    expected = _windows("w0")
    bad_window = _expected("w0", "md:#s1")
    bad_window[1] = "x"
    with pytest.raises(TypeError, match="keys must be str"):
        build_coverage_manifest([bad_window], [])
    bad_record = _record("w0", "md:#s1")
    bad_record[2] = "x"
    with pytest.raises(TypeError, match="keys must be str"):
        build_coverage_manifest(expected, [bad_record])
    bad_revision = _revision()
    bad_revision[3] = "x"
    records = _records_for(expected, w0={"candidate_revisions": [bad_revision]})
    with pytest.raises(TypeError, match="keys must be str"):
        build_coverage_manifest(expected, records)
    bad_exclusion = _exclusion()
    bad_exclusion[4] = "x"
    records = _records_for(expected, w0={"exclusions": [bad_exclusion]})
    with pytest.raises(TypeError, match="keys must be str"):
        build_coverage_manifest(expected, records)


@pytest.mark.parametrize("field", ["window_id", "source_loc",
                                   "file_hash", "mapped_fingerprint"])
def test_expected_window_rejects_whitespace_only_strings(field):
    expected = [_expected("w0", "md:#s1", **{field: "   "})]
    with pytest.raises(ValueError, match="non-empty string"):
        build_coverage_manifest(expected, [])


def test_record_rejects_whitespace_only_strings():
    expected = _windows("w0")
    for overrides in (
            {"source_loc": "  "},
            {"file_hash": "\t"},
            {"mapped_fingerprint": " \n "},
            {"candidate_revisions": [_revision("  ", 1)]},
            {"pending_rejections": ["   "]},
            {"exclusions": [_exclusion(exclusion_id="  ")]},
            {"exclusions": [_exclusion(reason="   ")]}):
        records = _records_for(expected, w0=overrides)
        with pytest.raises(ValueError, match="non-empty string"):
            build_coverage_manifest(expected, records)


def test_empty_expected_windows_rejected():
    # P01 section 4 item 5 / section 14.1: zero expected windows can
    # never prove file-level full-text coverage; a windows=[],
    # coverage_complete=true manifest would be a hollow "complete"
    # snapshot and is refused deterministically.
    with pytest.raises(ValueError,
                       match="expected_windows must contain at least one "
                             "window"):
        build_coverage_manifest([], [])


def test_top_level_arguments_must_be_lists():
    with pytest.raises(TypeError, match="expected_windows must be a list"):
        build_coverage_manifest({"window_id": "w0"}, [])
    with pytest.raises(TypeError, match="window_records must be a list"):
        build_coverage_manifest(_windows("w0"), {"window_id": "w0"})


def test_digest_matches_canonical_bytes_and_has_no_digest_field():
    expected = _windows("w0", "w1")
    records = _records_for(expected)
    result = build_coverage_manifest(expected, records)
    manifest = result["record"]
    assert "digest" not in manifest
    canonical = git_format.dumps_record(manifest)
    assert result["digest"] == hashlib.sha256(canonical).hexdigest()
    assert canonical.endswith(b"\n")


def test_same_input_same_canonical_bytes_and_digest():
    expected = _windows("w0", "w1", "w2")
    records = _records_for(expected, w1={
        "status": "pending",
        "pending_rejections": ["rej-1"],
        "exclusions": [_exclusion("ex-1", review_confirmed=False)],
        "unresolved": [{"field": "$.x", "reason": "not_stated"}],
    })
    first = build_coverage_manifest(expected, records)
    second = build_coverage_manifest(deepcopy(expected), deepcopy(records))
    assert git_format.dumps_record(first["record"]) == \
        git_format.dumps_record(second["record"])
    assert first["digest"] == second["digest"]


def test_dict_insertion_order_does_not_change_digest():
    expected_a = [_expected("w0", "md:#s1")]
    expected_b = [{
        "mapped_fingerprint": FINGERPRINT,
        "file_hash": FILE_HASH,
        "source_loc": "md:#s1",
        "window_id": "w0",
    }]
    record_a = [_record("w0", "md:#s1")]
    record_b = [{
        "unresolved": [],
        "exclusions": [],
        "pending_rejections": [],
        "candidate_revisions": [_revision()],
        "status": "reviewed",
        "mapped_fingerprint": FINGERPRINT,
        "file_hash": FILE_HASH,
        "source_loc": "md:#s1",
        "window_id": "w0",
    }]
    first = build_coverage_manifest(expected_a, record_a)
    second = build_coverage_manifest(expected_b, record_b)
    assert first["digest"] == second["digest"]


def test_inputs_are_never_mutated():
    expected = _windows("w0", "w1")
    records = _records_for(expected, w1={
        "status": "pending",
        "pending_rejections": ["rej-1"],
        "unresolved": [{"field": "$.x", "reason": "not_stated"}],
    })
    expected_before = deepcopy(expected)
    records_before = deepcopy(records)
    build_coverage_manifest(expected, records)
    assert expected == expected_before
    assert records == records_before


def test_gap_codes_are_stable_and_exhaustive():
    assert candidate_coverage.GAP_CODES == (
        "missing_window_record",
        "window_not_reviewed",
        "pending_rejections_open",
        "exclusion_unconfirmed",
        "reviewed_window_empty",
    )
