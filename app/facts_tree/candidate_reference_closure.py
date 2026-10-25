"""Pure fail-closed candidate reference closure for one fixed commit.

Approved designs 02-架构与接口设计.md §6 (the ``candidate_refs``
contract), §10.3–10.5 (commit/consumption semantics) and
97-业务记录与投影重建设计.md §2 (typed references), §3 (the
``input_candidate`` / ``operation`` / ``consumed_candidate`` record
rows), §4 item 5 (操作闭合), under DEC-030 / CHG-065. The host reads
every ``input_candidate``, ``operation`` and ``consumed_candidate``
payload from the same fixed commit C and hands all three collections
to this validator; that the payloads really come from one and the same
C is the caller's precondition, not something provable here. The
validator checks that the candidate references close exactly inside
that one C, and nothing beyond that closure.

Public entry point (pure, fail closed, never mutates its inputs):

    validate_candidate_reference_closure(
        input_candidates, operations, consumed_candidates,
        *, repo_id, company_id)

The three positional arguments are iterables of payload dicts (a bare
str/bytes is a caller TypeError, not an iterable of one-character
"records"). ``repo_id`` and ``company_id`` are the caller's expected
scope, checked as legal opaque IDs through ``git_format.encode_id``.
Returns None on success.

What is checked
---------------

- Every record is a dict with all-str keys carrying the closure
  fields: ``candidate_uid``, ``candidate_revision``, ``payload_digest``,
  ``repo_id`` and ``company_id`` on an input_candidate and on a
  consumed_candidate; ``operation_id``, ``repo_id``, ``company_id``
  and ``candidate_refs`` on an operation. A missing field is a
  ValueError.
- ``repo_id``, ``company_id``, ``operation_id`` and ``candidate_uid``
  are legal opaque host-issued IDs under the same read-only
  ``git_format.encode_id`` contract as ``repository_meta`` and
  ``bindings_current`` (this proves the ID's shape, not that the host
  actually issued it). Each record's own scope is checked as legal IDs
  first — a wrong-typed scope is a TypeError — then compared against
  the caller's expected scope, so a legal-but-foreign repo/company can
  never close (ValueError).
- ``candidate_revision`` is an exact built-in positive int —
  ``type(value) is int`` and ``value >= 1``, so bool and every int
  subclass are TypeErrors and never pass through Python's loose
  numeric equality — on every record and inside every candidate
  reference.
- ``payload_digest`` is a 64-character lowercase hexadecimal SHA-256
  string everywhere it appears (a non-str is a TypeError, a malformed
  digest a ValueError). Digests are only compared for equality; this
  validator never recomputes a digest from any payload.
- The input candidate identity ``(candidate_uid, candidate_revision)``
  is unique across the input_candidates collection and ``operation_id``
  is unique across the operations collection (a duplicate is a
  ValueError). Identity uniqueness is what makes every later reference
  resolution unambiguous: two inputs claiming the same identity can
  never pass and turn a reference ambiguous.
- ``candidate_refs`` on an operation is a list (TypeError otherwise)
  of typed references, each a dict with exactly the three keys
  ``candidate_uid``, ``candidate_revision`` and ``payload_digest`` (a
  non-dict entry is a TypeError, a wrong key set a ValueError). The
  references do not repeat inside one operation.
- Every candidate_refs target resolves to the one unique
  input_candidate of this commit carrying the same identity, with an
  identical ``payload_digest``: a dangling reference (no input for the
  identity, including a known uid under a revision that is not in this
  commit) and a digest disagreement are ValueErrors. A cross-scope
  target cannot arise here because every record must already share the
  caller's expected scope.
- A consumed_candidate's candidate identity exists among the
  input_candidates with an identical ``payload_digest``; its
  ``operation_id`` names one operation of the same collection; and
  that operation's ``candidate_refs`` contain exactly this
  ``(candidate_uid, candidate_revision, payload_digest)`` triple.
  Dangling consumption, a digest mismatch, a dangling operation and an
  operation that does not reference the consumed revision and digest
  are all ValueErrors.
- Each candidate identity is terminally consumed at most once: a
  second consumed_candidate for the same ``(candidate_uid,
  candidate_revision)`` is a ValueError, whatever operation it names.

Not every input_candidate must be referenced or consumed; an operation
may carry an empty ``candidate_refs`` list and the empty triple of
input collections passes: this validator checks the closure of what is
present, never that any candidate was processed at all.

Problems are reported in a fixed order: the caller arguments first
(expected repo_id, then company_id), then each input_candidate in
input order, then each operation in input order (own fields first,
then its references in list order), then each consumed_candidate in
input order (own fields, duplicate consumption, then the closure
checks), so the reported problem never depends on dict or set
iteration luck.

Error contract (same convention as ``repository_meta``,
``bindings_current``, ``operation_event_closure`` and
``git_tree_validation``): TypeError for wrong types (including bool or
int subclasses posing as plain ints) and ValueError for wrong values,
illegal IDs, missing fields, duplicate identities, digest/scope
disagreements and every closure violation.

Boundary (explicitly NOT done here): this validator checks only the
candidate reference closure named above. It does not validate the full
record schemas (schema_version/format_version/record_type wrappers,
request/effect digests, whitelisted candidate payload fields), fact
semantics, dispositions or result_ref contents; it never recomputes
candidate payloads or their digests; it does not check frozen task
manifests, coverage lists, authorization or approvals; it does not
verify Git paths or object existence; and it cannot prove that the
caller really read all payloads from one and the same commit C.
Additional payload fields beyond the closure set are therefore
accepted; the full record schema belongs to the record-schema
validators and the later full-tree card. The module is pure in-memory:
no Git, file, SQLite, network or model IO, no Commit Broker wiring, no
feature-gate change, and it is not a full tree validator; every
rejection happens before the caller could update any Git ref.
"""
import re

from app.facts_tree import git_format

_DIGEST_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

# The fields this closure needs on each record. This is deliberately not
# a full record schema: unknown additional fields stay the business of
# the record-schema validators, while a *missing* closure field makes
# the cross-record checks below impossible and is rejected.
_INPUT_REQUIRED_FIELDS = frozenset({
    "candidate_uid",
    "candidate_revision",
    "payload_digest",
    "repo_id",
    "company_id",
})
_OPERATION_REQUIRED_FIELDS = frozenset({
    "operation_id",
    "repo_id",
    "company_id",
    "candidate_refs",
})
_CONSUMED_REQUIRED_FIELDS = frozenset({
    "candidate_uid",
    "candidate_revision",
    "payload_digest",
    "operation_id",
    "repo_id",
    "company_id",
})
# A candidate reference is a typed reference with exactly these three
# keys (design §2.2 / §6): the candidate identity, its exact revision
# and the content digest of that revision.
_REFERENCE_KEYS = frozenset({
    "candidate_uid",
    "candidate_revision",
    "payload_digest",
})


def validate_candidate_reference_closure(input_candidates, operations,
                                         consumed_candidates, *, repo_id,
                                         company_id):
    """Close candidate references inside one fixed commit.

    See the module docstring for the full contract. Returns None on
    success; raises TypeError for wrong types and ValueError for illegal
    values, duplicate identities and every closure violation. Never
    mutates its inputs.
    """
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    input_records = _materialize("input_candidates", input_candidates)
    operation_records = _materialize("operations", operations)
    consumed_records = _materialize("consumed_candidates", consumed_candidates)

    inputs_by_identity = {}
    for record in input_records:
        _validate_input_candidate(record, repo_id, company_id)
        identity = (record["candidate_uid"], record["candidate_revision"])
        if identity in inputs_by_identity:
            raise ValueError(
                "duplicate input_candidate identity (%r, %r) in the same "
                "commit" % identity)
        inputs_by_identity[identity] = record

    operations_by_id = {}
    for operation in operation_records:
        _validate_operation(
            operation, inputs_by_identity, repo_id, company_id)
        operation_id = operation["operation_id"]
        if operation_id in operations_by_id:
            raise ValueError(
                "duplicate operation id %r in the same commit"
                % (operation_id,))
        operations_by_id[operation_id] = operation

    consumed_identities = set()
    for consumed in consumed_records:
        _validate_consumed(consumed, repo_id, company_id)
        identity = (consumed["candidate_uid"], consumed["candidate_revision"])
        if identity in consumed_identities:
            raise ValueError(
                "candidate identity (%r, %r) is terminally consumed more "
                "than once in the same commit" % identity)
        consumed_identities.add(identity)
        _check_consumed_closure(consumed, inputs_by_identity, operations_by_id)
    return None


def _materialize(kind, records):
    # A bare str/bytes would otherwise silently iterate into
    # single-character "records"; it is a caller type error, not an
    # iterable of payloads.
    if isinstance(records, (str, bytes)):
        raise TypeError(
            "%s must be an iterable of record dicts, not a single %s"
            % (kind, type(records).__name__))
    try:
        return list(records)
    except TypeError:
        raise TypeError(
            "%s must be an iterable of record dicts" % kind) from None


def _check_id(name, value):
    # Same read-only ID legality contract as repository_meta,
    # bindings_current and operation_event_closure: encode_id performs
    # the real check (non-empty str within the character and
    # encoded-segment budgets); its encoded output is not needed here.
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


def _require_str_keys(payload, label):
    for key in payload:
        if not isinstance(key, str):
            raise TypeError(
                "%s keys must all be str, got %r" % (label, key))


def _check_revision(name, value):
    # Exact built-in int only: bool and every int subclass are rejected
    # even though they compare equal to plain ints (section 2.1 forbids
    # bools posing as ints), and float/Decimal never pass.
    if type(value) is not int:
        raise TypeError(
            "%s must be a plain built-in int, got %s"
            % (name, type(value).__name__))
    if value <= 0:
        raise ValueError("%s must be positive, got %r" % (name, value))


def _check_digest(name, value):
    # Shape check only: a 64-char lowercase hex SHA-256 string. The
    # digest is never recomputed from any payload here.
    if not isinstance(value, str):
        raise TypeError(
            "%s must be a str, got %s" % (name, type(value).__name__))
    if not _DIGEST_PATTERN.match(value):
        raise ValueError(
            "%s must be a 64-character lowercase hexadecimal SHA-256 "
            "digest, got %r" % (name, value))


def _check_scope(kind, record_id, record, repo_id, company_id):
    # The record's own scope fields are checked as legal IDs before any
    # comparison, so a wrong-typed scope is the documented TypeError for
    # a wrong field type instead of a scope-mismatch ValueError, and a
    # legal-but-foreign scope stays a ValueError: cross-repo/company
    # records can never close.
    _check_id("%s %r repo_id" % (kind, record_id), record["repo_id"])
    _check_id("%s %r company_id" % (kind, record_id), record["company_id"])
    if record["repo_id"] != repo_id:
        raise ValueError(
            "%s %r belongs to repo %r, not the expected repo %r"
            % (kind, record_id, record["repo_id"], repo_id))
    if record["company_id"] != company_id:
        raise ValueError(
            "%s %r belongs to company %r, not the expected company %r"
            % (kind, record_id, record["company_id"], company_id))


def _validate_input_candidate(record, repo_id, company_id):
    if not isinstance(record, dict):
        raise TypeError(
            "input_candidate record must be a dict, got %s"
            % type(record).__name__)
    _require_str_keys(record, "input_candidate record")
    missing = _INPUT_REQUIRED_FIELDS - set(record)
    if missing:
        raise ValueError(
            "input_candidate record must carry the closure fields "
            "(missing: %s)" % (sorted(missing),))
    _check_id("candidate_uid", record["candidate_uid"])
    candidate_uid = record["candidate_uid"]
    _check_revision(
        "input_candidate %r candidate_revision" % (candidate_uid,),
        record["candidate_revision"])
    _check_digest(
        "input_candidate %r payload_digest" % (candidate_uid,),
        record["payload_digest"])
    _check_scope(
        "input_candidate", candidate_uid, record, repo_id, company_id)


def _validate_operation(operation, inputs_by_identity, repo_id, company_id):
    if not isinstance(operation, dict):
        raise TypeError(
            "operation record must be a dict, got %s"
            % type(operation).__name__)
    _require_str_keys(operation, "operation record")
    missing = _OPERATION_REQUIRED_FIELDS - set(operation)
    if missing:
        raise ValueError(
            "operation record must carry the closure fields (missing: %s)"
            % (sorted(missing),))
    _check_id("operation_id", operation["operation_id"])
    operation_id = operation["operation_id"]
    _check_scope("operation", operation_id, operation, repo_id, company_id)
    candidate_refs = operation["candidate_refs"]
    if not isinstance(candidate_refs, list):
        raise TypeError(
            "operation %r candidate_refs must be a list, got %s"
            % (operation_id, type(candidate_refs).__name__))
    seen = set()
    for reference in candidate_refs:
        _validate_reference(operation_id, reference)
        identity = (reference["candidate_uid"],
                    reference["candidate_revision"])
        if identity in seen:
            raise ValueError(
                "operation %r references candidate identity (%r, %r) more "
                "than once" % ((operation_id,) + identity))
        seen.add(identity)
        _check_reference_resolves(operation_id, reference, identity,
                                  inputs_by_identity)


def _validate_reference(operation_id, reference):
    label = "operation %r candidate_refs entry" % (operation_id,)
    if not isinstance(reference, dict):
        raise TypeError(
            "%s must be a dict, got %s" % (label, type(reference).__name__))
    _require_str_keys(reference, label)
    if set(reference) != _REFERENCE_KEYS:
        raise ValueError(
            "%s must be a typed reference with exactly the keys %s, got %s"
            % (label, sorted(_REFERENCE_KEYS), sorted(set(reference))))
    _check_id("%s candidate_uid" % (label,), reference["candidate_uid"])
    _check_revision(
        "%s candidate_revision" % (label,), reference["candidate_revision"])
    _check_digest(
        "%s payload_digest" % (label,), reference["payload_digest"])


def _check_reference_resolves(operation_id, reference, identity,
                              inputs_by_identity):
    # The identity uniqueness enforced over input_candidates above is
    # what makes this lookup unambiguous: the target either exists
    # exactly once or the reference dangles.
    source = inputs_by_identity.get(identity)
    if source is None:
        raise ValueError(
            "operation %r references candidate identity (%r, %r), which is "
            "not among the input_candidates of this commit (dangling "
            "reference)" % ((operation_id,) + identity))
    if reference["payload_digest"] != source["payload_digest"]:
        raise ValueError(
            "operation %r references candidate identity (%r, %r) with "
            "payload_digest %r, but the input_candidate carries %r "
            "(digest mismatch)"
            % ((operation_id,) + identity + (reference["payload_digest"],
                                             source["payload_digest"])))


def _validate_consumed(consumed, repo_id, company_id):
    if not isinstance(consumed, dict):
        raise TypeError(
            "consumed_candidate record must be a dict, got %s"
            % type(consumed).__name__)
    _require_str_keys(consumed, "consumed_candidate record")
    missing = _CONSUMED_REQUIRED_FIELDS - set(consumed)
    if missing:
        raise ValueError(
            "consumed_candidate record must carry the closure fields "
            "(missing: %s)" % (sorted(missing),))
    _check_id("consumed_candidate candidate_uid", consumed["candidate_uid"])
    candidate_uid = consumed["candidate_uid"]
    _check_revision(
        "consumed_candidate %r candidate_revision" % (candidate_uid,),
        consumed["candidate_revision"])
    _check_digest(
        "consumed_candidate %r payload_digest" % (candidate_uid,),
        consumed["payload_digest"])
    _check_id(
        "consumed_candidate %r operation_id" % (candidate_uid,),
        consumed["operation_id"])
    _check_scope("consumed_candidate", candidate_uid, consumed, repo_id,
                 company_id)


def _check_consumed_closure(consumed, inputs_by_identity, operations_by_id):
    candidate_uid = consumed["candidate_uid"]
    revision = consumed["candidate_revision"]
    digest = consumed["payload_digest"]
    operation_id = consumed["operation_id"]
    source = inputs_by_identity.get((candidate_uid, revision))
    if source is None:
        raise ValueError(
            "consumed_candidate (%r, %r) has no matching input_candidate "
            "in this commit (dangling consumption)"
            % (candidate_uid, revision))
    if digest != source["payload_digest"]:
        raise ValueError(
            "consumed_candidate (%r, %r) carries payload_digest %r, but the "
            "input_candidate carries %r (digest mismatch)"
            % (candidate_uid, revision, digest, source["payload_digest"]))
    operation = operations_by_id.get(operation_id)
    if operation is None:
        raise ValueError(
            "consumed_candidate (%r, %r) names operation %r, which is not "
            "among the operations of this commit (dangling reference)"
            % (candidate_uid, revision, operation_id))
    for reference in operation["candidate_refs"]:
        if (reference["candidate_uid"] == candidate_uid
                and reference["candidate_revision"] == revision
                and reference["payload_digest"] == digest):
            return
    raise ValueError(
        "consumed_candidate (%r, %r) names operation %r, but that operation "
        "does not reference exactly this candidate revision and digest; a "
        "terminal consumption must close over the consuming operation's "
        "candidate_refs" % (candidate_uid, revision, operation_id))
