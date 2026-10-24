"""Pure fail-closed checks for the bindings_current v1 validator.

Synthetic checks only, per approved design 97 sections 10.2-10.6 (plan
A): in-memory dicts built in this file, plus one integration check that
reads the in-repo ``generic-v2`` template YAML through
template_packs.validate_pack and git_taxonomy.build_taxonomy_records
(the same explicit draft load the existing git_taxonomy tests use). No
real company data, Git objects, refs, databases, models, services or
feature gates are touched; generic-v2 stays unreleased and unbound.

Covered (section 10.6): schema/format version and unknown-field
rejection; bool, 0, negative and non-integer version rejection; the
section-10.1 event record wrapper (schema_version equal to
EVENT_SCHEMA_VERSION 1, format_version equal to git_format.FORMAT_VERSION,
record_type the fixed string "event"; missing, wrong-typed, nonpositive
and unknown variants all rejected); the taxonomy manifest record's full
envelope (schema_version equal to git_taxonomy.SCHEMA_VERSION and
format_version equal to git_format.FORMAT_VERSION as exact built-in
ints, bool and int subclasses rejected, every wrapper field mandatory
and the record's own manifest_sha256 required to equal the recomputed
content digest); taxonomy_path_id never confused with
the business pack/version; the fixed non-recursive manifest digest
input; dangling, cross-repo and
cross-company event reference rejection; wrong-typed event repo/company
scope as TypeError (never a scope-mismatch ValueError); changed_refs
logical-reference revision strictness (bool, float, Decimal and
int-subclass revisions rejected without Python's loose numeric
equality); event-after vs current inconsistency; duplicate, gapped and
backward revisions; non-continuous parent-before/new-after transitions.
Every rejection is a pure in-memory check, so all of them happen before
any Git ref update.
"""
from decimal import Decimal

import pytest

from app.facts_tree import bindings_current
from app.facts_tree import git_format
from app.facts_tree import git_taxonomy
from app.facts_tree import template_packs

REPO_ID = "repo-synthetic-0001"
COMPANY_ID = "co-synthetic-0001"
PACK = "synthetic-pack"
PACK_VERSION_ID = "packver-synthetic-0001"
BUSINESS_VERSION = 3
EVENT_ID = "evt-synthetic-0001"


def _manifest_document():
    return {
        "pack": PACK,
        "version": BUSINESS_VERSION,
        "is_industry_pack": False,
        "trunks": [{"code": "T1", "order": 1, "directory": "t1"}],
        "leaves": [{"code": "L-T1-01", "trunk": "T1", "title": "leaf",
                    "scope": ""}],
    }


def _content_digest():
    return template_packs.manifest_digest(_manifest_document())


def _manifest_record(**overrides):
    record = {
        "schema_version": 1,
        "format_version": git_format.FORMAT_VERSION,
    }
    record.update(_manifest_document())
    record["manifest_sha256"] = _content_digest()
    record.update(overrides)
    return record


def _snapshot(revision, **overrides):
    snapshot = {
        "schema_version": bindings_current.SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "record_type": "bindings_current",
        "repo_id": REPO_ID,
        "company_id": COMPANY_ID,
        "template_pack": PACK,
        "template_version": BUSINESS_VERSION,
        "taxonomy_path_id": PACK_VERSION_ID,
        "manifest_sha256": _content_digest(),
        "binding_revision": revision,
    }
    snapshot.update(overrides)
    return snapshot


def _current(revision=1, event_id=EVENT_ID, **overrides):
    record = _snapshot(revision)
    record["change_event_ref"] = {
        "record_type": "event",
        "stable_id": event_id,
    }
    record.update(overrides)
    return record


def _event(event_id=EVENT_ID, revision=1,
           event_type=bindings_current.INITIALIZED_EVENT_TYPE, before=None,
           **overrides):
    event = {
        "schema_version": bindings_current.EVENT_SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "record_type": "event",
        "event_id": event_id,
        "event_type": event_type,
        "repo_id": REPO_ID,
        "company_id": COMPANY_ID,
        "before": before,
        "after": _snapshot(revision),
        "changed_refs": [{
            "record_type": "bindings_current",
            "stable_id": "current",
            "revision": revision,
        }],
        "actor": "installation-owner-synthetic",
        "reason": "synthetic validator test",
        "operation_id": "op-synthetic-0001",
        "base_commit": "0" * 40,
        "ordinal": revision,
    }
    event.update(overrides)
    return event


def _changed_event(event_id, before_revision, **overrides):
    overrides.setdefault("event_id", event_id)
    overrides.setdefault("revision", before_revision + 1)
    overrides.setdefault(
        "event_type", bindings_current.CHANGED_EVENT_TYPE)
    overrides.setdefault("before", _snapshot(before_revision))
    return _event(**overrides)


def _history(count):
    # A continuous chain: revision 1 initialized, then changed events up
    # to ``count``. Returns (events, current) for revision ``count``.
    events = [_event()]
    for revision in range(2, count + 1):
        events.append(_changed_event("evt-synthetic-%04d" % revision,
                                     revision - 1))
    return events, _current(count, events[-1]["event_id"])


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_valid_initial_binding_passes():
    assert bindings_current.validate_current_binding(
        _current(), manifest=_manifest_record(), events=[_event()],
        repo_id=REPO_ID, company_id=COMPANY_ID) is None


def test_valid_three_revision_history_passes():
    events, current = _history(3)
    assert bindings_current.validate_current_binding(
        current, manifest=_manifest_record(), events=events,
        repo_id=REPO_ID, company_id=COMPANY_ID) is None


def test_record_structure_validates_standalone():
    assert bindings_current.validate_bindings_current(_current()) is None


def test_initialized_before_may_be_empty_dict():
    events, current = _history(2)
    events[0] = _event(before={})
    assert bindings_current.validate_binding_history(
        current, events, repo_id=REPO_ID, company_id=COMPANY_ID) is None


def test_derived_paths_are_the_fixed_git_format_paths():
    assert bindings_current.derive_manifest_path(PACK_VERSION_ID) == (
        git_format.record_path(
            "taxonomy_manifest", pack_version=PACK_VERSION_ID))
    assert bindings_current.derive_event_path(EVENT_ID) == (
        git_format.record_path("event", event_id=EVENT_ID))
    assert bindings_current.derive_manifest_path(PACK_VERSION_ID) == (
        "taxonomy/%s/manifest.json" % PACK_VERSION_ID)
    assert bindings_current.derive_event_path(EVENT_ID) == (
        "events/%s.json" % EVENT_ID)


def test_closes_a_real_git_taxonomy_manifest_record():
    # Integration with the existing semantics: the manifest record built
    # by git_taxonomy from the validated draft pack must close against a
    # bindings_current record that carries the pack name, the business
    # version and the pack's own digest, at the derived canonical path.
    pack = template_packs.validate_pack("generic-v2")
    records = git_taxonomy.build_taxonomy_records(
        pack, pack_version=PACK_VERSION_ID)
    manifest_path, manifest_bytes = records[0]
    assert manifest_path == bindings_current.derive_manifest_path(
        PACK_VERSION_ID)
    manifest = git_format.loads_record(manifest_bytes)
    current = _current(
        template_pack=pack.pack, template_version=pack.version,
        manifest_sha256=pack.manifest_sha256)
    snapshot = {key: value for key, value in current.items()
                if key != "change_event_ref"}
    assert bindings_current.validate_current_binding(
        current, manifest=manifest, events=[_event(after=snapshot)],
        repo_id=REPO_ID, company_id=COMPANY_ID) is None


# ---------------------------------------------------------------------------
# Section 10.2: fixed fields and types
# ---------------------------------------------------------------------------

def test_non_dict_payload_rejected():
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current([_current()])


def test_non_str_keys_rejected():
    payload = _current()
    payload[1] = "x"
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(payload)


@pytest.mark.parametrize("field", sorted(
    bindings_current._CURRENT_FIELDS))
def test_missing_field_rejected(field):
    payload = _current()
    del payload[field]
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(payload)


def test_unknown_field_rejected():
    payload = _current()
    payload["manifest_path"] = "taxonomy/whatever/manifest.json"
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(payload)


def test_record_type_is_the_fixed_string():
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(
            _current(record_type="bindings_history"))
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(_current(record_type=7))


@pytest.mark.parametrize("field", ["schema_version", "format_version"])
@pytest.mark.parametrize("value", [2, 0, -1, 99])
def test_wrong_version_value_rejected(field, value):
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(_current(**{field: value}))


@pytest.mark.parametrize("field", ["schema_version", "format_version"])
@pytest.mark.parametrize("value", [True, False, "1", 1.0, Decimal(1), None])
def test_bool_and_non_int_versions_rejected(field, value):
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(_current(**{field: value}))


@pytest.mark.parametrize("field", ["template_version", "binding_revision"])
@pytest.mark.parametrize("value", [True, False, "1", 1.0, Decimal(1), None])
def test_bool_and_non_int_revisions_rejected(field, value):
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(_current(**{field: value}))


@pytest.mark.parametrize("field", ["template_version", "binding_revision"])
@pytest.mark.parametrize("value", [0, -1, -99])
def test_nonpositive_revisions_rejected(field, value):
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(_current(**{field: value}))


class _IntSubclass(int):
    pass


@pytest.mark.parametrize(
    "field", ["schema_version", "format_version", "template_version",
              "binding_revision"])
def test_int_subclass_versions_rejected(field):
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(
            _current(**{field: _IntSubclass(1)}))


@pytest.mark.parametrize(
    "field", ["repo_id", "company_id", "template_pack", "taxonomy_path_id"])
@pytest.mark.parametrize("value,exc", [
    ("", ValueError),
    ("x" * 129, ValueError),
    ("\ud800", ValueError),
    (7, TypeError),
    (None, TypeError),
])
def test_illegal_opaque_ids_rejected(field, value, exc):
    with pytest.raises(exc):
        bindings_current.validate_bindings_current(_current(**{field: value}))


@pytest.mark.parametrize("value", [
    "A" * 64, "0" * 63, "0" * 65, "z" * 64, "0123456789abcdef" * 3 + "xy",
    "", "sha256:" + "0" * 64,
])
def test_bad_manifest_digest_rejected(value):
    with pytest.raises(ValueError):
        bindings_current.validate_bindings_current(
            _current(manifest_sha256=value))


def test_non_str_manifest_digest_rejected():
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(_current(manifest_sha256=5))


# ---------------------------------------------------------------------------
# Section 10.4: the typed event reference on the current record
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,exc", [
    ({"record_type": "event"}, ValueError),
    ({"stable_id": EVENT_ID}, ValueError),
    ({}, ValueError),
    ({"record_type": "event", "stable_id": EVENT_ID, "revision": 1},
     ValueError),
    ({"record_type": "bindings_current", "stable_id": EVENT_ID}, ValueError),
    ({"record_type": "event", "stable_id": ""}, ValueError),
    ({"record_type": "event", "stable_id": 7}, TypeError),
    ({"record_type": 7, "stable_id": EVENT_ID}, TypeError),
    (EVENT_ID, TypeError),
    (None, TypeError),
])
def test_change_event_ref_shape_rejections(ref, exc):
    with pytest.raises(exc):
        bindings_current.validate_bindings_current(
            _current(change_event_ref=ref))


# ---------------------------------------------------------------------------
# Section 10.3: manifest identity and the fixed digest input
# ---------------------------------------------------------------------------

def test_template_pack_must_match_manifest_body():
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(template_pack="other-pack"), _manifest_record())


def test_template_version_must_match_manifest_business_version():
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(template_version=BUSINESS_VERSION + 1),
            _manifest_record())


def test_wrong_digest_rejected():
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(manifest_sha256="0" * 64), _manifest_record())


def test_digest_input_excludes_wrapper_fields():
    # The record carries the full wrapper, yet the validated digest is
    # exactly template_packs.manifest_digest of the wrapper-free body:
    # the outer schema_version/format_version/manifest_sha256 never
    # enter the digest input, so the digest is fixed and free of
    # self-reference.
    assert _content_digest() == template_packs.manifest_digest(
        _manifest_document())
    assert bindings_current.validate_manifest_identity(
        _current(), _manifest_record()) is None


@pytest.mark.parametrize("field", ["schema_version", "format_version"])
@pytest.mark.parametrize("value", [9, 2, 0, -1])
def test_manifest_unknown_version_value_rejected(field, value):
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(), _manifest_record(**{field: value}))


@pytest.mark.parametrize("field", ["schema_version", "format_version"])
@pytest.mark.parametrize(
    "value", [True, False, "1", 1.0, Decimal(1), None, _IntSubclass(1)])
def test_manifest_non_plain_int_versions_rejected(field, value):
    # The manifest wrapper versions must be exact built-in ints: bool,
    # int subclasses and every other type fail closed as TypeErrors even
    # when they compare equal to the supported version.
    with pytest.raises(TypeError):
        bindings_current.validate_manifest_identity(
            _current(), _manifest_record(**{field: value}))


@pytest.mark.parametrize(
    "field", ["schema_version", "format_version", "manifest_sha256"])
def test_manifest_missing_wrapper_field_rejected(field):
    record = _manifest_record()
    del record[field]
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(_current(), record)


def test_manifest_own_digest_field_must_agree_with_content():
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(), _manifest_record(manifest_sha256="0" * 64))


@pytest.mark.parametrize("value", [5, True, 1.0, Decimal(1), None])
def test_manifest_own_digest_non_str_rejected(value):
    with pytest.raises(TypeError):
        bindings_current.validate_manifest_identity(
            _current(), _manifest_record(manifest_sha256=value))


def test_manifest_missing_body_fields_rejected():
    record = _manifest_record()
    del record["pack"]
    del record["version"]
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(_current(), record)


def test_manifest_non_dict_rejected():
    with pytest.raises(TypeError):
        bindings_current.validate_manifest_identity(_current(), "manifest")


def test_path_id_is_never_confused_with_pack_or_version():
    # taxonomy_path_id stays opaque: pointing it at another legal ID
    # changes only the derived path, never the manifest body check, and
    # a current that swaps in the business version as a str fails the
    # plain-int rule instead of being reinterpreted as a path.
    other_id = "packver-synthetic-0002"
    assert bindings_current.derive_manifest_path(other_id) != (
        bindings_current.derive_manifest_path(PACK_VERSION_ID))
    assert bindings_current.validate_manifest_identity(
        _current(taxonomy_path_id=other_id), _manifest_record()) is None
    with pytest.raises(TypeError):
        bindings_current.validate_bindings_current(
            _current(template_version=str(BUSINESS_VERSION)))
    with pytest.raises(ValueError):
        bindings_current.validate_manifest_identity(
            _current(template_pack=PACK_VERSION_ID), _manifest_record())


# ---------------------------------------------------------------------------
# Section 10.4: single-event closure
# ---------------------------------------------------------------------------

def test_dangling_event_reference_rejected():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(), [], repo_id=REPO_ID, company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(event_id="evt-missing"), [_event()],
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_cross_repo_event_rejected():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(repo_id="repo-other"), repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_cross_company_event_rejected():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(company_id="co-other"), repo_id=REPO_ID,
            company_id=COMPANY_ID)


@pytest.mark.parametrize("field,value", [
    ("repo_id", 123),
    ("repo_id", None),
    ("repo_id", True),
    ("company_id", 123),
    ("company_id", None),
    ("company_id", ["co-synthetic-0001"]),
])
def test_wrong_typed_event_scope_is_a_type_error(field, value):
    # A wrong field type in the event's own scope is the documented
    # TypeError, checked before (and never reported as) the ValueError
    # scope mismatch; the int 123 must not ride a value comparison.
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(**{field: value}), repo_id=REPO_ID,
            company_id=COMPANY_ID)


@pytest.mark.parametrize("field", ["repo_id", "company_id"])
def test_illegal_str_event_scope_is_a_value_error(field):
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(**{field: ""}), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_full_closure_rejects_wrong_typed_event_scope_end_to_end():
    events, current = _history(1)
    events[0]["repo_id"] = 123
    with pytest.raises(TypeError):
        bindings_current.validate_current_binding(
            current, manifest=_manifest_record(), events=events,
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(event_type="binding_removed"), repo_id=REPO_ID,
            company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(event_type=7), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_initialized_requires_empty_before():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(before=_snapshot(1)), repo_id=REPO_ID,
            company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(before="none"), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_initialized_must_establish_revision_one():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(revision=2), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_changed_requires_a_full_before_snapshot():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _changed_event("evt-2", 1, before={}), repo_id=REPO_ID,
            company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _changed_event("evt-2", 1, before=None), repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_changed_requires_exactly_plus_one_revision():
    event = _event(
        event_id="evt-3", revision=3,
        event_type=bindings_current.CHANGED_EVENT_TYPE,
        before=_snapshot(1))
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            event, repo_id=REPO_ID, company_id=COMPANY_ID)


def test_after_snapshot_must_not_carry_the_event_ref():
    # No digest cycle between event and current: the snapshot field set
    # excludes change_event_ref, so an after (or before) carrying it is
    # rejected as an extra field.
    after = _current(1)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(after=after), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_after_snapshot_fields_are_fully_validated():
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(after=_snapshot(1, template_version=True)),
            repo_id=REPO_ID, company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(after=_snapshot(1, manifest_sha256="0" * 63)),
            repo_id=REPO_ID, company_id=COMPANY_ID)
    after = _snapshot(1)
    del after["taxonomy_path_id"]
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(after=after), repo_id=REPO_ID, company_id=COMPANY_ID)


@pytest.mark.parametrize("field", sorted(
    bindings_current._EVENT_REQUIRED_FIELDS))
def test_missing_event_field_rejected(field):
    event = _event()
    del event[field]
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            event, repo_id=REPO_ID, company_id=COMPANY_ID)


# ---------------------------------------------------------------------------
# Section 10.1: the event record wrapper (schema/format version, record_type)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["schema_version", "format_version",
                                   "record_type"])
def test_missing_event_wrapper_field_rejected(field):
    event = _event()
    del event[field]
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            event, repo_id=REPO_ID, company_id=COMPANY_ID)


@pytest.mark.parametrize("value", [2, 0, -1, 99])
def test_unknown_or_nonpositive_event_schema_version_rejected(value):
    # EVENT_SCHEMA_VERSION is 1 for this first event implementation; any
    # other plain int is an unknown version and fails closed.
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(schema_version=value), repo_id=REPO_ID,
            company_id=COMPANY_ID)


@pytest.mark.parametrize("value", [
    True, False, "1", 1.0, Decimal(1), None, _IntSubclass(1)])
def test_non_plain_int_event_schema_version_rejected(value):
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(schema_version=value), repo_id=REPO_ID,
            company_id=COMPANY_ID)


@pytest.mark.parametrize("value", [
    git_format.FORMAT_VERSION + 1,
    git_format.FORMAT_VERSION - 1 if git_format.FORMAT_VERSION > 1 else 0,
    0, -1, 99])
def test_wrong_event_format_version_rejected(value):
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(format_version=value), repo_id=REPO_ID,
            company_id=COMPANY_ID)


@pytest.mark.parametrize("value", [
    True, False, "1", 1.0, Decimal(1), None, _IntSubclass(
        git_format.FORMAT_VERSION)])
def test_non_plain_int_event_format_version_rejected(value):
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(format_version=value), repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_event_record_type_is_the_fixed_string():
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(record_type="bindings_current"), repo_id=REPO_ID,
            company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(record_type="operation"), repo_id=REPO_ID,
            company_id=COMPANY_ID)
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(record_type=7), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_wrapper_rejections_reach_the_full_closure():
    events, current = _history(1)
    events[0]["schema_version"] = 2
    with pytest.raises(ValueError):
        bindings_current.validate_current_binding(
            current, manifest=_manifest_record(), events=events,
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_generic_event_fields_need_presence_only():
    # Documented boundary: the inner generic event payload (actor,
    # reason, operation_id, base_commit, ordinal and approval
    # references) is not frozen by sections 10.2-10.6, so the validator
    # requires the fields to exist but leaves their inner shape (and any
    # extra event field) to the later full-tree closure card. The
    # section-10.1 wrapper fields above are NOT part of this boundary.
    event = _event(operation_id=123, actor={"kind": "owner"},
                   base_commit=None, ordinal="first", reason=["r"],
                   extra_audit="anything")
    assert bindings_current.validate_binding_event(
        event, repo_id=REPO_ID, company_id=COMPANY_ID) is None


@pytest.mark.parametrize("refs", [
    [],
    [{"record_type": "bindings_current", "stable_id": "current"}],
    [{"record_type": "bindings_current", "stable_id": "current",
      "revision": 2}],
    [{"record_type": "event", "stable_id": "current", "revision": 1}],
    [{"record_type": "bindings_current", "stable_id": "latest",
      "revision": 1}],
    "bindings_current/current/1",
])
def test_changed_refs_must_carry_the_logical_binding_ref(refs):
    exc = TypeError if isinstance(refs, str) else ValueError
    with pytest.raises(exc):
        bindings_current.validate_binding_event(
            _event(changed_refs=refs), repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_changed_refs_may_carry_additional_typed_refs():
    event = _event(changed_refs=[
        {"record_type": "operation", "stable_id": "op-synthetic-0001"},
        {"record_type": "bindings_current", "stable_id": "current",
         "revision": 1},
    ])
    assert bindings_current.validate_binding_event(
        event, repo_id=REPO_ID, company_id=COMPANY_ID) is None


def _logical_ref(revision):
    return {"record_type": "bindings_current", "stable_id": "current",
            "revision": revision}


@pytest.mark.parametrize("revision", [
    True, False, 1.0, Decimal(1), _IntSubclass(1), "1", None,
])
def test_changed_ref_revision_must_be_a_plain_int(revision):
    # Regression: dict equality treats True == 1, 1.0 == 1 and
    # Decimal(1) == 1, so the review-reproduced
    # changed_refs=[{..., "revision": True}] used to pass for an after
    # binding_revision of 1. The logical ref's revision must be a plain
    # built-in int: bool, float, Decimal and int subclasses are all
    # TypeErrors even when they loosely equal the expected revision.
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(changed_refs=[_logical_ref(revision)]),
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_changed_ref_bool_revision_rejected_end_to_end():
    events, current = _history(1)
    events[0]["changed_refs"] = [_logical_ref(True)]
    with pytest.raises(TypeError):
        bindings_current.validate_current_binding(
            current, manifest=_manifest_record(), events=events,
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_valid_logical_ref_does_not_mask_a_malformed_duplicate():
    with pytest.raises(TypeError):
        bindings_current.validate_binding_event(
            _event(changed_refs=[_logical_ref(1), _logical_ref(True)]),
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_changed_ref_plain_int_revision_still_matches_exactly():
    # The strict matcher keeps accepting the exact plain int and keeps
    # rejecting a plain int of the wrong value as a ValueError.
    assert bindings_current.validate_binding_event(
        _event(changed_refs=[_logical_ref(1)]),
        repo_id=REPO_ID, company_id=COMPANY_ID) is None
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(changed_refs=[_logical_ref(2)]),
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_changed_ref_extra_key_rejected():
    ref = _logical_ref(1)
    ref["digest"] = "0" * 64
    with pytest.raises(ValueError):
        bindings_current.validate_binding_event(
            _event(changed_refs=[ref]), repo_id=REPO_ID,
            company_id=COMPANY_ID)


# ---------------------------------------------------------------------------
# Sections 10.4-10.5: revision chain closure
# ---------------------------------------------------------------------------

def test_current_must_equal_latest_after_snapshot():
    events, _ = _history(2)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(2, events[-1]["event_id"],
                     template_version=BUSINESS_VERSION + 1),
            events, repo_id=REPO_ID, company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(1, events[0]["event_id"]),
            events, repo_id=REPO_ID, company_id=COMPANY_ID)


def test_current_must_point_at_the_latest_event():
    events, current = _history(3)
    stale = _current(3, events[1]["event_id"])
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            stale, events, repo_id=REPO_ID, company_id=COMPANY_ID)


def test_duplicate_event_ids_rejected():
    events, _ = _history(1)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(1, EVENT_ID), events + [_event()],
            repo_id=REPO_ID, company_id=COMPANY_ID)


def test_duplicate_revisions_rejected():
    events = [_event(), _changed_event("evt-2", 1),
              _changed_event("evt-3", 1)]
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(2, "evt-2"), events, repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_gapped_revisions_rejected():
    events = [_event(), _changed_event("evt-3", 2)]
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(3, "evt-3"), events, repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_backward_revision_step_rejected():
    # A changed event whose before snapshot steps back to revision 1
    # after revision 2 was already established breaks continuity.
    events = [_event(), _changed_event("evt-2", 1),
              _changed_event("evt-3", 2)]
    events[2]["after"] = _snapshot(2)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(2, "evt-2"), events, repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_non_continuous_before_after_transition_rejected():
    events, current = _history(3)
    # Same revision numbers, but the middle event's before snapshot
    # disagrees with the first event's after snapshot.
    events[1]["before"] = _snapshot(1, taxonomy_path_id="packver-other")
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            current, events, repo_id=REPO_ID, company_id=COMPANY_ID)


def test_history_scope_must_match_current_and_expected():
    events, current = _history(1)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            current, events, repo_id="repo-other", company_id=COMPANY_ID)
    with pytest.raises(ValueError):
        bindings_current.validate_binding_history(
            _current(company_id="co-other"), events, repo_id=REPO_ID,
            company_id=COMPANY_ID)


def test_events_container_type_enforced():
    with pytest.raises(TypeError):
        bindings_current.validate_binding_history(
            _current(), _event(), repo_id=REPO_ID, company_id=COMPANY_ID)


def test_full_closure_rejects_cross_scope_event_end_to_end():
    events, current = _history(1)
    events[0]["company_id"] = "co-other"
    with pytest.raises(ValueError):
        bindings_current.validate_current_binding(
            current, manifest=_manifest_record(), events=events,
            repo_id=REPO_ID, company_id=COMPANY_ID)
