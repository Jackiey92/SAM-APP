"""Deterministic full-text coverage manifest for host window records.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 3.3 (batch fields) and section 4 ("候选接收与全文覆盖",
candidate intake and full-text coverage). After the host has completed a
full-text review round for one file, P01 section 4 item 5 requires an
immutable candidate manifest covering every window, every candidate
revision, every exclusion, every unresolved clarification item and every
coverage gap. This module builds exactly that manifest as a pure
function: build_coverage_manifest(expected_windows, window_records)
takes the host-generated ordered list of expected read windows plus the
host's persisted and already-validated per-window records and returns a
canonical record together with its content digest.

What a pass here proves, and only this: the supplied host records
structurally cover the expected windows — every expected window has a
reviewed record bound to the expected window's exact source_loc,
file_hash and mapped_fingerprint, no window still waits for corrected
candidates after a rejection, every exclusion carries a non-empty
reason and was confirmed by the full-text review, and no reviewed
window silently reports "no content" (an empty result is only legal
when the window carries at least one candidate revision or at least
one review-confirmed exclusion with a reason, i.e. the employee
explicitly stated the segment holds no submittable statement and the
full-text review confirmed that exclusion, per P01 section 4 item 3).
Unresolved clarification items are preserved verbatim in the manifest
and never discarded, but an unresolved item alone never proves a
reviewed window has nothing to submit; candidate revisions, exclusions
and unresolved items are all carried into the result in full, never
reduced to summary counts. An expected window without a persisted
record is not an error: it yields a locatable missing_window_record
gap in the window's expected position.

Boundary (explicitly NOT done here, per P01 sections 3.2, 4 and 7):

- No caller, actor, task-permission, policy, delegation-chain or
  authorization authentication, and no verification that any read
  window was genuinely granted or genuinely read; the host alone vouches
  for the records it passes in.
- No fact-candidate/1 schema validation and no citation, quote,
  attribution or source-grant verification of candidate contents; those
  belong to candidate_schema / candidate_evidence before records are
  persisted.
- No persistence of anything; the manifest is computed, never stored.
- No acceptance or submission of facts into the formal tree, no
  integration-plan or Commit-Broker involvement, and no connection to
  any legacy reading entry point.
- The employee payload's ``complete`` flag is never accepted as input
  here and never counts as full-text coverage evidence (P01 section 4:
  the employee's self-report cannot replace programmatic window
  coverage verification); only caller-verified host records are read.

Determinism: the returned record contains no digest field; its
canonical bytes come from app.facts_tree.git_format.dumps_record and
the digest is their SHA-256, so the digest never depends on dict
iteration order. Identical inputs always produce identical canonical
bytes and an identical digest.

Input contract (both arguments are required lists, never mutated):

- expected_windows: the host-generated complete ordered window list.
  Each item must be a dict with exactly the keys window_id, source_loc,
  file_hash and mapped_fingerprint, each a non-empty string. Duplicate
  window_id values are rejected. The list itself must be non-empty:
  zero expected windows can never prove file-level full-text coverage
  (P01 section 4 item 5 / section 14.1), so an empty list is rejected
  with a ValueError instead of yielding a hollow windows=[],
  coverage_complete=true manifest.
- window_records: the host's persisted, already-validated per-window
  summaries. Each item must be a dict with exactly the keys window_id,
  source_loc, file_hash, mapped_fingerprint, status,
  candidate_revisions, pending_rejections, exclusions and unresolved:
  - source_loc, file_hash and mapped_fingerprint bind the record to one
    exact source-file version of the expected window; each is a
    non-empty string and must equal the expected window's value;
  - status is exactly "pending" or "reviewed";
  - candidate_revisions is a list of dicts with exactly the keys
    candidate_uid (non-empty string) and revision (positive int; a
    bool is never an int here); a duplicate (candidate_uid, revision)
    pair is rejected;
  - pending_rejections is a list of non-empty string rejection IDs; a
    duplicate rejection ID is rejected;
  - exclusions is a list of dicts with exactly the keys exclusion_id
    (non-empty string), reason (non-empty string) and review_confirmed
    (bool); a duplicate exclusion_id is rejected;
  - unresolved is a list of host-provided JSON objects (dicts with
    string keys); their contents are preserved verbatim and are
    validated only as far as the canonical serializer requires.

Non-empty strings never accept a whitespace-only value. A record whose
window_id is not an expected window, a record whose source_loc,
file_hash or mapped_fingerprint differs from the expected window's,
duplicate records for one window and every type / required-key
violation are rejected with an explicit TypeError or ValueError naming
the location; non-string mapping keys are reported as structural type
errors before any key-set comparison.

Stable gap codes (one gap entry per defect, each carrying its
locating fields): missing_window_record, window_not_reviewed,
pending_rejections_open, exclusion_unconfirmed,
reviewed_window_empty. coverage_complete is true exactly when no
window carries any gap.
"""
from copy import deepcopy
import hashlib

from app.facts_tree import git_format

RECORD_TYPE = "candidate_coverage_manifest"
FORMAT_VERSION = 1

STATUS_PENDING = "pending"
STATUS_REVIEWED = "reviewed"
_STATUS_VALUES = (STATUS_PENDING, STATUS_REVIEWED)

GAP_MISSING_WINDOW_RECORD = "missing_window_record"
GAP_WINDOW_NOT_REVIEWED = "window_not_reviewed"
GAP_PENDING_REJECTIONS_OPEN = "pending_rejections_open"
GAP_EXCLUSION_UNCONFIRMED = "exclusion_unconfirmed"
GAP_REVIEWED_WINDOW_EMPTY = "reviewed_window_empty"

GAP_CODES = (
    GAP_MISSING_WINDOW_RECORD,
    GAP_WINDOW_NOT_REVIEWED,
    GAP_PENDING_REJECTIONS_OPEN,
    GAP_EXCLUSION_UNCONFIRMED,
    GAP_REVIEWED_WINDOW_EMPTY,
)

_EXPECTED_WINDOW_KEYS = frozenset(
    ("window_id", "source_loc", "file_hash", "mapped_fingerprint"))
_WINDOW_RECORD_KEYS = frozenset((
    "window_id", "source_loc", "file_hash", "mapped_fingerprint",
    "status", "candidate_revisions", "pending_rejections", "exclusions",
    "unresolved"))
_CANDIDATE_REVISION_KEYS = frozenset(("candidate_uid", "revision"))
_EXCLUSION_KEYS = frozenset(("exclusion_id", "reason", "review_confirmed"))


def build_coverage_manifest(expected_windows, window_records):
    """Build the deterministic full-text coverage manifest.

    expected_windows is the host-generated complete ordered window
    list; window_records are the host's persisted, already-validated
    per-window summaries. Returns {"record": record, "digest": digest}
    where record is the canonical manifest (no digest field) and
    digest is the SHA-256 hex of its git_format.dumps_record bytes.
    Neither input is mutated. Invalid input raises TypeError or
    ValueError; a missing window record is not an exception but a
    missing_window_record gap in the expected window's position.
    """
    expected = _validate_expected_windows(expected_windows)
    records = _validate_window_records(window_records, expected)
    windows = []
    coverage_complete = True
    for expected_window in expected:
        record = records.get(expected_window["window_id"])
        window = _build_window(expected_window, record)
        if window["gaps"]:
            coverage_complete = False
        windows.append(window)
    manifest = {
        "record_type": RECORD_TYPE,
        "format_version": FORMAT_VERSION,
        "coverage_complete": coverage_complete,
        "windows": windows,
    }
    canonical_bytes = git_format.dumps_record(manifest)
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    return {"record": manifest, "digest": digest}


def _build_window(expected_window, record):
    window = {
        "window_id": expected_window["window_id"],
        "source_loc": expected_window["source_loc"],
        "file_hash": expected_window["file_hash"],
        "mapped_fingerprint": expected_window["mapped_fingerprint"],
        "record_present": record is not None,
    }
    if record is None:
        window.update({
            "status": None,
            "candidate_revisions": [],
            "exclusions": [],
            "unresolved": [],
            "gaps": [{"code": GAP_MISSING_WINDOW_RECORD}],
        })
        return window
    gaps = []
    if record["status"] != STATUS_REVIEWED:
        gaps.append({
            "code": GAP_WINDOW_NOT_REVIEWED,
            "status": record["status"],
        })
    if record["pending_rejections"]:
        gaps.append({
            "code": GAP_PENDING_REJECTIONS_OPEN,
            "rejection_ids": list(record["pending_rejections"]),
        })
    for exclusion in record["exclusions"]:
        if not exclusion["review_confirmed"]:
            gaps.append({
                "code": GAP_EXCLUSION_UNCONFIRMED,
                "exclusion_id": exclusion["exclusion_id"],
            })
    # P01 section 4 item 3/5: an empty result must never silently mean
    # "no content"; a reviewed window without candidate revisions is
    # only content-complete when the employee explicitly stated the
    # segment holds no submittable statement and the full-text review
    # confirmed that exclusion with a reason. Unresolved items are
    # preserved verbatim but never prove "no content" by themselves.
    if (record["status"] == STATUS_REVIEWED
            and not record["candidate_revisions"]
            and not any(exclusion["review_confirmed"]
                        for exclusion in record["exclusions"])):
        gaps.append({"code": GAP_REVIEWED_WINDOW_EMPTY})
    window.update({
        "status": record["status"],
        "candidate_revisions": deepcopy(record["candidate_revisions"]),
        "exclusions": deepcopy(record["exclusions"]),
        "unresolved": deepcopy(record["unresolved"]),
        "gaps": gaps,
    })
    return window


def _validate_expected_windows(expected_windows):
    if not isinstance(expected_windows, list):
        raise TypeError(
            "expected_windows must be a list, got %s"
            % type(expected_windows).__name__)
    if not expected_windows:
        raise ValueError(
            "expected_windows must contain at least one window: zero "
            "expected windows cannot prove file-level full-text "
            "coverage")
    windows = []
    seen = set()
    for index, item in enumerate(expected_windows):
        where = "expected_windows[%d]" % index
        _check_keys(item, _EXPECTED_WINDOW_KEYS, where)
        for name in sorted(_EXPECTED_WINDOW_KEYS):
            _check_nonempty_string(item[name], "%s.%s" % (where, name))
        if item["window_id"] in seen:
            raise ValueError(
                "duplicate expected window_id %r at %s"
                % (item["window_id"], where))
        seen.add(item["window_id"])
        windows.append({name: item[name]
                        for name in sorted(_EXPECTED_WINDOW_KEYS)})
    return windows


def _validate_window_records(window_records, expected_windows):
    if not isinstance(window_records, list):
        raise TypeError(
            "window_records must be a list, got %s"
            % type(window_records).__name__)
    expected_by_id = dict(
        (window["window_id"], window) for window in expected_windows)
    records = {}
    for index, item in enumerate(window_records):
        where = "window_records[%d]" % index
        _check_keys(item, _WINDOW_RECORD_KEYS, where)
        _check_nonempty_string(item["window_id"], "%s.window_id" % where)
        _check_nonempty_string(item["source_loc"], "%s.source_loc" % where)
        _check_nonempty_string(item["file_hash"], "%s.file_hash" % where)
        _check_nonempty_string(
            item["mapped_fingerprint"], "%s.mapped_fingerprint" % where)
        _check_status(item["status"], "%s.status" % where)
        _check_candidate_revisions(
            item["candidate_revisions"], "%s.candidate_revisions" % where)
        _check_pending_rejections(
            item["pending_rejections"], "%s.pending_rejections" % where)
        _check_exclusions(item["exclusions"], "%s.exclusions" % where)
        _check_unresolved(item["unresolved"], "%s.unresolved" % where)
        window_id = item["window_id"]
        if window_id in records:
            raise ValueError(
                "duplicate window record for window_id %r at %s"
                % (window_id, where))
        expected = expected_by_id.get(window_id)
        if expected is None:
            raise ValueError(
                "window record at %s names unknown window_id %r: not an "
                "expected window" % (where, window_id))
        # Bind the record to the exact source-file version of its
        # expected window: any of source_loc, file_hash or
        # mapped_fingerprint drifting from the expected window fails
        # closed instead of letting a stale or wrong-file record stand
        # in for the window.
        if item["source_loc"] != expected["source_loc"]:
            raise ValueError(
                "window record at %s has source_loc %r but expected "
                "window %r has source_loc %r: wrong-window source "
                "location" % (where, item["source_loc"], window_id,
                              expected["source_loc"]))
        if item["file_hash"] != expected["file_hash"]:
            raise ValueError(
                "window record at %s has file_hash %r but expected "
                "window %r has file_hash %r: wrong-window file hash"
                % (where, item["file_hash"], window_id,
                   expected["file_hash"]))
        if item["mapped_fingerprint"] != expected["mapped_fingerprint"]:
            raise ValueError(
                "window record at %s has mapped_fingerprint %r but "
                "expected window %r has mapped_fingerprint %r: "
                "wrong-window mapped fingerprint"
                % (where, item["mapped_fingerprint"], window_id,
                   expected["mapped_fingerprint"]))
        records[window_id] = item
    return records


def _check_keys(item, required_keys, where):
    if not isinstance(item, dict):
        raise TypeError(
            "%s must be a dict, got %s" % (where, type(item).__name__))
    # A non-string key is a structural type error and must be reported
    # before any key-set difference or sorting touches it (sorting a
    # mixed-type key set would crash with an unrelated TypeError).
    for key in item:
        if not isinstance(key, str):
            raise TypeError(
                "%s keys must be str, got %s"
                % (where, type(key).__name__))
    keys = set(item)
    missing = sorted(required_keys - keys)
    if missing:
        raise ValueError("%s is missing required keys %s" % (where, missing))
    extra = sorted(keys - required_keys)
    if extra:
        raise ValueError("%s has unknown keys %s" % (where, extra))


def _check_nonempty_string(value, where):
    if not isinstance(value, str):
        raise TypeError(
            "%s must be a non-empty string, got %s"
            % (where, type(value).__name__))
    if not value.strip():
        raise ValueError("%s must be a non-empty string" % where)


def _check_status(value, where):
    if not isinstance(value, str):
        raise TypeError(
            "%s must be one of %s, got %s"
            % (where, list(_STATUS_VALUES), type(value).__name__))
    if value not in _STATUS_VALUES:
        raise ValueError(
            "%s must be one of %s, got %r"
            % (where, list(_STATUS_VALUES), value))


def _check_candidate_revisions(value, where):
    if not isinstance(value, list):
        raise TypeError(
            "%s must be a list, got %s" % (where, type(value).__name__))
    seen = set()
    for index, entry in enumerate(value):
        entry_where = "%s[%d]" % (where, index)
        _check_keys(entry, _CANDIDATE_REVISION_KEYS, entry_where)
        _check_nonempty_string(
            entry["candidate_uid"], "%s.candidate_uid" % entry_where)
        revision = entry["revision"]
        # bool is an int subclass; a True/False flag must never
        # impersonate a positive revision counter.
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise TypeError(
                "%s.revision must be a positive int, got %s"
                % (entry_where, type(revision).__name__))
        if revision < 1:
            raise ValueError(
                "%s.revision must be a positive int, got %d"
                % (entry_where, revision))
        identity = (entry["candidate_uid"], revision)
        if identity in seen:
            raise ValueError(
                "duplicate candidate revision %r at %s"
                % (identity, entry_where))
        seen.add(identity)


def _check_pending_rejections(value, where):
    if not isinstance(value, list):
        raise TypeError(
            "%s must be a list, got %s" % (where, type(value).__name__))
    seen = set()
    for index, entry in enumerate(value):
        _check_nonempty_string(entry, "%s[%d]" % (where, index))
        if entry in seen:
            raise ValueError(
                "duplicate pending rejection id %r at %s[%d]"
                % (entry, where, index))
        seen.add(entry)


def _check_exclusions(value, where):
    if not isinstance(value, list):
        raise TypeError(
            "%s must be a list, got %s" % (where, type(value).__name__))
    seen = set()
    for index, entry in enumerate(value):
        entry_where = "%s[%d]" % (where, index)
        _check_keys(entry, _EXCLUSION_KEYS, entry_where)
        _check_nonempty_string(
            entry["exclusion_id"], "%s.exclusion_id" % entry_where)
        _check_nonempty_string(entry["reason"], "%s.reason" % entry_where)
        if not isinstance(entry["review_confirmed"], bool):
            raise TypeError(
                "%s.review_confirmed must be a bool, got %s"
                % (entry_where, type(entry["review_confirmed"]).__name__))
        if entry["exclusion_id"] in seen:
            raise ValueError(
                "duplicate exclusion_id %r at %s"
                % (entry["exclusion_id"], entry_where))
        seen.add(entry["exclusion_id"])


def _check_unresolved(value, where):
    if not isinstance(value, list):
        raise TypeError(
            "%s must be a list, got %s" % (where, type(value).__name__))
    for index, entry in enumerate(value):
        entry_where = "%s[%d]" % (where, index)
        if not isinstance(entry, dict):
            raise TypeError(
                "%s must be a host-provided JSON object, got %s"
                % (entry_where, type(entry).__name__))
        for key in entry:
            if not isinstance(key, str):
                raise TypeError("%s keys must be str" % entry_where)
