"""Explicit company binding service, not connected to registration/ingestion."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import RLock

from .. import settings
from ..db.schema import FACTS_TREE_SCHEMA
from ..memory.paths import identifier
from .manifest_registry import ManifestRegistry
from .template_packs import load_pack
from .tree_meta import DIGEST_SOURCES, put_tree_meta, read_tree_meta, require_enabled


class CompanyBindingStore:
    def __init__(self, database: str | Path | sqlite3.Connection, *, is_empty=None,
                 company_id: str | None = None, read_only: bool = False):
        self._owns_db = not isinstance(database, sqlite3.Connection)
        self.read_only = read_only
        if read_only and not self._owns_db:
            raise ValueError('read-only binding requires a database path')
        if self._owns_db and read_only:
            self.db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                      uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(str(database), check_same_thread=False) if self._owns_db else database
        self.company_id = identifier(company_id) if company_id is not None else None
        self.lock = RLock()
        if self.db.in_transaction:
            if self._owns_db:
                self.db.close()
            raise ValueError("binding store requires an idle connection")
        if not read_only:
            self.db.executescript(FACTS_TREE_SCHEMA)
            self.manifests = ManifestRegistry(self.db, ensure_schema=False)
            self._migrate_digest_baseline()
        elif self._binding_table_exists() and not (
                self._has_digest_columns() and self._history_table_exists()
                and self._manifest_table_exists()):
            # A pre-digest/pre-history/pre-manifest binding table must be
            # upgraded by a writable store; a database with no facts-tree
            # tables at all is simply unbound.
            raise ValueError(
                "binding schema predates the digest/history/manifest baseline; "
                "upgrade via a writable store")
        else:
            self.manifests = ManifestRegistry(self.db, ensure_schema=False)
        self.is_empty = is_empty if is_empty is not None else self._is_empty

    def _binding_table_exists(self):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_tree_meta'"
        ).fetchone() is not None

    def _has_digest_columns(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(company_tree_meta)")}
        return {"pack_digest", "digest_source", "binding_revision"} <= columns

    def _history_table_exists(self):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='company_template_binding_history'").fetchone() is not None

    def _manifest_table_exists(self):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='template_manifests'").fetchone() is not None

    def _migrate_digest_baseline(self):
        # Local, backward-compatible column addition for this opt-in table only;
        # each missing column is added independently so partially upgraded
        # schemas are tolerated. Rows that predate the digest baseline are
        # baselined against the exact manifest loadable today and marked
        # 'legacy-bootstrap'. A version that no longer matches cannot be
        # baselined losslessly, so fail closed.
        with self.lock, self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(company_tree_meta)")}
            if "pack_digest" not in columns:
                self.db.execute("ALTER TABLE company_tree_meta ADD COLUMN pack_digest TEXT")
            if "digest_source" not in columns:
                self.db.execute("ALTER TABLE company_tree_meta ADD COLUMN digest_source TEXT "
                                "CHECK(digest_source IN ('legacy-bootstrap','verified-manifest'))")
            revision_added = "binding_revision" not in columns
            if revision_added:
                self.db.execute("ALTER TABLE company_tree_meta ADD COLUMN binding_revision "
                                "INTEGER NOT NULL DEFAULT 1")
            rows = self.db.execute(
                "SELECT company_id,template_pack,pack_version,pack_digest,digest_source "
                "FROM company_tree_meta "
                "WHERE pack_digest IS NULL OR digest_source IS NULL").fetchall()
            baselined = []
            for company, pack_name, version, digest, source in rows:
                template = load_pack(pack_name)
                if template.version != version:
                    raise ValueError(
                        f"cannot baseline digest for {company!r}: bound version {version} "
                        f"of pack {pack_name!r} is unavailable")
                if digest is None and source is None:
                    # Pre-digest row: baseline both columns from today's manifest.
                    self.db.execute(
                        "UPDATE company_tree_meta SET pack_digest=?,digest_source='legacy-bootstrap' "
                        "WHERE company_id=?", (template.manifest_sha256, company))
                elif source is None:
                    # Half-migrated row with a stored digest: never rewrite it.
                    # Adopt it as the baseline only if it matches today's manifest.
                    if digest != template.manifest_sha256:
                        raise ValueError(
                            f"cannot baseline digest for {company!r}: stored digest does not "
                            f"match the current manifest of pack {pack_name!r}")
                    self.db.execute(
                        "UPDATE company_tree_meta SET digest_source='legacy-bootstrap' "
                        "WHERE company_id=?", (company,))
                else:
                    # A declared baseline origin without a digest cannot prove
                    # which manifest it vouches for; fail closed, fill nothing.
                    raise ValueError(
                        f"cannot baseline digest for {company!r}: digest_source {source!r} "
                        "is set but pack_digest is missing")
                baselined.append(company)
            # Baseline history for bindings that predate the history table:
            # exactly one revision-1 row recording the CURRENT state, written
            # now, explicitly stating that earlier history is unknown. The
            # row's bound_at is not claimed to prove any original event time.
            # Idempotent: companies that already have history are skipped.
            now = datetime.now(timezone.utc).isoformat()
            for (company,) in self.db.execute(
                    "SELECT company_id FROM company_tree_meta WHERE company_id NOT IN "
                    "(SELECT company_id FROM company_template_binding_history)").fetchall():
                row = self._raw_row(company)
                self.db.execute(
                    "INSERT INTO company_template_binding_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (company, row["binding_revision"], row["template_pack"], row["pack_version"],
                     row["pack_digest"], None, None, None, now, "system",
                     "legacy-bootstrap baseline of the current binding state; "
                     "earlier history is unknown", None, None, "legacy-bootstrap"))
                baselined.append(company)
            # Archive the currently bound manifest for every binding whose
            # stored identity exactly matches the loadable template. A
            # non-empty legacy digest that disagrees with today's manifest is
            # NOT archived here: it keeps failing closed at read time instead
            # of being laundered through the registry. A conflicting archive
            # for the same pack version fails the whole migration.
            for pack_name, version, digest, source in self.db.execute(
                    "SELECT DISTINCT template_pack,pack_version,pack_digest,digest_source "
                    "FROM company_tree_meta").fetchall():
                template = load_pack(pack_name)
                if (template.version == version and template.manifest_sha256 == digest
                        and source in DIGEST_SOURCES):
                    self.manifests.ensure_archived(
                        template, "system",
                        "legacy-bootstrap archive of the currently bound manifest")
            # Rebuild pre-existing projections from the authoritative rows so a
            # baselined company never exposes a digest/revision-less tree.meta.
            # Company and version are taken from the row, never changed.
            # Skipped when the tree is disabled; the next enabled write
            # re-projects anyway.
            if settings.SAM_FACTS_TREE:
                targets = set(baselined)
                if revision_added:
                    targets.update(r[0] for r in self.db.execute(
                        "SELECT company_id FROM company_tree_meta").fetchall())
                for company in targets:
                    exists = self.db.execute(
                        "SELECT 1 FROM company_tree_meta_projection WHERE company_id=?",
                        (company,)).fetchone()
                    if exists:
                        row = self._raw_row(company)
                        put_tree_meta(self.db, company,
                                      {k: v for k, v in row.items() if k != "company_id"})

    def close(self):
        if self._owns_db:
            with self.lock:
                self.db.close()

    def _require_writable(self):
        if self.read_only:
            raise PermissionError('binding store is read-only')

    def _company(self, company_id):
        company = identifier(company_id)
        if self.company_id is not None and company != self.company_id:
            raise ValueError("company differs from injected host scope")
        return company

    @staticmethod
    def _pack(pack):
        if pack != "generic":
            raise ValueError("segment A supports only generic")
        return load_pack(pack)

    @staticmethod
    def _verify(row):
        # Fail closed: the bound version and manifest digest must both match the
        # template loadable today. Never rewrite a stored digest here.
        template = load_pack(row["template_pack"])
        if template.version != row["pack_version"]:
            raise ValueError(
                f"bound pack version {row['pack_version']} of {row['template_pack']!r} "
                "is unavailable")
        if row["pack_digest"] is None or row["digest_source"] not in DIGEST_SOURCES:
            raise ValueError("binding has no digest baseline")
        if template.manifest_sha256 != row["pack_digest"]:
            raise ValueError(f"manifest digest mismatch for pack {row['template_pack']!r}")

    def _raw_row(self, company):
        if not self._binding_table_exists():
            return None
        cursor = self.db.execute("SELECT * FROM company_tree_meta WHERE company_id=?", (company,))
        row = cursor.fetchone()
        return dict(zip((c[0] for c in cursor.description), row)) if row is not None else None

    def _check_history(self, row):
        # Fail closed unless the append-only history is a valid chain: gap-free
        # revisions from 1, a first row of type initial/legacy-bootstrap with
        # no previous identity, every later row naming the exact identity of
        # its predecessor, and a latest row matching the current binding.
        rows = self.db.execute(
            "SELECT binding_revision,template_pack,pack_version,pack_digest,"
            "prev_template_pack,prev_pack_version,prev_pack_digest,record_type "
            "FROM company_template_binding_history WHERE company_id=? "
            "ORDER BY binding_revision", (row["company_id"],)).fetchall()
        if not rows:
            raise ValueError("binding has no history baseline")
        revisions = [r[0] for r in rows]
        if revisions != list(range(1, len(revisions) + 1)):
            raise ValueError("binding history has a revision gap")
        first = rows[0]
        if first[7] not in ("initial", "legacy-bootstrap"):
            raise ValueError(
                f"binding history starts with an invalid record type {first[7]!r}")
        if any(first[i] is not None for i in (4, 5, 6)):
            raise ValueError("first history row must not name a previous identity")
        for previous, current in zip(rows, rows[1:]):
            if (current[4], current[5], current[6]) != (previous[1], previous[2], previous[3]):
                raise ValueError(
                    "binding history chain does not link consecutive identities")
        if revisions[-1] != row["binding_revision"]:
            raise ValueError("current binding revision differs from the latest history row")
        latest = rows[-1]
        if (latest[1], latest[2], latest[3]) != (
                row["template_pack"], row["pack_version"], row["pack_digest"]):
            raise ValueError("current binding identity differs from the latest history row")

    def _check_manifest(self, row):
        # The current binding must have an exactly matching immutable archive;
        # the registry re-verifies the archived content hash and rejects any
        # digest different from the bound one.
        if self.manifests.get(row["template_pack"], row["pack_version"],
                              expected_digest=row["pack_digest"]) is None:
            raise ValueError("bound manifest has no immutable archive")

    def get(self, company_id):
        company = self._company(company_id)
        with self.lock:
            row = self._raw_row(company)
            if row is None:
                return None
            self._verify(row)
            self._check_history(row)
            self._check_manifest(row)
            return row

    def _is_empty(self, company_id):
        # Metadata is not a fact and lives outside these stores. Also check
        # unprojected atomic records and published leaves (including archived/conflict)
        # so missing vectors or fact-free prose cannot reopen a written tree.
        for table in ("m1_documents", "m1_chunks", "facts", "facts_tree_leaf_index"):
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists and self.db.execute(f"SELECT 1 FROM {table} WHERE company_id=? LIMIT 1", (company_id,)).fetchone():
                return False
        return True

    def _project(self, company):
        row = self.get(company)
        put_tree_meta(self.db, company, {k: v for k, v in row.items() if k != "company_id"},
                      scope=self.company_id)

    def register(self, company_id, display_name, pack="generic"):
        require_enabled()
        self._require_writable()
        company, template = self._company(company_id), self._pack(pack)
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display_name must be nonempty text")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self._raw_row(company) is not None:
                raise ValueError("company already registered")
            self.manifests.ensure_archived(template, "system",
                                           "archived with initial registration")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO company_tree_meta(company_id,display_name,template_pack,"
                "pack_version,bound_at,locked,pack_digest,digest_source,binding_revision) "
                "VALUES(?,?,?,?,?,0,?,'verified-manifest',1)",
                (company, display_name, template.pack, template.version,
                 now, template.manifest_sha256))
            self.db.execute(
                "INSERT INTO company_template_binding_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (company, 1, template.pack, template.version, template.manifest_sha256,
                 None, None, None, now, "system", "initial registration", None, None, "initial"))
            self._project(company)
        return self.get(company)

    def bind_pack(self, company_id, pack):
        require_enabled()
        self._require_writable()
        company, template = self._company(company_id), self._pack(pack)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._raw_row(company)
            if row is None:
                raise ValueError("company not registered")
            verify_error = None
            try:
                self._verify(row)
            except ValueError as exc:
                verify_error = exc
            same_target = (row["template_pack"], row["pack_version"]) == (template.pack, template.version)
            if verify_error is not None:
                # A failed verification may only be recovered through the
                # controlled rebind below: the company must be unlocked and
                # provably empty, and the target must be a different version.
                # Same version with a mismatched or missing digest is data
                # corruption, never rewritten here.
                if row["locked"] or not self.is_empty(company) or same_target:
                    raise verify_error
            elif row["locked"] or not self.is_empty(company):
                raise PermissionError("facts tree is locked or nonempty")
            if not same_target:
                self.manifests.ensure_archived(template, "system",
                                               "archived with controlled rebind")
                now = datetime.now(timezone.utc).isoformat()
                self.db.execute(
                    "UPDATE company_tree_meta SET template_pack=?,pack_version=?,bound_at=?,"
                    "pack_digest=?,digest_source='verified-manifest',binding_revision=? "
                    "WHERE company_id=?",
                    (template.pack, template.version, now, template.manifest_sha256,
                     row["binding_revision"] + 1, company))
                self.db.execute(
                    "INSERT INTO company_template_binding_history "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (company, row["binding_revision"] + 1, template.pack, template.version,
                     template.manifest_sha256, row["template_pack"], row["pack_version"],
                     row["pack_digest"], now, "system",
                     "controlled rebind of an empty, unlocked company", None, None, "rebind"))
            self._project(company)
        return self.get(company)

    def mark_locked(self, company_id):
        require_enabled()
        self._require_writable()
        company = self._company(company_id)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.get(company) is None:
                raise ValueError("company not registered")
            self.db.execute("UPDATE company_tree_meta SET locked=1 WHERE company_id=?", (company,))
            self._project(company)

    def is_locked(self, company_id):
        row = self.get(company_id)
        return bool(row and row["locked"])

    def read_tree_meta(self, company_id):
        with self.lock:
            return read_tree_meta(self.db, self._company(company_id), scope=self.company_id)
