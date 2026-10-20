"""Immutable template manifest registry (P2 §3.1, §7).

Caller-provided sqlite3.Connection only; methods never open their own
transactions. Callers (e.g. CompanyBindingStore) own the transaction
boundary, so an archive write commits or rolls back together with the
binding write it accompanies. The digest and canonical document are always
recomputed from the validated TemplatePack; caller-supplied fields are never
trusted.
"""
from datetime import datetime, timezone
import hashlib
import sqlite3

from ..db.schema import TEMPLATE_MANIFEST_SCHEMA
from .template_packs import manifest_digest, manifest_document, manifest_json

_COLUMNS = ("pack_id", "pack_version", "manifest_sha256", "manifest_json",
            "status", "actor", "reason", "archived_at")


class ManifestRegistry:
    def __init__(self, db, *, ensure_schema=True):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("manifest registry requires a caller-provided sqlite3.Connection")
        if db.in_transaction:
            raise ValueError("manifest registry requires an idle connection")
        self.db = db
        if ensure_schema:
            self.db.executescript(TEMPLATE_MANIFEST_SCHEMA)

    @staticmethod
    def _verify(record):
        if hashlib.sha256(record["manifest_json"].encode("utf-8")).hexdigest() \
                != record["manifest_sha256"]:
            raise ValueError(
                f"archived manifest {record['pack_id']!r}@{record['pack_version']} "
                "failed hash verification")
        return record

    def ensure_archived(self, pack, actor, reason):
        """Idempotently archive a validated TemplatePack's manifest.

        Recomputes the canonical document and digest; an identical archive
        returns the existing row, a conflicting one for the same pack version
        is hard-rejected and never overwritten.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be nonempty text")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be nonempty text")
        document = manifest_document(pack)
        canonical = manifest_json(document)
        digest = manifest_digest(document)
        if digest != pack.manifest_sha256:
            raise ValueError("recomputed manifest digest differs from the validated pack digest")
        cursor = self.db.execute(
            "SELECT * FROM template_manifests WHERE pack_id=? AND pack_version=?",
            (pack.pack, pack.version))
        row = cursor.fetchone()
        if row is not None:
            record = self._verify(dict(zip(_COLUMNS, row)))
            if record["manifest_sha256"] != digest or record["manifest_json"] != canonical:
                raise ValueError(
                    f"conflicting manifest already archived for {pack.pack!r}@{pack.version}")
            return record
        record = {"pack_id": pack.pack, "pack_version": pack.version,
                  "manifest_sha256": digest, "manifest_json": canonical,
                  "status": "published", "actor": actor, "reason": reason,
                  "archived_at": datetime.now(timezone.utc).isoformat()}
        self.db.execute(
            "INSERT INTO template_manifests VALUES(?,?,?,?,?,?,?,?)",
            tuple(record[c] for c in _COLUMNS))
        return record

    def get(self, pack_id, pack_version, *, expected_digest=None):
        """Exact lookup that re-verifies the archived content hash.

        Returns None only when no archive exists; a corrupt archive or a
        digest different from expected_digest fails closed.
        """
        cursor = self.db.execute(
            "SELECT * FROM template_manifests WHERE pack_id=? AND pack_version=?",
            (pack_id, pack_version))
        row = cursor.fetchone()
        if row is None:
            return None
        record = self._verify(dict(zip(_COLUMNS, row)))
        if expected_digest is not None and record["manifest_sha256"] != expected_digest:
            raise ValueError(
                f"archived manifest digest differs from the expected digest for "
                f"{pack_id!r}@{pack_version}")
        return record
