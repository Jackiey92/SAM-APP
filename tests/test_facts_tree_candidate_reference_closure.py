"""Pure fail-closed checks for the candidate reference closure validator.

Synthetic checks only, per approved designs 02 §6/§10.3–10.5 and 97
§2/§3/§4 items 1 and 5: in-memory dicts built in this file with
synthetic SHA-256 constants. No real company data, Git objects, refs,
databases, models, services or feature gates are touched; the
validators are pure and are exercised strictly through their public
entry points.

Covered (payload-only closure entry): successful closure with one and
with several candidates and operations; empty collections and
unreferenced/unconsumed inputs as legal; unknown additional record
fields accepted; arbitrary iterables; input immutability on success
and on rejection; missing closure fields; wrong-typed records, keys,
candidate_refs containers and reference entries; the exactly-three-key
typed reference shape; candidate_revision strictness (exact built-in
int — bool, float, Decimal and int subclasses rejected — positive);
payload_digest shape (64-char lowercase hex); legal-vs-illegal opaque
IDs through git_format.encode_id; wrong-typed record scope as TypeError
and legal-but-foreign scope as ValueError; duplicate input identities;
duplicate operation ids; duplicate candidate references inside one
operation; dangling and digest-mismatched references; duplicate
terminal consumption; consumed records with a missing input identity,
a digest mismatch, a dangling operation or an operation that does not
reference the consumed revision and digest.

Covered (path–payload identity entry, design 97 §4 item 1): successful
closure under the canonical git_format.record_path() paths of all
three families, including escape-encoded uids, extra payload fields
and arbitrary iterables; payload ID/revision disagreeing with the
path; wrong record family paths; non-canonical alias paths (escape
aliases, leading-zero and signed revision segments, raw unencoded
tokens, traversal, doubled slashes, wrong case); item shape (a
non-tuple item, a wrong-length tuple, a non-str path, a non-dict
payload); bare-string and non-iterable collections; missing path
identity fields; the ID/revision strictness and record_path() budgets
reused from the payload entry, including lone surrogates; expected
scope checked first; the full payload closure re-applied on top of
canonical paths; input immutability; and the payload-only entry
continuing to pass unchanged.
"""
import copy
from decimal import Decimal

import pytest

from app.facts_tree import git_format
from app.facts_tree.candidate_reference_closure import (
    validate_candidate_path_payload_identity,
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


# Canonical paths are always built through the one approved helper; the
# tests never hard-code a second path template.
def _input_path(uid="cand-a", revision=1):
    return git_format.record_path(
        "input_candidate", candidate_uid=uid, candidate_revision=revision)


def _operation_path(operation_id="op-1"):
    return git_format.record_path("operation", operation_id=operation_id)


def _consumed_path(uid="cand-a", revision=1):
    return git_format.record_path(
        "consumed_candidate", candidate_uid=uid, candidate_revision=revision)


def _validate_paths(inputs, operations, consumed):
    return validate_candidate_path_payload_identity(
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


# --------------------------------------------------------------------
# path-payload identity: successful closure
# --------------------------------------------------------------------

def test_path_identity_empty_collections_close():
    assert _validate_paths([], [], []) is None


def test_path_identity_single_candidate_full_closure():
    assert _validate_paths(
        [(_input_path(), _input_candidate())],
        [(_operation_path(), _operation(candidate_refs=[_ref()]))],
        [(_consumed_path(), _consumed())],
    ) is None


def test_path_identity_multiple_candidates_and_operations_close():
    inputs = [
        (_input_path(uid="cand-a", revision=1),
         _input_candidate(uid="cand-a", revision=1, digest=DIGEST_A1)),
        (_input_path(uid="cand-a", revision=2),
         _input_candidate(uid="cand-a", revision=2, digest=DIGEST_A2)),
        (_input_path(uid="cand-b", revision=1),
         _input_candidate(uid="cand-b", revision=1, digest=DIGEST_B1)),
        (_input_path(uid="cand-c", revision=7),
         _input_candidate(uid="cand-c", revision=7, digest=DIGEST_C1)),
    ]
    operations = [
        (_operation_path(operation_id="op-1"),
         _operation(operation_id="op-1", candidate_refs=[
             _ref(uid="cand-a", revision=1, digest=DIGEST_A1),
             _ref(uid="cand-b", revision=1, digest=DIGEST_B1),
         ])),
        (_operation_path(operation_id="op-2"),
         _operation(operation_id="op-2", candidate_refs=[
             _ref(uid="cand-a", revision=2, digest=DIGEST_A2),
             _ref(uid="cand-c", revision=7, digest=DIGEST_C1),
         ])),
    ]
    consumed = [
        (_consumed_path(uid="cand-a", revision=1),
         _consumed(uid="cand-a", revision=1, digest=DIGEST_A1,
                   operation_id="op-1")),
        (_consumed_path(uid="cand-b", revision=1),
         _consumed(uid="cand-b", revision=1, digest=DIGEST_B1,
                   operation_id="op-1")),
        (_consumed_path(uid="cand-a", revision=2),
         _consumed(uid="cand-a", revision=2, digest=DIGEST_A2,
                   operation_id="op-2")),
        (_consumed_path(uid="cand-c", revision=7),
         _consumed(uid="cand-c", revision=7, digest=DIGEST_C1,
                   operation_id="op-2")),
    ]
    assert _validate_paths(inputs, operations, consumed) is None


def test_path_identity_accepts_encoded_uids_and_extra_fields():
    # A uid needing escape encoding closes only under its canonical
    # encoded path, and additional payload fields beyond the
    # identity/closure set never influence the path.
    uid = "cand-Ä"
    assert _validate_paths(
        [(_input_path(uid=uid),
          _input_candidate(uid=uid, schema_version=1,
                           record_type="input_candidate"))],
        [(_operation_path(),
          _operation(candidate_refs=[_ref(uid=uid)], base_commit="9" * 40))],
        [(_consumed_path(uid=uid),
          _consumed(uid=uid, disposition="accept_fact"))],
    ) is None


def test_path_identity_accepts_arbitrary_iterables():
    assert _validate_paths(
        iter([(_input_path(), _input_candidate())]),
        ((_operation_path(), _operation(candidate_refs=[_ref()])),),
        (item for item in [(_consumed_path(), _consumed())]),
    ) is None


def test_path_identity_inputs_are_never_mutated():
    inputs = [(_input_path(), _input_candidate())]
    operations = [(_operation_path(), _operation(candidate_refs=[_ref()]))]
    consumed = [(_consumed_path(), _consumed())]
    before = (copy.deepcopy(inputs), copy.deepcopy(operations),
              copy.deepcopy(consumed))
    assert _validate_paths(inputs, operations, consumed) is None
    assert (inputs, operations, consumed) == before
    # Also on rejection: a consumed payload under its input-family path.
    broken_consumed = [(_input_path(), _consumed())]
    before = (copy.deepcopy(inputs), copy.deepcopy(operations),
              copy.deepcopy(broken_consumed))
    with pytest.raises(ValueError):
        _validate_paths(inputs, operations, broken_consumed)
    assert (inputs, operations, broken_consumed) == before


# --------------------------------------------------------------------
# path-payload identity: payload identity must match the path
# --------------------------------------------------------------------

def test_input_path_must_carry_the_payload_identity():
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(uid="cand-b"), _input_candidate(uid="cand-a"))],
            [], [])
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(revision=2), _input_candidate(revision=1))],
            [], [])
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(revision=1),
              _input_candidate(revision=2, digest=DIGEST_A2))],
            [], [])


def test_operation_path_must_carry_the_operation_id():
    with pytest.raises(ValueError):
        _validate_paths(
            [],
            [(_operation_path(operation_id="op-2"),
              _operation(operation_id="op-1"))],
            [])


def test_consumed_path_must_carry_the_payload_identity():
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [(_consumed_path(uid="cand-b"), _consumed(uid="cand-a"))])
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [(_consumed_path(revision=2), _consumed(revision=1))])


# --------------------------------------------------------------------
# path-payload identity: wrong record family
# --------------------------------------------------------------------

def test_wrong_record_family_paths_are_rejected():
    # Each family rebuilds only its own fixed record type, so a path
    # from another family can never match — whatever the payload.
    with pytest.raises(ValueError):
        _validate_paths([(_consumed_path(), _input_candidate())], [], [])
    with pytest.raises(ValueError):
        _validate_paths([(_operation_path(), _input_candidate())], [], [])
    with pytest.raises(ValueError):
        _validate_paths([], [(_input_path(), _operation())], [])
    with pytest.raises(ValueError):
        _validate_paths([], [(_consumed_path(), _operation())], [])
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [(_input_path(), _consumed())])
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [(_operation_path(), _consumed())])


def test_foreign_family_payload_still_fails_closed_after_path_identity():
    # A consumed payload carries an operation_id, so it does sit at a
    # canonical operation path; the reused payload closure still
    # rejects it because it is not an operation record.
    with pytest.raises(ValueError):
        _validate_paths([], [(_operation_path(), _consumed())], [])


# --------------------------------------------------------------------
# path-payload identity: non-canonical alias paths
# --------------------------------------------------------------------

def test_non_canonical_alias_paths_are_rejected():
    # "~2D" decodes to "-" but is not the canonical encoding; a
    # leading-zero or signed revision segment, traversal, doubled
    # slashes, a trailing slash, an absolute path and a wrong-case
    # suffix all differ from the rebuilt canonical path. No prefix or
    # substring of the canonical path ever passes.
    alias_items = [
        ("inputs/cand~2Da/1.json", _input_candidate()),
        ("inputs/cand-a/01.json", _input_candidate()),
        ("inputs/cand-a/0.json", _input_candidate()),
        ("inputs/cand-a/+1.json", _input_candidate()),
        ("inputs/cand-a/r1.json", _input_candidate()),
        ("/inputs/cand-a/1.json", _input_candidate()),
        ("inputs//cand-a/1.json", _input_candidate()),
        ("inputs/cand-a/1.json/", _input_candidate()),
        ("inputs/../inputs/cand-a/1.json", _input_candidate()),
        ("inputs/cand-a/1.JSON", _input_candidate()),
        ("Inputs/cand-a/1.json", _input_candidate()),
        ("inputs/cand-a/1.json ", _input_candidate()),
    ]
    for path, payload in alias_items:
        with pytest.raises(ValueError):
            _validate_paths([(path, payload)], [], [])
    # A raw unencoded non-ASCII token is not the canonical encoding.
    with pytest.raises(ValueError):
        _validate_paths(
            [("inputs/cand-Ä/1.json", _input_candidate(uid="cand-Ä"))],
            [], [])
    with pytest.raises(ValueError):
        _validate_paths([], [("operations/op~2D1.json", _operation())], [])


# --------------------------------------------------------------------
# path-payload identity: item shapes and types
# --------------------------------------------------------------------

def test_path_items_must_be_two_item_tuples():
    # A bare payload (the old payload-only shape) is a caller type
    # error here, as is any non-tuple item.
    with pytest.raises(TypeError):
        _validate_paths([_input_candidate()], [], [])
    with pytest.raises(TypeError):
        _validate_paths([["inputs/cand-a/1.json", _input_candidate()]],
                        [], [])
    with pytest.raises(TypeError):
        _validate_paths(["inputs/cand-a/1.json"], [], [])
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(),)], [], [])
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), _input_candidate(), "extra")],
                        [], [])
    with pytest.raises(ValueError):
        _validate_paths([], [(_operation_path(), _operation(), 1)], [])
    with pytest.raises(ValueError):
        _validate_paths([], [], [()])


def test_path_and_payload_types_are_checked():
    with pytest.raises(TypeError):
        _validate_paths([(None, _input_candidate())], [], [])
    with pytest.raises(TypeError):
        _validate_paths([(b"inputs/cand-a/1.json", _input_candidate())],
                        [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), "cand-a")], [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), None)], [], [])
    with pytest.raises(TypeError):
        _validate_paths([], [(_operation_path(), [_operation()])], [])
    with pytest.raises(TypeError):
        _validate_paths([], [], [(_consumed_path(), "consumed")])


def test_path_entry_bare_string_and_non_iterable_collections():
    with pytest.raises(TypeError):
        _validate_paths("cand", [], [])
    with pytest.raises(TypeError):
        _validate_paths([], b"op", [])
    with pytest.raises(TypeError):
        _validate_paths([], [], "consumed")
    with pytest.raises(TypeError):
        _validate_paths(7, [], [])


# --------------------------------------------------------------------
# path-payload identity: identity fields reuse the existing strictness
# --------------------------------------------------------------------

def test_missing_path_identity_fields_are_value_errors():
    without_uid = _input_candidate()
    del without_uid["candidate_uid"]
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), without_uid)], [], [])
    without_revision = _input_candidate()
    del without_revision["candidate_revision"]
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), without_revision)], [], [])
    without_operation_id = _operation()
    del without_operation_id["operation_id"]
    with pytest.raises(ValueError):
        _validate_paths([], [(_operation_path(), without_operation_id)], [])
    without_consumed_uid = _consumed()
    del without_consumed_uid["candidate_uid"]
    with pytest.raises(ValueError):
        _validate_paths([], [], [(_consumed_path(), without_consumed_uid)])


def test_path_identity_reuses_id_and_revision_strictness():
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), _input_candidate(uid=""))], [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), _input_candidate(uid=5))], [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), _input_candidate(revision=True))],
                        [], [])
    with pytest.raises(TypeError):
        _validate_paths(
            [(_input_path(), _input_candidate(revision=_IntSubclass(1)))],
            [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), _input_candidate(revision="1"))],
                        [], [])
    with pytest.raises(TypeError):
        _validate_paths([(_input_path(), _input_candidate(revision=1.0))],
                        [], [])
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), _input_candidate(revision=0))],
                        [], [])
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), _input_candidate(revision=-1))],
                        [], [])
    # A lone surrogate can never encode, so it never reaches a path.
    with pytest.raises(ValueError):
        _validate_paths([(_input_path(), _input_candidate(uid="\ud800"))],
                        [], [])
    with pytest.raises(TypeError):
        _validate_paths(
            [], [(_operation_path(), _operation(operation_id=b"op-1"))], [])
    with pytest.raises(ValueError):
        _validate_paths(
            [], [(_operation_path(), _operation(operation_id=""))], [])
    # An identity whose canonical path would exceed the record_path()
    # budget can never close onto any path.
    with pytest.raises(ValueError):
        _validate_paths(
            [(_input_path(), _input_candidate(revision=10 ** 1100))],
            [], [])


def test_path_entry_expected_scope_is_checked_first():
    with pytest.raises(TypeError):
        validate_candidate_path_payload_identity(
            [], [], [], repo_id=None, company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        validate_candidate_path_payload_identity(
            [], [], [], repo_id=REPO_ID, company_id=7)
    with pytest.raises(ValueError):
        validate_candidate_path_payload_identity(
            [], [], [], repo_id="", company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        validate_candidate_path_payload_identity(
            [], [], [], repo_id=REPO_ID, company_id="")


# --------------------------------------------------------------------
# path-payload identity: the payload closure still applies on top
# --------------------------------------------------------------------

def test_path_entry_enforces_the_full_payload_closure():
    # Canonical paths everywhere; every payload rule and cross-record
    # closure check of the payload-only entry still applies on top.
    with pytest.raises(ValueError):
        # legal-but-foreign scope
        _validate_paths(
            [(_input_path(), _input_candidate(repo_id=OTHER_REPO_ID))],
            [], [])
    with pytest.raises(ValueError):
        # malformed digest
        _validate_paths(
            [(_input_path(), _input_candidate(digest="a1" * 31))], [], [])
    without_digest = _input_candidate()
    del without_digest["payload_digest"]
    with pytest.raises(ValueError):
        # missing closure field beyond the path identity fields
        _validate_paths([(_input_path(), without_digest)], [], [])
    bad_keys = _input_candidate()
    bad_keys[1] = "x"
    with pytest.raises(TypeError):
        # non-str payload key alongside valid identity fields
        _validate_paths([(_input_path(), bad_keys)], [], [])
    with pytest.raises(ValueError):
        # duplicate identity behind two equal canonical paths
        _validate_paths(
            [(_input_path(), _input_candidate()),
             (_input_path(), _input_candidate())], [], [])
    with pytest.raises(ValueError):
        # dangling reference
        _validate_paths(
            [], [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [])
    with pytest.raises(ValueError):
        # reference digest mismatch
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(),
              _operation(candidate_refs=[_ref(digest=DIGEST_OTHER)]))],
            [])
    with pytest.raises(ValueError):
        # dangling consumption
        _validate_paths([], [], [(_consumed_path(), _consumed())])
    with pytest.raises(ValueError):
        # double terminal consumption
        _validate_paths(
            [(_input_path(), _input_candidate())],
            [(_operation_path(), _operation(candidate_refs=[_ref()]))],
            [(_consumed_path(), _consumed()), (_consumed_path(), _consumed())])


def test_payload_only_entry_still_closes_the_same_records():
    # The path–payload entry is an additional entrance; the payload-only
    # entry keeps accepting the very same payloads without any paths
    # and keeps rejecting the same closure violations.
    inputs = [_input_candidate()]
    operations = [_operation(candidate_refs=[_ref()])]
    consumed = [_consumed()]
    assert _validate(inputs, operations, consumed) is None
    with pytest.raises(ValueError):
        _validate(inputs,
                  [_operation(candidate_refs=[_ref(uid="cand-z")])],
                  consumed)
