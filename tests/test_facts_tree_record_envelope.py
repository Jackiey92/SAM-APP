"""Pure fail-closed checks for the shared record-envelope validator.

Synthetic checks only, per approved design 97 sections 2.1, 2.2, 3 and
10.1 (DEC-030 / CHG-065): in-memory dicts built in this file. No real
company data, Git objects, refs, databases, models, services or feature
gates are touched.

Covered: a legal envelope passes (including record-family-specific
extra fields, which pass through unchecked); a non-dict payload and
non-str keys are TypeErrors; each missing envelope field is a
ValueError; wrong-typed, empty and mismatched record_type; bool, int
subclass, float, Decimal and str schema/format versions as TypeErrors;
zero, negative, unknown and mismatched versions as ValueErrors;
format_version pinned to git_format.FORMAT_VERSION; illegal and
wrong-typed repo/company IDs on both the expected and the actual side;
cross-repo and cross-company scope rejection; and the input payload is
never mutated.
"""
import copy
from decimal import Decimal

import pytest

from app.facts_tree import git_format
from app.facts_tree import record_envelope

RECORD_TYPE = "operation"
SCHEMA_VERSION = 1
REPO_ID = "repo-synthetic-0001"
COMPANY_ID = "co-synthetic-0001"


class _IntSubclass(int):
    pass


def _payload(**overrides):
    payload = {
        "record_type": RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": REPO_ID,
        "company_id": COMPANY_ID,
    }
    payload.update(overrides)
    return payload


def _validate(payload):
    return record_envelope.validate_record_envelope(
        payload,
        record_type=RECORD_TYPE,
        schema_version=SCHEMA_VERSION,
        repo_id=REPO_ID,
        company_id=COMPANY_ID,
    )


def test_legal_envelope_passes():
    assert _validate(_payload()) is None


def test_family_specific_fields_pass_through_unchecked():
    # The envelope check never guesses family-specific shapes; extra
    # str keys — even ones a later family validator would reject — are
    # left alone here.
    payload = _payload(
        operation_id="op-synthetic-0001",
        actor={"anything": object().__class__.__name__},
        notes=[1, "two", None],
    )
    assert _validate(payload) is None


def test_input_is_not_mutated():
    payload = _payload(extra={"nested": [1, 2]})
    snapshot = copy.deepcopy(payload)
    _validate(payload)
    assert payload == snapshot


def test_non_dict_payload_rejected():
    for bad in ([], "operation", 1, None, True):
        with pytest.raises(TypeError):
            _validate(bad)


def test_non_str_keys_rejected():
    payload = _payload()
    payload[1] = "not-a-str-key"
    with pytest.raises(TypeError):
        _validate(payload)


@pytest.mark.parametrize("field", [
    "record_type", "schema_version", "format_version",
    "repo_id", "company_id",
])
def test_missing_envelope_field_rejected(field):
    payload = _payload()
    del payload[field]
    with pytest.raises(ValueError):
        _validate(payload)


def test_record_type_wrong_type_rejected():
    with pytest.raises(TypeError):
        _validate(_payload(record_type=1))
    with pytest.raises(TypeError):
        _validate(_payload(record_type=None))


def test_record_type_empty_rejected():
    with pytest.raises(ValueError):
        _validate(_payload(record_type=""))


def test_record_type_unknown_rejected():
    with pytest.raises(ValueError):
        _validate(_payload(record_type="event"))
    with pytest.raises(ValueError):
        _validate(_payload(record_type="Operation"))


def test_expected_record_type_checked():
    payload = _payload()
    with pytest.raises(TypeError):
        record_envelope.validate_record_envelope(
            payload, record_type=1, schema_version=SCHEMA_VERSION,
            repo_id=REPO_ID, company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        record_envelope.validate_record_envelope(
            payload, record_type="", schema_version=SCHEMA_VERSION,
            repo_id=REPO_ID, company_id=COMPANY_ID)


@pytest.mark.parametrize("bad", [
    True, False, _IntSubclass(1), 1.0, Decimal(1), "1", None,
])
def test_schema_version_non_int_rejected(bad):
    with pytest.raises(TypeError):
        _validate(_payload(schema_version=bad))


@pytest.mark.parametrize("bad", [0, -1])
def test_schema_version_non_positive_rejected(bad):
    with pytest.raises(ValueError):
        _validate(_payload(schema_version=bad))


def test_schema_version_unknown_rejected():
    # The caller's family version is authoritative; an older or newer
    # record version fails closed.
    with pytest.raises(ValueError):
        _validate(_payload(schema_version=SCHEMA_VERSION + 1))
    with pytest.raises(ValueError):
        record_envelope.validate_record_envelope(
            _payload(), record_type=RECORD_TYPE,
            schema_version=SCHEMA_VERSION + 1,
            repo_id=REPO_ID, company_id=COMPANY_ID)


@pytest.mark.parametrize("bad", [
    True, _IntSubclass(1), 1.0, Decimal(1), "1", None,
])
def test_expected_schema_version_non_int_rejected(bad):
    with pytest.raises(TypeError):
        record_envelope.validate_record_envelope(
            _payload(), record_type=RECORD_TYPE, schema_version=bad,
            repo_id=REPO_ID, company_id=COMPANY_ID)


@pytest.mark.parametrize("bad", [
    True, _IntSubclass(1), 1.0, Decimal(1), "1", None,
])
def test_format_version_non_int_rejected(bad):
    with pytest.raises(TypeError):
        _validate(_payload(format_version=bad))


@pytest.mark.parametrize("bad", [
    0, -1, git_format.FORMAT_VERSION + 1,
])
def test_format_version_wrong_value_rejected(bad):
    # format_version is pinned to the current serializer version; a
    # zero, negative or unknown serializer version fails closed.
    with pytest.raises(ValueError):
        _validate(_payload(format_version=bad))


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
def test_scope_id_wrong_type_rejected(field):
    with pytest.raises(TypeError):
        _validate(_payload(**{field: 123}))
    with pytest.raises(TypeError):
        _validate(_payload(**{field: None}))


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
def test_scope_id_illegal_rejected(field):
    with pytest.raises(ValueError):
        _validate(_payload(**{field: ""}))
    with pytest.raises(ValueError):
        _validate(_payload(**{field: "x" * (git_format.MAX_ID_CHARS + 1)}))
    with pytest.raises(ValueError):
        _validate(_payload(**{field: "lone-surrogate-\ud800"}))


def test_cross_scope_rejected():
    # Legal but foreign scope IDs are ValueErrors, never silently
    # accepted, on both scope fields independently.
    with pytest.raises(ValueError):
        _validate(_payload(repo_id="repo-other-0001"))
    with pytest.raises(ValueError):
        _validate(_payload(company_id="co-other-0001"))


def test_expected_scope_id_checked():
    payload = _payload()
    with pytest.raises(TypeError):
        record_envelope.validate_record_envelope(
            payload, record_type=RECORD_TYPE, schema_version=SCHEMA_VERSION,
            repo_id=123, company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        record_envelope.validate_record_envelope(
            payload, record_type=RECORD_TYPE, schema_version=SCHEMA_VERSION,
            repo_id="", company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        record_envelope.validate_record_envelope(
            payload, record_type=RECORD_TYPE, schema_version=SCHEMA_VERSION,
            repo_id=REPO_ID, company_id=None)
    with pytest.raises(ValueError):
        record_envelope.validate_record_envelope(
            payload, record_type=RECORD_TYPE, schema_version=SCHEMA_VERSION,
            repo_id=REPO_ID, company_id="")


def test_no_io_and_returns_none():
    # A second legal shape: a different record family with its own
    # caller-supplied schema version passes unchanged.
    payload = _payload(record_type="event", schema_version=7)
    assert record_envelope.validate_record_envelope(
        payload, record_type="event", schema_version=7,
        repo_id=REPO_ID, company_id=COMPANY_ID) is None
