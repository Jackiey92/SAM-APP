"""Pure fail-closed checks for the operation/event closure validator.

Synthetic checks only, per approved design 97 sections 2.1, 3, 4 item 5
and 5: in-memory dicts built in this file. No real company data, Git
objects, refs, databases, models, services or feature gates are touched;
the validator itself is pure and is exercised strictly through its
public entry point.

Covered: empty inputs and event-less operations as exact closure;
sha1/sha256 base_commit shapes including the legal all-zero genesis
OID; legal-vs-illegal opaque IDs through git_format.encode_id;
wrong-typed record scope as TypeError and legal-but-foreign scope as
ValueError; duplicate operation/event IDs and duplicate event_ids
entries; the both-directions listing closure (dangling listing,
unlisted orphan event, event referencing a missing operation, listing
an event owned by another operation); operation/event base_commit
agreement; ordinal strictness (exact built-in int — bool, float,
Decimal and int subclasses rejected — non-negative, unique and
consecutive inside one operation, starting value not prescribed,
input order never deciding anything); the section-5 rejection of
``accepted_commit`` on events; additional payload fields staying
accepted because the full record schema belongs to other validators;
input immutability and arbitrary-iterable inputs.
"""
import copy
from decimal import Decimal

import pytest

from app.facts_tree.operation_event_closure import (
    validate_operation_event_closure,
)

REPO_ID = "repo-synthetic-0001"
COMPANY_ID = "co-synthetic-0001"
BASE_SHA1 = "9f2c41ae8b0d5e6f7a31c029d84b6e5f7a012345"
GENESIS_SHA1 = "0" * 40
BASE_SHA256 = "4c1d8a0e6f2b" + "ab" * 26
GENESIS_SHA256 = "0" * 64

OPERATION_CLOSURE_FIELDS = (
    "operation_id", "repo_id", "company_id", "base_commit", "event_ids")
EVENT_CLOSURE_FIELDS = (
    "event_id", "operation_id", "repo_id", "company_id", "base_commit",
    "ordinal")


class _IntSubclass(int):
    """An int subclass must never pass for a plain built-in int."""


def _operation(operation_id="op-1", base_commit=BASE_SHA1, event_ids=None,
               **overrides):
    # event_ids is stored verbatim so wrong-typed listings (tuple, str)
    # reach the validator unchanged; None means the empty listing.
    record = {
        "operation_id": operation_id,
        "repo_id": REPO_ID,
        "company_id": COMPANY_ID,
        "base_commit": base_commit,
        "event_ids": [] if event_ids is None else event_ids,
    }
    record.update(overrides)
    return record


def _event(event_id, operation_id="op-1", ordinal=0, base_commit=BASE_SHA1,
           **overrides):
    record = {
        "event_id": event_id,
        "operation_id": operation_id,
        "repo_id": REPO_ID,
        "company_id": COMPANY_ID,
        "base_commit": base_commit,
        "ordinal": ordinal,
    }
    record.update(overrides)
    return record


def _call(operations, events, *, repo_id=REPO_ID, company_id=COMPANY_ID,
          object_format="sha1"):
    return validate_operation_event_closure(
        operations, events, repo_id=repo_id, company_id=company_id,
        object_format=object_format)


# --- exact closure on success -------------------------------------------

def test_empty_inputs_pass():
    assert _call([], []) is None


def test_operation_without_events_is_exact_closure():
    assert _call([_operation(event_ids=[])], []) is None


def test_single_operation_with_two_events_passes():
    operation = _operation(event_ids=["evt-1", "evt-2"])
    events = [
        _event("evt-1", ordinal=0),
        _event("evt-2", ordinal=1),
    ]
    assert _call([operation], events) is None


def test_multiple_operations_close_independently():
    operations = [
        _operation("op-1", base_commit=BASE_SHA1,
                   event_ids=["evt-1", "evt-2"]),
        _operation("op-2", base_commit=GENESIS_SHA1, event_ids=["evt-3"]),
        _operation("op-3", base_commit=BASE_SHA1, event_ids=[]),
    ]
    events = [
        _event("evt-1", operation_id="op-1", ordinal=0),
        _event("evt-2", operation_id="op-1", ordinal=1),
        _event("evt-3", operation_id="op-2", ordinal=0,
               base_commit=GENESIS_SHA1),
    ]
    assert _call(operations, events) is None


@pytest.mark.parametrize("object_format,genesis", [
    ("sha1", GENESIS_SHA1),
    ("sha256", GENESIS_SHA256),
])
def test_all_zero_base_commit_is_the_legal_genesis_boundary(
        object_format, genesis):
    base = BASE_SHA1 if object_format == "sha1" else BASE_SHA256
    operations = [
        _operation("op-1", base_commit=genesis, event_ids=["evt-1"]),
        _operation("op-2", base_commit=base, event_ids=[]),
    ]
    events = [_event("evt-1", base_commit=genesis, ordinal=0)]
    assert _call(operations, events, object_format=object_format) is None


def test_sha256_commits_pass():
    operation = _operation(base_commit=BASE_SHA256, event_ids=["evt-1"])
    events = [_event("evt-1", base_commit=BASE_SHA256, ordinal=0)]
    assert _call([operation], events, object_format="sha256") is None


@pytest.mark.parametrize("start", [0, 1, 7])
def test_ordinals_are_consecutive_but_start_is_not_prescribed(start):
    ids = ["evt-%d" % index for index in range(3)]
    operation = _operation(event_ids=ids)
    events = [
        _event(event_id, ordinal=start + index)
        for index, event_id in enumerate(ids)
    ]
    assert _call([operation], events) is None


def test_same_ordinal_under_different_operations_passes():
    operations = [
        _operation("op-1", event_ids=["evt-1"]),
        _operation("op-2", base_commit=GENESIS_SHA1, event_ids=["evt-2"]),
    ]
    events = [
        _event("evt-1", operation_id="op-1", ordinal=3),
        _event("evt-2", operation_id="op-2", ordinal=3,
               base_commit=GENESIS_SHA1),
    ]
    assert _call(operations, events) is None


def test_input_order_never_decides_closure_or_sequence():
    # The events list and the event_ids listing both run backwards
    # against the ordinals; ordinal alone defines the sequence.
    operation = _operation(event_ids=["evt-2", "evt-1", "evt-0"])
    events = [
        _event("evt-2", ordinal=12),
        _event("evt-1", ordinal=11),
        _event("evt-0", ordinal=10),
    ]
    assert _call([operation], events) is None


def test_additional_payload_fields_stay_accepted():
    # The full record schema belongs to other validators; unknown extra
    # fields must not break the closure check.
    operation = _operation(
        event_ids=["evt-1"],
        actor={"kind": "installation_owner", "id": "actor-1"},
        request_digest="d" * 64,
        schema_version=1,
    )
    event = _event(
        "evt-1", ordinal=0,
        event_type="fact_revised",
        changed_refs=[],
        reason="synthetic",
        schema_version=1,
    )
    assert _call([operation], [event]) is None


def test_accepts_arbitrary_iterables():
    operation = _operation(event_ids=["evt-1"])
    event = _event("evt-1", ordinal=0)
    assert _call(
        (item for item in [operation]),
        (item for item in [event]),
    ) is None


def test_inputs_are_not_mutated():
    operations = [_operation("op-1", event_ids=["evt-1", "evt-2"]),
                  _operation("op-2", event_ids=[])]
    events = [_event("evt-1", ordinal=1), _event("evt-2", ordinal=0)]
    operations_before = copy.deepcopy(operations)
    events_before = copy.deepcopy(events)
    assert _call(operations, events) is None
    assert operations == operations_before
    assert events == events_before


# --- section 5: an event never embeds its own accepted commit -----------

@pytest.mark.parametrize("accepted", [
    "9f2c41ae8b0d5e6f7a31c029d84b6e5f7a012345",
    None,
    0,
])
def test_event_accepted_commit_is_rejected(accepted):
    operation = _operation(event_ids=["evt-1"])
    event = _event("evt-1", ordinal=0, accepted_commit=accepted)
    with pytest.raises(ValueError, match="accepted_commit"):
        _call([operation], [event])


# --- caller argument contract -------------------------------------------

@pytest.mark.parametrize("object_format", [None, 1, b"sha1", ["sha1"]])
def test_non_str_object_format_is_a_type_error(object_format):
    with pytest.raises(TypeError):
        _call([], [], object_format=object_format)


@pytest.mark.parametrize("object_format", ["md5", "SHA1", "", "sha-1"])
def test_unsupported_object_format_is_a_value_error(object_format):
    with pytest.raises(ValueError, match="object_format"):
        _call([], [], object_format=object_format)


@pytest.mark.parametrize("kwargs", [
    {"repo_id": 123},
    {"repo_id": None},
    {"company_id": True},
    {"company_id": b"co"},
])
def test_wrong_typed_expected_scope_is_a_type_error(kwargs):
    with pytest.raises(TypeError):
        _call([], [], **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"repo_id": ""},
    {"company_id": ""},
    {"repo_id": "x" * 129},
    {"company_id": "\ud800"},
])
def test_illegal_expected_scope_is_a_value_error(kwargs):
    with pytest.raises(ValueError):
        _call([], [], **kwargs)


@pytest.mark.parametrize("kind", ["operations", "events"])
@pytest.mark.parametrize("bad", ["op-1", b"op-1", 42, None])
def test_bad_input_collection_is_a_type_error(kind, bad):
    kwargs = {kind: bad}
    with pytest.raises(TypeError):
        _call(kwargs.get("operations", []), kwargs.get("events", []))


# --- record shape ---------------------------------------------------------

@pytest.mark.parametrize("record", [None, 42, "op", [("operation_id", "op")]])
def test_non_dict_operation_is_a_type_error(record):
    with pytest.raises(TypeError):
        _call([record], [])


@pytest.mark.parametrize("record", [None, 42, "evt", [("event_id", "evt")]])
def test_non_dict_event_is_a_type_error(record):
    with pytest.raises(TypeError):
        _call([], [record])


@pytest.mark.parametrize("kind", ["operation", "event"])
def test_non_str_record_keys_are_a_type_error(kind):
    record = _operation() if kind == "operation" else _event("evt-1")
    record[1] = "x"
    if kind == "operation":
        with pytest.raises(TypeError):
            _call([record], [])
    else:
        with pytest.raises(TypeError):
            _call([], [record])


@pytest.mark.parametrize("field", OPERATION_CLOSURE_FIELDS)
def test_operation_missing_a_closure_field_is_a_value_error(field):
    operation = _operation(event_ids=["evt-1"])
    del operation[field]
    event = _event("evt-1", ordinal=0)
    with pytest.raises(ValueError, match="missing"):
        _call([operation], [event] if field != "event_ids" else [])


@pytest.mark.parametrize("field", EVENT_CLOSURE_FIELDS)
def test_event_missing_a_closure_field_is_a_value_error(field):
    operation = _operation(event_ids=["evt-1"])
    event = _event("evt-1", ordinal=0)
    del event[field]
    with pytest.raises(ValueError, match="missing"):
        _call([operation], [event])


# --- IDs and scope --------------------------------------------------------

@pytest.mark.parametrize("value", [123, None, True, b"op-1", ["op-1"]])
def test_wrong_typed_operation_id_is_a_type_error(value):
    with pytest.raises(TypeError):
        _call([_operation(operation_id=value)], [])


@pytest.mark.parametrize("value", ["", "x" * 129, "\ud800"])
def test_illegal_operation_id_is_a_value_error(value):
    with pytest.raises(ValueError):
        _call([_operation(operation_id=value)], [])


@pytest.mark.parametrize("value", ["", "x" * 129, "\udfff"])
def test_illegal_event_id_is_a_value_error(value):
    operation = _operation(event_ids=[value])
    with pytest.raises(ValueError):
        _call([operation], [_event(value, ordinal=0)])


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
@pytest.mark.parametrize("value", [123, None, b"x"])
def test_wrong_typed_record_scope_is_a_type_error(field, value):
    operation = _operation(**{field: value})
    with pytest.raises(TypeError):
        _call([operation], [])


@pytest.mark.parametrize("kind", ["operation", "event"])
@pytest.mark.parametrize("field", ["repo_id", "company_id"])
def test_legal_but_foreign_scope_never_closes(kind, field):
    override = {field: "foreign-synthetic-9999"}
    if kind == "operation":
        operation = _operation(event_ids=["evt-1"], **override)
        events = [_event("evt-1", ordinal=0)]
    else:
        operation = _operation(event_ids=["evt-1"])
        events = [_event("evt-1", ordinal=0, **override)]
    with pytest.raises(ValueError, match="not the expected"):
        _call([operation], events)


# --- base_commit ----------------------------------------------------------

@pytest.mark.parametrize("kind", ["operation", "event"])
@pytest.mark.parametrize("value", [None, 123, b"a" * 40, ["a" * 40]])
def test_non_str_base_commit_is_a_type_error(kind, value):
    if kind == "operation":
        records = ([_operation(base_commit=value, event_ids=[])], [])
    else:
        records = ([_operation(event_ids=["evt-1"])],
                   [_event("evt-1", ordinal=0, base_commit=value)])
    with pytest.raises(TypeError):
        _call(*records)


@pytest.mark.parametrize("value", [
    "a" * 39,
    "a" * 41,
    "9F2C41AE8B0D5E6F7A31C029D84B6E5F7A012345",
    "g" * 40,
    "",
])
def test_malformed_sha1_base_commit_is_a_value_error(value):
    with pytest.raises(ValueError):
        _call([_operation(base_commit=value, event_ids=[])], [])


def test_sha1_length_commit_under_sha256_is_a_value_error():
    with pytest.raises(ValueError):
        _call([_operation(base_commit=BASE_SHA1, event_ids=[])], [],
              object_format="sha256")


def test_operation_and_event_base_commit_must_agree():
    operation = _operation(base_commit=BASE_SHA1, event_ids=["evt-1"])
    event = _event("evt-1", ordinal=0, base_commit=GENESIS_SHA1)
    with pytest.raises(ValueError, match="base_commit"):
        _call([operation], [event])


# --- event_ids ------------------------------------------------------------

@pytest.mark.parametrize("value", [None, ("evt-1",), "evt-1", 42])
def test_non_list_event_ids_is_a_type_error(value):
    operation = _operation()
    operation["event_ids"] = value  # verbatim, bypassing the helper default
    with pytest.raises(TypeError):
        _call([operation], [])


@pytest.mark.parametrize("value", [None, 42, b"evt-1"])
def test_wrong_typed_event_ids_entry_is_a_type_error(value):
    with pytest.raises(TypeError):
        _call([_operation(event_ids=[value])], [])


def test_illegal_event_ids_entry_is_a_value_error():
    with pytest.raises(ValueError):
        _call([_operation(event_ids=[""])], [])


def test_repeated_event_ids_entry_is_a_value_error():
    operation = _operation(event_ids=["evt-1", "evt-1"])
    with pytest.raises(ValueError, match="more than once"):
        _call([operation], [_event("evt-1", ordinal=0)])


# --- uniqueness and cross-record closure ----------------------------------

def test_duplicate_operation_id_is_a_value_error():
    operations = [_operation("op-1"), _operation("op-1")]
    with pytest.raises(ValueError, match="duplicate operation id"):
        _call(operations, [])


def test_duplicate_event_id_is_a_value_error():
    operations = [
        _operation("op-1", event_ids=["evt-1"]),
        _operation("op-2", event_ids=[]),
    ]
    events = [
        _event("evt-1", operation_id="op-1", ordinal=0),
        _event("evt-1", operation_id="op-2", ordinal=0),
    ]
    with pytest.raises(ValueError, match="duplicate event id"):
        _call(operations, events)


def test_listed_event_without_a_record_is_a_value_error():
    operation = _operation(event_ids=["evt-1", "evt-missing"])
    with pytest.raises(ValueError, match="no corresponding event"):
        _call([operation], [_event("evt-1", ordinal=0)])


def test_unlisted_event_is_an_orphan_and_a_value_error():
    operation = _operation(event_ids=[])
    with pytest.raises(ValueError, match="orphan"):
        _call([operation], [_event("evt-1", ordinal=0)])


def test_partially_listed_events_are_a_value_error():
    operation = _operation(event_ids=["evt-1"])
    events = [_event("evt-1", ordinal=0), _event("evt-2", ordinal=1)]
    with pytest.raises(ValueError, match="does not list"):
        _call([operation], events)


def test_event_referencing_a_missing_operation_is_a_value_error():
    operations = [_operation("op-1", event_ids=[])]
    events = [_event("evt-1", operation_id="op-absent", ordinal=0)]
    with pytest.raises(ValueError, match="dangling reference"):
        _call(operations, events)


def test_listing_an_event_owned_by_another_operation_is_a_value_error():
    operations = [
        _operation("op-1", event_ids=["evt-1"]),
        _operation("op-2", event_ids=["evt-1"]),
    ]
    events = [_event("evt-1", operation_id="op-2", ordinal=0)]
    with pytest.raises(ValueError):
        _call(operations, events)


def test_events_without_operations_never_close():
    with pytest.raises(ValueError, match="dangling reference"):
        _call([], [_event("evt-1", ordinal=0)])


# --- ordinal strictness -----------------------------------------------------

@pytest.mark.parametrize("ordinal", [
    True, False, 2.0, 0.0, Decimal(2), Decimal("2.0"), "2", None,
    _IntSubclass(2),
])
def test_non_plain_int_ordinal_is_a_type_error(ordinal):
    operation = _operation(event_ids=["evt-1"])
    event = _event("evt-1", ordinal=ordinal)
    with pytest.raises(TypeError):
        _call([operation], [event])


@pytest.mark.parametrize("ordinal", [-1, -7])
def test_negative_ordinal_is_a_value_error(ordinal):
    operation = _operation(event_ids=["evt-1"])
    event = _event("evt-1", ordinal=ordinal)
    with pytest.raises(ValueError, match="non-negative"):
        _call([operation], [event])


def test_duplicate_ordinal_within_one_operation_is_a_value_error():
    operation = _operation(event_ids=["evt-1", "evt-2"])
    events = [
        _event("evt-1", ordinal=4),
        _event("evt-2", ordinal=4),
    ]
    with pytest.raises(ValueError, match="more than one event with ordinal"):
        _call([operation], events)


def test_ordinal_gap_within_one_operation_is_a_value_error():
    operation = _operation(event_ids=["evt-1", "evt-2"])
    events = [
        _event("evt-1", ordinal=0),
        _event("evt-2", ordinal=2),
    ]
    with pytest.raises(ValueError, match="consecutive"):
        _call([operation], events)
