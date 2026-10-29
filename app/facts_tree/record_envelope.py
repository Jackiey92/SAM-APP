"""Pure fail-closed validator for the shared new-business-record envelope.

Approved design 97-业务记录与投影重建设计.md (SHA-256
8126bf3d69ecb7d9a3a8f2c01ab3625de4a1243673385b11615c744eaffc57ff,
DEC-030 / CHG-065) sections 2.1, 2.2, 3 and 10.1: every new business
record — the operation and event families of section 3 among them —
carries one common envelope: a ``record_type`` label, its record
family's own ``schema_version``, the serializer ``format_version`` and
the repo/company scope. This module implements only that envelope check
as a reusable pure primitive; it deliberately defines no operation or
event schema, freezes no family schema-version value (the caller passes
the expected one in), and validates no family-specific field, no
cross-record closure and no path identity.

Contract of ``validate_record_envelope``:

- The payload must be a dict with only str keys carrying the five
  envelope fields ``record_type``, ``schema_version``,
  ``format_version``, ``repo_id`` and ``company_id``; any other str
  key passes through unchecked for the record family's own validator.
- Expected and actual ``record_type`` must each be a non-empty str and
  exactly equal; unknown types fail closed.
- Expected and actual ``schema_version`` must each be an exact built-in
  positive int (only ``type(value) is int`` passes, so bool and every
  int subclass are rejected, per section 2.1's rule that a bool never
  poses as an integer) and exactly equal; unknown versions fail closed.
- ``format_version`` must be an exact built-in positive int equal to
  ``git_format.FORMAT_VERSION``; the serializer version is fixed by
  git_format and never by the caller.
- Expected and actual ``repo_id``/``company_id`` must each be legal
  under the existing ``git_format.encode_id`` rules (a wrong-typed
  scope is a TypeError, an illegal ID a ValueError) and the actual
  scope must equal the expected one exactly; a legal-but-foreign scope
  is a ValueError, never silently accepted.
- Errors follow the repository_meta/bindings_current convention:
  TypeError for wrong types, ValueError for wrong values, missing
  fields and every mismatch. Validation is pure — no file, Git,
  SQLite, network or model I/O — never mutates its input, and returns
  None on success.

Boundary (explicitly NOT done here): no actor/approval references
(CHG-068 is still pending), no complete operation/event field sets or
event semantics, no Commit Broker, Git ref, projection, feature gate or
real company data wiring.
"""
from app.facts_tree import git_format

_ENVELOPE_FIELDS = frozenset({
    "record_type",
    "schema_version",
    "format_version",
    "repo_id",
    "company_id",
})


def _check_id(name, value):
    # Same read-only ID legality contract as repository_meta,
    # git_taxonomy and bindings_current: encode_id performs the real
    # check (non-empty str within the character and encoded-segment
    # budgets); its encoded output is not needed here, only the
    # validation. This proves the value's structure, not that the host
    # actually issued it.
    try:
        git_format.encode_id(value)
    except TypeError:
        raise TypeError("%s must be a str" % name) from None
    except UnicodeEncodeError as exc:
        # A lone surrogate can never encode to UTF-8; classify it as an
        # illegal ID instead of leaking the codec error.
        raise ValueError("%s is not a legal id: %s" % (name, exc)) from None
    except ValueError as exc:
        raise ValueError("%s is not a legal id: %s" % (name, exc)) from None


def _check_record_type(name, value):
    if not isinstance(value, str):
        raise TypeError("%s must be a str" % name)
    if not value:
        raise ValueError("%s must be a non-empty str" % name)


def _check_positive_int(name, value):
    # Only the built-in int type itself passes (type(value) is int), so
    # bool and every int subclass are TypeErrors; a plain int that is
    # not strictly positive is a ValueError.
    if type(value) is not int:
        raise TypeError("%s must be a plain int" % name)
    if value <= 0:
        raise ValueError("%s must be a strictly positive int" % name)


def validate_record_envelope(
        payload, *, record_type, schema_version, repo_id, company_id):
    """Check the shared section-10.1 envelope of one new business record.

    ``record_type``, ``schema_version``, ``repo_id`` and ``company_id``
    are the caller's expected envelope: the record family's own type
    label, the schema version that family currently supports (this
    module freezes no family version), and the repository scope the
    record must belong to. The payload must be a dict with only str
    keys carrying the five envelope fields, and each actual value must
    match the expected one exactly (``format_version`` is matched
    against ``git_format.FORMAT_VERSION`` instead, since the serializer
    version is not the caller's choice). All other str keys pass
    through unchecked; their shape belongs to the record family's own
    validator, which runs after this envelope check. Returns None on
    success; raises TypeError for wrong types and ValueError for
    missing fields, illegal values and every mismatch. The input is
    never mutated.
    """
    _check_record_type("expected record_type", record_type)
    _check_positive_int("expected schema_version", schema_version)
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    if not isinstance(payload, dict):
        raise TypeError("record payload must be a dict")
    for key in payload:
        if not isinstance(key, str):
            raise TypeError(
                "record payload keys must all be str, got %r" % (key,))
    missing = _ENVELOPE_FIELDS - set(payload)
    if missing:
        raise ValueError(
            "record payload must carry the section-10.1 envelope fields "
            "(missing: %s)" % (sorted(missing),))
    actual_type = payload["record_type"]
    _check_record_type("record_type", actual_type)
    if actual_type != record_type:
        raise ValueError(
            "record_type mismatch: record carries %r, expected %r"
            % (actual_type, record_type))
    actual_schema = payload["schema_version"]
    _check_positive_int("schema_version", actual_schema)
    if actual_schema != schema_version:
        raise ValueError(
            "schema_version mismatch: record carries %r, expected %r"
            % (actual_schema, schema_version))
    actual_format = payload["format_version"]
    _check_positive_int("format_version", actual_format)
    if actual_format != git_format.FORMAT_VERSION:
        raise ValueError(
            "format_version mismatch: record carries %r, expected the "
            "current serializer version %r"
            % (actual_format, git_format.FORMAT_VERSION))
    _check_id("repo_id", payload["repo_id"])
    _check_id("company_id", payload["company_id"])
    if payload["repo_id"] != repo_id:
        raise ValueError(
            "record belongs to repo %r, not the expected repo %r"
            % (payload["repo_id"], repo_id))
    if payload["company_id"] != company_id:
        raise ValueError(
            "record belongs to company %r, not the expected company %r"
            % (payload["company_id"], company_id))
    return None
