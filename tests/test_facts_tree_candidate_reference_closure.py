"""Pure fail-closed checks for the candidate reference closure validator.

Synthetic checks only, per approved designs 02 §6/§10.3–10.5 and 97
§2/§3/§4 item 5: in-memory dicts built in this file with synthetic
SHA-256 constants. No real company data, Git objects, refs, paths,
databases, models, services or feature gates are touched; the validator
is pure and is exercised strictly through its public entry point.

Covered: successful closure with one and with several candidates and
operations; empty collections and unreferenced/unconsumed inputs as
legal; unknown additional record fields accepted; arbitrary iterables;
input immutability on success and on rejection; missing closure fields;
wrong-typed records, keys, candidate_refs containers and reference
entries; the exactly-three-key typed reference shape; candidate_revision
strictness (exact built-in int — bool, float, Decimal and int subclasses
rejected — positive); payload_digest shape (64-char lowercase hex);
legal-vs-illegal opaque IDs through git_format.encode_id; wrong-typed
record scope as TypeError and legal-but-foreign scope as ValueError;
duplicate input identities; duplicate operation ids; duplicate candidate
references inside one operation; dangling and digest-mismatched
references; duplicate terminal consumption; consumed records with a
missing input identity, a digest mismatch, a dangling operation or an
operation that does not reference the consumed revision and digest.
"""
import copy
from decimal import Decimal

import pytest

from app.facts_tree.candidate_reference_closure import (
    validate_candidate_reference_closure,
)

REPO_ID = "repo-synthetic-0001"
COMPANY_ID = "co-synthetic-0001"
OTHER_REPO_ID = "repo-synthetic-0002"
OTHER_COMPANY_ID = "co-synthetic-0002"

# Synthetic SHA-256 constants; never recomputed from any payload.
DIGEST_A1 = "a1" * 32
DIGEST_A2 = "a2" * 32
DIGEST_B1 = "b1" * 32
DIGEST_C1 = "c1" * 32
DIGEST_OTHER = "0f1e2d3c4b5a69" + "78" * 25


class _IntSubclass(int):
    """An int subclass must never pass for a plain built-in int."""


def _input_candidate(uid="cand-a", revision=1, digest=DIGEST_A1,
                     repo_id=REPO_ID, company_id=COMPANY_ID, **extra):
    record = {
        "candidate_uid": uid,
        "candidate_revision": revision,
        "payload_digest": digest,
        "repo_id": repo_id,
        "company_id": company_id,
    }
    record.update(extra)
    return record


def _ref(uid="cand-a", revision=1, digest=DIGEST_A1, **extra):
    reference = {
        "candidate_uid": uid,
        "candidate_revision": revision,
        "payload_digest": digest,
    }
    reference.update(extra)
    return reference


def _operation(operation_id="op-1", candidate_refs=None, repo_id=REPO_ID,
               company_id=COMPANY_ID, **extra):
    # candidate_refs is stored verbatim so wrong-typed containers
    # (tuple, str, dict) reach the validator unchanged; None means the
    # empty listing.
    record = {
        "operation_id": operation_id,
        "repo_id": repo_id,
        "company_id": company_id,
        "candidate_refs": [] if candidate_refs is None else candidate_refs,
    }
    record.update(extra)
    return record


def _consumed(uid="cand-a", revision=1, digest=DIGEST_A1,
              operation_id="op-1", repo_id=REPO_ID, company_id=COMPANY_ID,
              **extra):
    record = {
        "candidate_uid": uid,
        "candidate_revision": revision,
        "payload_digest": digest,
        "operation_id": operation_id,
        "repo_id": repo_id,
        "company_id": company_id,
    }
    record.update(extra)
    return record


def _validate(inputs, operations, consumed):
    return validate_candidate_reference_closure(
        inputs, operations, consumed, repo_id=REPO_ID, company_id=COMPANY_ID)


# --------------------------------------------------------------------
# successful closure
# --------------------------------------------------------------------

def test_empty_collections_close():
    assert _validate([], [], []) is None


def test_single_candidate_full_closure():
    assert _validate(
        [_input_candidate()],
        [_operation(candidate_refs=[_ref()])],
        [_consumed()],
    ) is None


def test_multiple_candidates_and_operations_close():
    inputs = [
        _input_candidate(uid="cand-a", revision=1, digest=DIGEST_A1),
        _input_candidate(uid="cand-a", revision=2, digest=DIGEST_A2),
        _input_candidate(uid="cand-b", revision=1, digest=DIGEST_B1),
        _input_candidate(uid="cand-c", revision=7, digest=DIGEST_C1),
    ]
    operations = [
        _operation(operation_id="op-1", candidate_refs=[
            _ref(uid="cand-a", revision=1, digest=DIGEST_A1),
            _ref(uid="cand-b", revision=1, digest=DIGEST_B1),
        ]),
        _operation(operation_id="op-2", candidate_refs=[
            _ref(uid="cand-a", revision=2, digest=DIGEST_A2),
            _ref(uid="cand-c", revision=7, digest=DIGEST_C1),
        ]),
    ]
    consumed = [
        _consumed(uid="cand-a", revision=1, digest=DIGEST_A1,
                  operation_id="op-1"),
        _consumed(uid="cand-b", revision=1, digest=DIGEST_B1,
                  operation_id="op-1"),
        _consumed(uid="cand-a", revision=2, digest=DIGEST_A2,
                  operation_id="op-2"),
        _consumed(uid="cand-c", revision=7, digest=DIGEST_C1,
                  operation_id="op-2"),
    ]
    assert _validate(inputs, operations, consumed) is None


def test_unreferenced_and_unconsumed_inputs_are_legal():
    # Closure of what is present: cand-b is neither referenced nor
    # consumed, op-2 references nothing at all, and that passes.
    assert _validate(
        [_input_candidate(uid="cand-a"), _input_candidate(uid="cand-b",
                                                          digest=DIGEST_B1)],
        [_operation(operation_id="op-1", candidate_refs=[_ref()]),
         _operation(operation_id="op-2")],
        [_consumed()],
    ) is None


def test_unknown_extra_record_fields_are_accepted():
    # The full record schema belongs to the record-schema validators;
    # additional payload fields beyond the closure set pass here.
    assert _validate(
        [_input_candidate(schema_version=1, record_type="input_candidate",
                          source_id="src-9")],
        [_operation(base_commit="9" * 40, request_id="req-1",
                    candidate_refs=[_ref()])],
        [_consumed(disposition="accept_fact", result_ref={"x": 1})],
    ) is None


def test_arbitrary_iterables_are_accepted():
    assert _validate(
        iter([_input_candidate()]),
        (_operation(candidate_refs=[_ref()]),),
        (item for item in [_consumed()]),
    ) is None


def test_inputs_are_never_mutated_on_success_or_rejection():
    inputs = [_input_candidate(), _input_candidate(uid="cand-b",
                                                   digest=DIGEST_B1)]
    operations = [_operation(candidate_refs=[_ref(),
                                             _ref(uid="cand-zzz")])]
    consumed = [_consumed()]
    before = (copy.deepcopy(inputs), copy.deepcopy(operations),
              copy.deepcopy(consumed))
    with pytest.raises(ValueError):
        _validate(inputs, operations, consumed)
    assert (inputs, operations, consumed) == before
    operations[0]["candidate_refs"] = [_ref()]
    before = (copy.deepcopy(inputs), copy.deepcopy(operations),
              copy.deepcopy(consumed))
    assert _validate(inputs, operations, consumed) is None
    assert (inputs, operations, consumed) == before


# --------------------------------------------------------------------
# collection and record shapes
# --------------------------------------------------------------------

def test_bare_string_collections_are_type_errors():
    with pytest.raises(TypeError):
        _validate("cand", [], [])
    with pytest.raises(TypeError):
        _validate([], b"op", [])
    with pytest.raises(TypeError):
        _validate([], [], "consumed")


def test_non_iterable_collection_is_a_type_error():
    with pytest.raises(TypeError):
        _validate(7, [], [])


def test_non_dict_records_are_type_errors():
    with pytest.raises(TypeError):
        _validate(["cand-a"], [], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(), "op-1"], [])
    with pytest.raises(TypeError):
        _validate([], [], [None])


def test_non_str_record_keys_are_type_errors():
    with pytest.raises(TypeError):
        _validate([{1: "x"}], [], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(), {2: "y"}], [])
    with pytest.raises(TypeError):
        _validate([], [], [{3: "z"}])


def test_missing_closure_fields_are_value_errors():
    incomplete_input = _input_candidate()
    del incomplete_input["payload_digest"]
    with pytest.raises(ValueError):
        _validate([incomplete_input], [], [])
    incomplete_operation = _operation()
    del incomplete_operation["candidate_refs"]
    with pytest.raises(ValueError):
        _validate([], [incomplete_operation], [])
    incomplete_consumed = _consumed()
    del incomplete_consumed["operation_id"]
    with pytest.raises(ValueError):
        _validate([], [], [incomplete_consumed])


# --------------------------------------------------------------------
# expected scope and IDs
# --------------------------------------------------------------------

def test_expected_scope_is_checked_first():
    with pytest.raises(TypeError):
        validate_candidate_reference_closure([], [], [], repo_id=None,
                                             company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        validate_candidate_reference_closure([], [], [], repo_id=REPO_ID,
                                             company_id=7)
    with pytest.raises(ValueError):
        validate_candidate_reference_closure([], [], [], repo_id="",
                                             company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        validate_candidate_reference_closure([], [], [], repo_id=REPO_ID,
                                             company_id="")


def test_illegal_record_ids_are_rejected():
    with pytest.raises(ValueError):
        _validate([_input_candidate(uid="")], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate(uid=5)], [], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(operation_id=b"op-1")], [])
    with pytest.raises(ValueError):
        _validate([], [], [_consumed(operation_id="")])


def test_wrong_typed_record_scope_is_a_type_error():
    with pytest.raises(TypeError):
        _validate([_input_candidate(repo_id=7)], [], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(company_id=None)], [])
    with pytest.raises(TypeError):
        _validate([], [], [_consumed(repo_id=b"repo")])


def test_foreign_scope_never_closes():
    with pytest.raises(ValueError):
        _validate([_input_candidate(repo_id=OTHER_REPO_ID)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate(company_id=OTHER_COMPANY_ID)], [], [])
    with pytest.raises(ValueError):
        _validate([], [_operation(repo_id=OTHER_REPO_ID)], [])
    with pytest.raises(ValueError):
        _validate([], [_operation(company_id=OTHER_COMPANY_ID)], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate()], [_operation(candidate_refs=[_ref()])],
                  [_consumed(repo_id=OTHER_REPO_ID)])
    with pytest.raises(ValueError):
        _validate([_input_candidate()], [_operation(candidate_refs=[_ref()])],
                  [_consumed(company_id=OTHER_COMPANY_ID)])


# --------------------------------------------------------------------
# candidate_revision and payload_digest strictness
# --------------------------------------------------------------------

def test_bool_and_subclass_revisions_are_type_errors():
    with pytest.raises(TypeError):
        _validate([_input_candidate(revision=True)], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate(revision=_IntSubclass(1))], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate()],
                  [_operation(candidate_refs=[_ref(revision=False)])], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate()], [_operation(candidate_refs=[_ref()])],
                  [_consumed(revision=_IntSubclass(1))])


def test_non_int_revisions_are_type_errors():
    with pytest.raises(TypeError):
        _validate([_input_candidate(revision=1.0)], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate(revision=Decimal(1))], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate(revision="1")], [], [])


def test_non_positive_revisions_are_value_errors():
    with pytest.raises(ValueError):
        _validate([_input_candidate(revision=0)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate(revision=-1)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate()],
                  [_operation(candidate_refs=[_ref(revision=0)])], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate()], [_operation(candidate_refs=[_ref()])],
                  [_consumed(revision=-3)])


def test_malformed_digests_are_rejected():
    with pytest.raises(ValueError):
        _validate([_input_candidate(digest="A1" * 32)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate(digest="a1" * 31)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate(digest="g1" * 32)], [], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate(digest=b"a1" * 32)], [], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate()],
                  [_operation(candidate_refs=[_ref(digest="a1" * 33)])], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate()], [_operation(candidate_refs=[_ref()])],
                  [_consumed(digest=None)])


# --------------------------------------------------------------------
# duplicate identities
# --------------------------------------------------------------------

def test_duplicate_input_identity_is_rejected():
    with pytest.raises(ValueError):
        _validate([_input_candidate(), _input_candidate()], [], [])
    # Same identity with a disagreeing digest is still a duplicate, not
    # a legal pair of inputs.
    with pytest.raises(ValueError):
        _validate([_input_candidate(),
                   _input_candidate(digest=DIGEST_OTHER)], [], [])


def test_same_uid_different_revisions_are_distinct_identities():
    assert _validate(
        [_input_candidate(revision=1, digest=DIGEST_A1),
         _input_candidate(revision=2, digest=DIGEST_A2)],
        [_operation(candidate_refs=[_ref(revision=1, digest=DIGEST_A1),
                                    _ref(revision=2, digest=DIGEST_A2)])],
        [_consumed(revision=2, digest=DIGEST_A2)],
    ) is None


def test_duplicate_operation_id_is_rejected():
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(operation_id="op-1", candidate_refs=[_ref()]),
             _operation(operation_id="op-1")],
            [])


def test_duplicate_reference_inside_one_operation_is_rejected():
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(candidate_refs=[_ref(), _ref()])],
            [])
    # Same identity under disagreeing digests: the duplicate fires even
    # though both digests are well-formed.
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(candidate_refs=[_ref(), _ref(digest=DIGEST_OTHER)])],
            [])


# --------------------------------------------------------------------
# candidate_refs shape
# --------------------------------------------------------------------

def test_wrong_typed_candidate_refs_container_is_a_type_error():
    with pytest.raises(TypeError):
        _validate([], [_operation(candidate_refs=(_ref(),))], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(candidate_refs="cand-a")], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(candidate_refs=_ref())], [])


def test_non_dict_reference_entry_is_a_type_error():
    with pytest.raises(TypeError):
        _validate([], [_operation(candidate_refs=["cand-a"])], [])


def test_reference_key_set_is_exact():
    with pytest.raises(ValueError):
        _validate([], [_operation(candidate_refs=[
            {"candidate_uid": "cand-a", "candidate_revision": 1}])], [])
    with pytest.raises(ValueError):
        _validate([_input_candidate()], [_operation(candidate_refs=[
            _ref(extra_key="x")])], [])
    with pytest.raises(TypeError):
        _validate([], [_operation(candidate_refs=[
            {"candidate_uid": "cand-a", "candidate_revision": 1,
             "payload_digest": DIGEST_A1, 4: "bad"}])], [])


def test_reference_with_illegal_id_is_rejected():
    with pytest.raises(ValueError):
        _validate([_input_candidate()],
                  [_operation(candidate_refs=[_ref(uid="")])], [])
    with pytest.raises(TypeError):
        _validate([_input_candidate()],
                  [_operation(candidate_refs=[_ref(uid=None)])], [])


# --------------------------------------------------------------------
# reference closure
# --------------------------------------------------------------------

def test_dangling_reference_is_rejected():
    with pytest.raises(ValueError):
        _validate([], [_operation(candidate_refs=[_ref()])], [])
    # Known uid, revision not among this commit's inputs.
    with pytest.raises(ValueError):
        _validate([_input_candidate(revision=1)],
                  [_operation(candidate_refs=[_ref(revision=2)])], [])


def test_reference_digest_mismatch_is_rejected():
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(candidate_refs=[_ref(digest=DIGEST_OTHER)])],
            [])


def test_consumption_closes_over_a_different_operation_reference():
    # The consuming operation must reference the identity itself; a
    # different operation referencing it does not help.
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(operation_id="op-1"),
             _operation(operation_id="op-2", candidate_refs=[_ref()])],
            [_consumed(operation_id="op-1")])


# --------------------------------------------------------------------
# consumed closure
# --------------------------------------------------------------------

def test_consumed_identity_must_exist_in_inputs():
    with pytest.raises(ValueError):
        _validate([], [], [_consumed()])
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate(revision=1)],
            [_operation()],
            [_consumed(revision=2)])


def test_consumed_digest_mismatch_is_rejected():
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(candidate_refs=[_ref()])],
            [_consumed(digest=DIGEST_OTHER)])


def test_consumed_with_dangling_operation_is_rejected():
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate()],
            [_operation(operation_id="op-1", candidate_refs=[_ref()])],
            [_consumed(operation_id="op-9")])


def test_consumed_operation_must_reference_the_candidate():
    # The named operation exists but references another candidate.
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate(), _input_candidate(uid="cand-b",
                                                  digest=DIGEST_B1)],
            [_operation(candidate_refs=[_ref(uid="cand-b",
                                             digest=DIGEST_B1)])],
            [_consumed()])
    # The named operation references the right uid at another revision.
    with pytest.raises(ValueError):
        _validate(
            [_input_candidate(revision=1, digest=DIGEST_A1),
             _input_candidate(revision=2, digest=DIGEST_A2)],
            [_operation(candidate_refs=[_ref(revision=2, digest=DIGEST_A2)])],
            [_consumed(revision=1, digest=DIGEST_A1)])
    # An operation with an empty listing consumes nothing.
    with pytest.raises(ValueError):
        _validate([_input_candidate()], [_operation()], [_consumed()])


def test_duplicate_terminal_consumption_is_rejected():
    inputs = [_input_candidate()]
    operations = [_operation(candidate_refs=[_ref()])]
    with pytest.raises(ValueError):
        _validate(inputs, operations, [_consumed(), _consumed()])
    # Also across two different operations naming the same identity.
    other_operation = _operation(operation_id="op-2",
                                 candidate_refs=[_ref()])
    with pytest.raises(ValueError):
        _validate(inputs, operations + [other_operation],
                  [_consumed(operation_id="op-1"),
                   _consumed(operation_id="op-2")])


def test_two_consumed_records_for_one_operation_close():
    assert _validate(
        [_input_candidate(uid="cand-a", digest=DIGEST_A1),
         _input_candidate(uid="cand-b", digest=DIGEST_B1)],
        [_operation(candidate_refs=[_ref(uid="cand-a", digest=DIGEST_A1),
                                    _ref(uid="cand-b", digest=DIGEST_B1)])],
        [_consumed(uid="cand-a", digest=DIGEST_A1),
         _consumed(uid="cand-b", digest=DIGEST_B1)],
    ) is None


def test_unconsumed_reference_is_legal():
    # Referenced but not (yet) terminally consumed: closure of what is
    # present does not force a consumption record.
    assert _validate(
        [_input_candidate()],
        [_operation(candidate_refs=[_ref()])],
        [],
    ) is None
