"""F-001-R1: pure schemas and closure 1/5 for a synthetic fixed tree C.

Design 97 sections 2.1-2.3, table 3 (lines 75-78), section 4 items 1/5
(lines 98/102), and section 5 (109-111). Table 3 specifies *semantics*
(line 55); SCHEMA_FIELDS below publishes their v1 machine spelling.
Candidate identities/candidate_refs retain candidate_reference_closure's
existing spelling. No business action names, synonym tables or heuristics
are used. ``terminal`` and ``result_refs`` encode the table's terminal
disposition and output references: non-terminal issue/proposal results
represent hold/propose_revision; a terminal issue result represents
record_conflict and cannot claim a fact output (section 4, line 102).

Only four families have strict business schemas. Other RECORD_TYPES are
reference targets checked for canonical path identity and repo/company,
with an explicit schema-registration seam; their business schemas and
closures 2/3/4/6 are NOT implemented. External object references are
SHA-256 content addresses, not business typed refs; existence, persistence
and host authorization are not asserted. Actor audit_ref uses the existing
F-007 pure validator. Approval-use refs fail closed until F-007 publishes
their schema. An empty approval list makes no authorization assertion.

The path vocabulary comes exclusively from git_format, including its
private _match_template helper for reverse identity extraction. Typed refs
have record_type/stable_id and revision iff the fixed path has a second
identity component; opaque version/event IDs retain encode_id semantics.
Mutable current records cannot serve as historical targets. The already
frozen logical binding exception resolves an exact revision to its event
after snapshot; binding version-chain closure remains outside this card.

validate_record_schema_closure accepts a dict of path -> payload dict and
returns None or raises RecordSchemaClosureError (family/path/code/field).
loads_schema_record is the bytes/text boundary: duplicate keys cannot be
recovered from an already materialized dict. Both entries reuse git_format
for exact Decimal, UTF-8, canonical JSON and all serialization budgets.
No Git, Broker, file, database, model, network or feature-gate operations.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from app.facts_tree import bindings_current, git_format
from app.host_audit_refs import ActorAuditRefInvalid, validate_actor_audit_ref


_COMMON = frozenset({
    "schema_version", "format_version", "record_type", "repo_id", "company_id",
})
SCHEMA_FIELDS = MappingProxyType({
    # §3 input_candidate: digest, whitelist, safe envelope summary,
    # candidate/adapter/validation/coverage versions, source/mapping,
    # controlled immutable full payload/envelope addresses.
    "input_candidate": _COMMON | frozenset({
        "candidate_uid", "candidate_revision", "payload_digest",
        "whitelisted_fields", "envelope_summary", "candidate_rule_version",
        "adapter_rule_version", "validation_rule_version",
        "coverage_manifest_version", "source_id", "version_id",
        "mapped_fingerprint", "payload_ref", "envelope_ref",
    }),
    # §3 operation: exact inputs, coverage binding/check version, unit,
    # per-candidate disposition/output, facts, rules/model, audit, events/time.
    "operation": _COMMON | frozenset({
        "operation_id", "request_digest", "effect_digest", "request_id",
        "base_commit", "candidate_refs", "coverage_id", "coverage_digest",
        "coverage_validation_version", "unit_id", "disposition_result",
        "formal_fact_refs", "rule_version", "model_version", "actor",
        "approval_use_refs", "event_ids", "server_time",
    }),
    "consumed_candidate": _COMMON | frozenset({
        "candidate_uid", "candidate_revision", "payload_digest",
        "operation_id", "disposition_result",
    }),
    "event": _COMMON | frozenset({
        "event_id", "operation_id", "base_commit", "ordinal", "event_type",
        "changed_refs", "before", "after", "invalidation_domains",
        "affected_refs",
    }),
})
_CANDIDATE = frozenset({"candidate_uid", "candidate_revision", "payload_digest"})
_RESULT = frozenset({"terminal", "result_refs"})
_HEX = frozenset("0123456789abcdef")
_NONTERMINAL_RESULTS = frozenset({"issue_event", "proposal_event"})
_TERMINAL_RESULTS = frozenset({
    "fact_revision", "evidence", "link_revision", "placement_revision",
    "issue_event",
})
_UNSTABLE = frozenset({"HEAD", "current", "latest"})
# Safe summary/whitelist fields carry no free-form secret-bearing object.
# Full original fields belong to the two controlled external addresses.
_WHITELIST = frozenset({"local_id", "value_type", "statement_kind", "polarity"})
_ENVELOPE_SUMMARY = frozenset({"source_id", "version_id", "mapped_fingerprint"})
# §10.4 binding events retain their frozen full snapshots and actor/reason.
_BINDING_EVENT_FIELDS = SCHEMA_FIELDS["event"] | {"actor", "reason", "approval_use_refs"}


@dataclass(frozen=True)
class RecordSchemaClosureError(ValueError):
    """Stable, non-payload-bearing diagnostic; no naked validation errors."""

    family: str
    path: str
    code: str
    field: str

    def __str__(self):
        return f"{self.family}:{self.path}:{self.code}:{self.field}"


def _fail(family, path, code, field):
    raise RecordSchemaClosureError(family, path, code, field)


def _exact(value, keys, family, path, field):
    if not isinstance(value, dict) or any(type(k) is not str for k in value):
        _fail(family, path, "object_type", field)
    if set(value) - keys:
        _fail(family, path, "unknown_field", field)
    if keys - set(value):
        _fail(family, path, "missing_field", field)


def _id(value, family, path, field):
    try:
        git_format.encode_id(value)
    except (TypeError, ValueError):
        _fail(family, path, "invalid_id", field)


def _positive(value, family, path, field):
    try:
        git_format.encode_revision(value)
    except (TypeError, ValueError):
        _fail(family, path, "positive_integer", field)


def _digest(value, family, path, field):
    if type(value) is not str or len(value) != 64 or not set(value) <= _HEX:
        _fail(family, path, "sha256_digest", field)


def _array(value, family, path, field):
    if type(value) is not list:
        _fail(family, path, "array_type", field)


def _canonical(payload, family, path):
    try:
        return git_format.dumps_record(payload)
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail(family, path, "record_encoding", "$")


def _path_identity(path, payload, repo_id, company_id):
    family = "tree"
    try:
        family = git_format.canonical_record_type(path)
    except (TypeError, ValueError):
        _fail(family, "<invalid-path>", "record_path", "path")
    if family is None:
        _fail("tree", path, "record_path", "path")
    _canonical(payload, family, path)
    if not isinstance(payload, dict):
        _fail(family, path, "object_type", "$")
    # §2.1 explicitly exempts the frozen repository/taxonomy wrappers.
    # They are carried through, not upgraded to a new business schema.
    frozen_wrapper = family in {"repository_meta", "taxonomy_manifest", "taxonomy_leaf"}
    if not frozen_wrapper and payload.get("record_type") != family:
        _fail(family, path, "record_type", "record_type")
    for field, expected in (("repo_id", repo_id), ("company_id", company_id)):
        if family in {"taxonomy_manifest", "taxonomy_leaf"} and field not in payload:
            continue
        _id(payload.get(field), family, path, field)
        if payload[field] != expected:
            _fail(family, path, "scope_mismatch", field)
    # Reverse through the original template, then reconstruct exclusively
    # through record_path. No copy of the twenty-type path/id vocabulary.
    ids = git_format._match_template(family, path)
    for field, value in ids.items():
        if family in {"taxonomy_manifest", "taxonomy_leaf"}:
            # Opaque pack_version is intentionally not the business version.
            # Only a leaf's code has a corresponding payload path identity.
            if field == "leaf_id" and payload.get("code") != value:
                _fail(family, path, "path_identity", "code")
            continue
        actual = payload.get(field)
        if type(actual) is not type(value) or actual != value:
            _fail(family, path, "path_identity", field)
    try:
        rebuilt = git_format.record_path(family, **(
            ids if frozen_wrapper else {k: payload[k] for k in ids}))
    except (TypeError, ValueError, KeyError):
        _fail(family, path, "path_identity", "path")
    if rebuilt != path:
        _fail(family, path, "path_identity", "path")
    return family, ids


def _candidate(ref, family, path, field):
    _exact(ref, _CANDIDATE, family, path, field)
    _id(ref["candidate_uid"], family, path, field + ".candidate_uid")
    _positive(ref["candidate_revision"], family, path, field + ".candidate_revision")
    _digest(ref["payload_digest"], family, path, field + ".payload_digest")
    return ref["candidate_uid"], ref["candidate_revision"]


def _ref_key(ref, family, path, field):
    if not isinstance(ref, dict):
        _fail(family, path, "typed_ref_shape", field)
    kind = ref.get("record_type")
    if type(kind) is not str or kind not in git_format.RECORD_TYPES:
        _fail(family, path, "typed_ref_type", field)
    keys = {"record_type", "stable_id"}
    if "revision" in ref:
        keys.add("revision")
    _exact(ref, keys, family, path, field)
    _id(ref["stable_id"], family, path, field + ".stable_id")
    revision = ref.get("revision")
    logical_binding = kind == "bindings_current" and ref["stable_id"] == bindings_current.CURRENT_STABLE_ID
    if (ref["stable_id"] in _UNSTABLE and not logical_binding) or revision in ("HEAD", "current", "latest"):
        _fail(family, path, "unstable_history_ref", field)
    if logical_binding:
        _positive(revision, family, path, field + ".revision")
    if "revision" in ref:
        if type(revision) is int:
            _positive(revision, family, path, field + ".revision")
        else:
            _id(revision, family, path, field + ".revision")
    return kind, ref["stable_id"], revision


def _refs(value, family, path, field):
    _array(value, family, path, field)
    keys = [_ref_key(ref, family, path, f"{field}[{i}]")
            for i, ref in enumerate(value)]
    if len(set(keys)) != len(keys):
        _fail(family, path, "duplicate_ref", field)
    return keys


def _result(value, family, path, field):
    _exact(value, _RESULT, family, path, field)
    if type(value["terminal"]) is not bool:
        _fail(family, path, "terminal_boolean", field)
    keys = _refs(value["result_refs"], family, path, field + ".result_refs")
    if not keys:
        _fail(family, path, "missing_result_ref", field)
    kinds = {key[0] for key in keys}
    if not value["terminal"]:
        if not kinds <= _NONTERMINAL_RESULTS:
            _fail(family, path, "nonterminal_result_type", field)
        if len(kinds) != 1:
            _fail(family, path, "multiple_primary_result_types", field)
    elif not kinds <= _TERMINAL_RESULTS:
        _fail(family, path, "terminal_result_type", field)
    elif "issue_event" in kinds and "fact_revision" in kinds:
        _fail(family, path, "conflict_fact_output", field)
    return keys


def _oid(value, length, family, path):
    if type(value) is not str or len(value) != length or not set(value) <= _HEX:
        _fail(family, path, "base_commit_oid", "base_commit")


def _validate_input(payload, family, path, oid_length):
    _candidate({k: payload[k] for k in _CANDIDATE}, family, path, "candidate")
    for field in ("candidate_rule_version", "adapter_rule_version",
                  "validation_rule_version", "coverage_manifest_version",
                  "source_id", "version_id"):
        _id(payload[field], family, path, field)
    for field in ("mapped_fingerprint", "payload_ref", "envelope_ref"):
        _digest(payload[field], family, path, field)
    fields = payload["whitelisted_fields"]
    _exact(fields, _WHITELIST, family, path, "whitelisted_fields")
    for field in fields:
        _id(fields[field], family, path, "whitelisted_fields." + field)
    summary = payload["envelope_summary"]
    _exact(summary, _ENVELOPE_SUMMARY, family, path, "envelope_summary")
    for field in summary:
        if summary[field] != payload[field]:
            _fail(family, path, "envelope_binding", "envelope_summary." + field)
    if payload["payload_ref"] != payload["payload_digest"]:
        _fail(family, path, "payload_address_digest", "payload_ref")


def _validate_operation(payload, family, path, oid_length):
    for field in ("operation_id", "request_id", "coverage_id",
                  "coverage_validation_version", "unit_id", "rule_version",
                  "model_version"):
        _id(payload[field], family, path, field)
    for field in ("request_digest", "effect_digest", "coverage_digest"):
        _digest(payload[field], family, path, field)
    _oid(payload["base_commit"], oid_length, family, path)
    _array(payload["candidate_refs"], family, path, "candidate_refs")
    for i, candidate in enumerate(payload["candidate_refs"]):
        _candidate(candidate, family, path, f"candidate_refs[{i}]")
    _array(payload["disposition_result"], family, path, "disposition_result")
    for i, item in enumerate(payload["disposition_result"]):
        field = f"disposition_result[{i}]"
        _exact(item, _CANDIDATE | _RESULT, family, path, field)
        _candidate({k: item[k] for k in _CANDIDATE}, family, path, field)
        _result({k: item[k] for k in _RESULT}, family, path, field)
    facts = _refs(payload["formal_fact_refs"], family, path, "formal_fact_refs")
    if any(ref[0] != "fact_revision" for ref in facts):
        _fail(family, path, "formal_fact_type", "formal_fact_refs")
    try:
        validate_actor_audit_ref(payload["actor"])
    except ActorAuditRefInvalid:
        _fail(family, path, "actor_audit_ref", "actor")
    if payload["actor"]["company_id"] != payload["company_id"]:
        _fail(family, path, "audit_scope", "actor.company_id")
    _array(payload["approval_use_refs"], family, path, "approval_use_refs")
    if payload["approval_use_refs"]:
        _fail(family, path, "unsupported_approval_audit_ref", "approval_use_refs")
    _array(payload["event_ids"], family, path, "event_ids")
    for event_id in payload["event_ids"]:
        _id(event_id, family, path, "event_ids")
    if len(set(payload["event_ids"])) != len(payload["event_ids"]):
        _fail(family, path, "duplicate_event_id", "event_ids")
    # Server time is an explicit non-negative integer timestamp, never
    # used to derive event ordering or an accepted commit (design §5).
    if type(payload["server_time"]) is not int or payload["server_time"] < 0:
        _fail(family, path, "server_time", "server_time")


def _validate_consumed(payload, family, path, oid_length):
    _candidate({k: payload[k] for k in _CANDIDATE}, family, path, "candidate")
    _id(payload["operation_id"], family, path, "operation_id")
    _result(payload["disposition_result"], family, path, "disposition_result")
    if not payload["disposition_result"]["terminal"]:
        _fail(family, path, "nonterminal_consumed", "disposition_result")


def _validate_event(payload, family, path, oid_length):
    for field in ("event_id", "operation_id", "event_type"):
        _id(payload[field], family, path, field)
    _oid(payload["base_commit"], oid_length, family, path)
    if type(payload["ordinal"]) is not int or payload["ordinal"] < 0:
        _fail(family, path, "event_ordinal", "ordinal")
    refs = _refs(payload["changed_refs"], family, path, "changed_refs")
    if not refs:
        _fail(family, path, "missing_changed_ref", "changed_refs")
    _refs(payload["affected_refs"], family, path, "affected_refs")
    if payload["event_type"] in bindings_current.BINDING_EVENT_TYPES:
        try:
            bindings_current.validate_binding_event(
                payload, repo_id=payload["repo_id"], company_id=payload["company_id"])
            validate_actor_audit_ref(payload["actor"])
        except (TypeError, ValueError, ActorAuditRefInvalid):
            _fail(family, path, "binding_event_schema", "before/after/actor")
        if payload["actor"]["company_id"] != payload["company_id"]:
            _fail(family, path, "audit_scope", "actor.company_id")
        if type(payload["reason"]) is not str or not payload["reason"].strip():
            _fail(family, path, "reason_text", "reason")
        _array(payload["approval_use_refs"], family, path, "approval_use_refs")
        if payload["approval_use_refs"]:
            _fail(family, path, "unsupported_approval_audit_ref", "approval_use_refs")
        _array(payload["invalidation_domains"], family, path, "invalidation_domains")
        for domain in payload["invalidation_domains"]:
            _id(domain, family, path, "invalidation_domains")
        return
    for field in ("before", "after"):
        values = payload[field]
        _array(values, family, path, field)
        if len(values) != len(refs):
            _fail(family, path, "event_snapshot_count", field)
        for value in values:
            if value is None:
                continue
            if type(value) is int:
                _positive(value, family, path, field)
            else:
                _digest(value, family, path, field)
    for before, after in zip(payload["before"], payload["after"]):
        if before == after:
            _fail(family, path, "empty_event_change", "before/after")
    _array(payload["invalidation_domains"], family, path, "invalidation_domains")
    for domain in payload["invalidation_domains"]:
        _id(domain, family, path, "invalidation_domains")


# Registration seams are deliberately separate from git_format.RECORD_TYPES.
# Adding another business schema or closure does not alter the path grammar.
SCHEMA_VALIDATORS = MappingProxyType({
    "input_candidate": _validate_input,
    "operation": _validate_operation,
    "consumed_candidate": _validate_consumed,
    "event": _validate_event,
})


def validate_record_schema(path, payload, *, repo_id, company_id,
                           object_format="sha1"):
    """Validate one record; no cross-record closure or authorization claim."""
    if object_format not in ("sha1", "sha256"):
        _fail("tree", "<tree>", "object_format", "object_format")
    for field, value in (("repo_id", repo_id), ("company_id", company_id)):
        _id(value, "tree", "<tree>", field)
    family, _ = _path_identity(path, payload, repo_id, company_id)
    if family in SCHEMA_VALIDATORS:
        fields = SCHEMA_FIELDS[family]
        if family == "event" and payload.get("event_type") in tuple(bindings_current.BINDING_EVENT_TYPES):
            fields = _BINDING_EVENT_FIELDS
        _exact(payload, fields, family, path, "$")
        for field, expected in (("schema_version", 1),
                                ("format_version", git_format.FORMAT_VERSION)):
            _positive(payload[field], family, path, field)
            if payload[field] != expected:
                _fail(family, path, "unsupported_version", field)
        SCHEMA_VALIDATORS[family](
            payload, family, path, 40 if object_format == "sha1" else 64)
    return None


def loads_schema_record(data, *, path, repo_id, company_id, object_format="sha1"):
    """Parse canonical bytes/text, rejecting duplicates before dict conversion."""
    try:
        family = git_format.canonical_record_type(path) or "tree"
    except (TypeError, ValueError):
        _fail("tree", "<invalid-path>", "record_path", "path")
    try:
        payload = git_format.loads_record(data)
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail(family, path, "record_encoding", "$")
    encoded = _canonical(payload, family, path)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if raw != encoded:
        _fail(family, path, "noncanonical_encoding", "$")
    validate_record_schema(path, payload, repo_id=repo_id, company_id=company_id,
                           object_format=object_format)
    return payload


def _target_index(tree):
    index = {}
    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        ids = git_format._match_template(family, path)
        values = tuple(ids.values())
        # Mutable pointer paths have fewer identity components than their
        # actual historical target; require explicit revision resolution.
        if family in {"entity_current", "fact_current", "placement_current",
                      "bindings_current"}:
            continue
        if values:
            key = (family, values[0], values[1] if len(values) == 2 else None)
            index[key] = path
        if family == "event" and payload["event_type"] in bindings_current.BINDING_EVENT_TYPES:
            key = ("bindings_current", bindings_current.CURRENT_STABLE_ID,
                   payload["after"]["binding_revision"])
            if key in index:
                _fail("event", path, "ambiguous_binding_ref", "after.binding_revision")
            index[key] = path
    return index


def _resolve_refs(tree, index):
    def walk(value, family, path, field):
        if isinstance(value, dict):
            if "record_type" in value and "stable_id" in value:
                key = _ref_key(value, family, path, field)
                if key not in index:
                    _fail(family, path, "unresolved_typed_ref", field)
            else:
                for name, item in value.items():
                    walk(item, family, path, field + "." + name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, family, path, f"{field}[{i}]")

    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        walk(payload, family, path, "$")
        if family == "event" and payload["event_type"] not in bindings_current.BINDING_EVENT_TYPES:
            for i, (ref, after) in enumerate(zip(payload["changed_refs"], payload["after"])):
                key = _ref_key(ref, family, path, f"changed_refs[{i}]")
                target = tree[index[key]]
                expected = (ref["revision"] if type(ref.get("revision")) is int else
                            sha256(git_format.dumps_record(target)).hexdigest())
                if type(after) is not type(expected) or after != expected:
                    _fail(family, path, "event_after_mismatch", f"after[{i}]")


def _operation_closure(tree):
    inputs, operations, consumed, events = {}, {}, {}, {}
    events_by_operation = {}
    for path, payload in tree.items():
        family = git_format.canonical_record_type(path)
        if family == "input_candidate":
            inputs[(payload["candidate_uid"], payload["candidate_revision"])] = (path, payload)
        elif family == "operation":
            operations[payload["operation_id"]] = (path, payload)
        elif family == "consumed_candidate":
            consumed[(payload["candidate_uid"], payload["candidate_revision"])] = (path, payload)
        elif family == "event":
            events[payload["event_id"]] = (path, payload)
            events_by_operation.setdefault(payload["operation_id"], {})[payload["event_id"]] = payload
    owners = {}
    index = _target_index(tree)

    def referenced_facts(value, path):
        """Formal facts mentioned by a result (e.g. a support link endpoint).

        Referencing a fact is not a declaration that it was newly created.
        Do not force a support/conflict result to output the target fact.
        """
        found = set()
        if isinstance(value, dict):
            if value.get("record_type") == "fact_revision" and "stable_id" in value:
                found.add(_ref_key(value, "operation", path, "formal_fact_refs"))
            else:
                for item in value.values():
                    found.update(referenced_facts(item, path))
        elif isinstance(value, list):
            for item in value:
                found.update(referenced_facts(item, path))
        return found
    for operation_id, (path, operation) in operations.items():
        dispositions = {}
        for disposition in operation["disposition_result"]:
            key = (disposition["candidate_uid"], disposition["candidate_revision"])
            if key in dispositions:
                _fail("operation", path, "multiple_dispositions", "disposition_result")
            dispositions[key] = disposition
        listed = set()
        fact_outputs, output_refs = set(), set()
        for candidate in operation["candidate_refs"]:
            key = (candidate["candidate_uid"], candidate["candidate_revision"])
            if key in listed:
                _fail("operation", path, "duplicate_candidate", "candidate_refs")
            listed.add(key)
            if key in owners:
                _fail("operation", path, "multiple_operations", "candidate_refs")
            owners[key] = operation_id
            if key not in inputs:
                _fail("operation", path, "missing_input", "candidate_refs")
            input_record = inputs[key][1]
            if input_record["payload_digest"] != candidate["payload_digest"]:
                _fail("operation", path, "candidate_digest_mismatch", "candidate_refs")
            if key not in dispositions:
                _fail("operation", path, "missing_disposition", "disposition_result")
            disposition = dispositions[key]
            if disposition["payload_digest"] != candidate["payload_digest"]:
                _fail("operation", path, "disposition_digest_mismatch", "disposition_result")
            result = {k: disposition[k] for k in _RESULT}
            output_refs.update(_refs(result["result_refs"], "operation", path, "result_refs"))
            if result["terminal"]:
                if key not in consumed:
                    _fail("operation", path, "missing_consumed", "disposition_result")
                consumed_path, consumption = consumed[key]
                if consumption["operation_id"] != operation_id:
                    _fail("consumed_candidate", consumed_path, "consumed_operation_mismatch", "operation_id")
                if consumption["payload_digest"] != candidate["payload_digest"]:
                    _fail("consumed_candidate", consumed_path, "consumed_digest_mismatch", "payload_digest")
                if consumption["disposition_result"] != result:
                    _fail("consumed_candidate", consumed_path, "consumed_result_mismatch", "disposition_result")
            elif key in consumed:
                _fail("consumed_candidate", consumed[key][0], "nonterminal_consumed", "disposition_result")
            fact_outputs.update(_ref_key(ref, "operation", path, "result_refs")
                                for ref in result["result_refs"]
                                if ref["record_type"] == "fact_revision")
        if listed != set(dispositions):
            _fail("operation", path, "unlisted_disposition", "disposition_result")
        facts = set(_refs(operation["formal_fact_refs"], "operation", path, "formal_fact_refs"))
        referenced = set()
        for ref in output_refs:
            referenced.update(referenced_facts(tree[index[ref]], path))
        if facts != fact_outputs | referenced:
            _fail("operation", path, "formal_fact_output_mismatch", "formal_fact_refs")
        owned_events = events_by_operation.get(operation_id, {})
        if set(operation["event_ids"]) != set(owned_events):
            _fail("operation", path, "event_listing_mismatch", "event_ids")
        ordinals = sorted(event["ordinal"] for event in owned_events.values())
        if ordinals != list(range(len(owned_events))):
            _fail("operation", path, "event_ordinal_sequence", "event_ids")
        changed_refs, event_changes = set(), set()
        for event in owned_events.values():
            keys = _refs(event["changed_refs"], "operation", path, "changed_refs")
            changed_refs.update(keys)
            binding = event["event_type"] in bindings_current.BINDING_EVENT_TYPES
            for i, key in enumerate(keys):
                before = event["before"] if binding else event["before"][i]
                after = event["after"] if binding else event["after"][i]
                snapshot = git_format.dumps_record({"before": before, "after": after})
                change = (event["event_type"], key, snapshot)
                if change in event_changes:
                    _fail("event", events[event["event_id"]][0], "duplicate_event_change", "changed_refs")
                event_changes.add(change)
        if output_refs and changed_refs != output_refs:
            _fail("operation", path, "event_output_mismatch", "event_ids")
        for event_id, event in owned_events.items():
            if event["base_commit"] != operation["base_commit"]:
                _fail("event", events[event_id][0], "event_base_mismatch", "base_commit")
    for key, (path, _) in inputs.items():
        if key not in owners:
            _fail("input_candidate", path, "unowned_input", "candidate_uid")
    for key, (path, consumption) in consumed.items():
        if key not in inputs or owners.get(key) != consumption["operation_id"]:
            _fail("consumed_candidate", path, "orphan_consumed", "operation_id")
    for path, event in events.values():
        if event["operation_id"] not in operations:
            _fail("event", path, "orphan_event", "operation_id")


CLOSURE_VALIDATORS = MappingProxyType({1: _resolve_refs, 5: _operation_closure})


def validate_record_schema_closure(tree, *, repo_id, company_id,
                                   object_format="sha1"):
    """Validate strict four-family schemas, all typed refs, and operation closure.

    C is the supplied memory tree, including historical input/operation/
    consumed/output records. Duplicate paths cannot be represented by a
    dict; canonical path-to-identity equality also prevents identity aliases.
    Ordinals use the bounded zero-based run [0, event_count), independently
    of event_ids order, UUIDs or server time. Input objects are never changed.
    """
    if type(tree) is not dict:
        _fail("tree", "<tree>", "tree_type", "$")
    if object_format not in ("sha1", "sha256"):
        _fail("tree", "<tree>", "object_format", "object_format")
    for field, value in (("repo_id", repo_id), ("company_id", company_id)):
        _id(value, "tree", "<tree>", field)
    if any(type(path) is not str for path in tree):
        _fail("tree", "<invalid-path>", "record_path", "path")
    ordered = {path: tree[path] for path in sorted(tree)}
    for path, payload in ordered.items():
        validate_record_schema(path, payload, repo_id=repo_id,
                               company_id=company_id, object_format=object_format)
    CLOSURE_VALIDATORS[1](ordered, _target_index(ordered))
    CLOSURE_VALIDATORS[5](ordered)
    return None
