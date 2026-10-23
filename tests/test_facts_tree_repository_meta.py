"""Repository identity binding record construction and validation checks.

All repo_id / installation_id / company_id values below are synthetic
fixtures built in this file; no real company data, repositories, Git
objects, files, databases, models or services are touched. The contract
under test is F-001 P01 sections 10.1, 10.2.2 and 10.2.3 (approved
design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f): an immutable
repo/installation/company binding record carrying its own versioned
schema_version plus the git_format serializer's format_version as an
independent second version field. The checks only prove structure and
equality of caller-supplied values; they never prove an ID was actually
issued or authenticated by the host.
"""
from copy import deepcopy

import pytest

from app.facts_tree import git_format
from app.facts_tree import repository_meta
from app.facts_tree.repository_meta import (
    SCHEMA_VERSION,
    build_repository_meta,
    validate_repository_meta,
)

REPO_ID = "repo-synthetic-0001"
INSTALLATION_ID = "install-synthetic-0001"
COMPANY_ID = "acc-synthetic-0001"


def _valid_payload(**overrides):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": REPO_ID,
        "installation_id": INSTALLATION_ID,
        "company_id": COMPANY_ID,
    }
    payload.update(overrides)
    return payload


def _build(**overrides):
    ids = {
        "repo_id": REPO_ID,
        "installation_id": INSTALLATION_ID,
        "company_id": COMPANY_ID,
    }
    ids.update(overrides)
    return build_repository_meta(**ids)


def _validate(payload, **overrides):
    expected = {
        "repo_id": REPO_ID,
        "installation_id": INSTALLATION_ID,
        "company_id": COMPANY_ID,
    }
    expected.update(overrides)
    return validate_repository_meta(payload, **expected)


# -- build_repository_meta ------------------------------------------------


def test_build_returns_exact_record_fields():
    record = _build()
    assert record == _valid_payload()
    assert set(record) == {
        "schema_version", "format_version",
        "repo_id", "installation_id", "company_id"}


def test_build_uses_integer_schema_version_one():
    record = _build()
    assert record["schema_version"] == 1
    assert type(record["schema_version"]) is int


def test_build_uses_serializer_format_version():
    record = _build()
    assert record["format_version"] == git_format.FORMAT_VERSION
    assert type(record["format_version"]) is int


def test_record_schema_version_is_independent_of_serializer_version():
    # The record's own schema_version must not silently track the
    # git_format serializer version. Both happen to equal 1 in their
    # first versions; SCHEMA_VERSION is pinned here as this record's
    # own schema marker, defined by repository_meta, not re-exported
    # from git_format.
    assert SCHEMA_VERSION == 1
    assert git_format.FORMAT_VERSION == 1
    assert "FORMAT_VERSION" not in vars(repository_meta)


def test_record_carries_two_separate_version_fields():
    # schema_version and format_version are two independent fields in
    # the same record, not one field serving both roles.
    record = _build()
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["format_version"] == git_format.FORMAT_VERSION


def test_build_accepts_non_ascii_id():
    record = _build(company_id="acc-合成-公司")
    assert record["company_id"] == "acc-合成-公司"


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_build_rejects_non_str_id(field):
    with pytest.raises(TypeError):
        _build(**{field: 42})


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_build_rejects_empty_id(field):
    with pytest.raises(ValueError):
        _build(**{field: ""})


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_build_rejects_overlong_id(field):
    with pytest.raises(ValueError):
        _build(**{field: "x" * (git_format.MAX_ID_CHARS + 1)})


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_build_rejects_id_with_overlong_encoded_segment(field):
    # Every byte of "é" escapes to three characters, so a short enough
    # character count can still blow the encoded segment budget.
    with pytest.raises(ValueError):
        _build(**{field: "é" * git_format.MAX_ID_CHARS})


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_build_rejects_id_with_lone_surrogate(field):
    # A lone surrogate can never encode to UTF-8; the failure must be a
    # clear illegal-ID ValueError, not a leaked UnicodeEncodeError.
    with pytest.raises(ValueError):
        _build(**{field: "\ud800"})


def test_build_does_not_mutate_caller_strings():
    repo_id = REPO_ID
    record = build_repository_meta(repo_id, INSTALLATION_ID, COMPANY_ID)
    assert repo_id == REPO_ID
    assert record["repo_id"] == repo_id


def test_build_output_round_trips_through_canonical_serializer():
    record = _build()
    data = git_format.dumps_record(record)
    assert git_format.loads_record(data) == record


# -- validate_repository_meta: happy path ---------------------------------


def test_validate_accepts_freshly_built_record():
    record = _build()
    assert _validate(record) is None


def test_validate_does_not_mutate_payload():
    record = _build()
    snapshot = deepcopy(record)
    _validate(record)
    assert record == snapshot


def test_validate_accepts_non_ascii_matching_identity():
    record = _build(company_id="acc-合成-公司")
    assert _validate(record, company_id="acc-合成-公司") is None


# -- validate_repository_meta: structure ----------------------------------


@pytest.mark.parametrize("payload", [
    None,
    "not a dict",
    ["schema_version", "repo_id"],
    1,
    True,
])
def test_validate_rejects_non_dict_payload(payload):
    with pytest.raises(TypeError):
        _validate(payload)


@pytest.mark.parametrize("field", [
    "schema_version", "format_version",
    "repo_id", "installation_id", "company_id",
])
def test_validate_rejects_missing_field(field):
    payload = _valid_payload()
    del payload[field]
    with pytest.raises(ValueError):
        _validate(payload)


@pytest.mark.parametrize("extra_key", ["actor", "policy_revision", ""])
def test_validate_rejects_extra_field(extra_key):
    payload = _valid_payload(**{extra_key: "unexpected"})
    with pytest.raises(ValueError):
        _validate(payload)


def test_validate_rejects_missing_and_extra_field_together():
    payload = _valid_payload()
    del payload["repo_id"]
    payload["unexpected"] = "x"
    with pytest.raises(ValueError):
        _validate(payload)


def test_validate_rejects_non_string_dict_key():
    payload = _valid_payload()
    payload[1] = "unexpected"
    with pytest.raises(TypeError, match="keys must all be str"):
        _validate(payload)


def test_validate_rejects_mixed_dict_keys_without_sorted_leak():
    # A mixed str/int key set must fail with the explicit key-type
    # error, not with an incidental TypeError from sorted() comparing
    # str and int keys.
    payload = _valid_payload()
    del payload["repo_id"]
    payload[1] = "x"
    with pytest.raises(TypeError, match="keys must all be str"):
        _validate(payload)


# -- validate_repository_meta: schema_version -----------------------------


@pytest.mark.parametrize("version", [0, 2, -1, 9999])
def test_validate_rejects_unknown_schema_version(version):
    with pytest.raises(ValueError):
        _validate(_valid_payload(schema_version=version))


@pytest.mark.parametrize("version", [True, False])
def test_validate_rejects_bool_schema_version(version):
    with pytest.raises(TypeError):
        _validate(_valid_payload(schema_version=version))


@pytest.mark.parametrize("version", ["1", 1.0, None, [1]])
def test_validate_rejects_non_int_schema_version(version):
    with pytest.raises(TypeError):
        _validate(_valid_payload(schema_version=version))


# -- validate_repository_meta: format_version -----------------------------


@pytest.mark.parametrize("version", [0, 2, -1, 9999])
def test_validate_rejects_unknown_format_version(version):
    with pytest.raises(ValueError):
        _validate(_valid_payload(format_version=version))


@pytest.mark.parametrize("version", [True, False])
def test_validate_rejects_bool_format_version(version):
    with pytest.raises(TypeError):
        _validate(_valid_payload(format_version=version))


@pytest.mark.parametrize("version", ["1", 1.0, None, [1]])
def test_validate_rejects_non_int_format_version(version):
    with pytest.raises(TypeError):
        _validate(_valid_payload(format_version=version))


def test_validate_rejects_format_version_off_serializer_version():
    # The record's format_version must equal the serializer version the
    # host currently supports, independently of schema_version.
    payload = _valid_payload(format_version=git_format.FORMAT_VERSION + 1)
    with pytest.raises(ValueError):
        _validate(payload)


# -- validate_repository_meta: illegal recorded IDs ------------------------


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_non_str_recorded_id(field):
    with pytest.raises(TypeError):
        _validate(_valid_payload(**{field: 42}))


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_empty_recorded_id(field):
    with pytest.raises(ValueError):
        _validate(_valid_payload(**{field: ""}))


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_overlong_recorded_id(field):
    with pytest.raises(ValueError):
        _validate(_valid_payload(**{field: "x" * (git_format.MAX_ID_CHARS + 1)}))


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_lone_surrogate_recorded_id(field):
    with pytest.raises(ValueError):
        _validate(_valid_payload(**{field: "\ud800"}))


# -- validate_repository_meta: identity mismatch ---------------------------


def test_validate_rejects_repo_id_mismatch():
    payload = _valid_payload(repo_id="repo-synthetic-9999")
    with pytest.raises(ValueError):
        _validate(payload)


def test_validate_rejects_installation_id_mismatch():
    payload = _valid_payload(installation_id="install-synthetic-9999")
    with pytest.raises(ValueError):
        _validate(payload)


def test_validate_rejects_company_id_mismatch():
    payload = _valid_payload(company_id="acc-synthetic-9999")
    with pytest.raises(ValueError):
        _validate(payload)


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_mismatch_from_expected_side(field):
    # The comparison is symmetric: a legal expected value that differs
    # from the recorded one fails just like a tampered record.
    record = _build()
    expected = dict(
        repo_id=REPO_ID,
        installation_id=INSTALLATION_ID,
        company_id=COMPANY_ID,
    )
    expected[field] = "expected-" + expected[field]
    with pytest.raises(ValueError):
        validate_repository_meta(record, **expected)


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_illegal_expected_id(field):
    with pytest.raises(ValueError):
        _validate(_build(), **{field: ""})


def test_validate_rejects_empty_expected_id_type_error():
    with pytest.raises(TypeError):
        _validate(_build(), repo_id=None)


@pytest.mark.parametrize("field", ["repo_id", "installation_id", "company_id"])
def test_validate_rejects_lone_surrogate_expected_id(field):
    with pytest.raises(ValueError):
        _validate(_build(), **{field: "\ud800"})
