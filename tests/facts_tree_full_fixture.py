"""Pure full-family tree for appendix B v1.1 §2 (SAM-DOCS 7928e55).

Business payloads come from registered samples. The R1 five-node broker frame
uses the terminal issue as its output; the other business records are independent
records in the same fixed tree. No closure 2/3/4 rules are implemented here.
"""
from copy import deepcopy
from hashlib import sha256

from app.facts_tree import git_format
from app.facts_tree.record_schema import SCHEMA_BUILDERS


SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}
DIGEST = "a" * 64


def _ref(family, stable_id, revision=None):
    value = {"record_type": family, "stable_id": stable_id}
    if revision is not None:
        value["revision"] = revision
    return value


def full_tree():
    """Return a fresh 12-business-family + four-broker-family tree."""
    tree = {}

    def add(family, **ids):
        payload = SCHEMA_BUILDERS[family](**ids)
        payload.update(SCOPE)
        tree[git_format.record_path(family, **ids)] = payload
        return payload

    entity_ref = _ref("entity_revision", "entity-1", 1)
    evidence_ref = _ref("evidence", "evidence-1")
    source_ref = _ref("source_version", "src-1", "opaque-version")
    fact_ref = _ref("fact_revision", "fact-1", 1)

    add("entity_revision", entity_uid="entity-1", revision=1)
    add("entity_revision", entity_uid="entity-1", revision=2).update(
        predecessor_ref=deepcopy(entity_ref), status="active")
    add("entity_current", entity_uid="entity-1").update(revision=2, status="active")

    for revision in (1, 2):
        fact = add("fact_revision", fact_uid="fact-1", revision=revision)
        fact.update(entity_ref=deepcopy(entity_ref),
                    primary_evidence_refs=[deepcopy(evidence_ref)])
        fact["qualifiers"]["entity"].update(
            status="known", text="示例公司", value="entity-1",
            evidence_refs=[deepcopy(evidence_ref)])
        if revision == 2:
            fact.update(predecessor_ref=deepcopy(fact_ref), revision_kind="supersede")
    add("fact_current", fact_uid="fact-1").update(revision=2, status="active")

    source = add("source_version", source_id="src-1", version_id="opaque-version")
    source.update(content_fingerprint=DIGEST, fingerprint_algorithm="sha256")
    lifecycle = add("source_lifecycle", source_id="src-1", revision=1)
    lifecycle["version_scope"]["version_ref"] = deepcopy(source_ref)
    add("evidence", evidence_id="evidence-1").update(
        source_version_ref=deepcopy(source_ref), mapped_fingerprint=DIGEST,
        # A bare ref may point at this in-tree immutable synthetic quote target.
        # This does not assert external-object availability (closure 6).
        quote_excerpt_ref=deepcopy(source_ref),
        quote_digest=sha256(git_format.dumps_record(source)).hexdigest())

    add("link_revision", link_id="link-1", revision=1).update(
        link={"subtype": "fact_evidence", "role": "supports",
              "fact_ref": deepcopy(fact_ref), "statement_or_field": "statement",
              "field": None}, evidence_refs=[deepcopy(evidence_ref)])
    add("placement_revision", fact_uid="fact-1", revision=1).update(
        fact_ref=_ref("fact_revision", "fact-1", 2))
    add("placement_current", fact_uid="fact-1")

    issue_ref = _ref("issue_event", "issue-1", "issue-event-1")
    issue = add("issue_event", issue_uid="issue-1", event_id="issue-event-1")
    issue.update(status="resolved", disposition_result={
        "terminal": True, "result_refs": [deepcopy(issue_ref)]})
    add("proposal_event", proposal_uid="proposal-1", event_id="proposal-event-1")

    # Same candidate/operation/consumed/event structure as the R1 skinny tree.
    # Business records other than the issue stay outside operation outputs.
    def broker(family, **ids):
        payload = {"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
                   "record_type": family, **SCOPE, **ids}
        tree[git_format.record_path(family, **ids)] = payload
        return payload

    candidate = {"candidate_uid": "candidate-一", "candidate_revision": 1,
                 "payload_digest": DIGEST}
    base_commit = "b" * 40
    result = {"terminal": True, "result_refs": [deepcopy(issue_ref)]}
    broker("input_candidate", candidate_uid="candidate-一", candidate_revision=1).update(
        payload_digest=DIGEST,
        whitelisted_fields={"local_id": "local-1", "value_type": "number",
                            "statement_kind": "state", "polarity": "affirmed"},
        envelope_summary={"source_id": "src-1", "version_id": "opaque-version",
                          "mapped_fingerprint": DIGEST},
        candidate_rule_version="fact-candidate-1", adapter_rule_version="adapter-1",
        validation_rule_version="validation-1", coverage_manifest_version="coverage-1",
        source_id="src-1", version_id="opaque-version", mapped_fingerprint=DIGEST,
        payload_ref=DIGEST, envelope_ref="c" * 64)
    broker("operation", operation_id="operation-1").update(
        request_digest=DIGEST, effect_digest=DIGEST, request_id="request-1",
        base_commit=base_commit, candidate_refs=[deepcopy(candidate)],
        coverage_id="coverage-id", coverage_digest=DIGEST,
        coverage_validation_version="coverage-check-1", unit_id="unit-1",
        disposition_result=[{**candidate, **deepcopy(result)}], formal_fact_refs=[],
        rule_version="rules-1", model_version="model-1", actor=deepcopy(issue["actor"]),
        approval_use_refs=[], event_ids=["event-1"], server_time=1793318400)
    broker("consumed_candidate", candidate_uid="candidate-一", candidate_revision=1).update(
        payload_digest=DIGEST, operation_id="operation-1",
        disposition_result=deepcopy(result))
    broker("event", event_id="event-1").update(
        operation_id="operation-1", base_commit=base_commit, ordinal=0,
        event_type="host-issued-event-kind", changed_refs=[deepcopy(issue_ref)],
        before=[None], after=[sha256(git_format.dumps_record(issue)).hexdigest()],
        invalidation_domains=["facts"], affected_refs=[deepcopy(issue_ref)])
    return tree


def with_dangling_fact_current(tree):
    """Copy a full tree and point fact-1's current at an absent revision.

    Reusable negative example for closure 2; closures 1/5 do not resolve integer
    current pointers. The caller's tree is preserved.
    """
    mutated = deepcopy(tree)
    current = mutated[git_format.record_path("fact_current", fact_uid="fact-1")]
    current["revision"] = 1 + max(
        record["revision"] for record in mutated.values()
        if record["record_type"] == "fact_revision" and record["fact_uid"] == "fact-1")
    return mutated
