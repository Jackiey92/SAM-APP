"""Pure fail-closed operation/event cross-record closure for one commit.

Approved design 97-业务记录与投影重建设计.md (DEC-030 / CHG-065)
sections 2.1, 3 (the ``operation`` and ``event`` record rows), 4 item 5
(操作闭合) and 5 (事件契约). The host reads every ``operation`` and
``event`` payload from the same fixed commit C and hands both
collections to this validator; it checks that the cross-record
references between the two record families close exactly inside that
one C, and nothing beyond that closure.

Public entry point (pure, fail closed, never mutates its inputs):

    validate_operation_event_closure(
        operations, events, *, repo_id, company_id, object_format)

``operations`` and ``events`` are iterables of payload dicts (a bare
str/bytes is a caller TypeError, not an iterable of one-character
"records"). ``repo_id`` and ``company_id`` are the caller's expected
scope, checked as legal opaque IDs through ``git_format.encode_id``;
``object_format`` must be the str ``"sha1"`` or ``"sha256"`` and fixes
the full-length hex OID shape of every ``base_commit``. Returns None on
success.

What is checked
---------------

- Every operation and event payload is a dict with all-str keys and
  carries the closure fields: ``operation_id``, ``repo_id``,
  ``company_id``, ``base_commit`` and ``event_ids`` on an operation;
  ``event_id``, ``operation_id``, ``repo_id``, ``company_id``,
  ``base_commit`` and ``ordinal`` on an event. A missing field is a
  ValueError.
- ``operation_id``, ``event_id``, ``repo_id``, ``company_id`` and every
  ``event_ids`` entry are legal opaque host-issued IDs under the same
  read-only ``git_format.encode_id`` contract as ``repository_meta``
  and ``bindings_current`` (this proves the ID's shape, not that the
  host actually issued it), and each record's repo/company scope is
  checked as legal IDs first — a wrong-typed scope is a TypeError —
  then compared against the caller's expected scope, so a
  legal-but-foreign scope can never close (ValueError).
- ``base_commit`` is a full-length lowercase hex OID for the given
  ``object_format`` (40 characters for sha1, 64 for sha256). The
  all-zero OID of the format is deliberately accepted: it expresses the
  genesis boundary of an operation that has no prior commit to build
  on. Only ``"sha1"`` and ``"sha256"`` are supported formats.
- ``operation_id`` is unique across the given operations and
  ``event_id`` unique across the given events (ValueError on a
  duplicate). ``event_ids`` is a list (TypeError otherwise) of legal
  IDs with no repetition inside one operation.
- The listing closes exactly in both directions: every ID an operation
  lists must be an event of this same input whose ``operation_id`` is
  that operation (a listed ID with no such event — dangling, or owned
  by a different operation — is a ValueError), and every event must be
  listed by the exactly one operation it references (an unlisted
  "orphan" event and an ``operation_id`` pointing at no provided
  operation are both ValueErrors). ``event_ids=[]`` with no events for
  that operation is exact closure and passes; the empty pair of input
  collections passes.
- An operation and each of its events agree on ``repo_id``,
  ``company_id`` (both transitively through the expected-scope checks)
  and ``base_commit`` (pairwise equality; a disagreement is a
  ValueError).
- ``ordinal`` is an exact built-in non-negative int — ``type(value) is
  int``, so bool and every int subclass are TypeErrors and never pass
  through Python's loose numeric equality — and within one operation
  the ordinals are pairwise distinct and consecutive with no gap. The
  starting value is *not* prescribed (any [k, k+1, ...] run with k >= 0
  passes); order inside an operation is defined by ``ordinal`` alone,
  never by UUID, server time or the input order of the events list or
  of the ``event_ids`` listing.
- An event payload carrying ``accepted_commit`` is rejected
  (ValueError): per section 5 an event never embeds its own accepted
  commit; the accepting commit is determined by the immutable linear
  history position of the record, not by a payload field.

Problems are reported in a fixed order: the caller arguments first
(object_format, then expected repo/company), then each operation in
input order, then each event in input order (own fields, duplicate ID,
dangling operation reference), then the cross-record closure scanned in
sorted ``operation_id`` order and, inside one operation, sorted
``event_id`` / ``ordinal`` order, so the reported problem never depends
on dict or set iteration luck.

Error contract (same convention as ``repository_meta``,
``bindings_current`` and ``git_tree_validation``): TypeError for wrong
types (including bool or int subclasses posing as plain ints) and
ValueError for wrong values, illegal IDs, missing fields and every
closure violation.

Boundary (explicitly NOT done here): this validator checks only the
operation/event reference closure named above. It does not validate the
full operation or event record schema (schema_version/format_version/
record_type wrappers, request/effect digests, candidate manifests,
dispositions, actor/reason/approval internals), the meaning of any
event_type against changed_refs, paths or Git object identity, commit
history, or any other section-4 closure family; those belong to the
record-schema and event-specific validators and the later full-tree
card. Additional payload fields beyond the closure set are therefore
accepted (except the forbidden ``accepted_commit`` on events). The
module is pure in-memory: no file, Git object, ref, database, network
or model IO, no Commit Broker wiring, no feature-gate change; every
rejection happens before the caller could update any Git ref.
"""
from app.facts_tree import git_format

_OID_HEX_LENGTHS = {"sha1": 40, "sha256": 64}
_HEX_DIGITS = frozenset("0123456789abcdef")

# The fields this closure needs on each record. This is deliberately not
# a full record schema: unknown additional fields stay the business of
# the record-schema validators, while a *missing* closure field makes
# the cross-record checks below impossible and is rejected.
_OPERATION_REQUIRED_FIELDS = frozenset({
    "operation_id",
    "repo_id",
    "company_id",
    "base_commit",
    "event_ids",
})
_EVENT_REQUIRED_FIELDS = frozenset({
    "event_id",
    "operation_id",
    "repo_id",
    "company_id",
    "base_commit",
    "ordinal",
})


def validate_operation_event_closure(operations, events, *, repo_id,
                                     company_id, object_format):
    """Close operation/event cross-references inside one fixed commit.

    See the module docstring for the full contract. Returns None on
    success; raises TypeError for wrong types and ValueError for illegal
    values and every closure violation. Never mutates its inputs.
    """
    oid_length = _check_object_format(object_format)
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    operation_records = _materialize("operations", operations)
    event_records = _materialize("events", events)

    operations_by_id = {}
    for operation in operation_records:
        _validate_operation(operation, oid_length, repo_id, company_id)
        operation_id = operation["operation_id"]
        if operation_id in operations_by_id:
            raise ValueError(
                "duplicate operation id %r in the same commit"
                % (operation_id,))
        operations_by_id[operation_id] = operation

    events_by_operation = {}
    seen_event_ids = set()
    for event in event_records:
        _validate_event(event, oid_length, repo_id, company_id)
        event_id = event["event_id"]
        if event_id in seen_event_ids:
            raise ValueError(
                "duplicate event id %r in the same commit" % (event_id,))
        seen_event_ids.add(event_id)
        operation_id = event["operation_id"]
        if operation_id not in operations_by_id:
            raise ValueError(
                "event %r references operation %r, which is not among the "
                "operations of this commit (dangling reference)"
                % (event_id, operation_id))
        events_by_operation.setdefault(operation_id, []).append(event)

    for operation_id in sorted(operations_by_id):
        operation = operations_by_id[operation_id]
        owned = events_by_operation.get(operation_id, [])
        _check_listing_closes(operation_id, operation["event_ids"], owned)
        base_commit = operation["base_commit"]
        for event in sorted(owned, key=lambda item: item["event_id"]):
            if event["base_commit"] != base_commit:
                raise ValueError(
                    "event %r carries base_commit %r but its operation %r "
                    "carries base_commit %r; an operation and its events "
                    "must agree" % (event["event_id"], event["base_commit"],
                                     operation_id, base_commit))
        _check_ordinals(operation_id, owned)
    return None


def _check_object_format(object_format):
    if not isinstance(object_format, str):
        raise TypeError(
            "object_format must be a str, got %s"
            % type(object_format).__name__)
    if object_format not in _OID_HEX_LENGTHS:
        raise ValueError(
            "unsupported object_format %r: only 'sha1' and 'sha256' are "
            "supported" % (object_format,))
    return _OID_HEX_LENGTHS[object_format]


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
    # bindings_current and git_taxonomy: encode_id performs the real
    # check (non-empty str within the character and encoded-segment
    # budgets); its encoded output is not needed here.
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


def _check_commit(name, value, oid_length):
    # A full-length lowercase hex OID for the repository object format.
    # The all-zero OID is deliberately legal: it expresses the genesis
    # boundary, and nothing here may require a non-zero base.
    if not isinstance(value, str):
        raise TypeError(
            "%s must be a str, got %s" % (name, type(value).__name__))
    if len(value) != oid_length:
        raise ValueError(
            "%s must be exactly %d hex characters, got %d"
            % (name, oid_length, len(value)))
    if any(char not in _HEX_DIGITS for char in value):
        raise ValueError(
            "%s must be lowercase hexadecimal, got %r" % (name, value))


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


def _validate_operation(operation, oid_length, repo_id, company_id):
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
    _check_commit(
        "operation %r base_commit" % (operation_id,),
        operation["base_commit"], oid_length)
    event_ids = operation["event_ids"]
    if not isinstance(event_ids, list):
        raise TypeError(
            "operation %r event_ids must be a list, got %s"
            % (operation_id, type(event_ids).__name__))
    seen = set()
    for event_id in event_ids:
        _check_id(
            "operation %r event_ids entry" % (operation_id,), event_id)
        if event_id in seen:
            raise ValueError(
                "operation %r lists event id %r more than once"
                % (operation_id, event_id))
        seen.add(event_id)


def _validate_event(event, oid_length, repo_id, company_id):
    if not isinstance(event, dict):
        raise TypeError(
            "event record must be a dict, got %s" % type(event).__name__)
    _require_str_keys(event, "event record")
    # Section 5: an event never embeds its own accepted commit; the
    # accepting commit is the immutable linear-history position of the
    # record, so the field is forbidden outright whatever its value.
    if "accepted_commit" in event:
        raise ValueError(
            "event record must not carry accepted_commit: an event payload "
            "never embeds its own accepted commit")
    missing = _EVENT_REQUIRED_FIELDS - set(event)
    if missing:
        raise ValueError(
            "event record must carry the closure fields (missing: %s)"
            % (sorted(missing),))
    _check_id("event_id", event["event_id"])
    event_id = event["event_id"]
    _check_id("event %r operation_id" % (event_id,), event["operation_id"])
    _check_scope("event", event_id, event, repo_id, company_id)
    _check_commit(
        "event %r base_commit" % (event_id,), event["base_commit"],
        oid_length)
    ordinal = event["ordinal"]
    # Exact built-in int only: bool and every int subclass are rejected
    # even though they compare equal to plain ints (section 2.1 forbids
    # bools posing as ints), and float/Decimal never pass.
    if type(ordinal) is not int:
        raise TypeError(
            "event %r ordinal must be a plain built-in int, got %s"
            % (event_id, type(ordinal).__name__))
    if ordinal < 0:
        raise ValueError(
            "event %r ordinal must be non-negative, got %r"
            % (event_id, ordinal))


def _check_listing_closes(operation_id, event_ids, owned):
    listed = set(event_ids)
    owned_ids = {event["event_id"] for event in owned}
    phantom = sorted(listed - owned_ids)
    if phantom:
        raise ValueError(
            "operation %r lists event ids %s with no corresponding event "
            "of this operation in the same commit (dangling or "
            "foreign-owned listing)" % (operation_id, phantom))
    unlisted = sorted(owned_ids - listed)
    if unlisted:
        raise ValueError(
            "operation %r does not list its event ids %s; event_ids must "
            "close exactly over the events referencing this operation, "
            "with no orphan event" % (operation_id, unlisted))


def _check_ordinals(operation_id, owned):
    # Ordinals are unique and consecutive inside one operation; the
    # starting value is not prescribed, so only distinctness and
    # gap-freedom of the sorted run are checked. Input order never
    # decides anything here.
    ordinals = sorted(event["ordinal"] for event in owned)
    seen = set()
    for ordinal in ordinals:
        if ordinal in seen:
            raise ValueError(
                "operation %r has more than one event with ordinal %r; "
                "ordinals must be unique inside one operation"
                % (operation_id, ordinal))
        seen.add(ordinal)
    for previous, current in zip(ordinals, ordinals[1:]):
        if current != previous + 1:
            raise ValueError(
                "event ordinals of operation %r must be consecutive with "
                "no gap (%r is followed by %r)"
                % (operation_id, previous, current))
