"""Versioned fact placement attribution ledger (P2 design §3.3, §7).

Explicitly written by callers; never scans, backfills or derives placements
from fact_leaf_links or facts.module_key. V4 note publication writes the first
placement of each newly minted fact in the same transaction as that publish;
legacy facts remain untouched. ``resolve_current_identity`` is the verified
read seam for this ledger: one active identity's latest revision, its content
digest, the complete placement history chain, the current pointer and every
event's exact archived binding and manifest are re-checked in a single SQLite
snapshot, failing closed on any drift. Other read and merge flows do not
consult this ledger yet. One fact version (a facts.id mapped by
fact_revisions) has at most one current primary leaf; every reassignment
appends to the append-only history and moves the current pointer in the same
BEGIN IMMEDIATE transaction, so old placements are always preserved.

Only fact versions that already have an identity ledger revision and are
still the current revision of their identity (facts row not closed) may be
placed. The placement target identity is always derived from the company's
exact current binding — never supplied by the caller — and is verified
against the append-only binding history and the immutable manifest archive;
the trunk/leaf pair must exist in that archived manifest. actor/reason are
plain data fields: there is no authentication here and no real user
identity is claimed. Placement retirement is not supported in this slice.

Transaction seams (P2 §5.4): ``place_initial_in_transaction`` and
``reassign_in_transaction`` join a caller-owned transaction so a future
migration executor can place many facts inside its single
``BEGIN IMMEDIATE`` migration unit. They never BEGIN/COMMIT/ROLLBACK the
caller transaction; each single placement write is wrapped in a SAVEPOINT
so a mid-write failure rolls back only its own partial history/pointer
rows and leaves the caller transaction open. The executor must use
``BEGIN IMMEDIATE``: these methods verify a transaction exists but cannot
verify its mode.

Read-only construction (CHG-037): ``read_only=True`` adapts the store to a
caller-provided read-only SQLite connection (for example ``mode=ro``). It
runs no DDL, schema migration, writable PRAGMA or data repair; instead it
verifies with read-only schema queries that every table and column
``resolve_current_identity`` needs already exists, failing closed with a
clear ValueError otherwise — nothing is created or backfilled. Every
placement write entry point is explicitly refused in this mode even on a
writable connection; verified reads keep the same company scope, feature
gate, active identity, latest revision, content digest, placement history
chain, current pointer and exact archived binding/manifest checks. The
caller keeps connection and transaction ownership exactly as in the
default read-write mode.
"""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from threading import RLock

from ..db.database import _migrate_fact_placement_binding_revision
from ..db.schema import FACT_PLACEMENT_SCHEMA
from ..memory.paths import identifier
from .fact_identity import FactIdentityStore
from .tree_meta import DIGEST_SOURCES, require_enabled

_PREV_FIELDS = ("prev_pack_id", "prev_pack_version", "prev_manifest_sha256",
                "prev_binding_revision", "prev_trunk_code", "prev_leaf_code")
_IDENTITY_FIELDS = ("pack_id", "pack_version", "manifest_sha256",
                    "binding_revision", "trunk_code", "leaf_code")

# Tables/columns the verified read seam (resolve_current_identity and its
# helpers) reads. A read-only store checks this baseline with read-only
# schema queries instead of running DDL or migrations; anything missing
# fails closed and is never created or repaired.
_READ_SCHEMA = {
    "fact_identities": ("company_id", "fact_uid", "identity_state"),
    "fact_revisions": ("company_id", "fact_uid", "fact_version", "fact_row_id",
                       "content_sha256", "revision_kind"),
    "facts": ("id", "company_id", "entity", "attribute", "period", "value",
              "value_type", "unit", "superseded_by", "valid_to"),
    "fact_placements": ("company_id", "fact_row_id", "placement_seq", "pack_id",
                        "pack_version", "manifest_sha256", "binding_revision",
                        "trunk_code", "leaf_code") + _PREV_FIELDS,
    "fact_current_placement": ("company_id", "fact_row_id", "placement_seq",
                               "pack_id", "pack_version", "manifest_sha256",
                               "binding_revision", "trunk_code", "leaf_code"),
    "company_template_binding_history": ("company_id", "binding_revision",
                                         "template_pack", "pack_version",
                                         "pack_digest"),
    "template_manifests": ("pack_id", "pack_version", "manifest_sha256",
                           "manifest_json"),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class FactPlacementStore:
    def __init__(self, db, *, company_id=None, read_only=False):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("placement store requires a caller-provided sqlite3.Connection")
        # Checked before any PRAGMA or DDL: a caller transaction is left
        # untouched, never joined and never committed.
        if db.in_transaction:
            raise ValueError("placement store requires an idle connection; "
                             "refusing to join or commit a caller transaction")
        self.db = db
        self.company_id = identifier(company_id) if company_id is not None else None
        self.lock = RLock()
        self.read_only = bool(read_only)
        if self.read_only:
            self._verify_read_schema()
        else:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(FACT_PLACEMENT_SCHEMA)
            _migrate_fact_placement_binding_revision(self.db)

    def _verify_read_schema(self):
        """Fail closed unless the read seam's tables/columns already exist.

        Read-only mode never runs DDL, migrations or repairs; a missing
        table or column is a clear ValueError, not a setup step.
        """
        for table, required in _READ_SCHEMA.items():
            columns = {str(row[1]) for row in
                       self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            if not columns:
                raise ValueError(
                    f"read-only placement store requires existing table "
                    f"{table!r}; refusing to create or repair schema")
            for column in required:
                if column not in columns:
                    raise ValueError(
                        f"read-only placement store requires column "
                        f"{table}.{column}; refusing to create or repair schema")

    def _require_writable(self):
        """Shared checkpoint: read-only mode refuses every placement write."""
        if self.read_only:
            raise ValueError(
                "placement store is read-only; placement writes are refused")

    def _company(self, company_id):
        company = identifier(company_id)
        if self.company_id is not None and company != self.company_id:
            raise ValueError("company differs from injected host scope")
        return company

    @staticmethod
    def _validate_actor_reason(actor, reason):
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be nonempty text")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be nonempty text")

    @staticmethod
    def _validate_target(fact_row_id, trunk_code, leaf_code):
        if type(fact_row_id) is not int or fact_row_id < 1:
            raise ValueError("fact_row_id must be a positive integer")
        if not isinstance(trunk_code, str) or not trunk_code.strip():
            raise ValueError("trunk_code must be nonempty text")
        if not isinstance(leaf_code, str) or not leaf_code.strip():
            raise ValueError("leaf_code must be nonempty text")

    @staticmethod
    def _validate_migration_id(migration_id):
        if migration_id is not None and (
                not isinstance(migration_id, str) or not migration_id.strip()):
            raise ValueError("migration_id must be nonempty text when given")

    # -- read-only verification helpers (run inside the write transaction) --

    def _verify_binding(self, company):
        """Fully verified current binding plus its archived manifest document.

        Same fail-closed rules as CompanyBindingStore._verify/_check_history/
        _check_manifest, re-checked inside the placement transaction so a
        binding change between calls can never be laundered into a placement.
        """
        cursor = self.db.execute(
            "SELECT * FROM company_tree_meta WHERE company_id=?", (company,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"company {company!r} has no binding")
        meta = dict(zip((c[0] for c in cursor.description), row))
        if (meta["pack_digest"] is None
                or meta["digest_source"] not in DIGEST_SOURCES):
            raise ValueError("company binding has no digest baseline")
        history = self.db.execute(
            "SELECT binding_revision,template_pack,pack_version,pack_digest,"
            "prev_template_pack,prev_pack_version,prev_pack_digest,record_type "
            "FROM company_template_binding_history WHERE company_id=? "
            "ORDER BY binding_revision", (company,)).fetchall()
        if not history:
            raise ValueError("company binding has no history baseline")
        revisions = [r[0] for r in history]
        if revisions != list(range(1, len(revisions) + 1)):
            raise ValueError("binding history has a revision gap")
        first = history[0]
        if first[7] not in ("initial", "legacy-bootstrap"):
            raise ValueError(
                f"binding history starts with an invalid record type {first[7]!r}")
        if any(first[i] is not None for i in (4, 5, 6)):
            raise ValueError("first history row must not name a previous identity")
        for previous, current in zip(history, history[1:]):
            if (current[4], current[5], current[6]) != (previous[1], previous[2],
                                                        previous[3]):
                raise ValueError(
                    "binding history chain does not link consecutive identities")
        if revisions[-1] != meta["binding_revision"]:
            raise ValueError(
                "current binding revision differs from the latest history row")
        latest = history[-1]
        if (latest[1], latest[2], latest[3]) != (
                meta["template_pack"], meta["pack_version"], meta["pack_digest"]):
            raise ValueError(
                "current binding identity differs from the latest history row")
        manifest = self.db.execute(
            "SELECT manifest_sha256,manifest_json FROM template_manifests "
            "WHERE pack_id=? AND pack_version=?",
            (meta["template_pack"], meta["pack_version"])).fetchone()
        if manifest is None:
            raise ValueError("bound manifest has no immutable archive")
        if hashlib.sha256(manifest[1].encode("utf-8")).hexdigest() != manifest[0]:
            raise ValueError("archived manifest failed hash verification")
        if manifest[0] != meta["pack_digest"]:
            raise ValueError("archived manifest digest differs from the bound digest")
        try:
            document = json.loads(manifest[1])
        except ValueError as exc:
            raise ValueError("archived manifest is not valid JSON") from exc
        if not isinstance(document, dict):
            raise ValueError("archived manifest must decode to a JSON object")
        return {"pack_id": meta["template_pack"],
                "pack_version": meta["pack_version"],
                "manifest_sha256": meta["pack_digest"],
                "binding_revision": meta["binding_revision"],
                "document": document}

    @staticmethod
    def _verify_leaf(document, trunk_code, leaf_code):
        trunks = document.get("trunks")
        leaves = document.get("leaves")
        if not isinstance(trunks, list) or not isinstance(leaves, list):
            raise ValueError(
                "archived manifest has no verifiable trunk/leaf definitions")
        trunk_codes = {t.get("code") for t in trunks if isinstance(t, dict)}
        if trunk_code not in trunk_codes:
            raise ValueError(f"trunk {trunk_code!r} is not in the bound manifest")
        leaf = next((l for l in leaves
                     if isinstance(l, dict) and l.get("code") == leaf_code), None)
        if leaf is None:
            raise ValueError(f"leaf {leaf_code!r} is not in the bound manifest")
        if leaf.get("trunk") != trunk_code:
            raise ValueError(
                f"leaf {leaf_code!r} does not belong to trunk {trunk_code!r} "
                "in the bound manifest")

    def _validate_fact_version(self, company, fact_row_id):
        """The row must be a mapped, still-current revision of its identity."""
        fact = self.db.execute(
            "SELECT company_id,superseded_by,valid_to FROM facts WHERE id=?",
            (fact_row_id,)).fetchone()
        if fact is None:
            raise ValueError(f"fact row {fact_row_id} does not exist")
        if fact[0] != company:
            raise ValueError(f"fact row {fact_row_id} belongs to a different company")
        mapping = self.db.execute(
            "SELECT fact_uid FROM fact_revisions "
            "WHERE company_id=? AND fact_row_id=?",
            (company, fact_row_id)).fetchone()
        if mapping is None:
            raise ValueError(
                f"fact row {fact_row_id} has no identity ledger revision")
        rows = self.db.execute(
            "SELECT fact_version,fact_row_id FROM fact_revisions "
            "WHERE company_id=? AND fact_uid=? ORDER BY fact_version",
            (company, mapping[0])).fetchall()
        versions = [r[0] for r in rows]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError(f"fact identity {mapping[0]!r} has a revision gap")
        if rows[-1][1] != fact_row_id:
            raise ValueError(
                f"fact row {fact_row_id} is not the current revision of its identity")
        if fact[1] is not None or fact[2] is not None:
            raise ValueError(f"fact row {fact_row_id} is no longer current")

    def _latest_history(self, company, fact_row_id):
        cursor = self.db.execute(
            "SELECT * FROM fact_placements WHERE company_id=? AND fact_row_id=? "
            "ORDER BY placement_seq DESC LIMIT 1", (company, fact_row_id))
        row = cursor.fetchone()
        return (dict(zip((column[0] for column in cursor.description), row))
                if row is not None else None)

    def _pointer(self, company, fact_row_id):
        cursor = self.db.execute(
            "SELECT * FROM fact_current_placement WHERE company_id=? AND fact_row_id=?",
            (company, fact_row_id))
        row = cursor.fetchone()
        return (dict(zip((column[0] for column in cursor.description), row))
                if row is not None else None)

    def _write(self, company, fact_row_id, trunk_code, leaf_code, actor, reason,
               migration_id, *, expected_seq, base_type):
        """Store-owned transaction: BEGIN IMMEDIATE plus commit/rollback."""
        self._require_writable()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self._write_core(company, fact_row_id, trunk_code, leaf_code,
                                    actor, reason, migration_id,
                                    expected_seq=expected_seq, base_type=base_type)

    def _write_joined(self, company, fact_row_id, trunk_code, leaf_code, actor,
                      reason, migration_id, *, expected_seq, base_type):
        """Join a caller-owned transaction; SAVEPOINT-scoped partial rollback.

        The caller transaction is never started, committed or rolled back
        here. A failure anywhere inside the single placement write rolls
        back to the savepoint — undoing any partial history/pointer rows of
        this one write — releases it, and re-raises with the caller
        transaction still open.
        """
        self._require_writable()
        if not self.db.in_transaction:
            raise ValueError(
                "transaction-joining placement writes require a caller-owned "
                "transaction (the migration executor must use BEGIN IMMEDIATE); "
                "refusing to write on an idle connection")
        with self.lock:
            self.db.execute("SAVEPOINT sam_fact_placement_write")
            try:
                result = self._write_core(
                    company, fact_row_id, trunk_code, leaf_code, actor, reason,
                    migration_id, expected_seq=expected_seq, base_type=base_type)
            except Exception:
                self.db.execute("ROLLBACK TO SAVEPOINT sam_fact_placement_write")
                self.db.execute("RELEASE SAVEPOINT sam_fact_placement_write")
                raise
            self.db.execute("RELEASE SAVEPOINT sam_fact_placement_write")
            return result

    def _write_core(self, company, fact_row_id, trunk_code, leaf_code, actor,
                    reason, migration_id, *, expected_seq, base_type):
        """Shared single-transaction placement write.

        expected_seq is the caller's compare-and-swap token: None means the
        fact must have no placement yet (initial), an integer pins the exact
        latest sequence the caller saw (reassign). History append and pointer
        move commit or roll back together.
        """
        binding = self._verify_binding(company)
        self._verify_leaf(binding["document"], trunk_code, leaf_code)
        self._validate_fact_version(company, fact_row_id)
        latest = self._latest_history(company, fact_row_id)
        pointer = self._pointer(company, fact_row_id)
        if expected_seq is None:
            if latest is not None or pointer is not None:
                raise ValueError(
                    f"fact row {fact_row_id} already has a placement; "
                    "use reassign with the expected sequence")
            seq, previous = 1, None
        else:
            if latest is None:
                raise ValueError(
                    f"fact row {fact_row_id} has no placement history; "
                    "use place_initial")
            if latest.get("binding_revision") is None:
                raise ValueError(
                    "latest placement has no proven binding revision; "
                    "refusing to infer historical provenance")
            if pointer is not None and pointer.get("binding_revision") is None:
                raise ValueError(
                    "current placement pointer has no proven binding revision; "
                    "refusing to infer historical provenance")
            if pointer is None or any(pointer[f] != latest[f]
                                      for f in _IDENTITY_FIELDS) \
                    or pointer["placement_seq"] != latest["placement_seq"]:
                raise ValueError(
                    "current placement pointer is inconsistent with the "
                    "latest placement history; refusing to write")
            if latest["placement_seq"] != expected_seq:
                raise ValueError(
                    f"expected placement sequence {expected_seq} does not "
                    f"match the latest sequence {latest['placement_seq']}")
            seq, previous = expected_seq + 1, latest
        now = _now()
        record_type = base_type if migration_id is None else "migration"
        prev = {f: previous[f[5:]] for f in _PREV_FIELDS} if previous else \
            {f: None for f in _PREV_FIELDS}
        identity = {f: binding[f] for f in _IDENTITY_FIELDS[:4]}
        identity["trunk_code"], identity["leaf_code"] = trunk_code, leaf_code
        self.db.execute(
            "INSERT INTO fact_placements "
            "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
            "manifest_sha256,binding_revision,trunk_code,leaf_code,prev_pack_id,"
            "prev_pack_version,prev_manifest_sha256,prev_binding_revision,"
            "prev_trunk_code,prev_leaf_code,event_at,actor,reason,record_type,"
            "migration_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company, fact_row_id, seq, identity["pack_id"],
             identity["pack_version"], identity["manifest_sha256"],
             identity["binding_revision"], trunk_code, leaf_code,
             prev["prev_pack_id"], prev["prev_pack_version"],
             prev["prev_manifest_sha256"], prev["prev_binding_revision"],
             prev["prev_trunk_code"], prev["prev_leaf_code"],
             now, actor, reason, record_type, migration_id))
        if previous is None:
            self.db.execute(
                "INSERT INTO fact_current_placement "
                "(company_id,fact_row_id,placement_seq,pack_id,pack_version,"
                "manifest_sha256,binding_revision,trunk_code,leaf_code,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (company, fact_row_id, seq, identity["pack_id"],
                 identity["pack_version"], identity["manifest_sha256"],
                 identity["binding_revision"], trunk_code, leaf_code, now))
        else:
            cursor = self.db.execute(
                "UPDATE fact_current_placement SET placement_seq=?,pack_id=?,"
                "pack_version=?,manifest_sha256=?,binding_revision=?,"
                "trunk_code=?,leaf_code=?,"
                "updated_at=? WHERE company_id=? AND fact_row_id=? "
                "AND placement_seq=?",
                (seq, identity["pack_id"], identity["pack_version"],
                 identity["manifest_sha256"], identity["binding_revision"],
                 trunk_code, leaf_code, now,
                 company, fact_row_id, expected_seq))
            if cursor.rowcount != 1:
                raise ValueError("current placement pointer changed concurrently")
        return {"company_id": company, "fact_row_id": fact_row_id,
                "placement_seq": seq, "pack_id": identity["pack_id"],
                "pack_version": identity["pack_version"],
                "manifest_sha256": identity["manifest_sha256"],
                "binding_revision": identity["binding_revision"],
                "trunk_code": trunk_code, "leaf_code": leaf_code,
                "record_type": record_type, "migration_id": migration_id,
                "event_at": now}

    def place_initial(self, company_id, fact_row_id, trunk_code, leaf_code,
                      actor, reason, migration_id=None):
        """Record the first placement of one mapped, current fact version.

        The target identity is derived from the company's verified current
        binding; a fact that already has any placement history or pointer is
        rejected, never silently re-placed.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        self._validate_target(fact_row_id, trunk_code, leaf_code)
        self._validate_migration_id(migration_id)
        company = self._company(company_id)
        return self._write(company, fact_row_id, trunk_code, leaf_code,
                           actor, reason, migration_id,
                           expected_seq=None, base_type="initial")

    def reassign(self, company_id, fact_row_id, trunk_code, leaf_code,
                 actor, reason, *, expected_seq, migration_id=None):
        """Append a reassignment and move the current pointer, atomically.

        expected_seq is the compare-and-swap token: it must equal the latest
        history sequence, and the current pointer must be consistent with
        that latest event. Any mismatch fails closed and rolls back; old
        placements are never lost.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        self._validate_target(fact_row_id, trunk_code, leaf_code)
        self._validate_migration_id(migration_id)
        if type(expected_seq) is not int or expected_seq < 1:
            raise ValueError("expected_seq must be a positive integer")
        company = self._company(company_id)
        return self._write(company, fact_row_id, trunk_code, leaf_code,
                           actor, reason, migration_id,
                           expected_seq=expected_seq, base_type="reassign")

    def place_initial_in_transaction(self, company_id, fact_row_id, trunk_code,
                                     leaf_code, actor, reason, migration_id):
        """Joining variant of place_initial for a caller-owned transaction.

        migration_id is mandatory: a placement written inside a migration
        unit must be attributable to it, so record_type is always
        'migration'. All company-scope, binding/manifest, fact-version, leaf
        and CAS validations are identical to place_initial. The caller
        transaction (which a migration executor must open as
        BEGIN IMMEDIATE) stays open on success and on failure.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        self._validate_target(fact_row_id, trunk_code, leaf_code)
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise ValueError(
                "migration_id is required for transaction-joining placement writes")
        company = self._company(company_id)
        return self._write_joined(company, fact_row_id, trunk_code, leaf_code,
                                  actor, reason, migration_id,
                                  expected_seq=None, base_type="initial")

    def place_initial_for_publication_in_transaction(
            self, company_id, fact_row_id, trunk_code, leaf_code, actor, reason):
        """Record a new fact's primary leaf inside the V4 publish transaction.

        Unlike the migration joining API, this writes an ordinary ``initial``
        placement with no migration id. The caller owns BEGIN/COMMIT/ROLLBACK;
        the write is SAVEPOINT-scoped and re-verifies the current company
        binding, archived manifest, fact identity and target leaf.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        self._validate_target(fact_row_id, trunk_code, leaf_code)
        company = self._company(company_id)
        return self._write_joined(company, fact_row_id, trunk_code, leaf_code,
                                  actor, reason, None, expected_seq=None,
                                  base_type="initial")

    def reassign_in_transaction(self, company_id, fact_row_id, trunk_code,
                                leaf_code, actor, reason, *, expected_seq,
                                migration_id):
        """Joining variant of reassign for a caller-owned transaction.

        migration_id is mandatory (record_type is always 'migration');
        expected_seq keeps its compare-and-swap meaning. All validations are
        identical to reassign. The caller transaction stays open on success
        and on failure; a failed write leaves no partial history/pointer
        rows behind.
        """
        require_enabled()
        self._validate_actor_reason(actor, reason)
        self._validate_target(fact_row_id, trunk_code, leaf_code)
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise ValueError(
                "migration_id is required for transaction-joining placement writes")
        if type(expected_seq) is not int or expected_seq < 1:
            raise ValueError("expected_seq must be a positive integer")
        company = self._company(company_id)
        return self._write_joined(company, fact_row_id, trunk_code, leaf_code,
                                  actor, reason, migration_id,
                                  expected_seq=expected_seq, base_type="reassign")

    def history(self, company_id, fact_row_id):
        company = self._company(company_id)
        if type(fact_row_id) is not int or fact_row_id < 1:
            raise ValueError("fact_row_id must be a positive integer")
        cursor = self.db.execute(
            "SELECT * FROM fact_placements WHERE company_id=? AND fact_row_id=? "
            "ORDER BY placement_seq", (company, fact_row_id))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def current(self, company_id, fact_row_id):
        company = self._company(company_id)
        if type(fact_row_id) is not int or fact_row_id < 1:
            raise ValueError("fact_row_id must be a positive integer")
        return self._pointer(company, fact_row_id)

    def resolve_current_identity(self, company_id, fact_uid):
        """Resolve one active logical fact through its exact identity and placement.

        This is the verified read seam for the versioned ledger, not a legacy
        backfill path. It returns ``None`` for an unknown identity and a
        ``placement`` of ``None`` for a registered fact that has not yet been
        placed. Placements attach to an exact fact version: a correction
        appends a new fact row that never inherits the previous version's
        placement, so a freshly revised fact reports ``placement`` of
        ``None`` until it is explicitly placed again; the old version's
        placement history is kept untouched. Existing placement history must
        form a complete chain, the
        mutable pointer must match its final event, the fact body must still
        match its immutable revision digest, and every event must resolve to
        its exact archived binding and manifest. It never guesses a missing
        binding revision or consults ``facts.module_key``/``fact_leaf_links``.
        """
        require_enabled()
        company = self._company(company_id)
        if not isinstance(fact_uid, str) or not fact_uid.strip():
            raise ValueError("fact_uid must be nonempty text")

        # A multi-query ledger read must observe one SQLite snapshot. If the
        # caller already owns a transaction, use it without committing it.
        with self.lock:
            owns_snapshot = not self.db.in_transaction
            if owns_snapshot:
                self.db.execute("BEGIN")
            try:
                result = self._resolve_current_identity(company, fact_uid)
            except Exception:
                if owns_snapshot:
                    self.db.rollback()
                raise
            else:
                if owns_snapshot:
                    self.db.commit()
                return result

    def _resolve_current_identity(self, company, fact_uid):
        identity = self.db.execute(
            "SELECT identity_state FROM fact_identities "
            "WHERE company_id=? AND fact_uid=?", (company, fact_uid)).fetchone()
        if identity is None:
            return None
        if identity[0] != "active":
            raise ValueError(f"fact identity {fact_uid!r} is not active")

        cursor = self.db.execute(
            "SELECT fact_version,fact_row_id,content_sha256,revision_kind "
            "FROM fact_revisions WHERE company_id=? AND fact_uid=? "
            "ORDER BY fact_version", (company, fact_uid))
        revisions = [dict(zip((c[0] for c in cursor.description), row))
                     for row in cursor.fetchall()]
        versions = [row["fact_version"] for row in revisions]
        if not revisions or versions != list(range(1, len(revisions) + 1)):
            raise ValueError(f"fact identity {fact_uid!r} has no complete revision chain")
        revision = revisions[-1]
        fact_row_id = revision["fact_row_id"]
        self._validate_fact_version(company, fact_row_id)
        cursor = self.db.execute("SELECT * FROM facts WHERE id=?", (fact_row_id,))
        row = cursor.fetchone()
        fact = dict(zip((c[0] for c in cursor.description), row))
        if FactIdentityStore._content_digest(fact) != revision["content_sha256"]:
            raise ValueError(
                f"fact row {fact_row_id} differs from its immutable identity revision")

        history = self.history(company, fact_row_id)
        pointer = self._pointer(company, fact_row_id)
        if not history and pointer is None:
            placement = None
        else:
            if not history or pointer is None:
                raise ValueError(
                    "current placement pointer is inconsistent with placement history")
            for index, event in enumerate(history):
                if event["placement_seq"] != index + 1:
                    raise ValueError("placement history has a sequence gap")
                if index == 0:
                    if any(event[field] is not None for field in _PREV_FIELDS):
                        raise ValueError("initial placement must not name a previous placement")
                else:
                    previous = history[index - 1]
                    expected_previous = {
                        "prev_pack_id": previous["pack_id"],
                        "prev_pack_version": previous["pack_version"],
                        "prev_manifest_sha256": previous["manifest_sha256"],
                        "prev_binding_revision": previous["binding_revision"],
                        "prev_trunk_code": previous["trunk_code"],
                        "prev_leaf_code": previous["leaf_code"],
                    }
                    if any(event[field] != value
                           for field, value in expected_previous.items()):
                        raise ValueError("placement history predecessor link is inconsistent")
                self._verify_archived_placement(company, event)

            latest = history[-1]
            if (pointer["placement_seq"] != latest["placement_seq"]
                    or any(pointer[field] != latest[field]
                           for field in _IDENTITY_FIELDS)):
                raise ValueError(
                    "current placement pointer is inconsistent with the latest history event")
            placement = latest

        return {
            "company_id": company,
            "fact_uid": fact_uid,
            "fact_version": revision["fact_version"],
            "fact_row_id": fact_row_id,
            "content_sha256": revision["content_sha256"],
            "revision_kind": revision["revision_kind"],
            "identity_state": identity[0],
            "placement": placement,
        }

    def _verify_archived_placement(self, company, placement):
        revision = placement["binding_revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError(
                "placement has no proven binding revision; refusing to infer provenance")
        binding = self.db.execute(
            "SELECT template_pack,pack_version,pack_digest "
            "FROM company_template_binding_history "
            "WHERE company_id=? AND binding_revision=?", (company, revision)).fetchone()
        if binding is None:
            raise ValueError("placement binding revision is absent from company history")
        if tuple(binding) != (placement["pack_id"], placement["pack_version"],
                              placement["manifest_sha256"]):
            raise ValueError("placement identity differs from its archived company binding")
        archived = self.db.execute(
            "SELECT manifest_sha256,manifest_json FROM template_manifests "
            "WHERE pack_id=? AND pack_version=?",
            (placement["pack_id"], placement["pack_version"])).fetchone()
        if archived is None:
            raise ValueError("placement manifest has no immutable archive")
        digest, manifest_json = archived
        if (digest != placement["manifest_sha256"]
                or hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != digest):
            raise ValueError("placement manifest archive failed digest verification")
        try:
            document = json.loads(manifest_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("placement manifest archive is not valid JSON") from exc
        if not isinstance(document, dict):
            raise ValueError("placement manifest archive must be a JSON object")
        self._verify_leaf(document, placement["trunk_code"], placement["leaf_code"])
