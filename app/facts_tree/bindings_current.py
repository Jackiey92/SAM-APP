"""Pure fail-closed validator for the bindings/current.json v1 record.

Approved design 97-业务记录与投影重建设计.md sections 10.2-10.6
(bindings_current wire format v1, plan A adopted by 范范 on 2026-10-24).
This module implements only the first code card of section 10.6: pure
functional structural and reference-closure validation of the v1 record
against caller-supplied synthetic in-memory data. It is not connected to
the Commit Broker, SQLite, migrations, real company facts, any feature
gate or any business entry point.

Validation entry points (all pure, all fail closed, none mutate inputs):

- ``validate_bindings_current(payload)`` checks the record in isolation:
  exactly the eleven v1 fields, exact schema/format versions, the fixed
  record_type, legal opaque IDs, plain positive int business version and
  binding revision, a 64-char lowercase hex digest and a well-formed
  typed event reference.
- ``validate_manifest_identity(current, manifest)`` closes the taxonomy
  path identity and digest (section 10.3): the target manifest record
  must carry the full git_taxonomy envelope — an exact built-in int
  ``schema_version`` equal to ``git_taxonomy.SCHEMA_VERSION``, an exact
  built-in int ``format_version`` equal to ``git_format.FORMAT_VERSION``
  and its own ``manifest_sha256`` present and equal to the recomputed
  content digest (a wrong digest type is a TypeError, a disagreement a
  ValueError) — its body ``pack`` and business ``version`` must equal
  the record's ``template_pack`` and ``template_version``, and the
  record's ``manifest_sha256`` must equal
  ``template_packs.manifest_digest`` of the manifest content with the
  outer ``schema_version``, ``format_version`` and ``manifest_sha256``
  wrapper fields removed, so the digest never includes itself.
  ``template_pack`` and ``template_version`` are only ever read from the
  manifest body, never reverse-derived from ``taxonomy_path_id``; the
  path ID stays an opaque host-issued segment validated through
  ``git_format.encode_id``.
- ``validate_binding_event(event, *, repo_id, company_id)`` checks one
  binding event (sections 10.1 and 10.4): the record wrapper every new
  business record must carry — an exact built-in positive int
  ``schema_version`` equal to ``EVENT_SCHEMA_VERSION``, an exact built-in
  int ``format_version`` equal to ``git_format.FORMAT_VERSION`` and the
  fixed ``record_type`` string ``"event"`` — a legal host-issued
  ``event_id``, one of
  the two approved event types, the event's own repo/company fields
  checked as legal IDs (a wrong-typed scope is a TypeError, a
  legal-but-foreign scope a ValueError) against the expected scope, full
  before/after binding snapshots without ``change_event_ref`` (the event
  never forms a digest cycle with the current pointer), an empty before
  and revision 1 for ``binding_initialized``, an exactly-plus-one
  revision for ``binding_changed``, the logical bindings_current typed
  reference inside ``changed_refs`` matched strictly (its revision must
  be a plain built-in int, so True, 1.0 or Decimal(1) never pass for
  revision 1 through Python's loose numeric equality), and the presence
  of the generic event fields named by section 10.4.
- ``validate_binding_history(current, events, *, repo_id, company_id)``
  closes the whole revision chain: unique event IDs, exactly one event
  per revision, revisions continuous from 1 with no gap or backward
  step, each ``before`` snapshot equal to the previous event's ``after``
  snapshot, ``current`` field-by-field equal to the latest ``after``
  snapshot, and ``change_event_ref`` pointing at exactly that latest
  event (a reference to any other event, or to a missing one, is a
  dangling reference and rejected).
- ``validate_current_binding(current, *, manifest, events, repo_id,
  company_id)`` runs the full closure: record structure, expected
  repo/company scope, manifest identity and the event history.
- ``derive_manifest_path(taxonomy_path_id)`` and
  ``derive_event_path(event_id)`` are the only sanctioned path helpers;
  both delegate to ``git_format.record_path`` so consumers derive target
  paths from the record's own fields instead of reading model-supplied
  paths, and never store a second, conflicting ``manifest_path``.

Error contract (same convention as ``repository_meta``): TypeError for
wrong types (including bool or int subclasses posing as plain ints) and
ValueError for wrong values, wrong field sets and every inconsistency.
Validation never mutates its inputs; success returns None.

Boundary (explicitly NOT done here, per section 10.6): no file, Git
object, ref, database, network or model IO; no Commit Broker wiring; no
SQLite migration; no real company facts; no feature-gate change. Every
rejection this module can produce is a pure in-memory check, so a host
that validates before committing structurally satisfies "all rejections
happen before any Git ref update". The full-tree closure (scanning every
event of a commit C against Git object identity) and the event record's
inner generic payload — including approval-reference key names, which
the approved sections do not freeze — belong to later cards; this module
therefore requires the section-10.4 generic event fields to be present
but does not re-validate their inner shape, and it does not reject
additional fields on event records. Audited against the approved copy on
2026-10-24: section 10.1 requires every new business record to carry
schema_version, format_version, record_type and repo/company scope,
section 3 lists ``event`` as a record type, and section 10.4 binds this
event family, so the wrapper is not optional and is enforced here for
binding events: exact built-in int ``schema_version`` equal to
``EVENT_SCHEMA_VERSION`` (bool and int subclasses rejected), exact
built-in int ``format_version`` equal to ``git_format.FORMAT_VERSION``,
the fixed ``record_type`` string ``"event"`` and the repo/company scope
fields checked as legal IDs and compared against the expected scope. The
approved sections do not freeze the event family's schema-version
*value*; this module makes the documented implementation choice that
``EVENT_SCHEMA_VERSION = 1`` is the initial event schema version for
this first event implementation, mirroring the bindings_current v1
convention of section 10.2, and unknown versions fail closed. Only the
exact inner shape of actor/reason/operation/approval payload stays
inside the deferred generic event schema boundary.
"""
import re

from app.facts_tree import git_format
from app.facts_tree import git_taxonomy
from app.facts_tree import template_packs

SCHEMA_VERSION = 1
RECORD_TYPE = "bindings_current"
EVENT_RECORD_TYPE = "event"

# Implementation choice (documented per review of 2026-10-24): approved
# design section 10.1 requires every new business record to carry
# schema_version/format_version/record_type and repo/company scope, and
# section 3 lists "event" as a record type, but the approved sections do
# not freeze the event family's schema-version value. Since this is the
# first event implementation, its initial schema version is 1, mirroring
# the bindings_current v1 convention of section 10.2; any other value is
# an unknown version and fails closed.
EVENT_SCHEMA_VERSION = 1

INITIALIZED_EVENT_TYPE = "binding_initialized"
CHANGED_EVENT_TYPE = "binding_changed"
BINDING_EVENT_TYPES = frozenset({INITIALIZED_EVENT_TYPE, CHANGED_EVENT_TYPE})

# The logical typed reference under which binding events list the
# current pointer in changed_refs (section 10.4).
CURRENT_STABLE_ID = "current"

# Section 10.2: the record must carry exactly these eleven fields.
_CURRENT_FIELDS = frozenset({
    "schema_version",
    "format_version",
    "record_type",
    "repo_id",
    "company_id",
    "template_pack",
    "template_version",
    "taxonomy_path_id",
    "manifest_sha256",
    "binding_revision",
    "change_event_ref",
})

# A before/after binding snapshot is the full record minus the event
# reference: section 10.4 forbids change_event_ref inside the snapshots
# so event and current can never form a digest cycle.
_SNAPSHOT_FIELDS = _CURRENT_FIELDS - frozenset({"change_event_ref"})

# Section 10.4: change_event_ref uses exactly these two keys; events are
# immutable terminal records, so no revision key is allowed here.
_EVENT_REF_FIELDS = frozenset({"record_type", "stable_id"})

# Section 10.4: the logical binding typed ref inside changed_refs uses
# exactly these three keys.
_CHANGED_REF_FIELDS = frozenset({"record_type", "stable_id", "revision"})

# Fields a binding event must carry per sections 10.1 and 10.4: the
# record wrapper every new business record needs (schema_version,
# format_version, record_type, repo/company scope) plus the section-10.4
# event fields. The five generic fields are required to be present but
# their inner shape belongs to the generic event record schema, which
# the approved sections do not freeze; it is closed by the later
# full-tree card.
_EVENT_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "format_version",
    "record_type",
    "event_id",
    "event_type",
    "repo_id",
    "company_id",
    "before",
    "after",
    "changed_refs",
    "actor",
    "reason",
    "operation_id",
    "base_commit",
    "ordinal",
})

# Section 10.3: the digest input is the manifest content without the Git
# record wrapper versions and without any digest field, so the digest
# never includes itself.
_MANIFEST_WRAPPER_FIELDS = frozenset(
    {"schema_version", "format_version", "manifest_sha256"})
_MANIFEST_BODY_FIELDS = frozenset({"pack", "version"})

# git_taxonomy builds every taxonomy manifest record with the full
# section-10.1 wrapper (schema_version, format_version, body fields and
# its own manifest_sha256), so a manifest record missing any wrapper or
# body field is rejected outright instead of being digested loosely.
_MANIFEST_REQUIRED_FIELDS = _MANIFEST_WRAPPER_FIELDS | _MANIFEST_BODY_FIELDS

_DIGEST_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


def _check_id(name, value):
    # Same read-only ID legality contract as repository_meta and
    # git_taxonomy: encode_id performs the real check (non-empty str
    # within the character and encoded-segment budgets); its encoded
    # output is not needed here, only the validation. This proves the
    # value's structure, not that the host actually issued it.
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


def _check_exact_version(name, value, supported):
    # Only the built-in int type itself passes (type(value) is int), so
    # bool and every int subclass are rejected; a value that is a plain
    # int but not the exact supported version is a ValueError.
    if type(value) is not int:
        raise TypeError("%s must be a plain int" % name)
    if value != supported:
        raise ValueError("unsupported %s: %r" % (name, value))


def _check_positive_int(name, value):
    if type(value) is not int:
        raise TypeError("%s must be a plain int" % name)
    if value <= 0:
        raise ValueError("%s must be a strictly positive int" % name)


def _require_str_keys(payload, label):
    for key in payload:
        if not isinstance(key, str):
            raise TypeError(
                "%s keys must all be str, got %r" % (label, key))


def _require_exact_fields(payload, fields, label):
    present = set(payload)
    if present != fields:
        missing = sorted(fields - present)
        extra = sorted(present - fields)
        raise ValueError(
            "%s must carry exactly the fields %s (missing: %s, extra: %s)"
            % (label, sorted(fields), missing, extra))


def _check_binding_fields(payload):
    # Shared by the current record and by before/after snapshots: every
    # field except change_event_ref, checked with the section-10.2 v1
    # types. Field-set equality is checked by the caller.
    _check_exact_version(
        "schema_version", payload["schema_version"], SCHEMA_VERSION)
    _check_exact_version(
        "format_version", payload["format_version"], git_format.FORMAT_VERSION)
    record_type = payload["record_type"]
    if not isinstance(record_type, str):
        raise TypeError("record_type must be a str")
    if record_type != RECORD_TYPE:
        raise ValueError(
            "record_type must be the fixed string %r" % (RECORD_TYPE,))
    for name in ("repo_id", "company_id", "template_pack", "taxonomy_path_id"):
        _check_id(name, payload[name])
    _check_positive_int("template_version", payload["template_version"])
    _check_positive_int("binding_revision", payload["binding_revision"])
    digest = payload["manifest_sha256"]
    if not isinstance(digest, str):
        raise TypeError("manifest_sha256 must be a str")
    if not _DIGEST_PATTERN.match(digest):
        raise ValueError(
            "manifest_sha256 must be 64 lowercase hexadecimal characters")


def _check_snapshot(snapshot, label):
    if not isinstance(snapshot, dict):
        raise TypeError("%s must be a dict" % label)
    _require_str_keys(snapshot, label)
    _require_exact_fields(snapshot, _SNAPSHOT_FIELDS, label)
    _check_binding_fields(snapshot)


def _check_event_ref(ref):
    # Section 10.4: the typed reference has exactly record_type and
    # stable_id; stable_id equals the target event's event_id and carries
    # no revision and no arbitrary path.
    if not isinstance(ref, dict):
        raise TypeError("change_event_ref must be a typed reference dict")
    _require_str_keys(ref, "change_event_ref")
    _require_exact_fields(ref, _EVENT_REF_FIELDS, "change_event_ref")
    ref_type = ref["record_type"]
    if not isinstance(ref_type, str):
        raise TypeError("change_event_ref.record_type must be a str")
    if ref_type != EVENT_RECORD_TYPE:
        raise ValueError(
            "change_event_ref.record_type must be the fixed string %r"
            % (EVENT_RECORD_TYPE,))
    _check_id("change_event_ref.stable_id", ref["stable_id"])


def _check_logical_changed_ref(event_id, changed_refs, expected_revision):
    # Section 10.4: changed_refs must carry the logical binding typed
    # ref {record_type: bindings_current, stable_id: current, revision:
    # after.binding_revision}. The revision match must never use
    # Python's loose numeric equality (True == 1, 1.0 == 1 and
    # Decimal(1) == 1 would otherwise all pass for revision 1; section
    # 2.1 forbids bools posing as ints), so an entry claiming the
    # logical reference identity is checked strictly: exactly the three
    # keys, a plain built-in int revision and exact equality with the
    # after snapshot's binding_revision. Unrelated additional typed refs
    # are skipped, not rejected, but every entry claiming the logical
    # identity is checked, so a well-formed entry never masks a
    # malformed duplicate.
    found = False
    for item in changed_refs:
        if not isinstance(item, dict):
            continue
        if item.get("record_type") != RECORD_TYPE:
            continue
        if item.get("stable_id") != CURRENT_STABLE_ID:
            continue
        if set(item) != _CHANGED_REF_FIELDS:
            raise ValueError(
                "changed_refs entry of binding event %r claiming the "
                "logical binding reference must carry exactly the keys %s"
                % (event_id, sorted(_CHANGED_REF_FIELDS)))
        revision = item["revision"]
        if type(revision) is not int:
            raise TypeError(
                "changed_refs logical binding reference of binding event "
                "%r must carry a plain int revision" % (event_id,))
        if revision != expected_revision:
            raise ValueError(
                "changed_refs logical binding reference of binding event "
                "%r must carry revision %r, got %r"
                % (event_id, expected_revision, revision))
        found = True
    if not found:
        raise ValueError(
            "changed_refs of binding event %r must carry the logical typed "
            "reference %r" % (event_id, {
                "record_type": RECORD_TYPE,
                "stable_id": CURRENT_STABLE_ID,
                "revision": expected_revision,
            }))


def derive_manifest_path(taxonomy_path_id):
    """Derive the target taxonomy manifest path from the opaque path ID.

    Section 10.3: consumers call this single canonical path helper with
    the record's ``taxonomy_path_id``; they never read a model-supplied
    path and never store a second, conflicting ``manifest_path``.
    """
    _check_id("taxonomy_path_id", taxonomy_path_id)
    return git_format.record_path(
        "taxonomy_manifest", pack_version=taxonomy_path_id)


def derive_event_path(event_id):
    """Derive the ``events/<event_id>.json`` path from a host-issued ID.

    Section 10.4: the host derives the event path from the typed
    reference's stable_id; HEAD/current/latest are never substitutes.
    """
    _check_id("event_id", event_id)
    return git_format.record_path("event", event_id=event_id)


def validate_bindings_current(payload):
    """Check that payload is exactly a v1 bindings_current record.

    The payload must be a dict carrying exactly the eleven section-10.2
    fields with the v1 types: plain int schema_version equal to
    SCHEMA_VERSION, plain int format_version equal to the current
    git_format serializer version (bool and int subclasses are rejected
    for both even though bool subclasses int; the two versions are
    independent fields), the fixed record_type string, four legal
    opaque IDs, a plain positive int template_version and
    binding_revision, a 64-char lowercase hex manifest_sha256 and a
    two-key typed event reference. Returns None on success; raises
    TypeError for wrong types and ValueError for wrong values or a wrong
    field set. Nothing here proves any ID was actually host-issued.
    """
    if not isinstance(payload, dict):
        raise TypeError("bindings_current record must be a dict")
    _require_str_keys(payload, "bindings_current record")
    _require_exact_fields(payload, _CURRENT_FIELDS, "bindings_current record")
    _check_binding_fields(payload)
    _check_event_ref(payload["change_event_ref"])
    return None


def validate_manifest_identity(current, manifest):
    """Close taxonomy path identity and the manifest digest (10.3).

    ``manifest`` is the taxonomy manifest record the host read at
    ``derive_manifest_path(current["taxonomy_path_id"])`` in the fixed
    target commit; this pure validator never reads it itself. Checks the
    full git_taxonomy envelope first: the record must carry every
    wrapper and body field, its ``schema_version`` must be an exact
    built-in int equal to ``git_taxonomy.SCHEMA_VERSION`` and its
    ``format_version`` an exact built-in int equal to
    ``git_format.FORMAT_VERSION`` (bool and int subclasses are
    TypeErrors, wrong plain-int values ValueErrors), and its own
    ``manifest_sha256`` must be a str equal to the recomputed content
    digest (a wrong type is a TypeError, a disagreement a ValueError).
    Identity checks: the manifest body's ``pack`` equals
    ``current["template_pack"]``, its business ``version`` equals
    ``current["template_version"]``, and
    ``template_packs.manifest_digest`` of the manifest content with the
    outer schema_version/format_version/manifest_sha256 wrapper fields
    removed equals ``current["manifest_sha256"]``. The digest input
    never includes any digest or wrapper field, so it cannot
    self-reference, and pack/version are never reverse-derived from the
    opaque ``taxonomy_path_id``.
    """
    validate_bindings_current(current)
    if not isinstance(manifest, dict):
        raise TypeError("taxonomy manifest record must be a dict")
    _require_str_keys(manifest, "taxonomy manifest record")
    missing = _MANIFEST_REQUIRED_FIELDS - set(manifest)
    if missing:
        raise ValueError(
            "taxonomy manifest record carries no %s"
            % ", ".join(sorted(missing)))
    # Section 10.1 wrapper: git_taxonomy only ever builds manifest
    # records at exactly these versions; _check_exact_version rejects
    # bool and int subclasses as TypeErrors and any other plain-int
    # value as a ValueError, so an unknown wrapper version fails closed.
    _check_exact_version(
        "taxonomy manifest schema_version", manifest["schema_version"],
        git_taxonomy.SCHEMA_VERSION)
    _check_exact_version(
        "taxonomy manifest format_version", manifest["format_version"],
        git_format.FORMAT_VERSION)
    own_digest = manifest["manifest_sha256"]
    if not isinstance(own_digest, str):
        raise TypeError(
            "taxonomy manifest record's own manifest_sha256 must be a str")
    pack = manifest["pack"]
    if not isinstance(pack, str):
        raise TypeError("taxonomy manifest pack must be a str")
    if pack != current["template_pack"]:
        raise ValueError(
            "template_pack mismatch: record carries %r but the target "
            "taxonomy manifest pack is %r"
            % (current["template_pack"], pack))
    version = manifest["version"]
    if type(version) is not int:
        raise TypeError("taxonomy manifest business version must be a plain int")
    if version != current["template_version"]:
        raise ValueError(
            "template_version mismatch: record carries %r but the target "
            "taxonomy manifest business version is %r"
            % (current["template_version"], version))
    content = {
        key: value for key, value in manifest.items()
        if key not in _MANIFEST_WRAPPER_FIELDS
    }
    try:
        digest = template_packs.manifest_digest(content)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "taxonomy manifest content is not digestible: %s" % (exc,)) from None
    if digest != current["manifest_sha256"]:
        raise ValueError(
            "manifest_sha256 mismatch: record carries %r but the target "
            "taxonomy manifest content digests to %r"
            % (current["manifest_sha256"], digest))
    if own_digest != digest:
        raise ValueError(
            "taxonomy manifest record's own manifest_sha256 disagrees "
            "with its content digest")
    return None


def validate_binding_event(event, *, repo_id, company_id):
    """Check one binding event against the section-10.1/10.4 rules.

    ``repo_id`` and ``company_id`` are the caller's expected scope; the
    event's own repo/company fields are first checked as legal IDs (a
    wrong-typed scope raises TypeError, not a scope-mismatch ValueError)
    and any legal-but-foreign scope is rejected (cross-scope references
    can never bind). The event must carry the section-10.1 record
    wrapper every new business record needs: a plain built-in positive
    int ``schema_version`` equal to EVENT_SCHEMA_VERSION (1 for this
    first event implementation; bool, int subclasses, zero, negatives
    and unknown versions all fail closed), a plain built-in int
    ``format_version`` equal to git_format.FORMAT_VERSION and the fixed
    ``record_type`` string ``"event"``. It must also carry every
    section-10.4 field: a
    legal opaque ``event_id``, an ``event_type`` of exactly
    ``binding_initialized`` or ``binding_changed``, full before/after
    binding snapshots whose field set excludes ``change_event_ref``, and
    the generic fields ``actor``, ``reason``, ``operation_id``,
    ``base_commit``, ``ordinal`` and ``changed_refs``. The generic
    fields are required to be present but their inner shape belongs to
    the not-yet-frozen generic event schema and is not re-validated
    here; additional event fields are likewise left to the full-tree
    closure card. ``binding_initialized`` requires an empty before
    (None or {}) and establishes binding_revision 1;
    ``binding_changed`` requires a full before snapshot and an after
    revision of exactly before + 1. ``changed_refs`` must be a list
    containing the logical typed reference {record_type:
    bindings_current, stable_id: current, revision: after revision};
    every entry claiming that identity is matched strictly — exactly
    those three keys and a plain built-in int revision, so bool, float,
    Decimal and int subclasses are TypeErrors and never pass through
    Python's loose numeric equality — while unrelated additional typed
    refs are accepted.
    """
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    if not isinstance(event, dict):
        raise TypeError("binding event must be a dict")
    _require_str_keys(event, "binding event")
    missing = _EVENT_REQUIRED_FIELDS - set(event)
    if missing:
        raise ValueError(
            "binding event must carry the section-10.1/10.4 fields "
            "(missing: %s)" % (sorted(missing),))
    # Section 10.1 wrapper: every new business record carries exact
    # schema_version/format_version/record_type. EVENT_SCHEMA_VERSION
    # (1) is this module's documented implementation choice for the
    # first event implementation; _check_exact_version rejects bool and
    # int subclasses as TypeErrors and zero, negatives and unknown
    # versions as ValueErrors.
    _check_exact_version(
        "event schema_version", event["schema_version"], EVENT_SCHEMA_VERSION)
    _check_exact_version(
        "event format_version", event["format_version"],
        git_format.FORMAT_VERSION)
    record_type = event["record_type"]
    if not isinstance(record_type, str):
        raise TypeError("event record_type must be a str")
    if record_type != EVENT_RECORD_TYPE:
        raise ValueError(
            "event record_type must be the fixed string %r"
            % (EVENT_RECORD_TYPE,))
    _check_id("event_id", event["event_id"])
    event_type = event["event_type"]
    if not isinstance(event_type, str):
        raise TypeError("event_type must be a str")
    if event_type not in BINDING_EVENT_TYPES:
        raise ValueError(
            "event_type must be one of %s, got %r"
            % (sorted(BINDING_EVENT_TYPES), event_type))
    # The event's own scope fields are checked as legal IDs before any
    # comparison, so a wrong-typed scope (repo_id=123, say) is the
    # documented TypeError for a wrong field type instead of a
    # scope-mismatch ValueError, and a legal-but-foreign scope stays a
    # ValueError.
    _check_id("binding event repo_id", event["repo_id"])
    _check_id("binding event company_id", event["company_id"])
    if event["repo_id"] != repo_id:
        raise ValueError(
            "binding event %r belongs to repo %r, not the expected repo %r"
            % (event["event_id"], event["repo_id"], repo_id))
    if event["company_id"] != company_id:
        raise ValueError(
            "binding event %r belongs to company %r, not the expected "
            "company %r" % (event["event_id"], event["company_id"], company_id))
    after = event["after"]
    _check_snapshot(after, "binding event after snapshot")
    before = event["before"]
    if event_type == INITIALIZED_EVENT_TYPE:
        # Section 10.4: revision 1 uses binding_initialized with an
        # empty before. The approved text says "empty" without choosing
        # between JSON null and an empty object, so both forms pass and
        # everything else fails.
        if before is not None and before != {}:
            raise ValueError(
                "binding_initialized event %r must carry an empty before "
                "snapshot" % (event["event_id"],))
        if after["binding_revision"] != 1:
            raise ValueError(
                "binding_initialized event %r must establish "
                "binding_revision 1, got %r"
                % (event["event_id"], after["binding_revision"]))
    else:
        _check_snapshot(before, "binding event before snapshot")
        if after["binding_revision"] != before["binding_revision"] + 1:
            raise ValueError(
                "binding_changed event %r must increment binding_revision "
                "by exactly 1 (before %r, after %r)"
                % (event["event_id"], before["binding_revision"],
                   after["binding_revision"]))
    changed_refs = event["changed_refs"]
    if not isinstance(changed_refs, list):
        raise TypeError("changed_refs must be a list")
    _check_logical_changed_ref(
        event["event_id"], changed_refs, after["binding_revision"])
    return None


def validate_binding_history(current, events, *, repo_id, company_id):
    """Close the continuous binding revision chain (sections 10.4-10.5).

    ``events`` is the list or tuple of every binding event the host read
    from the fixed target commit. Checks, all fail closed: every event
    passes ``validate_binding_event`` against the expected scope; event
    IDs are unique; each binding_revision has exactly one event;
    revisions are continuous from 1 with no gap and no backward step;
    revision 1 is the ``binding_initialized`` event with an empty
    before; every later ``binding_changed`` event's before snapshot is
    field-by-field equal to the previous event's after snapshot; the
    current record's snapshot fields are field-by-field equal to the
    latest after snapshot; and ``current["change_event_ref"]`` points at
    exactly that latest event, so a reference to an older, foreign or
    missing event is rejected as dangling or stale.
    """
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    validate_bindings_current(current)
    if current["repo_id"] != repo_id:
        raise ValueError(
            "bindings_current record belongs to repo %r, not the expected "
            "repo %r" % (current["repo_id"], repo_id))
    if current["company_id"] != company_id:
        raise ValueError(
            "bindings_current record belongs to company %r, not the "
            "expected company %r" % (current["company_id"], company_id))
    if not isinstance(events, (list, tuple)):
        raise TypeError("binding events must be a list or tuple of records")
    by_revision = {}
    seen_event_ids = set()
    for event in events:
        validate_binding_event(event, repo_id=repo_id, company_id=company_id)
        event_id = event["event_id"]
        if event_id in seen_event_ids:
            raise ValueError("duplicate binding event id %r" % (event_id,))
        seen_event_ids.add(event_id)
        revision = event["after"]["binding_revision"]
        if revision in by_revision:
            raise ValueError(
                "binding_revision %r is claimed by more than one event"
                % (revision,))
        by_revision[revision] = event
    if not by_revision:
        raise ValueError(
            "binding history carries no events; the change_event_ref of "
            "the current record is dangling")
    revisions = sorted(by_revision)
    if revisions != list(range(1, len(revisions) + 1)):
        raise ValueError(
            "binding revisions must be continuous from 1 with no gap and "
            "no backward step, got %r" % (revisions,))
    previous_after = None
    for revision in revisions:
        event = by_revision[revision]
        if revision == 1:
            if event["event_type"] != INITIALIZED_EVENT_TYPE:
                raise ValueError(
                    "binding_revision 1 must be established by a %s event"
                    % (INITIALIZED_EVENT_TYPE,))
        else:
            if event["event_type"] != CHANGED_EVENT_TYPE:
                raise ValueError(
                    "binding_revision %r must be established by a %s event"
                    % (revision, CHANGED_EVENT_TYPE))
            if event["before"] != previous_after:
                raise ValueError(
                    "binding event %r is not continuous: its before "
                    "snapshot differs from the after snapshot of binding "
                    "revision %r" % (event["event_id"], revision - 1))
        previous_after = event["after"]
    latest = by_revision[revisions[-1]]
    stable_id = current["change_event_ref"]["stable_id"]
    if stable_id != latest["event_id"]:
        raise ValueError(
            "change_event_ref must point at the latest binding revision "
            "event %r, got %r (dangling or stale reference)"
            % (latest["event_id"], stable_id))
    for field in sorted(_SNAPSHOT_FIELDS):
        if current[field] != latest["after"][field]:
            raise ValueError(
                "bindings_current field %s disagrees with the latest "
                "event after snapshot" % (field,))
    return None


def validate_current_binding(current, *, manifest, events, repo_id,
                             company_id):
    """Run the full v1 closure for one candidate current binding.

    ``repo_id`` and ``company_id`` are the caller's expected repository
    scope (the same values a ``repository_meta`` validation would use);
    ``manifest`` is the taxonomy manifest record at
    ``derive_manifest_path(current["taxonomy_path_id"])`` and ``events``
    every binding event of the fixed target commit, both read by the
    host. Runs, in order: record structure, expected scope equality,
    manifest identity and digest, and the continuous event history.
    Returns None on success; every inconsistency raises before the
    caller could update any Git ref.
    """
    _check_id("expected repo_id", repo_id)
    _check_id("expected company_id", company_id)
    validate_bindings_current(current)
    if current["repo_id"] != repo_id:
        raise ValueError(
            "bindings_current record belongs to repo %r, not the expected "
            "repo %r" % (current["repo_id"], repo_id))
    if current["company_id"] != company_id:
        raise ValueError(
            "bindings_current record belongs to company %r, not the "
            "expected company %r" % (current["company_id"], company_id))
    validate_manifest_identity(current, manifest)
    validate_binding_history(current, events, repo_id=repo_id,
                             company_id=company_id)
    return None
