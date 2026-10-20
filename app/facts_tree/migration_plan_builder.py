"""Pure-data migration plan builder (P2 §5.1).

This module only reshapes caller-supplied, already-read structured snapshots
and per-fact human dispositions into a complete canonical plan document
ready for ``MigrationPlanStateLedger.register_plan``. It never connects to a
database, never scans a company, never touches real materials, and never
runs semantic classification, approval, execution or rollback.

Determinism contract: identical inputs produce byte-identical canonical
output. Every caller-supplied collection is re-sorted by a deterministic key
before entering the document, so input list order never affects the digest.
Nothing is dropped: complete duplicate records are preserved and counted.

`prechecks` are caller-provided results/notes recorded verbatim — the
builder validates only their shape and never claims they were actually run;
the `validated` state and the pre-execution gate still require trusted
upper layers to really check them.
"""
from .migration_plans import (
    COVERAGE_EMPTY_RECORD_STATUSES, SNAPSHOT_CATEGORIES, _canonical_json, _digest,
    normalize_coverage)
from .source_registry import EVIDENCE_RELATIONS, LINK_STATUSES

_SNAPSHOT_CATEGORIES = SNAPSHOT_CATEGORIES
_ACTIONS = ("keep", "reassign", "split", "merge", "unclassified", "retire")
_EVIDENCE_FIELDS = (
    "fact_row_id", "evidence_id", "relation", "status",
    "source_id", "version_id", "locator", "coordinate", "quote_fingerprint",
    "fact_fingerprint", "source_lifecycle")
_SOURCE_LIFECYCLES = frozenset({"active", "pending_removal", "removed"})
_ASSESSMENT_KEYS = (
    "boundary_disagreements", "insufficient_sources",
    "numeric_coordinate_anomalies", "todo_invalidations",
    "diagnostic_invalidations", "projection_rebuild_targets")
_PRECHECK_STATUSES = ("pass", "fail", "not_run")
# Operations that produce a planned fact; unclassified/retire never do.
_FACT_OPERATIONS = ("keep", "reassign", "split", "merge")
_DISPOSITION_FIELDS = frozenset({"fact_row_id", "action", "reason", "planned_fact_refs"})
# Open-todo records carry their authoritative source kind (ruling 2026-10-17
# #4); backfill quarantine membership gates fact dispositions (ruling #6).
_OPEN_TODO_KINDS = ("todo", "md_leaf_todo", "backfill_quarantine")
_PLANNED_FACT_FIELDS = frozenset({
    "plan_fact_ref", "operation", "source_fact_row_ids",
    "target_primary_leaf", "evidence_ids", "fact_content"})


def _fail(message):
    raise ValueError(message)


def _is_hex64(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_text(value, what):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{what} must be nonempty text")
    return value


def _require_serializable(value, what):
    try:
        _canonical_json(value)
    except (ValueError, TypeError) as exc:
        _fail(f"{what} must be JSON serializable: {exc}")


def _validate_binding_ref(ref, what):
    if not isinstance(ref, dict):
        _fail(f"{what} must be a JSON object")
    unknown = set(ref) - {"pack_id", "pack_version", "manifest_sha256"}
    if unknown:
        _fail(f"{what} has unknown fields {sorted(unknown)}")
    _require_text(ref.get("pack_id"), f"{what}.pack_id")
    if not _is_positive_int(ref.get("pack_version")):
        _fail(f"{what}.pack_version must be a positive integer")
    if not _is_hex64(ref.get("manifest_sha256")):
        _fail(f"{what}.manifest_sha256 must be a 64-char lowercase hex digest")
    return {"pack_id": ref["pack_id"], "pack_version": ref["pack_version"],
            "manifest_sha256": ref["manifest_sha256"]}


def _normalize_leaf(leaf, what):
    if isinstance(leaf, dict):
        unknown = set(leaf) - {"trunk_code", "leaf_code"}
        if unknown:
            _fail(f"{what} has unknown fields {sorted(unknown)}")
        trunk, code = leaf.get("trunk_code"), leaf.get("leaf_code")
    elif isinstance(leaf, (list, tuple)) and len(leaf) == 2:
        trunk, code = leaf
    else:
        _fail(f"{what} must be a {{trunk_code, leaf_code}} object")
    _require_text(trunk, f"{what}.trunk_code")
    _require_text(code, f"{what}.leaf_code")
    return {"trunk_code": trunk, "leaf_code": code}


def _leaf_key(leaf):
    return (leaf["trunk_code"], leaf["leaf_code"])


def _validate_snapshot(snapshot, company_id):
    if not isinstance(snapshot, dict):
        _fail("snapshot must be a JSON object")
    keys = set(snapshot)
    expected = set(_SNAPSHOT_CATEGORIES)
    if keys != expected:
        _fail(f"snapshot must contain exactly the categories {sorted(expected)}; "
              f"missing {sorted(expected - keys)}, unexpected {sorted(keys - expected)}")
    result = {}
    for category in _SNAPSHOT_CATEGORIES:
        records = snapshot[category]
        if not isinstance(records, list):
            _fail(f"snapshot.{category} must be a list")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                _fail(f"snapshot.{category}[{index}] must be a JSON object")
            _require_serializable(record, f"snapshot.{category}[{index}]")
            if "company_id" in record and record["company_id"] != company_id:
                _fail(f"snapshot.{category}[{index}].company_id must equal the plan company")
        result[category] = records
    # Facts: unique positive fact_row_id.
    seen_rows = set()
    for record in result["facts"]:
        row_id = record.get("fact_row_id")
        if not _is_positive_int(row_id):
            _fail("snapshot.facts entries require a unique positive integer fact_row_id")
        if row_id in seen_rows:
            _fail(f"snapshot.facts has duplicate fact_row_id {row_id}")
        seen_rows.add(row_id)
    # Evidence links: full S1 field set, coordinate object. The S1
    # fact_evidence_links primary key is (fact, evidence, relation): the same
    # fact may link the same evidence_id under different relations, and every
    # relation/status row is preserved verbatim; only a fully identical
    # (fact_row_id, evidence_id, relation) triple is a duplicate.
    seen_evidence = set()
    for record in result["evidence_links"]:
        missing = [field for field in _EVIDENCE_FIELDS if field not in record]
        if missing:
            _fail(f"snapshot.evidence_links entry is missing fields {missing}")
        if not _is_positive_int(record["fact_row_id"]):
            _fail("snapshot.evidence_links fact_row_id must be a positive integer")
        _require_text(record["evidence_id"], "snapshot.evidence_links evidence_id")
        for field in ("source_id", "version_id", "locator", "quote_fingerprint"):
            _require_text(record[field], f"snapshot.evidence_links {field}")
        _require_text(record["fact_fingerprint"],
                      "snapshot.evidence_links fact_fingerprint")
        fingerprint = record["fact_fingerprint"]
        if (len(fingerprint) != 36 or not fingerprint.startswith("sha_")
                or any(c not in "0123456789abcdef" for c in fingerprint[4:])):
            _fail("snapshot.evidence_links fact_fingerprint must be sha_ plus 32 lowercase hex chars")
        quote_fingerprint = record["quote_fingerprint"]
        if not _is_hex64(quote_fingerprint):
            _fail("snapshot.evidence_links quote_fingerprint must be a 64-char lowercase hex digest")
        _require_text(record["source_lifecycle"],
                      "snapshot.evidence_links source_lifecycle")
        if record["source_lifecycle"] not in _SOURCE_LIFECYCLES:
            _fail("snapshot.evidence_links source_lifecycle is unknown")
        if not isinstance(record["coordinate"], dict):
            _fail("snapshot.evidence_links coordinate must be a JSON object")
        # S1 enums only: an uninterpretable relation/status can never be
        # evaluated for disposition eligibility, so it fails closed here.
        if record["relation"] not in EVIDENCE_RELATIONS:
            _fail(f"snapshot.evidence_links relation must be one of {list(EVIDENCE_RELATIONS)}")
        if record["status"] not in LINK_STATUSES:
            _fail(f"snapshot.evidence_links status must be one of {list(LINK_STATUSES)}")
        # Canonicalize the relation for the dedup key so the triple stays
        # hashable even though no text form is required beyond the enum.
        triple = (record["fact_row_id"], record["evidence_id"],
                  _canonical_json(record["relation"]))
        if triple in seen_evidence:
            _fail(f"snapshot.evidence_links has a conflicting duplicate triple "
                  f"(fact_row_id={record['fact_row_id']}, "
                  f"evidence_id={record['evidence_id']!r}, "
                  f"relation={record['relation']!r}); different relations are kept, "
                  "identical triples are not")
        seen_evidence.add(triple)
    source_lifecycle_by_id = {}
    for record in result["source_lifecycle"]:
        if record.get("record_kind") != "source":
            continue
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            _fail("snapshot.source_lifecycle source record requires source_id")
        if source_id in source_lifecycle_by_id:
            _fail(f"snapshot.source_lifecycle has duplicate source {source_id!r}")
        lifecycle = record.get("lifecycle")
        if lifecycle not in _SOURCE_LIFECYCLES:
            _fail(f"snapshot.source_lifecycle source {source_id!r} has unknown lifecycle")
        source_lifecycle_by_id[source_id] = lifecycle
    for record in result["evidence_links"]:
        source_id = record["source_id"]
        if source_id not in source_lifecycle_by_id:
            _fail(f"snapshot.evidence_links source {source_id!r} has no lifecycle record")
        if record["source_lifecycle"] != source_lifecycle_by_id[source_id]:
            _fail(f"snapshot.evidence_links source {source_id!r} lifecycle disagrees "
                  "with snapshot.source_lifecycle")
    # Open todos keep their source kind verbatim; quarantine membership is
    # the input to the disposition evidence gate, never an auto-arbitration.
    for record in result["open_todos"]:
        kind = record.get("kind")
        if kind not in _OPEN_TODO_KINDS:
            _fail(f"snapshot.open_todos entries require a kind in {list(_OPEN_TODO_KINDS)}")
        if kind == "backfill_quarantine" and not _is_positive_int(record.get("fact_row_id")):
            _fail("snapshot.open_todos backfill_quarantine entries require a "
                  "positive integer fact_row_id")
    # Deterministic order; exact duplicates are preserved and counted.
    ordered = {}
    for category, records in result.items():
        if category == "facts":
            ordered[category] = sorted(records, key=lambda r: r["fact_row_id"])
        elif category == "evidence_links":
            ordered[category] = sorted(
                records, key=lambda r: (r["fact_row_id"], r["evidence_id"], str(r["relation"])))
        else:
            ordered[category] = sorted(records, key=_canonical_json)
    return ordered


def normalize_input_snapshot(snapshot, coverage, company_id):
    """Return the canonical, hash-bound input snapshot used by a plan.

    This is shared with the host-side freshness check so the snapshot used
    during approval is normalized and fingerprinted by exactly the same
    rules as the original plan. It does not decide whether coverage is
    adequate; the state ledger owns that gate.
    """
    ordered_snapshot = _validate_snapshot(snapshot, company_id)
    normalized_coverage = normalize_coverage(coverage)
    snapshot_section = {}
    for category in _SNAPSHOT_CATEGORIES:
        records = ordered_snapshot[category]
        snapshot_section[category] = {
            "count": len(records),
            "sha256": _digest(_canonical_json(records)),
            "records": records}
    snapshot_section["coverage"] = normalized_coverage
    snapshot_fingerprint = _digest(_canonical_json({
        "categories": {
            category: {"count": snapshot_section[category]["count"],
                       "sha256": snapshot_section[category]["sha256"]}
            for category in _SNAPSHOT_CATEGORIES},
        "coverage": normalized_coverage}))
    return {"snapshot": ordered_snapshot,
            "coverage": normalized_coverage,
            "input_snapshot": snapshot_section,
            "snapshot_fingerprint": snapshot_fingerprint}


def _validate_dispositions(dispositions, fact_row_ids):
    if not isinstance(dispositions, list):
        _fail("dispositions must be a list")
    normalized = {}
    for index, disposition in enumerate(dispositions):
        if not isinstance(disposition, dict):
            _fail(f"dispositions[{index}] must be a JSON object")
        unknown = set(disposition) - _DISPOSITION_FIELDS
        if unknown:
            _fail(f"dispositions[{index}] has unknown fields {sorted(unknown)}")
        row_id = disposition.get("fact_row_id")
        if not _is_positive_int(row_id):
            _fail(f"dispositions[{index}].fact_row_id must be a positive integer")
        if row_id in normalized:
            _fail(f"dispositions has duplicate fact_row_id {row_id}")
        action = disposition.get("action")
        if action not in _ACTIONS:
            _fail(f"dispositions[{index}].action must be one of {list(_ACTIONS)}")
        _require_text(disposition.get("reason"), f"dispositions[{index}].reason")
        refs = disposition.get("planned_fact_refs")
        if not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs):
            _fail(f"dispositions[{index}].planned_fact_refs must be a list of nonempty text")
        if len(set(refs)) != len(refs):
            _fail(f"dispositions[{index}].planned_fact_refs must not repeat")
        expected = {"keep": 1, "reassign": 1, "merge": 1}.get(action)
        if expected is not None and len(refs) != expected:
            _fail(f"dispositions[{index}] action {action!r} requires exactly {expected} "
                  "planned_fact_refs")
        if action == "split" and len(refs) < 2:
            _fail(f"dispositions[{index}] action 'split' requires at least 2 planned_fact_refs")
        if action in ("unclassified", "retire") and refs:
            _fail(f"dispositions[{index}] action {action!r} must not have planned_fact_refs")
        normalized[row_id] = {"fact_row_id": row_id, "action": action,
                              "reason": disposition["reason"],
                              "planned_fact_refs": sorted(refs)}
    missing = fact_row_ids - set(normalized)
    extra = set(normalized) - fact_row_ids
    if missing or extra:
        _fail(f"dispositions must cover exactly the snapshot facts; "
              f"missing {sorted(missing)}, unexpected {sorted(extra)}")
    return [normalized[row_id] for row_id in sorted(normalized)]


def _validate_planned_facts(planned_facts, plan_id, fact_row_ids, leaf_keys,
                            evidence_by_row, dispositions_by_row):
    if not isinstance(planned_facts, list):
        _fail("planned_facts must be a list")
    prefix = f"plan:{plan_id}:"
    normalized = {}
    for index, fact in enumerate(planned_facts):
        what = f"planned_facts[{index}]"
        if not isinstance(fact, dict):
            _fail(f"{what} must be a JSON object")
        unknown = set(fact) - _PLANNED_FACT_FIELDS
        if unknown:
            _fail(f"{what} has unknown fields {sorted(unknown)}")
        ref = fact.get("plan_fact_ref")
        _require_text(ref, f"{what}.plan_fact_ref")
        if not ref.startswith(prefix):
            _fail(f"{what}.plan_fact_ref must start with {prefix!r}; it is a plan-local "
                  "reference, never a fact_uid")
        if ref in normalized:
            _fail(f"duplicate plan_fact_ref {ref!r}")
        operation = fact.get("operation")
        if operation not in _FACT_OPERATIONS:
            _fail(f"{what}.operation must be one of {list(_FACT_OPERATIONS)}")
        rows = fact.get("source_fact_row_ids")
        if (not isinstance(rows, list) or not rows
                or any(not _is_positive_int(r) for r in rows)):
            _fail(f"{what}.source_fact_row_ids must be a nonempty list of positive integers")
        if len(set(rows)) != len(rows):
            _fail(f"{what}.source_fact_row_ids must not repeat")
        unknown_rows = set(rows) - fact_row_ids
        if unknown_rows:
            _fail(f"{what}.source_fact_row_ids not in snapshot: {sorted(unknown_rows)}")
        leaf = _normalize_leaf(fact.get("target_primary_leaf"),
                               f"{what}.target_primary_leaf")
        if _leaf_key(leaf) not in leaf_keys:
            _fail(f"{what}.target_primary_leaf {_leaf_key(leaf)} is not a target pack leaf")
        evidence_ids = fact.get("evidence_ids")
        if (not isinstance(evidence_ids, list) or not evidence_ids
                or any(not isinstance(e, str) or not e for e in evidence_ids)):
            _fail(f"{what}.evidence_ids must be a nonempty list of nonempty text")
        if len(set(evidence_ids)) != len(evidence_ids):
            _fail(f"{what}.evidence_ids must not repeat")
        linked = set().union(*(evidence_by_row.get(row, set()) for row in rows))
        stray = [e for e in evidence_ids if e not in linked]
        if stray:
            _fail(f"{what}.evidence_ids not linked to its source facts: {stray}")
        if operation in ("split", "merge"):
            content = fact.get("fact_content")
            if not isinstance(content, dict) or not content:
                _fail(f"{what} operation {operation!r} requires nonempty structured "
                      "fact_content")
            _require_serializable(content, f"{what}.fact_content")
        entry = {"plan_fact_ref": ref, "operation": operation,
                 "source_fact_row_ids": sorted(rows), "target_primary_leaf": leaf,
                 "evidence_ids": sorted(evidence_ids)}
        if "fact_content" in fact:
            entry["fact_content"] = fact["fact_content"]
        normalized[ref] = entry
    # Bidirectional cross-checks against dispositions.
    referenced = {}
    for disposition in dispositions_by_row.values():
        for ref in disposition["planned_fact_refs"]:
            referenced.setdefault(ref, []).append(disposition)
    for ref, rows in referenced.items():
        if ref not in normalized:
            _fail(f"plan_fact_ref {ref!r} has no planned fact")
        if len(rows) > 1:
            fact = normalized[ref]
            row_ids = sorted(d["fact_row_id"] for d in rows)
            if fact["operation"] != "merge" or fact["source_fact_row_ids"] != row_ids:
                _fail(f"plan_fact_ref {ref!r} is referenced by more than one disposition; "
                      "composite split-then-merge graphs are not expressible in one plan")
    for ref, fact in normalized.items():
        rows = fact["source_fact_row_ids"]
        if fact["operation"] in ("keep", "reassign"):
            if len(rows) != 1:
                _fail(f"planned fact {ref!r} operation {fact['operation']!r} requires "
                      "exactly one source fact")
            disposition = dispositions_by_row[rows[0]]
            if disposition["action"] != fact["operation"]:
                _fail(f"planned fact {ref!r} operation disagrees with the disposition "
                      f"of fact {rows[0]}")
            if disposition["planned_fact_refs"] != [ref]:
                _fail(f"planned fact {ref!r} is not the disposition reference of fact {rows[0]}")
        elif fact["operation"] == "split":
            if len(rows) != 1:
                _fail(f"planned fact {ref!r} operation 'split' requires exactly one "
                      "source fact")
            disposition = dispositions_by_row[rows[0]]
            if disposition["action"] != "split":
                _fail(f"planned fact {ref!r} operation disagrees with the disposition "
                      f"of fact {rows[0]}")
        else:  # merge
            if len(rows) < 2:
                _fail(f"planned fact {ref!r} operation 'merge' requires at least two "
                      "distinct source facts")
            for row in rows:
                disposition = dispositions_by_row[row]
                if disposition["action"] != "merge":
                    _fail(f"merge planned fact {ref!r} includes fact {row} whose "
                          f"disposition is {disposition['action']!r}; composite "
                          "split-then-merge graphs are not expressible in one plan")
                if disposition["planned_fact_refs"] != [ref]:
                    _fail(f"merge planned fact {ref!r} is not the disposition reference "
                          f"of fact {row}")
    # Split disposition refs must exactly match the split planned facts.
    for disposition in dispositions_by_row.values():
        if disposition["action"] == "split":
            row = disposition["fact_row_id"]
            actual = sorted(ref for ref, fact in normalized.items()
                            if fact["operation"] == "split"
                            and fact["source_fact_row_ids"] == [row])
            if actual != disposition["planned_fact_refs"]:
                _fail(f"split disposition of fact {row} does not match its planned facts")
    return [normalized[ref] for ref in sorted(normalized)]


def _validate_prechecks(prechecks, created_by):
    if not isinstance(prechecks, dict):
        _fail("prechecks must be a JSON object")
    for key in ("reversibility", "target_invariants"):
        entry = prechecks.get(key)
        if not isinstance(entry, dict):
            _fail(f"prechecks.{key} must be a JSON object")
        if entry.get("status") not in _PRECHECK_STATUSES:
            _fail(f"prechecks.{key}.status must be one of {list(_PRECHECK_STATUSES)}")
        _require_text(entry.get("detail"), f"prechecks.{key}.detail")
    reviewer = _require_text(prechecks.get("independent_reviewer"),
                             "prechecks.independent_reviewer")
    if reviewer == created_by:
        _fail("prechecks.independent_reviewer must differ from created_by")
    _require_serializable(prechecks, "prechecks")
    return prechecks


def _validate_assessments(assessments):
    if not isinstance(assessments, dict):
        _fail("impact_assessments must be a JSON object")
    keys = set(assessments)
    expected = set(_ASSESSMENT_KEYS)
    if keys != expected:
        _fail(f"impact_assessments must contain exactly {sorted(expected)}")
    for key in _ASSESSMENT_KEYS:
        if not isinstance(assessments[key], list):
            _fail(f"impact_assessments.{key} must be a list")
        _require_serializable(assessments[key], f"impact_assessments.{key}")
    return assessments


def build_migration_plan(*, plan_id, company_id, created_by, created_at,
                         source, target, binding_revision, snapshot, coverage,
                         dispositions, planned_facts, target_leaf_codes, prechecks,
                         impact_assessments):
    """Build the complete canonical migration plan document.

    Pure function over caller-supplied data. The returned dict can be passed
    directly to ``MigrationPlanStateLedger.register_plan``; it never contains
    ``plan_sha256`` (the store owns the digest). Any change to a snapshot
    record, a coverage entry or a disposition changes the plan digest and
    therefore voids prior approval bound to the old digest.

    ``coverage`` is the mandatory per-category snapshot provenance section
    (P2 §5.1 rulings of 2026-10-17). It is shape-validated and embedded
    verbatim into ``input_snapshot.coverage`` and the snapshot fingerprint;
    blocking statuses are recorded for the state-ledger gate, never silently
    cleared here. Dispositions are evidence-gated: a fact without at least
    one ``supports``+``active`` S1 evidence link to a still-active source, or
    with an unresolved backfill-quarantine todo, may only be ``unclassified``
    and can never enter a planned target leaf.
    """
    _require_text(plan_id, "plan_id")
    _require_text(company_id, "company_id")
    _require_text(created_by, "created_by")
    _require_text(created_at, "created_at")
    if not _is_positive_int(binding_revision):
        _fail("binding_revision must be a positive integer")
    source_ref = _validate_binding_ref(source, "source")
    target_ref = _validate_binding_ref(target, "target")

    if isinstance(target_leaf_codes, (str, bytes)) or not hasattr(target_leaf_codes, "__iter__"):
        _fail("target_leaf_codes must be an iterable of {trunk_code, leaf_code} leaves")
    leaves = [_normalize_leaf(leaf, "target_leaf_codes entry")
              for leaf in target_leaf_codes]
    leaf_keys = {_leaf_key(leaf) for leaf in leaves}

    normalized_input = normalize_input_snapshot(snapshot, coverage, company_id)
    ordered_snapshot = normalized_input["snapshot"]
    fact_row_ids = {record["fact_row_id"] for record in ordered_snapshot["facts"]}
    evidence_by_row = {}
    for record in ordered_snapshot["evidence_links"]:
        evidence_by_row.setdefault(record["fact_row_id"], set()).add(record["evidence_id"])

    normalized_coverage = normalized_input["coverage"]
    for category in _SNAPSHOT_CATEGORIES:
        status = normalized_coverage[category]["status"]
        if status in COVERAGE_EMPTY_RECORD_STATUSES and ordered_snapshot[category]:
            _fail(f"coverage.{category} status {status!r} asserts absence; "
                  "the snapshot records must be empty")

    normalized_dispositions = _validate_dispositions(dispositions, fact_row_ids)
    dispositions_by_row = {d["fact_row_id"]: d for d in normalized_dispositions}
    normalized_facts = _validate_planned_facts(
        planned_facts, plan_id, fact_row_ids, leaf_keys, evidence_by_row,
        dispositions_by_row)

    # Evidence gate (ruling 2026-10-17 #6): only facts with at least one
    # S1 supports+active link to an active source and no unresolved backfill
    # quarantine may be dispositioned into the target tree; anything else
    # stays unclassified. Ordinary open todos never influence this predicate.
    eligible = {record["fact_row_id"] for record in ordered_snapshot["evidence_links"]
                if record["relation"] == "supports" and record["status"] == "active"
                and record["source_lifecycle"] == "active"}
    eligible -= {record["fact_row_id"] for record in ordered_snapshot["open_todos"]
                 if record["kind"] == "backfill_quarantine"}
    for disposition in normalized_dispositions:
        if disposition["action"] != "unclassified" \
                and disposition["fact_row_id"] not in eligible:
            _fail(f"fact {disposition['fact_row_id']} lacks an active supports "
                  "evidence link or is in unresolved backfill quarantine; its "
                  "disposition must be 'unclassified'")
    for fact in normalized_facts:
        ineligible = [row for row in fact["source_fact_row_ids"] if row not in eligible]
        if ineligible:
            _fail(f"planned fact {fact['plan_fact_ref']!r} includes source facts "
                  f"{ineligible} that lack active supports evidence or are in "
                  "unresolved backfill quarantine")

    normalized_prechecks = _validate_prechecks(prechecks, created_by)
    normalized_assessments = _validate_assessments(impact_assessments)

    snapshot_section = normalized_input["input_snapshot"]
    snapshot_fingerprint = normalized_input["snapshot_fingerprint"]

    action_counts = {action: 0 for action in _ACTIONS}
    for disposition in normalized_dispositions:
        action_counts[disposition["action"]] += 1
    affected = sorted({_leaf_key(fact["target_primary_leaf"]) for fact in normalized_facts})
    impact = {
        "input_fact_count": len(fact_row_ids),
        "action_counts": action_counts,
        "unclassified_count": action_counts["unclassified"],
        "retire_count": action_counts["retire"],
        "output_fact_count": len(normalized_facts),
        "affected_target_leaves": [
            {"trunk_code": trunk, "leaf_code": code} for trunk, code in affected],
        "assessments": normalized_assessments,
    }

    return {
        "plan_id": plan_id,
        "company_id": company_id,
        "created_by": created_by,
        "created_at": created_at,
        "source": source_ref,
        "target": target_ref,
        "binding_revision": binding_revision,
        "input_snapshot": snapshot_section,
        "snapshot_fingerprint": snapshot_fingerprint,
        "dispositions": normalized_dispositions,
        "planned_facts": normalized_facts,
        "prechecks": normalized_prechecks,
        "impact": impact,
    }
