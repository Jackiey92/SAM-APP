"""Pure-data migration plan builder; synthetic data only, no real materials."""
import copy
import sqlite3

import pytest

from app.facts_tree.migration_plan_builder import build_migration_plan
from app.facts_tree.migration_plans import (
    MigrationPlanStateLedger, SNAPSHOT_CATEGORIES, _canonical_json, _digest)

PLAN_ID = "plan:acc-a:0001"
REF_KEEP = f"plan:{PLAN_ID}:keep-1"
REF_SPLIT_A = f"plan:{PLAN_ID}:split-a"
REF_SPLIT_B = f"plan:{PLAN_ID}:split-b"
REF_MERGE = f"plan:{PLAN_ID}:merge-1"


def coverage_ok(**overrides):
    coverage = {category: {"status": "ok"} for category in SNAPSHOT_CATEGORIES}
    coverage.update(overrides)
    return coverage


def evidence_link(row, evidence_id, **overrides):
    link = {
        "fact_row_id": row, "evidence_id": evidence_id, "relation": "supports",
        "status": "active", "source_id": "src-1", "version_id": "v1",
        "locator": "p.3", "coordinate": {"page": 3, "offset": 12},
        "quote_fingerprint": "a" * 64,
        "fact_fingerprint": "sha_" + "f" * 32,
        "source_lifecycle": "active",
    }
    link.update(overrides)
    return link


def valid_inputs():
    return {
        "plan_id": PLAN_ID,
        "company_id": "acc-a",
        "created_by": "范范",
        "created_at": "2026-10-17T00:00:00+00:00",
        "source": {"pack_id": "generic", "pack_version": 1, "manifest_sha256": "a" * 64},
        "target": {"pack_id": "generic", "pack_version": 2, "manifest_sha256": "b" * 64},
        "binding_revision": 1,
        "snapshot": {
            "facts": [
                {"fact_row_id": 2, "fact_uid": "fact:2", "fact_version": 1, "text": "乙"},
                {"fact_row_id": 1, "fact_uid": "fact:1", "fact_version": 1, "text": "甲"},
            ],
            "placements": [
                {"fact_row_id": 1, "trunk_code": "m1", "leaf_code": "l0", "current": True}],
            "evidence_links": [
                evidence_link(2, "ev-2", status="pending_verification"),
                evidence_link(1, "ev-1"),
            ],
            "source_lifecycle": [
                {"record_kind": "source", "source_id": "src-1",
                 "lifecycle": "active"}],
            "leaf_revisions": [{"trunk_code": "m1", "leaf_code": "l0", "revision": 3}],
            "open_todos": [{"todo_id": "todo-1", "fact_row_id": 1, "kind": "todo",
                            "source_status": "open"}],
            "diagnostic_refs": [],
            "active_tasks": [],
        },
        "coverage": coverage_ok(
            diagnostic_refs={"status": "not_implemented",
                             "notes": ["诊断/专题无权威存储"]}),
        "dispositions": [
            {"fact_row_id": 2, "action": "unclassified",
             "reason": "证据仅 pending_verification，不合格", "planned_fact_refs": []},
            {"fact_row_id": 1, "action": "keep", "reason": "仍然成立",
             "planned_fact_refs": [REF_KEEP]},
        ],
        "planned_facts": [
            {"plan_fact_ref": REF_KEEP, "operation": "keep", "source_fact_row_ids": [1],
             "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
             "evidence_ids": ["ev-1"]},
        ],
        "target_leaf_codes": [{"trunk_code": "m1", "leaf_code": "l1"}],
        "prechecks": {
            "reversibility": {"status": "pass", "detail": "回滚清单可生成（调用方声明）"},
            "target_invariants": {"status": "not_run", "detail": "尚未运行"},
            "independent_reviewer": "饭团",
        },
        "impact_assessments": {
            "boundary_disagreements": [{"fact_row_id": 2, "note": "边界分歧"}],
            "insufficient_sources": [],
            "numeric_coordinate_anomalies": [],
            "todo_invalidations": [{"todo_id": "todo-1"}],
            "diagnostic_invalidations": [],
            "projection_rebuild_targets": [{"trunk_code": "m1", "leaf_code": "l1"}],
        },
    }


def build(**overrides):
    kwargs = valid_inputs()
    kwargs.update(overrides)
    return build_migration_plan(**kwargs)


def test_deterministic_and_order_independent():
    first = build()
    second = build()
    assert _canonical_json(first) == _canonical_json(second)
    # Shuffling every caller-supplied list changes nothing.
    shuffled = valid_inputs()
    for category, records in shuffled["snapshot"].items():
        records.reverse()
    shuffled["dispositions"].reverse()
    assert _canonical_json(build(**shuffled)) == _canonical_json(first)
    # Facts are sorted by fact_row_id in the document.
    rows = [r["fact_row_id"] for r in first["input_snapshot"]["facts"]["records"]]
    assert rows == sorted(rows) == [1, 2]
    # Changing any single record changes the snapshot fingerprint.
    changed = valid_inputs()
    changed["snapshot"]["open_todos"].append(
        {"todo_id": "todo-2", "fact_row_id": 2, "kind": "todo"})
    altered = build(**changed)
    assert altered["snapshot_fingerprint"] != first["snapshot_fingerprint"]
    assert _digest(_canonical_json(altered)) != _digest(_canonical_json(first))
    assert altered["input_snapshot"]["open_todos"]["count"] == 2


def test_header_and_binding_validation():
    for field in ("plan_id", "company_id", "created_by", "created_at"):
        with pytest.raises(ValueError, match=field):
            build(**{field: " "})
    for bad in (0, -1, "1", True):
        with pytest.raises(ValueError, match="binding_revision"):
            build(binding_revision=bad)
    for side in ("source", "target"):
        ref = valid_inputs()[side]
        with pytest.raises(ValueError, match="manifest_sha256"):
            build(**{side: {**ref, "manifest_sha256": "zz"}})
        with pytest.raises(ValueError, match="pack_version"):
            build(**{side: {**ref, "pack_version": 0}})
        with pytest.raises(ValueError, match="unknown fields"):
            build(**{side: {**ref, "extra": 1}})


def test_snapshot_categories_and_records():
    snapshot = valid_inputs()["snapshot"]
    missing = {k: v for k, v in snapshot.items() if k != "open_todos"}
    with pytest.raises(ValueError, match="exactly the categories"):
        build(snapshot=missing)
    with pytest.raises(ValueError, match="exactly the categories"):
        build(snapshot={**snapshot, "extra_category": []})
    with pytest.raises(ValueError, match="must be a list"):
        build(snapshot={**snapshot, "facts": {}})
    with pytest.raises(ValueError, match="must be a JSON object"):
        build(snapshot={**snapshot, "open_todos": ["x"]})
    with pytest.raises(ValueError, match="company_id must equal"):
        build(snapshot={**snapshot, "open_todos": [
            {"todo_id": "t", "fact_row_id": 1, "company_id": "acc-b"}]})
    with pytest.raises(ValueError, match="fact_row_id"):
        build(snapshot={**snapshot, "facts": [
            {"fact_row_id": 0}, {"fact_row_id": 1, "fact_uid": "x", "fact_version": 1}]})
    with pytest.raises(ValueError, match="duplicate fact_row_id"):
        build(snapshot={**snapshot, "facts": [
            {"fact_row_id": 1, "fact_uid": "a", "fact_version": 1},
            {"fact_row_id": 1, "fact_uid": "b", "fact_version": 1},
            {"fact_row_id": 2, "fact_uid": "c", "fact_version": 1}]})


def test_evidence_link_shape_and_uniqueness():
    snapshot = valid_inputs()["snapshot"]
    for field in ("relation", "status", "source_id", "version_id", "locator",
                  "coordinate", "quote_fingerprint"):
        broken = copy.deepcopy(snapshot)
        del broken["evidence_links"][0][field]
        with pytest.raises(ValueError, match="missing fields"):
            build(snapshot=broken)
    broken = copy.deepcopy(snapshot)
    broken["evidence_links"][0]["coordinate"] = "p.3"
    with pytest.raises(ValueError, match="coordinate"):
        build(snapshot=broken)
    # S1 semantics: the same fact may link the same evidence_id under
    # different relations; every relation row enters the canonical snapshot
    # with its status untouched.
    ok = copy.deepcopy(snapshot)
    ok["evidence_links"].append(evidence_link(1, "ev-1", relation="contradicts",
                                              status="active"))
    plan = build(snapshot=ok)
    records = plan["input_snapshot"]["evidence_links"]["records"]
    assert plan["input_snapshot"]["evidence_links"]["count"] == 3
    by_triple = {(l["fact_row_id"], l["evidence_id"], l["relation"]): l["status"]
                 for l in records}
    assert by_triple[(1, "ev-1", "supports")] == "active"
    assert by_triple[(1, "ev-1", "contradicts")] == "active"
    # An identical (fact_row_id, evidence_id, relation) triple is a
    # conflicting duplicate even when other fields such as status differ.
    broken = copy.deepcopy(snapshot)
    broken["evidence_links"].append(evidence_link(1, "ev-1", status="active"))
    with pytest.raises(ValueError, match="conflicting duplicate triple"):
        build(snapshot=broken)
    # The same evidence_id on a different fact is fine.
    ok = copy.deepcopy(snapshot)
    ok["evidence_links"].append(evidence_link(2, "ev-1", status="legacy_backfilled"))
    assert build(snapshot=ok)["input_snapshot"]["evidence_links"]["count"] == 3


def test_evidence_relation_status_enums_are_s1_only():
    snapshot = valid_inputs()["snapshot"]
    for field, bad in (("relation", "cites"), ("status", "confirmed"),
                       ("status", "legacy"), ("status", "pending")):
        broken = copy.deepcopy(snapshot)
        broken["evidence_links"][0][field] = bad
        with pytest.raises(ValueError, match=field):
            build(snapshot=broken)


def test_planned_evidence_ids_are_a_deduped_source_set():
    # Multi-relation rows never confuse planned-fact evidence validation:
    # evidence_ids remain a deduplicated source set over the link rows.
    kwargs = valid_inputs()
    kwargs["snapshot"]["evidence_links"].append(
        evidence_link(1, "ev-1", relation="contradicts", status="active"))
    plan = build(**kwargs)
    assert plan["planned_facts"][0]["evidence_ids"] == ["ev-1"]
    # A planned fact may reference an evidence id that exists under any
    # relation of one of its source facts.
    kwargs["snapshot"]["evidence_links"].append(
        evidence_link(1, "ev-3", relation="references"))
    kwargs["planned_facts"][0] = dict(kwargs["planned_facts"][0],
                                      evidence_ids=["ev-1", "ev-3"])
    assert build(**kwargs)["planned_facts"][0]["evidence_ids"] == ["ev-1", "ev-3"]


def test_disposition_coverage_and_shape():
    dispositions = valid_inputs()["dispositions"]
    with pytest.raises(ValueError, match="cover exactly"):
        build(dispositions=dispositions[:1])
    extra = dispositions + [{"fact_row_id": 9, "action": "retire", "reason": "x",
                             "planned_fact_refs": []}]
    with pytest.raises(ValueError, match="cover exactly"):
        build(dispositions=extra)
    duplicate = dispositions + [dict(dispositions[0])]
    with pytest.raises(ValueError, match="duplicate fact_row_id"):
        build(dispositions=duplicate)
    with pytest.raises(ValueError, match="action"):
        build(dispositions=[dict(dispositions[0], action="guess"), dispositions[1]])
    with pytest.raises(ValueError, match="reason"):
        build(dispositions=[dict(dispositions[0], reason=" "), dispositions[1]])
    with pytest.raises(ValueError, match="exactly 1"):
        build(dispositions=[dict(dispositions[1], planned_fact_refs=[]), dispositions[0]])
    with pytest.raises(ValueError, match="at least 2"):
        build(dispositions=[dict(dispositions[0], action="split",
                                 planned_fact_refs=[REF_SPLIT_A]), dispositions[1]])
    with pytest.raises(ValueError, match="must not have planned_fact_refs"):
        build(dispositions=[dict(dispositions[0], planned_fact_refs=[REF_SPLIT_A]),
                            dispositions[1]])


def split_inputs():
    kwargs = valid_inputs()
    kwargs["dispositions"] = [
        {"fact_row_id": 1, "action": "split", "reason": "两个主题",
         "planned_fact_refs": [REF_SPLIT_B, REF_SPLIT_A]},
        {"fact_row_id": 2, "action": "unclassified", "reason": "找不到新叶",
         "planned_fact_refs": []},
    ]
    kwargs["planned_facts"] = [
        {"plan_fact_ref": REF_SPLIT_A, "operation": "split", "source_fact_row_ids": [1],
         "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
         "evidence_ids": ["ev-1"], "fact_content": {"text": "甲-子1"}},
        {"plan_fact_ref": REF_SPLIT_B, "operation": "split", "source_fact_row_ids": [1],
         "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
         "evidence_ids": ["ev-1"], "fact_content": {"text": "甲-子2"}},
    ]
    return kwargs


def merge_inputs():
    kwargs = valid_inputs()
    # Both merge parents must pass the evidence gate: give fact 2 an active
    # supports link instead of the default pending_verification one.
    kwargs["snapshot"]["evidence_links"][0] = evidence_link(2, "ev-2")
    kwargs["dispositions"] = [
        {"fact_row_id": 1, "action": "merge", "reason": "重复项", "planned_fact_refs": [REF_MERGE]},
        {"fact_row_id": 2, "action": "merge", "reason": "重复项", "planned_fact_refs": [REF_MERGE]},
    ]
    kwargs["planned_facts"] = [
        {"plan_fact_ref": REF_MERGE, "operation": "merge", "source_fact_row_ids": [2, 1],
         "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
         "evidence_ids": ["ev-1", "ev-2"], "fact_content": {"text": "合并后"}},
    ]
    return kwargs


def test_split_and_merge_legal_shapes():
    split_plan = build(**split_inputs())
    assert split_plan["impact"]["action_counts"]["split"] == 1
    assert split_plan["impact"]["output_fact_count"] == 2
    merge_plan = build(**merge_inputs())
    assert merge_plan["impact"]["action_counts"]["merge"] == 2
    assert merge_plan["impact"]["output_fact_count"] == 1
    # Refs are plan-local: the prefix is enforced and they are never fact_uids.
    for fact in split_plan["planned_facts"]:
        assert fact["plan_fact_ref"].startswith(f"plan:{PLAN_ID}:")
        assert not fact["plan_fact_ref"].startswith("fact:")


def test_planned_fact_cross_reference_rejections():
    kwargs = split_inputs()
    with pytest.raises(ValueError, match="plan_fact_ref must start"):
        build(**{**kwargs, "planned_facts": [
            dict(kwargs["planned_facts"][0], plan_fact_ref="fact:999"),
            kwargs["planned_facts"][1]]})
    with pytest.raises(ValueError, match="duplicate plan_fact_ref"):
        build(**{**kwargs, "planned_facts": [
            kwargs["planned_facts"][0],
            dict(kwargs["planned_facts"][1], plan_fact_ref=REF_SPLIT_A)]})
    with pytest.raises(ValueError, match="not in snapshot"):
        build(**{**kwargs, "planned_facts": [
            dict(kwargs["planned_facts"][0], source_fact_row_ids=[9]),
            kwargs["planned_facts"][1]]})
    with pytest.raises(ValueError, match="not a target pack leaf"):
        build(**{**kwargs, "planned_facts": [
            dict(kwargs["planned_facts"][0],
                 target_primary_leaf={"trunk_code": "m1", "leaf_code": "nope"}),
            kwargs["planned_facts"][1]]})
    with pytest.raises(ValueError, match="not linked to its source"):
        build(**{**kwargs, "planned_facts": [
            dict(kwargs["planned_facts"][0], evidence_ids=["ev-2"]),
            kwargs["planned_facts"][1]]})
    with pytest.raises(ValueError, match="nonempty structured"):
        build(**{**kwargs, "planned_facts": [
            dict(kwargs["planned_facts"][0], fact_content={}),
            kwargs["planned_facts"][1]]})
    # Split disposition refs must match the planned facts exactly: a
    # referenced-but-missing planned fact is rejected...
    with pytest.raises(ValueError, match="has no planned fact"):
        build(**{**kwargs, "planned_facts": kwargs["planned_facts"][:1]})
    # ...as is a planned fact no disposition points to.
    orphan = dict(kwargs["planned_facts"][0], plan_fact_ref=f"plan:{PLAN_ID}:orphan")
    with pytest.raises(ValueError, match="does not match"):
        build(**{**kwargs, "planned_facts": kwargs["planned_facts"] + [orphan]})
    # Merge with a single source or a non-merge parent is rejected. The
    # single-source variant is made self-consistent (fact 2 retires, only
    # fact 1 merges) so the merge arity check is what actually fires.
    merge = merge_inputs()
    merge["dispositions"][1] = {"fact_row_id": 2, "action": "retire",
                                "reason": "不再合并", "planned_fact_refs": []}
    with pytest.raises(ValueError, match="at least two"):
        build(**{**merge, "planned_facts": [
            dict(merge["planned_facts"][0], source_fact_row_ids=[1],
                 evidence_ids=["ev-1"])]})
    # Composite split-then-merge: fact 1 is split, so it cannot join a merge.
    composite = merge_inputs()
    composite["dispositions"][0] = {
        "fact_row_id": 1, "action": "split", "reason": "先拆",
        "planned_fact_refs": [REF_SPLIT_A, REF_SPLIT_B]}
    composite["planned_facts"].append(
        {"plan_fact_ref": REF_SPLIT_A, "operation": "split", "source_fact_row_ids": [1],
         "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
         "evidence_ids": ["ev-1"], "fact_content": {"text": "x"}})
    composite["planned_facts"].append(
        {"plan_fact_ref": REF_SPLIT_B, "operation": "split", "source_fact_row_ids": [1],
         "target_primary_leaf": {"trunk_code": "m1", "leaf_code": "l1"},
         "evidence_ids": ["ev-1"], "fact_content": {"text": "y"}})
    with pytest.raises(ValueError, match="composite split-then-merge"):
        build(**composite)


def test_source_status_is_preserved_not_upgraded():
    plan = build()
    links = {(l["fact_row_id"], l["evidence_id"], l["relation"]): l["status"]
             for l in plan["input_snapshot"]["evidence_links"]["records"]}
    assert links[(1, "ev-1", "supports")] == "active"
    assert links[(2, "ev-2", "supports")] == "pending_verification"
    lifecycle = plan["input_snapshot"]["source_lifecycle"]["records"]
    assert lifecycle[0]["lifecycle"] == "active"
    # Planned facts only reference evidence ids; they carry no status at all.
    assert "status" not in plan["planned_facts"][0]


def test_prechecks_and_reviewer():
    prechecks = valid_inputs()["prechecks"]
    with pytest.raises(ValueError, match="status"):
        build(prechecks={**prechecks, "reversibility": {"status": "ok", "detail": "x"}})
    with pytest.raises(ValueError, match="detail"):
        build(prechecks={**prechecks, "target_invariants": {"status": "pass", "detail": " "}})
    with pytest.raises(ValueError, match="independent_reviewer"):
        build(prechecks={**prechecks, "independent_reviewer": "范范"})
    plan = build()
    assert plan["prechecks"]["reversibility"]["status"] == "pass"
    assert plan["prechecks"]["independent_reviewer"] == "饭团"


def test_impact_counts_and_assessments():
    plan = build()
    impact = plan["impact"]
    assert impact["input_fact_count"] == 2
    assert impact["action_counts"] == {
        "keep": 1, "reassign": 0, "split": 0, "merge": 0,
        "unclassified": 1, "retire": 0}
    assert impact["retire_count"] == 0
    assert impact["unclassified_count"] == 1
    assert impact["output_fact_count"] == 1
    assert impact["affected_target_leaves"] == [{"trunk_code": "m1", "leaf_code": "l1"}]
    # Assessments are kept verbatim, counts included by the caller.
    assert impact["assessments"]["boundary_disagreements"] == [
        {"fact_row_id": 2, "note": "边界分歧"}]
    # Unclassified facts may be built (preview) but are counted for the gate.
    unclassified = split_inputs()
    assert build(**unclassified)["impact"]["unclassified_count"] == 1
    assessments = valid_inputs()["impact_assessments"]
    with pytest.raises(ValueError, match="exactly"):
        build(impact_assessments={k: v for k, v in assessments.items()
                                  if k != "insufficient_sources"})
    with pytest.raises(ValueError, match="must be a list"):
        build(impact_assessments={**assessments, "insufficient_sources": {}})


def test_coverage_required_shape_and_fixed_status_table():
    kwargs = valid_inputs()
    del kwargs["coverage"]
    with pytest.raises(TypeError):
        build_migration_plan(**kwargs)
    coverage = coverage_ok()
    with pytest.raises(ValueError, match="exactly the categories"):
        build(coverage={k: v for k, v in coverage.items() if k != "open_todos"})
    with pytest.raises(ValueError, match="exactly the categories"):
        build(coverage={**coverage, "extra": {"status": "ok"}})
    with pytest.raises(ValueError, match="not a known coverage status"):
        build(coverage=coverage_ok(facts={"status": "everything_is_fine"}))
    # facts-only status is rejected anywhere else.
    with pytest.raises(ValueError, match="only valid for facts"):
        build(coverage=coverage_ok(placements={"status": "legacy_bootstrap_derived"}))
    # No caller-settable blocking flag exists to override the status table.
    with pytest.raises(ValueError, match="unknown fields"):
        build(coverage=coverage_ok(facts={"status": "ok", "blocking": False}))
    with pytest.raises(ValueError, match="notes"):
        build(coverage=coverage_ok(facts={"status": "ok", "notes": "not-a-list"}))
    # Absence-asserting statuses cannot smuggle snapshot records.
    with pytest.raises(ValueError, match="must be empty"):
        build(coverage=coverage_ok(open_todos={"status": "table_absent"}))


def test_confirmed_absent_requires_complete_proof():
    proof = {"statement": "该公司无任何诊断/专题对象", "confirmed_by": "范范",
             "confirmed_at": "2026-10-17T01:00:00+00:00", "method": "人工巡检记录 rev-1"}
    plan = build(coverage=coverage_ok(
        diagnostic_refs={"status": "confirmed_absent", "proof": proof}))
    entry = plan["input_snapshot"]["coverage"]["diagnostic_refs"]
    assert entry["status"] == "confirmed_absent" and entry["proof"] == proof
    for field in ("statement", "confirmed_by", "confirmed_at", "method"):
        broken = dict(proof)
        del broken[field]
        with pytest.raises(ValueError, match="proof"):
            build(coverage=coverage_ok(
                diagnostic_refs={"status": "confirmed_absent", "proof": broken}))
    with pytest.raises(ValueError, match="proof"):
        build(coverage=coverage_ok(
            diagnostic_refs={"status": "confirmed_absent",
                             "proof": {**proof, "statement": " "}}))
    # A proof on any other status is meaningless and rejected.
    with pytest.raises(ValueError, match="only allowed for confirmed_absent"):
        build(coverage=coverage_ok(facts={"status": "ok", "proof": proof}))


def test_coverage_enters_fingerprint_and_plan_digest():
    base = build()
    assert base["input_snapshot"]["coverage"]["diagnostic_refs"]["status"] \
        == "not_implemented"
    noted = valid_inputs()
    noted["coverage"]["open_todos"] = {"status": "ok", "notes": ["人工确认仅一条"]}
    noted_plan = build(**noted)
    assert noted_plan["snapshot_fingerprint"] != base["snapshot_fingerprint"]
    assert _digest(_canonical_json(noted_plan)) != _digest(_canonical_json(base))
    lifted = valid_inputs()
    lifted["coverage"]["diagnostic_refs"] = {
        "status": "confirmed_absent",
        "proof": {"statement": "无诊断对象", "confirmed_by": "范范",
                  "confirmed_at": "2026-10-17T01:00:00+00:00", "method": "巡检"}}
    lifted_plan = build(**lifted)
    assert lifted_plan["snapshot_fingerprint"] != base["snapshot_fingerprint"]
    # Notes order never changes the digest: normalization sorts them.
    reordered = valid_inputs()
    reordered["coverage"]["open_todos"] = {"status": "ok", "notes": ["b 注记", "a 注记"]}
    reordered_again = valid_inputs()
    reordered_again["coverage"]["open_todos"] = {
        "status": "ok", "notes": ["a 注记", "b 注记"]}
    assert _canonical_json(build(**reordered)) == _canonical_json(build(**reordered_again))


def test_open_todos_kind_contract_and_no_business_judgment():
    snapshot = valid_inputs()["snapshot"]
    with pytest.raises(ValueError, match="kind"):
        build(snapshot={**snapshot, "open_todos": [{"todo_id": "t"}]})
    with pytest.raises(ValueError, match="kind"):
        build(snapshot={**snapshot, "open_todos": [
            {"todo_id": "t", "fact_row_id": 1, "kind": "mystery"}]})
    with pytest.raises(ValueError, match="fact_row_id"):
        build(snapshot={**snapshot, "open_todos": [
            {"batch_id": "b1", "kind": "backfill_quarantine"}]})
    # An ordinary open todo referencing fact 1 never changes its eligibility:
    # the builder keeps it verbatim and still accepts the keep disposition.
    plan = build()
    todos = plan["input_snapshot"]["open_todos"]["records"]
    assert todos == [{"todo_id": "todo-1", "fact_row_id": 1, "kind": "todo",
                      "source_status": "open"}]
    assert plan["dispositions"][0]["action"] == "keep"


def test_disposition_evidence_gate():
    # Non-supporting evidence is not enough: every non-unclassified
    # disposition is rejected, but the evidence id remains correctly linked.
    kwargs = valid_inputs()
    kwargs["snapshot"]["evidence_links"][1] = evidence_link(
        1, "ev-1", relation="references")
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)
    kwargs["dispositions"][1] = {"fact_row_id": 1, "action": "unclassified",
                                 "reason": "无合格证据", "planned_fact_refs": []}
    kwargs["planned_facts"] = []
    assert build(**kwargs)["impact"]["unclassified_count"] == 2
    # legacy_backfilled / pending_verification never qualify as support.
    for status in ("legacy_backfilled", "pending_verification"):
        kwargs = valid_inputs()
        kwargs["snapshot"]["evidence_links"][1] = evidence_link(1, "ev-1", status=status)
        with pytest.raises(ValueError, match="must be 'unclassified'"):
            build(**kwargs)
    # An active-looking link to a removed source cannot support migration.
    kwargs = valid_inputs()
    for link in kwargs["snapshot"]["evidence_links"]:
        link["source_lifecycle"] = "removed"
    kwargs["snapshot"]["source_lifecycle"][0]["lifecycle"] = "removed"
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)
    # Conflicting lifecycle copies in the evidence and source ledgers fail closed.
    kwargs = valid_inputs()
    kwargs["snapshot"]["source_lifecycle"][0]["lifecycle"] = "removed"
    with pytest.raises(ValueError, match="lifecycle disagrees"):
        build(**kwargs)
    # Unresolved backfill quarantine disqualifies despite active evidence.
    kwargs = valid_inputs()
    kwargs["snapshot"]["open_todos"].append(
        {"batch_id": "b1", "fact_row_id": 1, "kind": "backfill_quarantine",
         "source_status": "unresolved"})
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)
    # Retire is not a bypass for an ineligible fact.
    kwargs = valid_inputs()
    kwargs["dispositions"][1] = {"fact_row_id": 1, "action": "retire",
                                 "reason": "想绕过", "planned_fact_refs": []}
    kwargs["planned_facts"] = []
    kwargs["snapshot"]["evidence_links"][1] = evidence_link(
        1, "ev-1", status="pending_verification")
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)


def test_split_and_merge_cannot_bypass_the_evidence_gate():
    # Split parent loses its active evidence -> split disposition rejected.
    kwargs = split_inputs()
    kwargs["snapshot"]["evidence_links"][1] = evidence_link(
        1, "ev-1", status="legacy_backfilled")
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)
    # One merge parent ineligible -> the whole merge is rejected, both at the
    # disposition level and (defense in depth) at the planned-fact level.
    kwargs = merge_inputs()
    kwargs["snapshot"]["evidence_links"][0] = evidence_link(
        2, "ev-2", status="pending_verification")
    with pytest.raises(ValueError, match="must be 'unclassified'"):
        build(**kwargs)


def test_output_registers_as_draft_and_digest_binding(tmp_path):
    plan = build()
    assert "plan_sha256" not in plan
    assert "plan_sha256" not in _canonical_json(plan)
    conn = sqlite3.connect(tmp_path / "register.db")
    try:
        ledger = MigrationPlanStateLedger(conn)
        with conn:
            result = ledger.register_plan(plan)
        assert result["state"] == "draft"
        assert result["plan_sha256"] == _digest(_canonical_json(plan))
        # Identical replay is idempotent; any changed decision is a conflict.
        with conn:
            assert ledger.register_plan(build())["event_count"] == 1
        changed = build(dispositions=[
            dict(valid_inputs()["dispositions"][1], reason="换一个理由"),
            valid_inputs()["dispositions"][0]])
        with pytest.raises(ValueError, match="conflicting migration plan"):
            with conn:
                ledger.register_plan(changed)
    finally:
        conn.close()
