"""Closure 5 for the supplied in-memory tree."""
from ._primitives import (
    _RESULT, _fail, _ref_key, _refs, _target_index, bindings_current, git_format,
)
from ._registry import register_closure


@register_closure(5)
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
