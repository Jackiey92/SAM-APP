"""Read-only per-company migration snapshot reader (P2 §5.1).

Assembles the eight input categories of a migration plan from the SQLite
ledger for ONE company, plus the per-category coverage section that the
plan builder embeds into the plan document and the state-ledger gate
enforces. This reader:

* takes only a caller-provided idle ``sqlite3.Connection`` — never a path,
  never opens a database, never constructs a DDL-running store;
* executes SELECTs only, inside one deferred read transaction that is
  rolled back at the end; it never creates missing tables, never writes,
  never calls ``require_enabled`` (the gate decision belongs to the host);
* fails closed (``SnapshotRejected``) when authoritative data needed for
  the snapshot is corrupt or inconsistent — a tampered binding chain,
  manifest, ledger JSON or identity ledger must not produce a plan;
* marks categories whose authoritative source is missing/unimplemented
  with explicit blocking coverage statuses instead of silently returning
  empty collections;
* never scans runtime KV by prefix: checkpoints and leases are read by
  exact URI/job_id derived from the company's ``parse_jobs`` rows only,
  and the coverage notes always state that orphan KV keys cannot be
  enumerated (their absence is NOT proven).

It produces data only: no dispositions, no approval, no execution, no
migration, and nothing is written back to any ledger.
"""
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import time

from .fact_identity import FactIdentityStore
from .migration_plans import SNAPSHOT_CATEGORIES  # noqa: F401  (contract reference)
from .source_registry import digest as _source_digest

_COMPANY_RE = re.compile(r'^[^\x00\r\n]{1,128}$')

_BINDING_REQUIRED_COLUMNS = (
    "template_pack", "pack_version", "pack_digest", "binding_revision")
_IDENTITY_LEDGER_TABLES = (
    "fact_identities", "fact_revisions", "fact_lineage_events", "fact_lineage_edges")
_EVIDENCE_TABLES = (
    "fact_evidence_links", "source_evidence", "source_versions", "sources")
_SOURCE_LIFECYCLE_TABLES = ("sources", "source_versions", "source_events")
_SOURCE_LIFECYCLES = frozenset({"active", "pending_removal", "removed"})
_TODO_TABLES = ("todos", "md_leaf_todos", "source_backfill_quarantine")
_PLACEMENT_LEDGER_TABLES = ("fact_placements", "fact_current_placement")
_PLACEMENT_HISTORY_REQUIRED_COLUMNS = frozenset({
    "binding_revision", "prev_binding_revision"})
_PLACEMENT_POINTER_REQUIRED_COLUMNS = frozenset({"binding_revision"})
_ACTIVE_JOB_STATUSES = ("queued", "parsing")
_RESUMABLE_JOB_STATUSES = ("failed", "canceled", "needs_human", "partial")
_TERMINAL_JOB_STATUSES = ("done",)
_KNOWN_JOB_STATUSES = (_ACTIVE_JOB_STATUSES + _RESUMABLE_JOB_STATUSES
                       + _TERMINAL_JOB_STATUSES)
_RUNTIME_URIS = {
    "md_direct": "sam://kv/rt/md-direct/",
    "upload_manifest": "sam://kv/rt/md-upload-manifest/",
    "upload_options": "sam://kv/rt/md-upload-options/",
}
_ORPHAN_KV_NOTE = ("orphan runtime KV keys cannot be enumerated without a "
                   "prohibited prefix scan; their absence is not proven")
_DIAGNOSTIC_NOTE = ("no authoritative diagnostic/topic store exists; L1/L0 "
                    "rollups are rebuildable projections, not diagnostic "
                    "reference records")


class SnapshotRejected(ValueError):
    """Authoritative data is missing, corrupt or inconsistent; fail closed."""


def _fetch_dicts(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


class MigrationSnapshotReader:
    """Company-scoped read-only snapshot for one explicit construction."""

    def __init__(self, db):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError(
                "migration snapshot reader requires a caller-provided sqlite3.Connection")
        if db.in_transaction:
            raise ValueError(
                "migration snapshot reader requires an idle connection; "
                "refusing to join a caller transaction")
        self.db = db

    def read(self, *, company_id):
        """One consistent deferred-transaction snapshot; rolled back at the end."""
        if (not isinstance(company_id, str) or not company_id.strip()
                or not _COMPANY_RE.match(company_id)):
            raise ValueError("snapshot reader requires a host-injected bounded company_id")
        self.db.execute("BEGIN")
        try:
            return self.read_in_transaction(company_id=company_id)
        finally:
            self.db.rollback()

    def read_in_transaction(self, *, company_id):
        """Read under a transaction owned by a trusted host coordinator.

        This is SELECT-only and neither starts nor ends the transaction. It
        exists for the approval coordinator's ``BEGIN IMMEDIATE`` boundary,
        which must cover both the live snapshot read and the resulting audit
        event so no writer can change the company between those operations.
        """
        if not self.db.in_transaction:
            raise ValueError("read_in_transaction requires a caller-owned transaction")
        if (not isinstance(company_id, str) or not company_id.strip()
                or not _COMPANY_RE.match(company_id)):
            raise ValueError("snapshot reader requires a host-injected bounded company_id")
        return self._read(company_id)

    # -- low-level helpers (SELECT only) --

    def _table_exists(self, name):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None

    def _select(self, sql, params):
        return _fetch_dicts(self.db.execute(sql, params))

    @staticmethod
    def _parse_time(value):
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _loads(text, what, *, expect=dict):
        try:
            value = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise SnapshotRejected(f"{what} is not valid JSON") from exc
        if expect is not None and not isinstance(value, expect):
            raise SnapshotRejected(f"{what} must decode to {expect.__name__}")
        return value

    # -- binding identity and provenance intervals --

    def _read(self, company):
        binding, intervals = self._binding(company)
        snapshot, coverage = {}, {}
        snapshot["facts"], coverage["facts"] = self._facts(company)
        snapshot["placements"], coverage["placements"] = \
            self._placements(company, binding, intervals)
        snapshot["evidence_links"], coverage["evidence_links"] = \
            self._evidence_links(company)
        snapshot["source_lifecycle"], coverage["source_lifecycle"] = \
            self._source_lifecycle(company)
        snapshot["leaf_revisions"], coverage["leaf_revisions"] = \
            self._leaf_revisions(company, binding, intervals)
        snapshot["open_todos"], coverage["open_todos"] = self._open_todos(company)
        # Ruling 2026-10-17 #1: never inferred absent; only a caller-supplied
        # auditable confirmed_absent proof (at plan build time) may lift this.
        snapshot["diagnostic_refs"], coverage["diagnostic_refs"] = [], {
            "status": "not_implemented", "notes": [_DIAGNOSTIC_NOTE]}
        snapshot["active_tasks"], coverage["active_tasks"] = self._active_tasks(company)
        return {"company_id": company, "binding": binding,
                "snapshot": snapshot, "coverage": coverage}

    def _binding(self, company):
        """Current binding plus ordered provenance intervals, or (None, None).

        Any inconsistency between company_tree_meta, the append-only binding
        history and the immutable manifest archive fails closed: a snapshot
        built on an unverifiable binding is worse than no snapshot.
        """
        if not self._table_exists("company_tree_meta"):
            return None, None
        rows = self._select(
            "SELECT * FROM company_tree_meta WHERE company_id=?", (company,))
        if not rows:
            return None, None
        meta = rows[0]
        missing = [column for column in _BINDING_REQUIRED_COLUMNS
                   if column not in meta or meta[column] is None]
        if missing:
            raise SnapshotRejected(
                f"company binding lacks required identity fields {missing}; "
                "the authorized writable baseline upgrade must run before reading")
        if not self._table_exists("company_template_binding_history"):
            raise SnapshotRejected("company binding has no history baseline")
        history = self._select(
            "SELECT * FROM company_template_binding_history WHERE company_id=? "
            "ORDER BY binding_revision", (company,))
        self._check_history(meta, history)
        if not self._table_exists("template_manifests"):
            raise SnapshotRejected("bound manifest has no immutable archive table")
        manifest = self.db.execute(
            "SELECT manifest_sha256,manifest_json FROM template_manifests "
            "WHERE pack_id=? AND pack_version=?",
            (meta["template_pack"], meta["pack_version"])).fetchone()
        if manifest is None:
            raise SnapshotRejected("bound manifest has no immutable archive")
        if hashlib.sha256(manifest[1].encode("utf-8")).hexdigest() != manifest[0]:
            raise SnapshotRejected("archived manifest failed hash verification")
        if manifest[0] != meta["pack_digest"]:
            raise SnapshotRejected(
                "archived manifest digest differs from the bound digest")
        binding = {"pack_id": meta["template_pack"],
                   "pack_version": meta["pack_version"],
                   "manifest_sha256": meta["pack_digest"],
                   "binding_revision": meta["binding_revision"]}
        intervals = []
        for entry in history:
            when = self._parse_time(entry["event_at"])
            if when is None:
                raise SnapshotRejected(
                    "binding history event_at is not a comparable timestamp")
            intervals.append((when, {
                "pack_id": entry["template_pack"],
                "pack_version": entry["pack_version"],
                "manifest_sha256": entry["pack_digest"],
                "binding_revision": entry["binding_revision"]}))
        return binding, intervals

    @staticmethod
    def _check_history(meta, history):
        """Same chain rules as CompanyBindingStore._check_history, read-only."""
        if not history:
            raise SnapshotRejected("binding has no history baseline")
        revisions = [row["binding_revision"] for row in history]
        if revisions != list(range(1, len(revisions) + 1)):
            raise SnapshotRejected("binding history has a revision gap")
        first = history[0]
        if first["record_type"] not in ("initial", "legacy-bootstrap"):
            raise SnapshotRejected(
                f"binding history starts with an invalid record type "
                f"{first['record_type']!r}")
        if any(first[key] is not None for key in (
                "prev_template_pack", "prev_pack_version", "prev_pack_digest")):
            raise SnapshotRejected("first history row must not name a previous identity")
        for previous, current in zip(history, history[1:]):
            if (current["prev_template_pack"], current["prev_pack_version"],
                    current["prev_pack_digest"]) != (
                    previous["template_pack"], previous["pack_version"],
                    previous["pack_digest"]):
                raise SnapshotRejected(
                    "binding history chain does not link consecutive identities")
        if revisions[-1] != meta["binding_revision"]:
            raise SnapshotRejected(
                "current binding revision differs from the latest history row")
        latest = history[-1]
        if (latest["template_pack"], latest["pack_version"], latest["pack_digest"]) != (
                meta["template_pack"], meta["pack_version"], meta["pack_digest"]):
            raise SnapshotRejected(
                "current binding identity differs from the latest history row")

    def _provenance(self, created_at, intervals):
        """Map created_at to the unique binding interval strictly containing it.

        A timestamp before the first history record, exactly equal to any
        binding switch, or not comparable is unprovable (None) — ruling
        2026-10-17 #2 forbids falling back to the current binding.
        """
        if intervals is None:
            return None
        when = self._parse_time(created_at)
        if when is None:
            return None
        for index, (start, identity) in enumerate(intervals):
            end = intervals[index + 1][0] if index + 1 < len(intervals) else None
            try:
                if start < when and (end is None or when < end):
                    return dict(identity)
            except TypeError:
                return None  # mixed naive/aware timestamps are not comparable
        return None

    # -- category readers --

    def _facts(self, company):
        if not self._table_exists("facts"):
            return [], {"status": "table_absent", "notes": ["facts table is absent"]}
        records = []
        for row in self._select(
                "SELECT * FROM facts WHERE company_id=? ORDER BY id", (company,)):
            record = dict(row)
            record["fact_row_id"] = row["id"]
            records.append(record)
        missing = [name for name in _IDENTITY_LEDGER_TABLES
                   if not self._table_exists(name)]
        if missing:
            return records, {"status": "table_absent", "notes": sorted(
                f"identity ledger table {name} is absent" for name in missing)}
        identities = self._select(
            "SELECT * FROM fact_identities WHERE company_id=?", (company,))
        revisions = self._select(
            "SELECT * FROM fact_revisions WHERE company_id=?", (company,))
        events = self._select(
            "SELECT * FROM fact_lineage_events WHERE company_id=?", (company,))
        edges = self._select(
            "SELECT * FROM fact_lineage_edges WHERE company_id=?", (company,))
        if not identities and not revisions and not events and not edges:
            # Ruling 2026-10-17 #5: deterministic read-only legacy identities;
            # nothing is written back to the ledger.
            for record in records:
                record["identity"] = {
                    "fact_uid": f"legacy:{company}:{record['fact_row_id']}",
                    "fact_version": 1, "source": "derived_legacy_bootstrap"}
            return records, {"status": "legacy_bootstrap_derived", "notes": [
                "no identity ledger rows; deterministic legacy identities "
                "derived read-only, nothing written back"]}
        by_row, problems = self._cross_check_identity(
            records, identities, revisions, events, edges)
        if problems:
            return records, {"status": "identity_mismatch",
                             "notes": sorted(problems)}
        fact_ids = {record["fact_row_id"] for record in records}
        mapped = set(by_row)
        unmapped = fact_ids - mapped
        if mapped and unmapped:
            return records, {"status": "partial_identity_ledger", "notes": [
                f"{len(unmapped)} of {len(fact_ids)} facts have no identity "
                "ledger row; partial ledgers block validation"]}
        if not mapped and fact_ids:
            return records, {"status": "identity_mismatch", "notes": [
                "identity ledger rows exist but no company fact is mapped"]}
        for record in records:
            revision = by_row[record["fact_row_id"]]
            record["identity"] = {
                "fact_uid": revision["fact_uid"],
                "fact_version": revision["fact_version"],
                "revision_kind": revision["revision_kind"],
                "source": "ledger"}
        return records, {"status": "ok"}

    @staticmethod
    def _cross_check_identity(records, identities, revisions, events, edges):
        """Read-only consistency audit of the identity/revision/lineage ledger."""
        by_row = {}
        problems = []
        fact_by_id = {record["fact_row_id"]: record for record in records}
        identity_uids = {row["fact_uid"] for row in identities}
        revisions_by_uid = {}
        for revision in revisions:
            row_id = revision["fact_row_id"]
            fact = fact_by_id.get(row_id)
            if fact is None:
                problems.append(
                    f"fact_revisions references fact_row_id {row_id} outside "
                    "this company snapshot")
            else:
                expected = FactIdentityStore._content_digest(fact)
                if revision["content_sha256"] != expected:
                    problems.append(
                        f"content digest mismatch for fact_row_id {row_id}")
                by_row[row_id] = revision
            if revision["fact_uid"] not in identity_uids:
                problems.append(
                    f"fact_revisions references unknown fact_uid "
                    f"{revision['fact_uid']!r}")
            revisions_by_uid.setdefault(revision["fact_uid"], []).append(
                revision["fact_version"])
        for uid in identity_uids:
            versions = sorted(revisions_by_uid.get(uid, []))
            if not versions:
                problems.append(f"fact identity {uid!r} has no revisions")
            elif versions != list(range(1, len(versions) + 1)):
                problems.append(f"fact identity {uid!r} has a revision gap")
        events_by_id = {row["event_id"]: row for row in events}
        edges_by_event = {}
        expected_relations = {
            "correction": "supersedes", "split": "split_into",
            "merge": "merged_into", "retract": "retires",
            "migration": "migrated_into"}
        for edge in edges:
            edges_by_event.setdefault(edge["event_id"], []).append(edge)
        for event in events:
            if not edges_by_event.get(event["event_id"]):
                problems.append(
                    f"lineage event {event['event_id']!r} has no lineage edges")
        revision_keys = {(row["fact_uid"], row["fact_version"]) for row in revisions}
        for edge in edges:
            event = events_by_id.get(edge["event_id"])
            if event is None:
                problems.append(
                    f"lineage edge references unknown event {edge['event_id']!r}")
            else:
                expected_relation = expected_relations.get(event["event_type"])
                if expected_relation is None:
                    problems.append(
                        f"lineage event has unknown event type "
                        f"{event['event_type']!r}")
                elif edge["relation"] != expected_relation:
                    problems.append(
                        f"lineage edge relation {edge['relation']!r} disagrees with "
                        f"event type {event['event_type']!r}")
            if (edge["parent_fact_uid"], edge["parent_fact_version"]) \
                    not in revision_keys:
                problems.append("lineage edge references a missing parent revision")
            if edge["relation"] != "retires" and (
                    edge["child_fact_uid"], edge["child_fact_version"]) \
                    not in revision_keys:
                problems.append("lineage edge references a missing child revision")
        return by_row, problems

    def _placements(self, company, binding, intervals):
        """Legacy fact_leaf_links plus the versioned placement ledger.

        The legacy reader semantics are unchanged. The versioned ledger is a
        pair of tables: exactly one of them present is partial corruption and
        fails closed; both absent keeps the legacy-only behavior; both
        present with rows triggers full read-only verification (company/fact
        version, continuous sequence, prev-chain, archived manifest digest
        and leaf/trunk correspondence, strict binding-interval containment
        with identity equality, and current-pointer consistency). Ledger
        records are never folded into legacy records by text or leaf code.
        """
        records, coverage = self._legacy_placements(company, binding, intervals)
        present = [name for name in _PLACEMENT_LEDGER_TABLES
                   if self._table_exists(name)]
        if len(present) == 1:
            missing = sorted(set(_PLACEMENT_LEDGER_TABLES) - set(present))
            raise SnapshotRejected(
                f"versioned placement ledger is partially present: {missing} "
                "missing; refusing to read a corrupt ledger")
        if not present:
            return records, coverage
        versioned, v_status, v_notes = self._versioned_placements(
            company, binding, intervals)
        records = records + versioned
        if v_status != "ok":
            return records, {"status": v_status,
                             "notes": v_notes + coverage.get("notes", [])}
        if coverage["status"] != "ok":
            return records, coverage
        return records, {"status": "ok"}

    def _legacy_placements(self, company, binding, intervals):
        if not self._table_exists("fact_leaf_links"):
            return [], {"status": "table_absent",
                        "notes": ["fact_leaf_links table is absent"]}
        records = []
        unproven = []
        for row in self._select(
                "SELECT * FROM fact_leaf_links WHERE company_id=? ORDER BY id",
                (company,)):
            identity = self._provenance(row["created_at"], intervals)
            records.append({
                "company_id": company, "fact_row_id": row["fact_id"],
                "trunk_code": row["trunk"], "leaf_code": row["leaf_code"],
                "source_file": row["source_file"], "job_id": row["job_id"],
                "created_at": row["created_at"], "binding_identity": identity})
            if identity is None:
                unproven.append(row["fact_id"])
        if unproven:
            notes = [f"{len(unproven)} placement(s) have an unprovable "
                     f"creation-time binding: fact_row_ids {sorted(unproven)[:10]}"]
            if binding is None:
                notes.append("company has no binding baseline")
            return records, {"status": "ambiguous_provenance", "notes": notes}
        return records, {"status": "ok"}

    def _archived_manifest(self, cache, pack_id, pack_version):
        """Digest-verified archived manifest document, cached per pack version."""
        key = (pack_id, pack_version)
        if key in cache:
            return cache[key]
        row = self.db.execute(
            "SELECT manifest_sha256,manifest_json FROM template_manifests "
            "WHERE pack_id=? AND pack_version=?", key).fetchone()
        result = None
        if row is not None:
            if hashlib.sha256(row[1].encode("utf-8")).hexdigest() != row[0]:
                result = {"error": "archived manifest failed hash verification"}
            else:
                result = {"manifest_sha256": row[0],
                          "document": self._loads(row[1], "archived manifest")}
        cache[key] = result
        return result

    def _versioned_placements(self, company, binding, intervals):
        history = self._select(
            "SELECT * FROM fact_placements WHERE company_id=? "
            "ORDER BY fact_row_id,placement_seq", (company,))
        pointers = self._select(
            "SELECT * FROM fact_current_placement WHERE company_id=? "
            "ORDER BY fact_row_id", (company,))
        if not history and not pointers:
            return [], "ok", []
        problems = []
        if history:
            missing = sorted(_PLACEMENT_HISTORY_REQUIRED_COLUMNS - set(history[0]))
            if missing:
                problems.append(
                    "fact_placements lacks exact binding provenance fields: "
                    + ", ".join(missing))
        if pointers:
            missing = sorted(_PLACEMENT_POINTER_REQUIRED_COLUMNS - set(pointers[0]))
            if missing:
                problems.append(
                    "fact_current_placement lacks exact binding provenance fields: "
                    + ", ".join(missing))
        if self._table_exists("fact_revisions"):
            mapped_rows = {r["fact_row_id"] for r in self._select(
                "SELECT fact_row_id FROM fact_revisions WHERE company_id=?",
                (company,))}
        else:
            mapped_rows = set()
            problems.append("identity ledger table fact_revisions is absent; "
                            "placement fact versions cannot be verified")
        manifest_cache = {}
        records = []
        groups = {}
        for row in history:
            groups.setdefault(row["fact_row_id"], []).append(row)
        pointer_by_fact = {p["fact_row_id"]: p for p in pointers}
        for row in history:
            fact_row_id = row["fact_row_id"]
            tag = f"placement fact_row_id {fact_row_id} seq {row['placement_seq']}"
            if fact_row_id not in mapped_rows:
                problems.append(
                    f"{tag} references a fact version without an identity "
                    "ledger revision in this company")
            revision = row.get("binding_revision")
            if type(revision) is not int or revision < 1:
                problems.append(
                    f"{tag} has no explicit positive binding_revision; "
                    "historical provenance is not inferred")
            archive = self._archived_manifest(
                manifest_cache, row["pack_id"], row["pack_version"])
            if archive is None:
                problems.append(f"{tag} names a manifest with no immutable archive")
            elif "error" in archive:
                problems.append(f"{tag}: {archive['error']}")
            else:
                if archive["manifest_sha256"] != row["manifest_sha256"]:
                    problems.append(
                        f"{tag} manifest digest differs from the archived digest")
                document = archive["document"]
                leaves = document.get("leaves") if isinstance(document, dict) else None
                trunks = document.get("trunks") if isinstance(document, dict) else None
                if not isinstance(leaves, list) or not isinstance(trunks, list):
                    problems.append(
                        f"{tag}: archived manifest has no verifiable "
                        "trunk/leaf definitions")
                else:
                    leaf = next((l for l in leaves
                                 if isinstance(l, dict)
                                 and l.get("code") == row["leaf_code"]), None)
                    if leaf is None:
                        problems.append(
                            f"{tag} names leaf {row['leaf_code']!r} which is "
                            "not in the archived manifest")
                    elif leaf.get("trunk") != row["trunk_code"]:
                        problems.append(
                            f"{tag}: leaf {row['leaf_code']!r} does not belong "
                            f"to trunk {row['trunk_code']!r} in the archived manifest")
            interval_identity = self._provenance(row["event_at"], intervals)
            binding_identity = None
            if interval_identity is None:
                problems.append(
                    f"{tag} event time has no provable unique binding interval")
            elif (interval_identity["pack_id"], interval_identity["pack_version"],
                  interval_identity["manifest_sha256"],
                  interval_identity["binding_revision"]) != (
                  row["pack_id"], row["pack_version"], row["manifest_sha256"],
                  revision):
                problems.append(
                    f"{tag} identity differs from its binding interval identity")
            elif type(revision) is int and revision > 0:
                # Timestamp intervals are only a consistency check. They do
                # not supply a missing or conflicting revision. The returned
                # identity is anchored by the revision explicitly stored on
                # the placement event.
                binding_identity = {
                    "pack_id": row["pack_id"],
                    "pack_version": row["pack_version"],
                    "manifest_sha256": row["manifest_sha256"],
                    "binding_revision": revision}
            records.append({
                "company_id": company, "fact_row_id": fact_row_id,
                "placement_seq": row["placement_seq"],
                "pack_id": row["pack_id"], "pack_version": row["pack_version"],
                "manifest_sha256": row["manifest_sha256"],
                "binding_revision": revision,
                "trunk_code": row["trunk_code"], "leaf_code": row["leaf_code"],
                "prev_pack_id": row["prev_pack_id"],
                "prev_pack_version": row["prev_pack_version"],
                "prev_manifest_sha256": row["prev_manifest_sha256"],
                "prev_binding_revision": row.get("prev_binding_revision"),
                "prev_trunk_code": row["prev_trunk_code"],
                "prev_leaf_code": row["prev_leaf_code"],
                "event_at": row["event_at"], "actor": row["actor"],
                "reason": row["reason"], "record_type": row["record_type"],
                "migration_id": row["migration_id"],
                "binding_identity": binding_identity,
                "source": "versioned_placement_ledger",
                "is_current": False})
        for fact_row_id, rows in groups.items():
            seqs = [r["placement_seq"] for r in rows]
            if seqs != list(range(1, len(rows) + 1)):
                problems.append(
                    f"placement history of fact_row_id {fact_row_id} has a "
                    "sequence gap")
            first = rows[0]
            if any(first.get(key) is not None for key in (
                    "prev_pack_id", "prev_pack_version", "prev_manifest_sha256",
                    "prev_binding_revision",
                    "prev_trunk_code", "prev_leaf_code")):
                problems.append(
                    f"first placement of fact_row_id {fact_row_id} must not "
                    "name a previous identity")
            for previous, current in zip(rows, rows[1:]):
                if (current["prev_pack_id"], current["prev_pack_version"],
                        current["prev_manifest_sha256"], current.get("prev_binding_revision"),
                        current["prev_trunk_code"],
                        current["prev_leaf_code"]) != (
                        previous["pack_id"], previous["pack_version"],
                        previous["manifest_sha256"], previous.get("binding_revision"),
                        previous["trunk_code"],
                        previous["leaf_code"]):
                    problems.append(
                        f"placement history of fact_row_id {fact_row_id} does "
                        "not link consecutive identities")
            pointer = pointer_by_fact.get(fact_row_id)
            latest = rows[-1]
            if pointer is None:
                problems.append(
                    f"fact_row_id {fact_row_id} has placement history but no "
                    "current placement pointer")
            elif (pointer["placement_seq"], pointer["pack_id"],
                  pointer["pack_version"], pointer["manifest_sha256"],
                  pointer.get("binding_revision"),
                  pointer["trunk_code"], pointer["leaf_code"]) != (
                  latest["placement_seq"], latest["pack_id"],
                  latest["pack_version"], latest["manifest_sha256"],
                  latest.get("binding_revision"),
                  latest["trunk_code"], latest["leaf_code"]):
                problems.append(
                    f"current placement pointer of fact_row_id {fact_row_id} "
                    "differs from the latest placement event")
            else:
                for record in records:
                    if record["fact_row_id"] == fact_row_id and \
                            record["placement_seq"] == pointer["placement_seq"]:
                        record["is_current"] = True
        for fact_row_id in pointer_by_fact:
            pointer = pointer_by_fact[fact_row_id]
            pointer_revision = pointer.get("binding_revision")
            if type(pointer_revision) is not int or pointer_revision < 1:
                problems.append(
                    f"current placement pointer of fact_row_id {fact_row_id} "
                    "has no explicit positive binding_revision; historical "
                    "provenance is not inferred")
            if fact_row_id not in groups:
                problems.append(
                    f"current placement pointer of fact_row_id {fact_row_id} "
                    "has no placement history")
        if problems:
            return records, "identity_mismatch", sorted(problems)
        return records, "ok", []

    def _leaf_revisions(self, company, binding, intervals):
        if not self._table_exists("leaf_revisions"):
            return [], {"status": "table_absent",
                        "notes": ["leaf_revisions table is absent"]}
        records = []
        unproven = []
        for row in self._select(
                "SELECT * FROM leaf_revisions WHERE company_id=? "
                "ORDER BY leaf_code,version", (company,)):
            identity = self._provenance(row["created_at"], intervals)
            records.append({
                "company_id": company, "leaf_code": row["leaf_code"],
                "version": row["version"], "content_sha": row["content_sha"],
                "source_files": self._loads(row["source_files"],
                                            "leaf_revisions.source_files", expect=list),
                "fact_ids": self._loads(row["fact_ids"],
                                        "leaf_revisions.fact_ids", expect=list),
                "owner_skill": row["owner_skill"],
                "attribution_id": row["attribution_id"],
                "change_note": row["change_note"], "created_at": row["created_at"],
                "binding_identity": identity})
            if identity is None:
                unproven.append(f"{row['leaf_code']}@{row['version']}")
        if unproven:
            notes = [f"{len(unproven)} leaf revision(s) have an unprovable "
                     f"creation-time binding: {sorted(unproven)[:10]}"]
            if binding is None:
                notes.append("company has no binding baseline")
            return records, {"status": "ambiguous_provenance", "notes": notes}
        return records, {"status": "ok"}

    def _evidence_links(self, company):
        missing = [name for name in _EVIDENCE_TABLES
                   if not self._table_exists(name)]
        if missing:
            return [], {"status": "table_absent", "notes": sorted(
                f"source registry table {name} is absent" for name in missing)}
        rows = self._select(
            "SELECT l.company_id AS company_id, l.fact_id AS fact_row_id,"
            " l.evidence_id AS evidence_id, l.relation AS relation,"
            " l.status AS status, l.fact_fingerprint AS fact_fingerprint,"
            " l.basis AS basis, l.created_at AS created_at,"
            " e.source_id AS source_id, e.version_id AS version_id,"
            " e.locator AS locator, e.quote_fingerprint AS quote_fingerprint,"
            " e.coordinate AS coordinate, e.authorization AS authorization,"
            " v.content_fingerprint AS content_fingerprint,"
            " v.file_hash AS file_hash,"
            " s.kind AS source_kind, s.lifecycle AS source_lifecycle,"
            " f.id AS linked_fact_row_id, f.entity AS fact_entity,"
            " f.attribute AS fact_attribute, f.period AS fact_period,"
            " f.value AS fact_value, f.unit AS fact_unit,"
            " f.value_type AS fact_value_type, f.source_file AS fact_source_file"
            " FROM fact_evidence_links AS l"
            " LEFT JOIN facts AS f"
            "   ON f.company_id=l.company_id AND f.id=l.fact_id"
            " LEFT JOIN source_evidence AS e"
            "   ON e.company_id=l.company_id AND e.evidence_id=l.evidence_id"
            " LEFT JOIN source_versions AS v"
            "   ON v.company_id=e.company_id AND v.source_id=e.source_id"
            "  AND v.version_id=e.version_id"
            " LEFT JOIN sources AS s"
            "   ON s.company_id=v.company_id AND s.source_id=v.source_id"
            " WHERE l.company_id=?"
            " ORDER BY l.fact_id,l.evidence_id,l.relation", (company,))
        records = []
        dangling = []
        for row in rows:
            if row["source_id"] is None or row["content_fingerprint"] is None \
                    or row["source_kind"] is None or row["linked_fact_row_id"] is None:
                dangling.append((row["fact_row_id"], row["evidence_id"]))
                continue
            if row["source_lifecycle"] not in _SOURCE_LIFECYCLES:
                dangling.append((row["fact_row_id"], row["evidence_id"]))
                continue
            fact_identity_payload = [
                company, row["linked_fact_row_id"], row["fact_entity"],
                row["fact_attribute"], row["fact_period"], row["fact_value"],
                row["fact_unit"], row["fact_value_type"], row["fact_source_file"]]
            expected_fact_fingerprint = "sha_" + _source_digest(
                fact_identity_payload)[:32]
            if row["fact_fingerprint"] != expected_fact_fingerprint:
                dangling.append((row["fact_row_id"], row["evidence_id"]))
                continue
            records.append({
                "company_id": company,
                "fact_row_id": row["fact_row_id"],
                "evidence_id": row["evidence_id"],
                "relation": row["relation"], "status": row["status"],
                "source_id": row["source_id"], "version_id": row["version_id"],
                "locator": row["locator"],
                "coordinate": self._loads(row["coordinate"],
                                          "source_evidence.coordinate"),
                "quote_fingerprint": row["quote_fingerprint"],
                "fact_fingerprint": row["fact_fingerprint"],
                "basis": self._loads(row["basis"], "fact_evidence_links.basis"),
                "authorization": self._loads(row["authorization"],
                                             "source_evidence.authorization"),
                "content_fingerprint": row["content_fingerprint"],
                "file_hash": row["file_hash"],
                "source_kind": row["source_kind"],
                "source_lifecycle": row["source_lifecycle"],
                "created_at": row["created_at"]})
        if dangling:
            return records, {"status": "identity_mismatch", "notes": [
                f"{len(dangling)} evidence link(s) have no resolvable "
                f"fact/source/version chain or have a mismatched fact fingerprint: "
                f"{sorted(dangling)[:10]}"]}
        return records, {"status": "ok"}

    def _source_lifecycle(self, company):
        missing = [name for name in _SOURCE_LIFECYCLE_TABLES
                   if not self._table_exists(name)]
        if missing:
            return [], {"status": "table_absent", "notes": sorted(
                f"source registry table {name} is absent" for name in missing)}
        records = []
        for kind, table in (("source", "sources"),
                            ("source_version", "source_versions"),
                            ("source_event", "source_events")):
            for row in self._select(
                    f"SELECT * FROM {table} WHERE company_id=?", (company,)):
                records.append({"record_kind": kind, **row})
        pending = sorted(record["source_id"] for record in records
                         if record["record_kind"] == "source"
                         and record["lifecycle"] == "pending_removal")
        if pending:
            return records, {"status": "pending_source_withdrawal",
                             "notes": [f"sources pending removal: {pending}"]}
        return records, {"status": "ok"}

    def _open_todos(self, company):
        missing = [name for name in _TODO_TABLES if not self._table_exists(name)]
        records = []
        if self._table_exists("todos"):
            for row in self._select(
                    "SELECT * FROM todos WHERE company_id=? AND status='open' "
                    "ORDER BY id", (company,)):
                records.append({**row, "kind": "todo",
                                "source_status": row["status"]})
        if self._table_exists("md_leaf_todos"):
            for row in self._select(
                    "SELECT * FROM md_leaf_todos WHERE company_id=? "
                    "AND status='pending' ORDER BY operation_id,ordinal", (company,)):
                records.append({**row, "kind": "md_leaf_todo",
                                "source_status": row["status"],
                                "payload": self._loads(row["payload"],
                                                       "md_leaf_todos.payload")})
        if self._table_exists("source_backfill_quarantine"):
            for row in self._select(
                    "SELECT * FROM source_backfill_quarantine WHERE company_id=? "
                    "AND resolved_at IS NULL ORDER BY batch_id,fact_id", (company,)):
                records.append({**row, "kind": "backfill_quarantine",
                                "fact_row_id": row["fact_id"],
                                "source_status": "unresolved",
                                "detail": self._loads(
                                    row["detail"], "source_backfill_quarantine.detail")})
        if missing:
            return records, {"status": "table_absent", "notes": sorted(
                f"todo table {name} is absent; open todos cannot be proven empty"
                for name in missing)}
        return records, {"status": "ok"}

    def _active_tasks(self, company):
        notes = [_ORPHAN_KV_NOTE]
        if not self._table_exists("parse_jobs"):
            return [], {"status": "table_absent",
                        "notes": ["parse_jobs table is absent"] + notes}
        jobs = self._select(
            "SELECT job_id,status,stage,file_hash FROM parse_jobs "
            "WHERE company_id=? ORDER BY job_id", (company,))
        records = []
        issues, pending_removals, active = [], [], []
        for job in jobs:
            if job["status"] in _ACTIVE_JOB_STATUSES:
                active.append(f"parse_job {job['job_id']} status={job['status']}")
                records.append({"company_id": company, "kind": "parse_job",
                                "job_id": job["job_id"], "status": job["status"],
                                "stage": job["stage"]})
            elif job["status"] in _RESUMABLE_JOB_STATUSES:
                # ParseJobManager.retry accepts these states; a future retry
                # could still write facts under the pre-migration contract.
                active.append(f"retryable parse_job {job['job_id']} "
                              f"status={job['status']}")
                records.append({"company_id": company, "kind": "parse_job",
                                "job_id": job["job_id"], "status": "resumable",
                                "source_status": job["status"], "stage": job["stage"]})
        # Exact lease/checkpoint lookups only, keyed by the company's own
        # parse_jobs job_ids; runtime KV is never prefix-scanned. Upload
        # manifests and retry options are also kept under exact job-scoped URIs.
        leases_table = self._table_exists("md_job_leases")
        kv_table = self._table_exists("memory_kv")
        if jobs and not kv_table:
            return records, {"status": "table_absent", "notes": notes + [
                "memory_kv table is absent; checkpoint absence cannot be verified"]}
        if jobs and not leases_table:
            return records, {"status": "table_absent", "notes": notes + [
                "md_job_leases table is absent; lease absence cannot be verified"]}
        now = time.time()
        for job in jobs:
            job_id = job["job_id"]
            lease = self.db.execute(
                "SELECT company_id,owner,generation,expires_at "
                "FROM md_job_leases WHERE job_id=?", (job_id,)).fetchone()
            if lease is not None:
                if lease[0] != company:
                    issues.append(f"lease for job {job_id} belongs to a "
                                  "different company")
                    records.append({"company_id": company, "kind": "job_lease",
                                    "job_id": job_id,
                                    "issue": "lease company mismatch"})
                elif lease[3] > now:
                    active.append(f"job_lease {job_id}")
                    records.append({"company_id": company, "kind": "job_lease",
                                    "job_id": job_id, "status": "active",
                                    "generation": lease[2]})

            direct = self._runtime_record(
                company, job, "md_direct", "read_checkpoint")
            if direct is not None:
                if "issue" in direct:
                    issues.append(f"md-direct checkpoint for job {job_id}: "
                                  f"{direct['issue']}")
                elif job["status"] in _ACTIVE_JOB_STATUSES:
                    active.append(f"read_checkpoint {job_id}")
                    direct["status"] = "active"
                elif job["status"] in _RESUMABLE_JOB_STATUSES:
                    active.append(f"resumable read checkpoint {job_id}")
                    direct["status"] = "resumable"
                elif job["status"] in _TERMINAL_JOB_STATUSES:
                    # A company-bound checkpoint attached to a known terminal
                    # job is historical state. It is included in the snapshot
                    # digest, but does not masquerade as a live worker.
                    direct["status"] = "historical_terminal"
                else:
                    direct["issue"] = "checkpoint/job terminal state is unclear"
                    issues.append(f"md-direct checkpoint for job {job_id}: "
                                  f"{direct['issue']}")
                direct.pop("state", None)
                records.append(direct)

            manifest = self._runtime_record(
                company, job, "upload_manifest", "upload_manifest")
            if manifest is not None:
                if "issue" in manifest:
                    issues.append(f"upload manifest for job {job_id}: "
                                  f"{manifest['issue']}")
                elif job["status"] in _ACTIVE_JOB_STATUSES:
                    active.append(f"upload manifest {job_id}")
                    manifest["status"] = "active"
                elif job["status"] in _RESUMABLE_JOB_STATUSES:
                    active.append(f"resumable upload manifest {job_id}")
                    manifest["status"] = "resumable"
                elif job["status"] in _TERMINAL_JOB_STATUSES:
                    manifest["status"] = "historical_terminal"
                else:
                    manifest["issue"] = "manifest/job terminal state is unclear"
                    issues.append(f"upload manifest for job {job_id}: "
                                  f"{manifest['issue']}")
                records.append(manifest)

            options = self._runtime_record(
                company, job, "upload_options", "upload_options")
            if options is not None:
                if "issue" in options:
                    issues.append(f"upload options for job {job_id}: "
                                  f"{options['issue']}")
                records.append(options)

            if job["status"] not in _KNOWN_JOB_STATUSES:
                issues.append(f"parse job {job_id} has unknown status "
                              f"{job['status']!r}")
        # Verified notes not yet published are pending publication work.
        if self._table_exists("md_read_notes"):
            if not self._table_exists("md_note_publications"):
                return records, {"status": "table_absent", "notes": notes + [
                    "md_note_publications table is absent; unpublished note "
                    "absence cannot be verified"]}
            for row in self._select(
                    "SELECT n.job_id AS job_id, n.note_id AS note_id "
                    "FROM md_read_notes AS n "
                    "WHERE n.company_id=? AND n.status='verified' AND NOT EXISTS ("
                    " SELECT 1 FROM md_note_publications AS p "
                    " WHERE p.company_id=n.company_id AND p.job_id=n.job_id "
                    "  AND p.note_id=n.note_id) ORDER BY n.job_id,n.note_id",
                    (company,)):
                active.append(f"unpublished_note {row['job_id']}/{row['note_id']}")
                records.append({"company_id": company, "kind": "unpublished_note",
                                **row})
        if self._table_exists("source_backfill_batches"):
            for row in self._select(
                    "SELECT batch_id,started_at FROM source_backfill_batches "
                    "WHERE company_id=? AND finished_at IS NULL ORDER BY batch_id",
                    (company,)):
                active.append(f"backfill_batch {row['batch_id']}")
                records.append({"company_id": company, "kind": "backfill_batch",
                                **row})
        if self._table_exists("sources"):
            for row in self._select(
                    "SELECT source_id FROM sources WHERE company_id=? "
                    "AND lifecycle='pending_removal' ORDER BY source_id", (company,)):
                pending_removals.append(row["source_id"])
                records.append({"company_id": company, "kind": "source_removal",
                                "source_id": row["source_id"],
                                "status": "pending_removal"})
        notes += sorted(issues
                        + [f"pending source removal: {source_id}"
                           for source_id in pending_removals]
                        + active)
        if issues:
            status = "unidentified_checkpoint"
        elif pending_removals:
            status = "pending_source_withdrawal"
        elif active:
            status = "active_tasks_present"
        else:
            status = "ok"
        return records, {"status": status, "notes": notes}

    def _runtime_record(self, company, job, uri_kind, record_kind):
        """Read one known runtime KV key for a company-owned job only."""
        row = self.db.execute(
            "SELECT content FROM memory_kv WHERE uri=?",
            (_RUNTIME_URIS[uri_kind] + job["job_id"],)).fetchone()
        if row is None:
            return None
        try:
            content = json.loads(row[0])
        except (ValueError, TypeError):
            return {"company_id": company, "kind": record_kind,
                    "job_id": job["job_id"], "issue": "runtime record is not valid JSON"}
        if not isinstance(content, dict):
            return {"company_id": company, "kind": record_kind,
                    "job_id": job["job_id"], "issue": "runtime record is not a JSON object"}
        try:
            canonical = json.dumps(content, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return {"company_id": company, "kind": record_kind,
                    "job_id": job["job_id"],
                    "issue": "runtime record is not canonicalizable JSON"}
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if uri_kind == "md_direct":
            if content.get("company_id") != company:
                return {"company_id": company, "kind": record_kind,
                        "job_id": job["job_id"],
                        "issue": "checkpoint company differs or is unreadable"}
            if content.get("file_hash") not in (None, job["file_hash"]):
                return {"company_id": company, "kind": record_kind,
                        "job_id": job["job_id"],
                        "issue": "checkpoint file hash differs from its job"}
            return {"company_id": company, "kind": record_kind,
                    "job_id": job["job_id"], "state": content,
                    "content_sha256": digest}
        if uri_kind == "upload_manifest":
            manifest = content.get("manifest")
            if (not isinstance(content.get("staging_id"), str)
                    or not content["staging_id"].strip()
                    or not isinstance(manifest, dict)):
                return {"company_id": company, "kind": record_kind,
                        "job_id": job["job_id"], "issue": "manifest shape is unreadable"}
            if manifest.get("file_hash") != job["file_hash"]:
                return {"company_id": company, "kind": record_kind,
                        "job_id": job["job_id"],
                        "issue": "manifest file hash differs from its job"}
            return {"company_id": company, "kind": record_kind,
                    "job_id": job["job_id"], "staging_id": content["staging_id"],
                    "file_hash": manifest["file_hash"], "content_sha256": digest}
        return {"company_id": company, "kind": record_kind,
                "job_id": job["job_id"], "content_sha256": digest}
