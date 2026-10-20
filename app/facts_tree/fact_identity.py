"""Fact identity, revision and lineage ledger (P2 storage contract).

Explicitly instantiated by callers; never scans or backfills existing facts.
V4 note publication uses the transaction-joining initial-registration method
for newly minted facts; existing legacy facts remain unmapped. Read and merge
paths do not consult this ledger yet. The caller owns the sqlite3.Connection:
the store refuses a connection with an open transaction (before touching
PRAGMA or DDL), and store-owned writes use BEGIN IMMEDIATE.
facts.id stays the permanent fact-row key; fact_uid is the stable logical
identity. One facts row belongs to exactly one identity; nothing here merges
or collapses facts by text similarity. Corrections append a new revision of
the same identity; splits and merges create new identities plus lineage
events instead of stuffing several logical facts into one uid.
"""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from threading import RLock
import uuid

from ..db.schema import FACT_IDENTITY_SCHEMA
from ..memory.paths import identifier
from .tree_meta import require_enabled

EVENT_RELATIONS = {
    "correction": "supersedes",
    "split": "split_into",
    "merge": "merged_into",
    "retract": "retires",
    "migration": "migrated_into",
}
_CONTENT_FIELDS = ("entity", "attribute", "period", "value", "value_type", "unit")
_LEGACY_REASON = ("legacy-bootstrap baseline of an unmapped legacy fact row; "
                  "earlier identity history is unknown")


def _now():
    return datetime.now(timezone.utc).isoformat()


class FactIdentityStore:
    def __init__(self, db, *, company_id=None):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("identity store requires a caller-provided sqlite3.Connection")
        # Checked before any PRAGMA or DDL: a caller transaction is left
        # untouched, never joined and never committed.
        if db.in_transaction:
            raise ValueError("identity store requires an idle connection; "
                             "refusing to join or commit a caller transaction")
        self.db = db
        self.company_id = identifier(company_id) if company_id is not None else None
        self.lock = RLock()
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(FACT_IDENTITY_SCHEMA)

    def _company(self, company_id):
        company = identifier(company_id)
        if self.company_id is not None and company != self.company_id:
            raise ValueError("company differs from injected host scope")
        return company

    @staticmethod
    def _uid(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("fact_uid must be nonempty text")
        return value

    @staticmethod
    def _validate_actor_reason(actor, reason):
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be nonempty text")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be nonempty text")

    def _fact_row(self, company, fact_row_id):
        if type(fact_row_id) is not int or fact_row_id < 1:
            raise ValueError("fact_row_id must be a positive integer")
        cursor = self.db.execute("SELECT * FROM facts WHERE id=?", (fact_row_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"fact row {fact_row_id} does not exist")
        fact = dict(zip((c[0] for c in cursor.description), row))
        if fact["company_id"] != company:
            raise ValueError(f"fact row {fact_row_id} belongs to a different company")
        return fact

    @staticmethod
    def _content_digest(fact):
        canonical = json.dumps({k: fact[k] for k in _CONTENT_FIELDS},
                               ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _row_mapping(self, fact_row_id):
        return self.db.execute(
            "SELECT company_id,fact_uid FROM fact_revisions WHERE fact_row_id=?",
            (fact_row_id,)).fetchone()

    def _revision_rows(self, company, fact_uid):
        rows = self.db.execute(
            "SELECT fact_version,fact_row_id FROM fact_revisions "
            "WHERE company_id=? AND fact_uid=? ORDER BY fact_version",
            (company, fact_uid)).fetchall()
        versions = [r[0] for r in rows]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError(f"fact identity {fact_uid!r} has a revision gap")
        return rows

    def _insert_identity_rows(self, company, fact, fact_uid, kind, actor, reason,
                              *, idempotent_uid=None):
        existing = self._row_mapping(fact["id"])
        if existing is not None:
            if idempotent_uid is not None and existing == (company, idempotent_uid):
                return  # same deterministic identity: idempotent replay
            raise ValueError(f"fact row {fact['id']} already has an identity")
        now = _now()
        self.db.execute("INSERT INTO fact_identities VALUES(?,?,?,?)",
                        (company, fact_uid, now, "active"))
        self.db.execute(
            "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,"
            "content_sha256,revision_kind,supersedes_row_id,actor,reason,created_at) "
            "VALUES(?,?,1,?,?,?,NULL,?,?,?)",
            (company, fact_uid, fact["id"], self._content_digest(fact),
             kind, actor, reason, now))

    def _identity_result(self, fact_row_id):
        mapped = self._row_mapping(fact_row_id)
        if mapped is None:
            raise ValueError(f"fact row {fact_row_id} has no identity after registration")
        recorded = self.db.execute(
            "SELECT revision_kind FROM fact_revisions WHERE fact_row_id=?",
            (fact_row_id,)).fetchone()
        return {"company_id": mapped[0], "fact_uid": mapped[1], "fact_version": 1,
                "fact_row_id": fact_row_id, "revision_kind": recorded[0]}

    def _create_identity(self, company, fact, fact_uid, kind, actor, reason,
                         *, idempotent_uid=None):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._insert_identity_rows(company, fact, fact_uid, kind, actor, reason,
                                       idempotent_uid=idempotent_uid)
        return self._identity_result(fact["id"])

    def _create_identity_joined(self, company, fact, fact_uid, kind, actor, reason):
        if not self.db.in_transaction:
            raise ValueError("transaction-joining identity writes require a caller-owned transaction")
        with self.lock:
            self.db.execute("SAVEPOINT sam_fact_identity_write")
            try:
                self._insert_identity_rows(company, fact, fact_uid, kind, actor, reason)
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT sam_fact_identity_write")
                self.db.execute("RELEASE SAVEPOINT sam_fact_identity_write")
                raise
            self.db.execute("RELEASE SAVEPOINT sam_fact_identity_write")
        return self._identity_result(fact["id"])

    def register_initial(self, company_id, fact_row_id, actor, reason):
        """Create a new logical identity with version 1 for an existing fact row.

        The not-yet-mapped check runs inside the write transaction; a row that
        already belongs to any identity is rejected, never silently re-mapped.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        company = self._company(company_id)
        fact = self._fact_row(company, fact_row_id)
        return self._create_identity(company, fact, f"fact:{uuid.uuid4().hex}",
                                     "initial", actor, reason)

    def register_initial_in_transaction(self, company_id, fact_row_id, actor, reason):
        """Register a new fact identity inside an active caller transaction.

        Intended for V4 publication, where the fact row and its leaf must be
        committed atomically with its identity and primary placement. This
        method owns only a SAVEPOINT; the caller retains transaction control.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        if not self.db.in_transaction:
            raise ValueError("transaction-joining identity writes require a caller-owned transaction")
        company = self._company(company_id)
        fact = self._fact_row(company, fact_row_id)
        return self._create_identity_joined(
            company, fact, f"fact:{uuid.uuid4().hex}", "initial", actor, reason)

    def bootstrap_legacy(self, company_id, fact_row_id):
        """Deterministic legacy identity for one caller-named, unmapped fact row.

        Idempotent only when the row is already mapped to this same legacy
        uid; a row mapped to any other identity is rejected. Never merges
        multiple rows by text.
        """
        require_enabled()
        company = self._company(company_id)
        fact = self._fact_row(company, fact_row_id)
        return self._create_identity(company, fact, f"legacy:{company}:{fact_row_id}",
                                     "legacy-bootstrap", "system", _LEGACY_REASON,
                                     idempotent_uid=f"legacy:{company}:{fact_row_id}")

    def append_revision(self, company_id, fact_uid, new_fact_row_id, actor, reason,
                        kind="correction"):
        """Append the next correction version of an existing identity.

        Only corrections extend an identity. Requires the identity to exist,
        the new fact row to be unmapped and in this company, a gap-free
        revision chain, and the current facts row to still be current
        (superseded_by IS NULL AND valid_to IS NULL). In one transaction: the
        old facts row is closed (superseded_by + valid_to), the new revision
        with actor/reason is recorded, and a correction lineage event with one
        supersedes edge is appended.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        if kind != "correction":
            raise ValueError("revision kind must be 'correction'; use new identities "
                             "plus lineage events for splits and merges")
        company = self._company(company_id)
        fact_uid = self._uid(fact_uid)
        fact = self._fact_row(company, new_fact_row_id)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM fact_identities WHERE company_id=? AND fact_uid=?",
                               (company, fact_uid)).fetchone() is None:
                raise ValueError(f"unknown fact identity {fact_uid!r}")
            if self._row_mapping(new_fact_row_id) is not None:
                raise ValueError(f"fact row {new_fact_row_id} already has an identity")
            # Re-verify inside the transaction: an already closed or expired
            # new row must never become the identity's current revision.
            new_state = self.db.execute(
                "SELECT superseded_by, valid_to FROM facts WHERE id=?",
                (new_fact_row_id,)).fetchone()
            if new_state[0] is not None or new_state[1] is not None:
                raise ValueError(f"fact row {new_fact_row_id} is already closed")
            rows = self._revision_rows(company, fact_uid)
            if not rows:
                raise ValueError(f"fact identity {fact_uid!r} has no revisions")
            old_version, old_row_id = rows[-1]
            current = self.db.execute("SELECT superseded_by, valid_to FROM facts WHERE id=?",
                                      (old_row_id,)).fetchone()
            if current[0] is not None or current[1] is not None:
                raise ValueError(f"current version of {fact_uid!r} is no longer current")
            now = _now()
            cursor = self.db.execute(
                "UPDATE facts SET superseded_by=?, valid_to=? WHERE id=? "
                "AND superseded_by IS NULL AND valid_to IS NULL",
                (new_fact_row_id, now, old_row_id))
            if cursor.rowcount != 1:
                raise ValueError("failed to supersede the old facts row")
            new_version = old_version + 1
            self.db.execute(
                "INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,"
                "content_sha256,revision_kind,supersedes_row_id,actor,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (company, fact_uid, new_version, new_fact_row_id,
                 self._content_digest(fact), "correction", old_row_id, actor, reason, now))
            event_id = f"evt:{uuid.uuid4().hex}"
            self.db.execute(
                "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
                (event_id, company, "correction", actor, reason, None, now))
            self.db.execute(
                "INSERT INTO fact_lineage_edges VALUES(?,?,?,?,?,?,?,?)",
                (event_id, 1, company, fact_uid, old_version,
                 fact_uid, new_version, "supersedes"))
        return {"company_id": company, "fact_uid": fact_uid, "fact_version": new_version,
                "fact_row_id": new_fact_row_id, "revision_kind": "correction",
                "supersedes_row_id": old_row_id, "event_id": event_id}

    def record_lineage(self, company_id, event_type, edges, actor, reason, migration_id=None):
        """Append one lineage event with its edges, atomically.

        Event shapes: correction = exactly one parent→child edge; split = one
        shared parent to at least two distinct child identities; merge = at
        least two distinct parent identities into one shared child; migration =
        one or more complete edges; retract = exactly one parent-only edge (a
        retracted version has no child and is never self-referential). Every
        endpoint must be an existing revision in this company. Facts bodies
        are never copied, merged or rewritten here.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        company = self._company(company_id)
        if event_type not in EVENT_RELATIONS:
            raise ValueError(f"unknown lineage event type {event_type!r}")
        if not isinstance(edges, (list, tuple)) or not edges:
            raise ValueError("lineage event requires at least one edge")
        relation = EVENT_RELATIONS[event_type]
        normalized = []
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) - {
                    "parent_fact_uid", "parent_fact_version",
                    "child_fact_uid", "child_fact_version", "relation"}:
                raise ValueError("invalid lineage edge fields")
            if edge.get("relation", relation) != relation:
                raise ValueError(f"relation is invalid for a {event_type} event")
            if "parent_fact_uid" not in edge or "parent_fact_version" not in edge:
                raise ValueError("lineage edge is missing a parent endpoint")
            parent_uid = self._uid(edge["parent_fact_uid"])
            parent_version = edge["parent_fact_version"]
            if type(parent_version) is not int or parent_version < 1:
                raise ValueError("edge endpoint versions must be positive integers")
            child_uid = edge.get("child_fact_uid")
            child_version = edge.get("child_fact_version")
            if event_type == "retract":
                if child_uid is not None or child_version is not None:
                    raise ValueError("a retract edge names only a parent; there is no child")
                child_uid, child_version = None, None
            else:
                if child_uid is None or child_version is None:
                    raise ValueError("lineage edge is missing a child endpoint")
                child_uid = self._uid(child_uid)
                if type(child_version) is not int or child_version < 1:
                    raise ValueError("edge endpoint versions must be positive integers")
            normalized.append((parent_uid, parent_version, child_uid, child_version))
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate lineage edge")
        parents = {(p, pv) for p, pv, _, _ in normalized}
        children = {(c, cv) for _, _, c, cv in normalized}
        if event_type in ("correction", "retract") and len(normalized) != 1:
            raise ValueError(f"a {event_type} event takes exactly one edge")
        if event_type == "split":
            if len(parents) != 1:
                raise ValueError("split edges must share exactly one parent")
            if len(children) < 2 or len({c for c, _ in children}) < 2:
                raise ValueError("split requires at least two distinct child identities")
        if event_type == "merge":
            if len(children) != 1:
                raise ValueError("merge edges must share exactly one child")
            if len(parents) < 2 or len({p for p, _ in parents}) < 2:
                raise ValueError("merge requires at least two distinct parent identities")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for parent_uid, parent_version, child_uid, child_version in normalized:
                endpoints = [(parent_uid, parent_version)]
                if child_uid is not None:
                    endpoints.append((child_uid, child_version))
                for uid, version in endpoints:
                    if self.db.execute(
                            "SELECT 1 FROM fact_revisions WHERE company_id=? AND fact_uid=? "
                            "AND fact_version=?", (company, uid, version)).fetchone() is None:
                        raise ValueError(
                            f"edge endpoint ({uid!r}, v{version}) does not exist in this company")
            event_id = f"evt:{uuid.uuid4().hex}"
            self.db.execute(
                "INSERT INTO fact_lineage_events VALUES(?,?,?,?,?,?,?)",
                (event_id, company, event_type, actor, reason, migration_id, _now()))
            for seq, (parent_uid, parent_version, child_uid, child_version) in enumerate(
                    normalized, start=1):
                self.db.execute(
                    "INSERT INTO fact_lineage_edges VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, seq, company, parent_uid, parent_version,
                     child_uid, child_version, relation))
        return {"event_id": event_id, "company_id": company, "event_type": event_type,
                "edge_count": len(normalized)}

    def get_identity(self, company_id, fact_uid):
        company = self._company(company_id)
        cursor = self.db.execute("SELECT * FROM fact_identities WHERE company_id=? AND fact_uid=?",
                                 (company, self._uid(fact_uid)))
        row = cursor.fetchone()
        return dict(zip((c[0] for c in cursor.description), row)) if row is not None else None

    def identity_for_row(self, company_id, fact_row_id):
        company = self._company(company_id)
        mapped = self._row_mapping(fact_row_id)
        if mapped is None:
            return None
        if mapped[0] != company:
            raise ValueError(f"fact row {fact_row_id} belongs to a different company")
        return {"company_id": mapped[0], "fact_uid": mapped[1]}

    def revisions(self, company_id, fact_uid):
        company = self._company(company_id)
        fact_uid = self._uid(fact_uid)
        with self.lock:
            self._revision_rows(company, fact_uid)  # fail closed on gaps
            cursor = self.db.execute(
                "SELECT * FROM fact_revisions WHERE company_id=? AND fact_uid=? "
                "ORDER BY fact_version", (company, fact_uid))
            return [dict(zip((c[0] for c in cursor.description), r)) for r in cursor.fetchall()]

    def lineage_events(self, company_id):
        company = self._company(company_id)
        cursor = self.db.execute(
            "SELECT * FROM fact_lineage_events WHERE company_id=? ORDER BY created_at, event_id",
            (company,))
        return [dict(zip((c[0] for c in cursor.description), r)) for r in cursor.fetchall()]

    def lineage_edges(self, company_id):
        company = self._company(company_id)
        cursor = self.db.execute(
            "SELECT * FROM fact_lineage_edges WHERE company_id=? ORDER BY event_id, edge_seq",
            (company,))
        return [dict(zip((c[0] for c in cursor.description), r)) for r in cursor.fetchall()]
