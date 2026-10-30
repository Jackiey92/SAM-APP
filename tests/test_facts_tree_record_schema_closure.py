"""R1 mechanism fixtures are independent of business-family schemas.

Only the structural-result integration cases use business sample builders and
the full public schema/closure entry point. Mechanism tests keep the broker
schemas and common path/encoding boundary, then call closures 1/5 directly.
族卡注册样例后结构结果用例自动转为真实校验，最终须 0 skipped。
"""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256

import pytest

from app.facts_tree import git_format
from app.facts_tree.record_schema._primitives import _path_identity, _target_index
from app.facts_tree.record_schema._registry import R1_CLOSURE_FAMILIES
from app.facts_tree.record_schema_closure import (
    CLOSURE_VALIDATORS, SCHEMA_FIELDS, SCHEMA_VALIDATORS, SCHEMA_BUILDERS,
    RecordSchemaClosureError, loads_schema_record, validate_record_schema,
    validate_record_schema_closure,
)


SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
DIGEST = "a" * 64
BASE = "b" * 40

# Explicit closure-5 coverage targets; schema plugins supply all business fields.
STRUCTURAL_RESULT_TARGETS = (
    ("fact_revision", True), ("evidence", True), ("link_revision", True),
    ("placement_revision", True), ("issue_event", True),
    ("issue_event", False), ("proposal_event", False),
)


def record(family, **ids):
    return git_format.record_path(family, **ids), {
        "schema_version": 1, "format_version": git_format.FORMAT_VERSION,
        "record_type": family, **SCOPE, **ids,
    }


def typed(family, stable_id, revision=None):
    ref = {"record_type": family, "stable_id": stable_id}
    if revision is not None:
        ref["revision"] = revision
    return ref


def candidate():
    return {"candidate_uid": "candidate-一", "candidate_revision": 1,
            "payload_digest": DIGEST}


def actor():
    return {"schema_version": 1, "audit_ref_type": "user_actor",
            "installation_id": "install-test", "company_id": SCOPE["company_id"],
            "principal_id": "owner-test", "member_revision": 1,
            "session_ref": "session-audit-id"}


def synthetic_tree(result_type="fact_revision", terminal=True):
    if result_type in ("issue_event", "proposal_event"):
        id_name = "issue_uid" if result_type == "issue_event" else "proposal_uid"
        result_path, output = record(result_type, **{id_name: "output-1", "event_id": "output-event"})
        result_ref = typed(result_type, "output-1", "output-event")
    elif result_type == "evidence":
        result_path, output = record(result_type, evidence_id="output-1")
        result_ref = typed(result_type, "output-1")
    else:
        id_name = "link_id" if result_type == "link_revision" else "fact_uid"
        result_path, output = record(result_type, **{id_name: "output-1", "revision": 1})
        result_ref = typed(result_type, "output-1", 1)
    result = {"terminal": terminal, "result_refs": [result_ref]}
    input_path, input_record = record("input_candidate", candidate_uid="candidate-一", candidate_revision=1)
    input_record.update({
        "payload_digest": DIGEST,
        "whitelisted_fields": {"local_id": "local-1", "value_type": "number",
                               "statement_kind": "state", "polarity": "affirmed"},
        "envelope_summary": {"source_id": "source-1", "version_id": "v-1",
                             "mapped_fingerprint": DIGEST},
        "candidate_rule_version": "fact-candidate-1", "adapter_rule_version": "adapter-1",
        "validation_rule_version": "validation-1", "coverage_manifest_version": "coverage-1",
        "source_id": "source-1", "version_id": "v-1", "mapped_fingerprint": DIGEST,
        "payload_ref": DIGEST, "envelope_ref": "c" * 64,
    })
    operation_path, operation = record("operation", operation_id="operation-1")
    operation.update({
        "request_digest": DIGEST, "effect_digest": DIGEST, "request_id": "request-1",
        "base_commit": BASE, "candidate_refs": [candidate()],
        "coverage_id": "coverage-id", "coverage_digest": DIGEST,
        "coverage_validation_version": "coverage-check-1", "unit_id": "unit-1",
        "disposition_result": [{**candidate(), **deepcopy(result)}],
        "formal_fact_refs": [deepcopy(result_ref)] if result_type == "fact_revision" else [],
        "rule_version": "rules-1", "model_version": "model-1", "actor": actor(),
        "approval_use_refs": [], "event_ids": ["event-1"], "server_time": 1793318400,
    })
    consumed_path, consumed = record("consumed_candidate", candidate_uid="candidate-一", candidate_revision=1)
    consumed.update({"payload_digest": DIGEST, "operation_id": "operation-1",
                     "disposition_result": deepcopy(result)})
    event_path, event = record("event", event_id="event-1")
    after = 1 if type(result_ref.get("revision")) is int else sha256(git_format.dumps_record(output)).hexdigest()
    event.update({"operation_id": "operation-1", "base_commit": BASE, "ordinal": 0,
                  "event_type": "host-issued-event-kind", "changed_refs": [deepcopy(result_ref)],
                  "before": [None], "after": [after], "invalidation_domains": ["facts"],
                  "affected_refs": [deepcopy(result_ref)]})
    tree = {input_path: input_record, operation_path: operation,
            event_path: event, result_path: output}
    if terminal:
        tree[consumed_path] = consumed
    return tree, (input_path, operation_path, consumed_path, event_path, result_path)


def validate(tree, **kwargs):
    return validate_record_schema_closure(tree, **SCOPE, **kwargs)


def mechanism_tree(result_type="fact_revision", terminal=True):
    """Hand-built broker frame with schema-free, path-valid reference targets.

    Extra reference fields belong to closure probes, never to a business schema.
    Deliberately do not consult SCHEMA_BUILDERS for these mechanism records.
    """
    return synthetic_tree(result_type, terminal)


def validate_mechanism(tree, *, object_format="sha1"):
    """Check the R1 frame and common boundary, then invoke the real closures.

    Broker schemas remain strict (including nested results, actor and events).
    Other families contribute canonical identity/encoding and typed references;
    their business-field validation is exercised by the integration cases.
    """
    ordered = {path: tree[path] for path in sorted(tree)}
    for path, payload in ordered.items():
        family, _ = _path_identity(path, payload, **SCOPE)
        if family in R1_CLOSURE_FAMILIES:
            validate_record_schema(path, payload, **SCOPE, object_format=object_format)
    CLOSURE_VALIDATORS[1](ordered, _target_index(ordered))
    CLOSURE_VALIDATORS[5](ordered, _target_index(ordered))
    return None


def rejected(tree, code, family=None, path=None):
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_mechanism(tree)
    error = caught.value
    assert error.code == code
    assert error.path in tree or error.path.startswith("<")
    assert error.field
    if family:
        assert error.family == family
    if path:
        assert error.path == path
    assert error.code in str(error) and error.family in str(error) and error.path in str(error)
    return error


@pytest.mark.parametrize("family", R1_CLOSURE_FAMILIES)
def test_each_family_valid_standalone_and_in_closed_tree(family):
    tree, _ = mechanism_tree()
    path, payload = next((p, r) for p, r in tree.items() if r["record_type"] == family)
    if family in SCHEMA_BUILDERS:
        payload = SCHEMA_BUILDERS[family](**git_format._match_template(family, path))
        payload.update(SCOPE)
        tree[path] = payload
    assert validate_record_schema(path, payload, **SCOPE) is None
    assert validate_mechanism(tree) is None
    assert set(payload) == SCHEMA_FIELDS[family]


@pytest.mark.parametrize("result_type,terminal", [
    pytest.param(family, terminal, marks=(
        () if family in SCHEMA_BUILDERS else
        pytest.mark.skip(reason=f"{family} family sample pending")))
    for family, terminal in STRUCTURAL_RESULT_TARGETS
])
def test_structural_results_new_support_revision_placement_conflict_hold_propose(result_type, terminal):
    tree, paths = synthetic_tree(result_type, terminal)
    result_path = paths[4]
    output = SCHEMA_BUILDERS[result_type](**git_format._match_template(result_type, result_path))
    output.update(SCOPE)
    tree[result_path] = output
    event = tree[paths[3]]
    result_ref = event["changed_refs"][0]
    event["after"] = [result_ref["revision"] if type(result_ref.get("revision")) is int
                      else sha256(git_format.dumps_record(output)).hexdigest()]
    assert validate(tree) is None


def test_empty_tree_and_registration_boundaries():
    assert validate({}) is None
    assert set(SCHEMA_VALIDATORS) == set(SCHEMA_FIELDS)
    assert {1, 5} <= set(CLOSURE_VALIDATORS)
    assert set(SCHEMA_FIELDS) < set(git_format.RECORD_TYPES)
    with pytest.raises(TypeError):
        SCHEMA_FIELDS["fact_revision"] = frozenset()


@pytest.mark.parametrize("family", R1_CLOSURE_FAMILIES)
@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 0), ("schema_version", -1),
    ("schema_version", "1"), ("schema_version", Decimal("1")),
    ("format_version", True), ("format_version", 0), ("format_version", -1),
    ("format_version", "1"), ("format_version", Decimal("1")),
])
def test_exact_positive_versions(family, field, value):
    tree, _ = mechanism_tree()
    path = next(p for p, r in tree.items() if r["record_type"] == family)
    tree[path][field] = value
    rejected(tree, "positive_integer", family, path)


@pytest.mark.parametrize("family", R1_CLOSURE_FAMILIES)
@pytest.mark.parametrize("field", ["schema_version", "format_version"])
def test_unknown_versions(family, field):
    tree, _ = mechanism_tree()
    path = next(p for p, r in tree.items() if r["record_type"] == family)
    tree[path][field] = 2
    rejected(tree, "unsupported_version", family, path)


@pytest.mark.parametrize("family", R1_CLOSURE_FAMILIES)
@pytest.mark.parametrize("missing", [False, True])
def test_exact_field_set(family, missing):
    tree, _ = mechanism_tree()
    path = next(p for p, r in tree.items() if r["record_type"] == family)
    if missing:
        del tree[path]["schema_version"]
    else:
        tree[path]["unknown"] = "blocked"
    rejected(tree, "missing_field" if missing else "unknown_field", family, path)


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("scope", ["repo_id", "company_id"])
def test_all_tree_records_share_scope(index, scope):
    tree, paths = mechanism_tree()
    tree[paths[index]][scope] = "foreign"
    rejected(tree, "scope_mismatch", path=paths[index])


@pytest.mark.parametrize("value", ["", None, True, "x" * (git_format.MAX_ID_CHARS + 1), "\ud800"])
def test_invalid_scope(value):
    tree, paths = mechanism_tree()
    tree[paths[0]]["repo_id"] = value
    rejected(tree, "record_encoding" if value == "\ud800" else "invalid_id")


@pytest.mark.parametrize("index,field", [(0, "candidate_uid"), (1, "operation_id"),
                                       (2, "candidate_revision"), (3, "event_id"), (4, "fact_uid")])
def test_exact_path_payload_identity(index, field):
    tree, paths = mechanism_tree()
    tree[paths[index]][field] = 2 if field == "candidate_revision" else "different-id"
    rejected(tree, "path_identity", path=paths[index])


@pytest.mark.parametrize("bad_path", ["../operations/operation-1.json", "operations//operation-1.json",
                                     "operations/~6Fperation-1.json", "operations/operation-1.json/extra"])
def test_path_aliases_and_prefixes_rejected(bad_path):
    tree, paths = mechanism_tree()
    tree[bad_path] = tree.pop(paths[1])
    rejected(tree, "record_path", "tree", bad_path)


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), Decimal("NaN"),
                                   Decimal("Infinity"), Decimal("0E-999999")])
def test_number_encoding_rejected_anywhere(value):
    tree, paths = mechanism_tree()
    path = paths[1]
    tree[path]["server_time"] = value
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema(path, tree[path], **SCOPE)
    assert caught.value.code == "record_encoding"
    assert caught.value.family == "operation" and caught.value.path == path
    assert caught.value.field == "$"


def test_exact_decimal_and_original_text_preserved_without_mutation():
    # This is a codec guarantee, not a claim about any business field set.
    payload = {"value_raw": Decimal("12345678901234567890.00100"),
               "statement": "尚未收款\n原文"}
    original = deepcopy(payload)
    raw = git_format.dumps_record(payload)
    assert payload == original
    assert b"12345678901234567890.00100" in raw
    assert git_format.loads_record(raw) == payload


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}\n', b'{"a":{"b":1,"b":2}}\n',
                                 b'{"a":NaN}\n', b'{"a":Infinity}\n', b'{"a":"\xff"}\n',
                                 b'{"a":', b'[]\n', b'{"a":"\\ud800"}\n'])
def test_raw_boundary_duplicate_keys_nonfinite_invalid_json(raw):
    path = git_format.record_path("event", event_id="event-1")
    with pytest.raises(RecordSchemaClosureError) as caught:
        loads_schema_record(raw, path=path, **SCOPE)
    assert caught.value.code == "record_encoding"
    assert caught.value.family == "event" and caught.value.path == path


@pytest.mark.parametrize("rewrite", [lambda b: b.replace(b"\n", b"\r\n"), lambda b: b.rstrip(),
                                     lambda b: b" " + b])
def test_raw_boundary_canonical_lf_sorting(rewrite):
    tree, paths = mechanism_tree()
    raw = git_format.dumps_record(tree[paths[0]])
    with pytest.raises(RecordSchemaClosureError) as caught:
        loads_schema_record(rewrite(raw), path=paths[0], **SCOPE)
    assert caught.value.code == "noncanonical_encoding"


def test_text_parser_valid_utf8_sorted_keys_and_decimal():
    tree, paths = mechanism_tree()
    payload = tree[paths[0]]
    raw = git_format.dumps_record(payload)
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r" not in raw and "candidate-一" in raw.decode()
    assert loads_schema_record(raw.decode(), path=paths[0], **SCOPE) == payload


@pytest.mark.parametrize("revision", [None, "HEAD", "current", "latest", True, 0, -1, "1"])
def test_missing_unstable_or_wrong_revision_rejected(revision):
    tree, paths = mechanism_tree()
    ref = tree[paths[1]]["disposition_result"][0]["result_refs"][0]
    if revision is None:
        del ref["revision"]
    else:
        ref["revision"] = revision
    code = ("unstable_history_ref" if revision in ("HEAD", "current", "latest") else
            "positive_integer" if type(revision) is int and revision <= 0 else
            "invalid_id" if revision is True else "unresolved_typed_ref")
    rejected(tree, code)


@pytest.mark.parametrize("change,code", [
    ({"stable_id": "missing"}, "unresolved_typed_ref"),
    ({"record_type": "entity_revision"}, "terminal_result_type"),
    ({"record_type": "made_up"}, "typed_ref_type"),
    ({"repo_id": "foreign"}, "unknown_field"),
    ({"company_id": "foreign"}, "unknown_field"),
    ({"path": "facts/output-1/revisions/1.json"}, "unknown_field"),
])
def test_dangling_wrong_type_cross_domain_and_model_paths(change, code):
    tree, paths = mechanism_tree()
    tree[paths[1]]["disposition_result"][0]["result_refs"][0].update(change)
    rejected(tree, code)


def test_every_typed_ref_in_out_of_scope_target_is_resolved():
    tree, paths = mechanism_tree()
    tree[paths[4]]["source_ref"] = typed("source_version", "missing", "v1")
    rejected(tree, "unresolved_typed_ref", "fact_revision", paths[4])


def test_opaque_source_and_issue_revisions_resolve_without_numeric_coercion():
    tree, paths = mechanism_tree()
    source_path, source = record("source_version", source_id="source/一", version_id="v01")
    tree[source_path] = source
    tree[paths[4]]["source_ref"] = typed("source_version", "source/一", "v01")
    assert validate_mechanism(tree) is None


def test_mutable_current_cannot_replace_precise_history():
    tree, paths = mechanism_tree()
    current_path, current = record("fact_current", fact_uid="output-1")
    tree[current_path] = current
    tree[paths[4]]["history_ref"] = typed("fact_current", "output-1")
    rejected(tree, "unresolved_typed_ref", "fact_revision", paths[4])


def test_candidate_manifest_and_disposition_digests_close():
    tree, paths = mechanism_tree()
    tree[paths[1]]["candidate_refs"][0]["payload_digest"] = "c" * 64
    rejected(tree, "candidate_digest_mismatch", "operation", paths[1])


def test_duplicate_primary_disposition():
    tree, paths = mechanism_tree()
    tree[paths[1]]["disposition_result"] *= 2
    rejected(tree, "multiple_dispositions", "operation", paths[1])


@pytest.mark.parametrize("field,code", [("candidate_refs", "duplicate_candidate"),
                                        ("disposition_result", "multiple_dispositions")])
def test_duplicate_candidate_structures(field, code):
    tree, paths = mechanism_tree()
    tree[paths[1]][field].append(deepcopy(tree[paths[1]][field][0]))
    rejected(tree, code, "operation", paths[1])


@pytest.mark.parametrize("where", ["operation", "consumed_candidate"])
def test_terminal_consumption_requires_nonempty_result_refs(where):
    tree, paths = mechanism_tree()
    index = 1 if where == "operation" else 2
    result = tree[paths[index]]["disposition_result"]
    (result[0] if where == "operation" else result)["result_refs"] = []
    rejected(tree, "missing_result_ref", where, paths[index])


def test_consumed_result_cannot_be_forged_even_if_target_exists():
    tree, paths = mechanism_tree()
    other_path, other = record("fact_revision", fact_uid="other", revision=1)
    tree[other_path] = other
    tree[paths[2]]["disposition_result"]["result_refs"] = [typed("fact_revision", "other", 1)]
    rejected(tree, "consumed_result_mismatch", "consumed_candidate", paths[2])


def test_missing_output_is_rejected_before_consumption_acceptance():
    tree, paths = mechanism_tree()
    del tree[paths[4]]
    rejected(tree, "unresolved_typed_ref")


@pytest.mark.parametrize("in_formal", [False, True])
def test_conflict_cannot_claim_fact_output(in_formal):
    tree, paths = mechanism_tree("issue_event", True)
    fact_path, fact = record("fact_revision", fact_uid="new-fact", revision=1)
    tree[fact_path] = fact
    ref = typed("fact_revision", "new-fact", 1)
    if in_formal:
        tree[paths[1]]["formal_fact_refs"] = [ref]
    else:
        tree[paths[1]]["disposition_result"][0]["result_refs"].append(ref)
    rejected(tree, "formal_fact_output_mismatch" if in_formal else "conflict_fact_output")


@pytest.mark.parametrize("result_type", ["issue_event", "proposal_event"])
def test_hold_and_proposal_forbid_terminal_consumed(result_type):
    tree, paths = mechanism_tree(result_type, False)
    consumed_path, consumed = record("consumed_candidate", candidate_uid="candidate-一", candidate_revision=1)
    consumed.update({"payload_digest": DIGEST, "operation_id": "operation-1",
                     "disposition_result": {"terminal": False,
                         "result_refs": deepcopy(tree[paths[1]]["disposition_result"][0]["result_refs"])}})
    tree[consumed_path] = consumed
    rejected(tree, "nonterminal_consumed", "consumed_candidate", consumed_path)


def test_nonterminal_operation_cannot_pair_with_terminal_consumed():
    tree, paths = mechanism_tree("issue_event", True)
    tree[paths[1]]["disposition_result"][0]["terminal"] = False
    rejected(tree, "nonterminal_consumed", "consumed_candidate", paths[2])


@pytest.mark.parametrize("terminal", [True, False])
def test_same_candidate_cannot_belong_to_multiple_operations(terminal):
    tree, paths = mechanism_tree("issue_event", terminal)
    other_path = git_format.record_path("operation", operation_id="operation-2")
    other = deepcopy(tree[paths[1]])
    other.update(operation_id="operation-2", event_ids=[])
    tree[other_path] = other
    rejected(tree, "multiple_operations", "operation", other_path)


def test_duplicate_consumption_under_other_path_fails_identity():
    tree, paths = mechanism_tree()
    extra = git_format.record_path("consumed_candidate", candidate_uid="other", candidate_revision=1)
    tree[extra] = deepcopy(tree[paths[2]])
    rejected(tree, "path_identity", "consumed_candidate", extra)


@pytest.mark.parametrize("mutate,code", [
    (lambda t, p: t.pop(p[2]), "missing_consumed"),
    (lambda t, p: t[p[1]].update(disposition_result=[]), "missing_disposition"),
    (lambda t, p: t[p[2]].update(payload_digest="c" * 64), "consumed_digest_mismatch"),
    (lambda t, p: t[p[2]].update(operation_id="missing"), "consumed_operation_mismatch"),
    (lambda t, p: t[p[1]]["disposition_result"][0].update(payload_digest="c" * 64), "disposition_digest_mismatch"),
])
def test_candidate_closure_missing_and_disagreement(mutate, code):
    tree, paths = mechanism_tree()
    mutate(tree, paths)
    rejected(tree, code)


@pytest.mark.parametrize("ordinal", [True, -1, "0", Decimal("0"), 0.0])
def test_event_ordinal_exact_nonnegative_integer(ordinal):
    tree, paths = mechanism_tree()
    tree[paths[3]]["ordinal"] = ordinal
    rejected(tree, "record_encoding" if type(ordinal) is float else "event_ordinal", "event", paths[3])


def add_event(tree, paths, ordinal=1):
    new_path = git_format.record_path("event", event_id="event-2")
    event = deepcopy(tree[paths[3]])
    event.update(event_id="event-2", ordinal=ordinal)
    evidence_path, evidence = record("evidence", evidence_id="second-output")
    tree[evidence_path] = evidence
    ref = typed("evidence", "second-output")
    event.update(changed_refs=[deepcopy(ref)], before=[None],
                 after=[sha256(git_format.dumps_record(evidence)).hexdigest()])
    tree[paths[1]]["disposition_result"][0]["result_refs"].append(deepcopy(ref))
    tree[paths[2]]["disposition_result"]["result_refs"].append(deepcopy(ref))
    tree[new_path] = event
    tree[paths[1]]["event_ids"].append("event-2")
    return new_path


@pytest.mark.parametrize("ordinal", [0, 2, 999999999999999999999999])
def test_event_ordinals_unique_contiguous_and_bounded(ordinal):
    tree, paths = mechanism_tree()
    add_event(tree, paths, ordinal)
    rejected(tree, "event_ordinal_sequence", "operation", paths[1])


def test_event_order_does_not_depend_on_listing_or_tree_order():
    tree, paths = mechanism_tree()
    add_event(tree, paths)
    tree[paths[1]]["event_ids"].reverse()
    assert validate_mechanism(dict(reversed(list(tree.items())))) is None


@pytest.mark.parametrize("field", ["accepted_commit", "accepted_commit_oid", "commit_oid"])
def test_event_must_not_embed_its_accepting_commit(field):
    tree, paths = mechanism_tree()
    tree[paths[3]][field] = BASE
    rejected(tree, "unknown_field", "event", paths[3])


@pytest.mark.parametrize("event_ids", [[], ["missing"], ["event-1", "event-1"], ["event-1", "extra"]])
def test_operation_event_listing_exact(event_ids):
    tree, paths = mechanism_tree()
    tree[paths[1]]["event_ids"] = event_ids
    rejected(tree, "duplicate_event_id" if len(event_ids) != len(set(event_ids)) else "event_listing_mismatch")


def test_event_base_commit_matches_operation():
    tree, paths = mechanism_tree()
    tree[paths[3]]["base_commit"] = "c" * 40
    rejected(tree, "event_base_mismatch", "event", paths[3])


@pytest.mark.parametrize("object_format,length", [("sha1", 40), ("sha256", 64)])
def test_object_format_full_oid_and_genesis_zero(object_format, length):
    tree, paths = mechanism_tree()
    for index in (1, 3):
        tree[paths[index]]["base_commit"] = "0" * length
    assert validate_mechanism(tree, object_format=object_format) is None
    tree[paths[1]]["base_commit"] = "HEAD"
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_mechanism(tree, object_format=object_format)
    assert caught.value.code == "base_commit_oid"


def test_actor_audit_reference_is_external_and_company_bound():
    tree, paths = mechanism_tree()
    tree[paths[1]]["actor"]["company_id"] = "foreign"
    rejected(tree, "audit_scope", "operation", paths[1])


@pytest.mark.parametrize("change", [{"approval_id": "fake"}, {"session_secret": "DO-NOT-PRINT"},
                                    {"member_revision": True}, {"audit_ref_type": "approval"}])
def test_actor_reuses_strict_host_schema_and_redacts_payload(change):
    tree, paths = mechanism_tree()
    tree[paths[1]]["actor"].update(change)
    error = rejected(tree, "actor_audit_ref", "operation", paths[1])
    assert "DO-NOT-PRINT" not in str(error)


def test_approval_use_ref_fails_closed_until_host_publishes_schema():
    tree, paths = mechanism_tree()
    tree[paths[1]]["approval_use_refs"] = [{"approval_id": "unverified"}]
    rejected(tree, "unsupported_approval_audit_ref", "operation", paths[1])


@pytest.mark.parametrize("field", ["payload_digest", "mapped_fingerprint", "payload_ref", "envelope_ref"])
def test_input_digest_algorithms_are_explicit_sha256(field):
    tree, paths = mechanism_tree()
    tree[paths[0]][field] = "a" * 40
    rejected(tree, "sha256_digest", "input_candidate", paths[0])


def test_input_safe_summary_and_whitelist_reject_credential_fields():
    tree, paths = mechanism_tree()
    tree[paths[0]]["envelope_summary"]["session_token"] = "DO-NOT-PRINT"
    error = rejected(tree, "unknown_field", "input_candidate", paths[0])
    assert "DO-NOT-PRINT" not in str(error)
    del tree[paths[0]]["envelope_summary"]["session_token"]
    tree[paths[0]]["whitelisted_fields"]["raw_payload"] = "DO-NOT-PRINT"
    rejected(tree, "unknown_field", "input_candidate", paths[0])


@pytest.mark.parametrize("field", ["source_id", "version_id", "mapped_fingerprint"])
def test_input_summary_binds_source_and_mapping(field):
    tree, paths = mechanism_tree()
    tree[paths[0]]["envelope_summary"][field] = "other"
    rejected(tree, "envelope_binding", "input_candidate", paths[0])


def test_serialization_byte_and_depth_budgets():
    tree, paths = mechanism_tree()
    path = paths[1]
    payload = tree[path]
    payload["server_time"] = "x" * git_format.MAX_RECORD_BYTES
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema(path, payload, **SCOPE)
    assert (caught.value.code, caught.value.family, caught.value.path,
            caught.value.field) == ("record_encoding", "operation", path, "$")
    nested = {}
    payload["server_time"] = nested
    for _ in range(git_format.MAX_DEPTH):
        nested["next"] = {}
        nested = nested["next"]
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema(path, payload, **SCOPE)
    assert (caught.value.code, caught.value.family, caught.value.path,
            caught.value.field) == ("record_encoding", "operation", path, "$")


@pytest.mark.parametrize("tree", [None, [], "tree", {None: {}}, {"events/event.json": None}])
def test_malformed_inputs_never_escape_as_bare_errors(tree):
    with pytest.raises(RecordSchemaClosureError):
        validate(tree)


def test_no_git_broker_database_network_or_model_io(monkeypatch):
    import builtins
    import socket
    import sqlite3
    import subprocess

    tree, _ = mechanism_tree()
    def forbidden(*args, **kwargs):
        pytest.fail("pure validator attempted IO")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original = deepcopy(tree)
    assert validate_mechanism(tree) is None
    assert tree == original


@pytest.mark.parametrize("after", [2, "d" * 64, None])
def test_event_after_matches_exact_target_revision(after):
    tree, paths = mechanism_tree()
    tree[paths[3]]["after"] = [after]
    rejected(tree, "empty_event_change" if after is None else "event_after_mismatch", "event", paths[3])


def test_event_after_matches_immutable_target_digest():
    tree, paths = mechanism_tree("evidence")
    tree[paths[3]]["after"] = ["d" * 64]
    rejected(tree, "event_after_mismatch", "event", paths[3])


@pytest.mark.parametrize("before,after", [(None, None), (1, 1)])
def test_each_changed_ref_describes_a_nonempty_change(before, after):
    tree, paths = mechanism_tree()
    tree[paths[3]].update(before=[before], after=[after])
    rejected(tree, "empty_event_change", "event", paths[3])


def test_real_output_requires_its_operation_event():
    tree, paths = mechanism_tree()
    del tree[paths[3]]
    tree[paths[1]]["event_ids"] = []
    rejected(tree, "event_output_mismatch", "operation", paths[1])


def test_unrelated_changed_object_cannot_cover_real_output():
    tree, paths = mechanism_tree()
    other_path, other = record("fact_revision", fact_uid="unrelated", revision=1)
    tree[other_path] = other
    tree[paths[3]]["changed_refs"] = [typed("fact_revision", "unrelated", 1)]
    rejected(tree, "event_output_mismatch", "operation", paths[1])


@pytest.mark.parametrize("result_type,terminal", [("link_revision", True), ("issue_event", True),
                                                 ("proposal_event", False)])
def test_support_conflict_and_proposal_reference_existing_formal_fact(result_type, terminal):
    tree, paths = mechanism_tree(result_type, terminal)
    target_path, target = record("fact_revision", fact_uid="existing-fact", revision=3)
    tree[target_path] = target
    target_ref = typed("fact_revision", "existing-fact", 3)
    tree[paths[4]]["target_ref"] = target_ref
    tree[paths[1]]["formal_fact_refs"] = [deepcopy(target_ref)]
    if type(tree[paths[3]]["changed_refs"][0].get("revision")) is not int:
        tree[paths[3]]["after"] = [sha256(git_format.dumps_record(tree[paths[4]])).hexdigest()]
    assert validate_mechanism(tree) is None


def test_frozen_repository_and_taxonomy_wrappers_are_not_given_new_fields():
    tree, _ = mechanism_tree()
    meta_path = git_format.record_path("repository_meta")
    tree[meta_path] = {"schema_version": 1, "format_version": 1,
                       "installation_id": "install-test", **SCOPE}
    manifest_path = git_format.record_path("taxonomy_manifest", pack_version="opaque-pack-path")
    tree[manifest_path] = {"schema_version": 1, "format_version": 1,
                          "pack": "business-pack", "version": 7, "manifest_sha256": DIGEST}
    leaf_path = git_format.record_path("taxonomy_leaf", pack_version="opaque-pack-path", leaf_id="leaf-1")
    tree[leaf_path] = {"schema_version": 1, "format_version": 1, "pack": "business-pack",
                      "version": 7, "code": "leaf-1", "trunk": "trunk-1", "title": "标题", "scope": "范围"}
    before = deepcopy(tree)
    assert validate_mechanism(tree) is None
    assert tree == before


def binding_tree():
    tree, paths = mechanism_tree()
    del tree[paths[0]], tree[paths[2]], tree[paths[4]]
    tree[paths[1]].update(candidate_refs=[], disposition_result=[], formal_fact_refs=[])
    snapshot = {"schema_version": 1, "format_version": 1, "record_type": "bindings_current",
                **SCOPE, "template_pack": "generic", "template_version": 1,
                "taxonomy_path_id": "opaque-pack-id", "manifest_sha256": DIGEST,
                "binding_revision": 1}
    ref = typed("bindings_current", "current", 1)
    tree[paths[3]].update(event_type="binding_initialized", before=None,
                          after=snapshot, changed_refs=[ref], affected_refs=[deepcopy(ref)],
                          actor=actor(), reason="synthetic-installation", approval_use_refs=[])
    current_path = git_format.record_path("bindings_current")
    tree[current_path] = {**snapshot, "change_event_ref": typed("event", "event-1")}
    return tree, paths, current_path


def test_binding_event_preserves_frozen_snapshot_and_exact_logical_ref():
    tree, _, _ = binding_tree()
    assert validate_mechanism(tree) is None


def test_binding_logical_reference_requires_exact_positive_revision():
    tree, paths, _ = binding_tree()
    tree[paths[3]]["affected_refs"][0]["revision"] = 2
    rejected(tree, "unresolved_typed_ref", "event", paths[3])


@pytest.mark.parametrize("field", ["actor", "reason", "approval_use_refs"])
def test_binding_event_additions_are_required_only_in_binding_subtype(field):
    tree, paths, _ = binding_tree()
    del tree[paths[3]][field]
    rejected(tree, "missing_field", "event", paths[3])


def test_missing_binding_history_is_not_resolved_through_mutable_current():
    tree, paths, current = binding_tree()
    del tree[paths[3]]
    tree[paths[1]]["event_ids"] = []
    rejected(tree, "unresolved_typed_ref", "bindings_current", current)


@pytest.mark.parametrize("family", R1_CLOSURE_FAMILIES)
def test_record_type_label_cannot_disagree_with_path(family):
    tree, _ = mechanism_tree()
    path = next(p for p, payload in tree.items() if payload["record_type"] == family)
    tree[path]["record_type"] = "fact_revision"
    rejected(tree, "record_type", family, path)


class IntSubclass(int):
    pass


@pytest.mark.parametrize("field", ["schema_version", "format_version", "candidate_revision"])
def test_integer_subclasses_never_pass_loose_numeric_equality(field):
    tree, paths = mechanism_tree()
    tree[paths[0]][field] = IntSubclass(1)
    rejected(tree, "path_identity" if field == "candidate_revision" else "positive_integer")


@pytest.mark.parametrize("mutate,code", [
    (lambda t, p: t[p[1]].update(candidate_refs={}), "array_type"),
    (lambda t, p: t[p[1]].update(disposition_result=None), "array_type"),
    (lambda t, p: t[p[1]]["disposition_result"][0].update(terminal=1), "terminal_boolean"),
    (lambda t, p: t[p[1]]["disposition_result"][0].update(unknown=1), "unknown_field"),
    (lambda t, p: t[p[2]]["disposition_result"].pop("result_refs"), "missing_field"),
    (lambda t, p: t[p[3]].update(changed_refs=[]), "missing_changed_ref"),
    (lambda t, p: t[p[3]].update(before=[]), "event_snapshot_count"),
    (lambda t, p: t[p[3]].update(event_type=""), "invalid_id"),
])
def test_nested_schemas_fail_closed_with_structured_diagnostics(mutate, code):
    tree, paths = mechanism_tree()
    mutate(tree, paths)
    rejected(tree, code)


def test_several_candidates_share_one_operation_with_one_disposition_each():
    tree, paths = mechanism_tree()
    new_input_path = git_format.record_path("input_candidate", candidate_uid="candidate-2", candidate_revision=1)
    new_input = deepcopy(tree[paths[0]])
    new_input["candidate_uid"] = "candidate-2"
    tree[new_input_path] = new_input
    new_consumed_path = git_format.record_path("consumed_candidate", candidate_uid="candidate-2", candidate_revision=1)
    new_consumed = deepcopy(tree[paths[2]])
    new_consumed["candidate_uid"] = "candidate-2"
    tree[new_consumed_path] = new_consumed
    new_candidate = {**candidate(), "candidate_uid": "candidate-2"}
    tree[paths[1]]["candidate_refs"].append(new_candidate)
    tree[paths[1]]["disposition_result"].append({**new_candidate, **deepcopy(new_consumed["disposition_result"])})
    assert validate_mechanism(tree) is None


def test_nonterminal_cannot_encode_hold_and_proposal_as_two_primary_types():
    tree, paths = mechanism_tree("issue_event", False)
    proposal_path, proposal = record("proposal_event", proposal_uid="proposal-1", event_id="proposal-event")
    tree[proposal_path] = proposal
    tree[paths[1]]["disposition_result"][0]["result_refs"].append(
        typed("proposal_event", "proposal-1", "proposal-event"))
    rejected(tree, "multiple_primary_result_types", "operation", paths[1])


def test_terminal_conflict_can_also_record_evidence_without_fact_output():
    tree, paths = mechanism_tree("issue_event", True)
    evidence_path, evidence = record("evidence", evidence_id="conflict-evidence")
    tree[evidence_path] = evidence
    ref = typed("evidence", "conflict-evidence")
    tree[paths[1]]["disposition_result"][0]["result_refs"].append(deepcopy(ref))
    tree[paths[2]]["disposition_result"]["result_refs"].append(deepcopy(ref))
    tree[paths[3]]["changed_refs"].append(deepcopy(ref))
    tree[paths[3]]["before"].append(None)
    tree[paths[3]]["after"].append(sha256(git_format.dumps_record(evidence)).hexdigest())
    assert validate_mechanism(tree) is None


def test_duplicate_event_cannot_describe_same_change_with_a_new_id():
    tree, paths = mechanism_tree()
    new_path = git_format.record_path("event", event_id="event-2")
    tree[new_path] = {**deepcopy(tree[paths[3]]), "event_id": "event-2", "ordinal": 1}
    tree[paths[1]]["event_ids"].append("event-2")
    rejected(tree, "duplicate_event_change", "event", new_path)


def test_binding_reason_is_explanation_text_not_an_opaque_id():
    tree, paths, _ = binding_tree()
    tree[paths[3]]["reason"] = "绑定原因。" * 100
    assert validate_mechanism(tree) is None


# Registry discovery probes replay this R1 module with deliberately minimal
# business plugins. Full-family integration cases require the real field schemas;
# do not collect them in those probes or change their frozen pass/skip counts.
if all(family in SCHEMA_BUILDERS and field in SCHEMA_FIELDS.get(family, ())
       for family, field in (
           ("entity_revision", "raw_name"), ("entity_current", "status"),
           ("fact_revision", "qualifiers"), ("fact_current", "status"),
           ("source_version", "content_fingerprint"), ("source_lifecycle", "version_scope"),
           ("evidence", "quote_excerpt_ref"), ("link_revision", "link"),
           ("placement_revision", "fact_ref"), ("placement_current", "revision"),
           ("issue_event", "disposition_result"), ("proposal_event", "input_impact"),
       )):
    def test_full_family_tree_passes_schema_and_registered_closures():
        from tests.facts_tree_full_fixture import full_tree

        tree = full_tree()
        original = deepcopy(tree)
        business_families = {
            "entity_revision", "entity_current", "fact_revision", "fact_current",
            "source_version", "source_lifecycle", "evidence", "link_revision",
            "placement_revision", "placement_current", "issue_event", "proposal_event",
        }
        assert {record["record_type"] for record in tree.values()} == (
            business_families | set(R1_CLOSURE_FAMILIES))
        assert len(tree) == 18  # Sixteen families plus the entity/fact second revisions.
        assert validate_record_schema_closure(tree, **SCOPE) is None
        assert tree == original
        for revision in (1, 2):
            fact = tree[git_format.record_path("fact_revision", fact_uid="fact-1", revision=revision)]
            assert fact["entity_ref"] == typed("entity_revision", "entity-1", 1)
        for family, ids in (("entity_current", {"entity_uid": "entity-1"}),
                            ("fact_current", {"fact_uid": "fact-1"})):
            current = tree[git_format.record_path(family, **ids)]
            assert current["revision"] == 2
            assert current["status"] == "active"
        placement = tree[git_format.record_path("placement_current", fact_uid="fact-1")]
        assert set(placement) == {
            "schema_version", "format_version", "record_type", "repo_id", "company_id",
            "fact_uid", "revision",
        }
        assert full_tree() == original


    def test_dangling_current_example_preserves_original_and_single_record_schema():
        from tests.facts_tree_full_fixture import full_tree, with_dangling_fact_current

        tree = full_tree()
        original = deepcopy(tree)
        mutated = with_dangling_fact_current(tree)
        path = git_format.record_path("fact_current", fact_uid="fact-1")
        revision = mutated[path]["revision"]
        assert git_format.record_path("fact_revision", fact_uid="fact-1", revision=revision) not in mutated
        assert tree == original
        assert mutated != tree
        # A dangling integer pointer is schema-valid; closure 2 will resolve it.
        assert validate_record_schema(path, mutated[path], **SCOPE) is None
