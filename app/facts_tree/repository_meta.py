"""Immutable identity binding record for a facts Git repository.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 10.1 (each local installation serves exactly one
company; the facts repository is bound to an immutable repo_id,
installation_id and company_id), section 10.2.2 (the binding record
lives at the repository-relative path ``meta/repository.json``) and
section 10.2.3 (record schema and serializer are versioned
independently).

This module is a pair of pure functions:

- ``build_repository_meta(repo_id, installation_id, company_id)``
  constructs the first-version binding record: a dict holding two
  independent integer version fields plus exactly the three identity
  fields and nothing else. ``schema_version`` is SCHEMA_VERSION, the
  version of this record's own schema; ``format_version`` is
  git_format.FORMAT_VERSION, the version of the serializer rules the
  record is written with. Both currently equal 1, but they version
  different things and are never merged, derived from each other, or
  used one in place of the other.
- ``validate_repository_meta(payload, *, repo_id, installation_id,
  company_id)`` checks that a payload is exactly such a record and
  that each recorded identity equals the value the calling host
  passes in as its expected context.

ID legality reuses the read-only ``git_format.encode_id`` contract:
every identity must be a non-empty str within the ID character and
encoded-segment budgets. These checks prove only the structure of the
caller-supplied values; they cannot prove that any ID was actually
issued or authenticated by the host. A lone surrogate that cannot
encode to UTF-8 is reported as a clear illegal-ID ValueError, never
leaked as a raw UnicodeEncodeError. Validation never mutates the
payload; it returns None on success and expresses problems as
TypeError (wrong types, non-string dict keys) or ValueError (wrong
values, wrong field set, identity mismatch).

Boundary (explicitly NOT done here, per P01 sections 10.1-10.2):

- The comparison only proves the record equals the caller-supplied
  expected identity. It does not authenticate any actor, supply
  actor/policy/delegation authorization, confirm ownership of any
  repository, or prove the IDs were issued by any party; the host
  alone vouches for the expected values.
- Nothing here reads or writes ``meta/repository.json`` or any file,
  Git object, ref or database; persistence belongs to the host and
  the Commit Broker path.
- No connection to the facts-tree write flow, business APIs, legacy
  entry points or the runtime; no default feature gate is touched.
"""
from app.facts_tree import git_format

SCHEMA_VERSION = 1

_ID_FIELDS = ("repo_id", "installation_id", "company_id")
_RECORD_FIELDS = frozenset(
    ("schema_version", "format_version") + _ID_FIELDS)


def _check_id(name, value):
    # encode_id performs the full ID legality check (non-empty str
    # within the character and encoded-segment budgets); its encoded
    # output is not needed here, only the validation. This proves the
    # value's structure, not that any party actually issued it.
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


def _check_version(name, value, supported):
    # Each version field is checked against its own supported version:
    # the record schema against SCHEMA_VERSION, the serializer version
    # against the current git_format.FORMAT_VERSION.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s must be a plain int" % name)
    if value != supported:
        raise ValueError("unsupported %s: %r" % (name, value))


def build_repository_meta(repo_id, installation_id, company_id):
    """Build the immutable identity binding record for one facts repo.

    All three identities are validated as legal IDs; the returned dict
    is a fresh object the caller may serialize with
    git_format.dumps_record. Constructing the record does not write it
    anywhere.
    """
    _check_id("repo_id", repo_id)
    _check_id("installation_id", installation_id)
    _check_id("company_id", company_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "repo_id": repo_id,
        "installation_id": installation_id,
        "company_id": company_id,
    }


def validate_repository_meta(payload, *, repo_id, installation_id, company_id):
    """Check that payload is exactly this record with this identity.

    The payload must be a dict carrying exactly the five record fields:
    a plain int schema_version equal to SCHEMA_VERSION, a plain int
    format_version equal to the current git_format.FORMAT_VERSION (bool
    is rejected for both even though it subclasses int; the two
    versions are independent fields and each must equal its own
    supported version) and the three identity fields, each a legal ID
    whose value equals the caller-supplied expected value. Returns None
    on success and never mutates the payload. A mismatch between the
    recorded identity and the expected context is a ValueError; it is
    not an authentication result of any kind.
    """
    if not isinstance(payload, dict):
        raise TypeError("repository meta record must be a dict")
    for key in payload:
        if not isinstance(key, str):
            raise TypeError(
                "repository meta record keys must all be str, got %r"
                % (key,))
    fields = set(payload)
    if fields != _RECORD_FIELDS:
        missing = sorted(_RECORD_FIELDS - fields)
        extra = sorted(fields - _RECORD_FIELDS)
        raise ValueError(
            "repository meta record must carry exactly the fields %s "
            "(missing: %s, extra: %s)"
            % (sorted(_RECORD_FIELDS), missing, extra))
    _check_version(
        "schema_version", payload["schema_version"], SCHEMA_VERSION)
    _check_version(
        "format_version", payload["format_version"],
        git_format.FORMAT_VERSION)
    expected = {
        "repo_id": repo_id,
        "installation_id": installation_id,
        "company_id": company_id,
    }
    for name in _ID_FIELDS:
        _check_id("expected %s" % name, expected[name])
        _check_id(name, payload[name])
        if payload[name] != expected[name]:
            raise ValueError(
                "%s mismatch: record carries %r but the caller expected %r"
                % (name, payload[name], expected[name]))
    return None
