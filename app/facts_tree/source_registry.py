"""S1 gate-local universal source registry.

Company-scoped source / immutable version / evidence / fact-evidence links.
This module only registers provenance. It never:

* judges whether a ``legacy_backfilled`` link is trustworthy support (S2),
* removes or withdraws sources (S3),
* ingests conversation or connector material (S4/S5),
* touches facts, fact_leaf_links, leaf publication, L1/L0 or retrieval.

The tables are created only when a registry is explicitly constructed with
the facts-tree gate open; there is no global schema migration and the closed
default never creates them.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re

from ..db.database import connect
from .leaf_store import LeafRejected
from .tree_meta import require_enabled

SOURCE_KIND_FILE = 'file'
SOURCE_KINDS = ('file', 'conversation', 'external')
SOURCE_ZONES = ('internal', 'quarantine')
EVIDENCE_RELATIONS = ('supports', 'contradicts', 'references')
# S1 registers links only; pending/withdrawn evidence states arrive with S2/S3.
LINK_STATUSES = ('active', 'legacy_backfilled', 'pending_verification')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources (
 company_id TEXT NOT NULL, source_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('file','conversation','external')),
 origin_zone TEXT NOT NULL, origin_key TEXT NOT NULL,
 display_name TEXT NOT NULL,
 lifecycle TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle IN ('active','pending_removal','removed')),
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(company_id,source_id),
 UNIQUE(company_id,kind,origin_zone,origin_key));
CREATE TABLE IF NOT EXISTS source_versions (
 company_id TEXT NOT NULL, source_id TEXT NOT NULL, version_id TEXT NOT NULL,
 seq INTEGER NOT NULL,
 content_fingerprint TEXT NOT NULL,
 file_hash TEXT,
 locator TEXT,
 kind_meta TEXT NOT NULL DEFAULT '{}',
 captured_at TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,source_id,version_id),
 UNIQUE(company_id,source_id,content_fingerprint),
 FOREIGN KEY(company_id,source_id) REFERENCES sources(company_id,source_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_versions_file
 ON source_versions(company_id,file_hash) WHERE file_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS source_evidence (
 company_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 source_id TEXT NOT NULL, version_id TEXT NOT NULL,
 locator TEXT NOT NULL, quote_fingerprint TEXT NOT NULL,
 coordinate TEXT NOT NULL, authorization TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,evidence_id),
 UNIQUE(company_id,source_id,version_id,locator,quote_fingerprint,coordinate),
 FOREIGN KEY(company_id,source_id,version_id)
   REFERENCES source_versions(company_id,source_id,version_id));
CREATE TABLE IF NOT EXISTS fact_evidence_links (
 company_id TEXT NOT NULL, fact_id INTEGER NOT NULL, evidence_id TEXT NOT NULL,
 relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','references')),
 status TEXT NOT NULL CHECK(status IN ('active','legacy_backfilled','pending_verification')),
 fact_fingerprint TEXT NOT NULL,
 basis TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,fact_id,evidence_id,relation),
 FOREIGN KEY(company_id,evidence_id) REFERENCES source_evidence(company_id,evidence_id),
 FOREIGN KEY(fact_id) REFERENCES facts(id));
CREATE TABLE IF NOT EXISTS source_backfill_batches (
 company_id TEXT NOT NULL, batch_id TEXT NOT NULL, request_sha TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, counts TEXT, result TEXT,
 PRIMARY KEY(company_id,batch_id));
CREATE TABLE IF NOT EXISTS source_backfill_quarantine (
 company_id TEXT NOT NULL, batch_id TEXT NOT NULL, fact_id INTEGER NOT NULL,
 reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, resolved_at TEXT, resolved_batch_id TEXT,
 PRIMARY KEY(company_id,batch_id,fact_id));
CREATE INDEX IF NOT EXISTS idx_sbq_open
 ON source_backfill_quarantine(company_id,fact_id) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS source_events (
 company_id TEXT NOT NULL, event_id TEXT NOT NULL,
 action TEXT NOT NULL, subject TEXT NOT NULL,
 before_revision INTEGER, after_revision INTEGER,
 task_id TEXT, reason TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,event_id));
CREATE TRIGGER IF NOT EXISTS sr_sources_no_update BEFORE UPDATE ON sources
 BEGIN SELECT RAISE(ABORT,'append-only source registry'); END;
CREATE TRIGGER IF NOT EXISTS sr_sources_no_delete BEFORE DELETE ON sources
 BEGIN SELECT RAISE(ABORT,'append-only source registry'); END;
CREATE TRIGGER IF NOT EXISTS sr_versions_no_update BEFORE UPDATE ON source_versions
 BEGIN SELECT RAISE(ABORT,'append-only source version'); END;
CREATE TRIGGER IF NOT EXISTS sr_versions_no_delete BEFORE DELETE ON source_versions
 BEGIN SELECT RAISE(ABORT,'append-only source version'); END;
CREATE TRIGGER IF NOT EXISTS sr_evidence_no_update BEFORE UPDATE ON source_evidence
 BEGIN SELECT RAISE(ABORT,'append-only source evidence'); END;
CREATE TRIGGER IF NOT EXISTS sr_evidence_no_delete BEFORE DELETE ON source_evidence
 BEGIN SELECT RAISE(ABORT,'append-only source evidence'); END;
CREATE TRIGGER IF NOT EXISTS sr_events_no_update BEFORE UPDATE ON source_events
 BEGIN SELECT RAISE(ABORT,'append-only source event audit'); END;
CREATE TRIGGER IF NOT EXISTS sr_events_no_delete BEFORE DELETE ON source_events
 BEGIN SELECT RAISE(ABORT,'append-only source event audit'); END;
'''

RUNTIME_TABLES = ('sources', 'source_versions', 'source_evidence', 'fact_evidence_links',
                  'source_backfill_batches', 'source_backfill_quarantine', 'source_events')

_FILE_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
_COMPANY_RE = re.compile(r'^[^\x00\r\n]{1,128}$')


class SourceRejected(LeafRejected):
    """Structural/authorization failure in source registration; no repair by host."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value, *, name, limit, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise SourceRejected(f'{name} must be a nonempty string')
    if len(value) > limit:
        raise SourceRejected(f'{name} exceeds {limit} characters')
    return value


def quote_fingerprint(quote):
    # Whitespace-normalized, matching the literal-evidence reading guards: the
    # same literal at two coordinates is still distinguished by coordinate.
    return hashlib.sha256(''.join(quote.split()).encode()).hexdigest()


class SourceRegistry:
    """Company-scoped source ledger for one explicit gate-open construction."""

    def __init__(self, db_path, *, company_id, connection=None):
        require_enabled()
        if not isinstance(company_id, str) or not _COMPANY_RE.match(company_id or ''):
            raise SourceRejected('source registry requires a host-injected bounded company_id')
        self.db_path = db_path
        self.company_id = company_id
        if connection is not None:
            # Caller-owned connection (e.g. the V4 publication transaction
            # connection): the registry never opens or closes it. The gate
            # schema is ensured here, strictly outside any write transaction;
            # an injected busy connection would let executescript silently
            # commit the caller's pending work.
            if connection.in_transaction:
                raise SourceRejected('injected registry connection must be idle; '
                                     'schema is ensured outside write transactions')
            self.db = connection
            self._owns_db = False
        else:
            self.db = connect(db_path)
            self._owns_db = True
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        if self._owns_db:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _unit(self):
        """Standalone calls own a short write transaction; batch backfill joins."""
        db = self.db
        joined = db.in_transaction
        if not joined:
            db.execute('BEGIN IMMEDIATE')
        try:
            yield
            if not joined:
                db.commit()
        except Exception:
            if not joined:
                db.rollback()
            raise

    def _event(self, action, subject, *, after_revision=None, task_id=None, reason=None):
        event_id = 'evt_' + digest([self.company_id, action, subject])[:40]
        self.db.execute(
            'INSERT OR IGNORE INTO source_events'
            '(company_id,event_id,action,subject,before_revision,after_revision,task_id,reason,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (self.company_id, event_id, action, canonical(subject), None, after_revision,
             task_id, reason, _now()))

    def _file_scope(self, file_hash):
        """Equivalent of LeafPublishService._sources company visibility check."""
        row = self.db.execute('SELECT * FROM source_files WHERE file_hash=?', (file_hash,)).fetchone()
        if row is None:
            raise SourceRejected('file hash is not registered in source_files')
        scoped = self.db.execute('''SELECT 1 FROM file_classifications WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM parse_jobs WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM facts WHERE company_id=? AND source_file=? LIMIT 1''',
            (self.company_id, file_hash, self.company_id, file_hash,
             self.company_id, file_hash)).fetchone()
        if scoped is None:
            raise SourceRejected('file source is outside the host company scope')
        return row

    def register_file_source(self, *, file_hash, origin_zone=None, locator=None,
                             original_name=None, captured_at=None, task_id=None):
        """Register (or return the existing) company file source and its version.

        ``source_id`` is a deterministic company-scoped business identifier and
        is never the raw file hash; the hash stays version metadata.
        """
        if not isinstance(file_hash, str) or not _FILE_HASH_RE.match(file_hash):
            raise SourceRejected('file source requires a sha256 file hash')
        row = self._file_scope(file_hash)
        zone = origin_zone or row['origin_zone'] or 'internal'
        if zone not in SOURCE_ZONES:
            raise SourceRejected(f'unsupported source origin zone: {zone}')
        name = _bounded_text(original_name or row['original_name'], name='original_name', limit=512)
        if locator is not None:
            _bounded_text(locator, name='locator', limit=1024)
        captured = captured_at or _now()
        source_id = 'src_' + digest([self.company_id, SOURCE_KIND_FILE, zone, file_hash])[:40]
        version_id = 'ver_' + digest([self.company_id, source_id, file_hash])[:40]
        kind_meta = canonical(dict(original_name=name))
        with self._unit():
            now = _now()
            created = self.db.execute(
                'INSERT OR IGNORE INTO sources'
                '(company_id,source_id,kind,origin_zone,origin_key,display_name,lifecycle,revision,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,\'active\',1,?,?)',
                (self.company_id, source_id, SOURCE_KIND_FILE, zone, file_hash, name, now, now))
            if created.rowcount:
                self._event('source_registered',
                            dict(source_id=source_id, kind=SOURCE_KIND_FILE,
                                 origin_zone=zone, origin_key=file_hash),
                            after_revision=1, task_id=task_id)
            seq_row = self.db.execute(
                'SELECT COALESCE(MAX(seq),0) AS seq FROM source_versions '
                'WHERE company_id=? AND source_id=?', (self.company_id, source_id)).fetchone()
            versioned = self.db.execute(
                'INSERT OR IGNORE INTO source_versions'
                '(company_id,source_id,version_id,seq,content_fingerprint,file_hash,locator,kind_meta,captured_at,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.company_id, source_id, version_id, seq_row['seq'] + 1, file_hash, file_hash,
                 locator or name, kind_meta, captured, now))
            if versioned.rowcount:
                self._event('source_version_registered',
                            dict(source_id=source_id, version_id=version_id,
                                 content_fingerprint=file_hash, file_hash=file_hash),
                            after_revision=seq_row['seq'] + 1, task_id=task_id)
        return dict(source_id=source_id, version_id=version_id,
                    source_created=bool(created.rowcount), version_created=bool(versioned.rowcount),
                    display_name=name, origin_zone=zone)

    def _resolve_file_version(self, file_hash):
        # Zone is part of source identity; look up the registered row rather than guessing.
        row = self.db.execute(
            'SELECT s.source_id, v.version_id FROM sources AS s '
            'JOIN source_versions AS v ON v.company_id=s.company_id AND v.source_id=s.source_id '
            'WHERE s.company_id=? AND s.kind=? AND s.origin_key=? AND v.file_hash=?',
            (self.company_id, SOURCE_KIND_FILE, file_hash, file_hash)).fetchone()
        if row is None:
            raise SourceRejected('file source/version is not registered for this company')
        return row['source_id'], row['version_id']

    def add_evidence(self, *, locator, quote, coordinate=None, authorization=None,
                     file_hash=None, source_id=None, version_id=None, task_id=None):
        """Register one immutable evidence fragment; replay is identity-idempotent."""
        _bounded_text(locator, name='locator', limit=2048)
        _bounded_text(quote, name='quote', limit=16000)
        if coordinate is None:
            coordinate = {}
        if not isinstance(coordinate, dict):
            raise SourceRejected('evidence coordinate must be an object')
        if authorization is None:
            authorization = {}
        if not isinstance(authorization, dict):
            raise SourceRejected('evidence authorization must be an object')
        if file_hash is not None:
            source_id, version_id = self._resolve_file_version(file_hash)
        elif source_id is not None and version_id is not None:
            row = self.db.execute(
                'SELECT 1 FROM source_versions WHERE company_id=? AND source_id=? AND version_id=?',
                (self.company_id, source_id, version_id)).fetchone()
            if row is None:
                raise SourceRejected('evidence source version is outside this company')
        else:
            raise SourceRejected('evidence requires a registered file_hash or source/version id')
        qfp = quote_fingerprint(quote)
        coordinate_text = canonical(coordinate)
        evidence_id = 'evd_' + digest(
            [self.company_id, source_id, version_id, locator, qfp, coordinate_text])[:40]
        with self._unit():
            now = _now()
            inserted = self.db.execute(
                'INSERT OR IGNORE INTO source_evidence'
                '(company_id,evidence_id,source_id,version_id,locator,quote_fingerprint,coordinate,authorization,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (self.company_id, evidence_id, source_id, version_id, locator, qfp,
                 coordinate_text, canonical(authorization), now))
            if inserted.rowcount:
                self._event('source_evidence_registered',
                            dict(evidence_id=evidence_id, source_id=source_id,
                                 version_id=version_id, locator=locator),
                            task_id=task_id)
        return dict(evidence_id=evidence_id, source_id=source_id, version_id=version_id,
                    created=bool(inserted.rowcount))

    def _require_fact(self, fact_id):
        if type(fact_id) is not int or fact_id < 1:
            raise SourceRejected('fact_id must be a positive integer')
        fact = self.db.execute('SELECT * FROM facts WHERE id=?', (fact_id,)).fetchone()
        if fact is None or fact['company_id'] != self.company_id:
            raise SourceRejected('fact is outside the host company scope')
        return fact

    def link_fact(self, *, fact_id, evidence_id, relation='supports', status='active',
                  basis=None, task_id=None):
        """Attach one evidence record to one company fact. No trust scoring here."""
        if relation not in EVIDENCE_RELATIONS:
            raise SourceRejected(f'relation must be one of {EVIDENCE_RELATIONS}')
        if status not in LINK_STATUSES:
            raise SourceRejected(f'link status must be one of {LINK_STATUSES}')
        if basis is None:
            basis = {}
        if not isinstance(basis, dict):
            raise SourceRejected('link basis must be an object')
        _bounded_text(evidence_id, name='evidence_id', limit=128)
        fact = self._require_fact(fact_id)
        owned = self.db.execute(
            'SELECT 1 FROM source_evidence WHERE company_id=? AND evidence_id=?',
            (self.company_id, evidence_id)).fetchone()
        if owned is None:
            raise SourceRejected('evidence is outside the host company scope')
        fact_fingerprint = 'sha_' + digest([
            self.company_id, fact['id'], fact['entity'], fact['attribute'], fact['period'],
            fact['value'], fact['unit'], fact['value_type'], fact['source_file']])[:32]
        with self._unit():
            now = _now()
            inserted = self.db.execute(
                'INSERT OR IGNORE INTO fact_evidence_links'
                '(company_id,fact_id,evidence_id,relation,status,fact_fingerprint,basis,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (self.company_id, fact_id, evidence_id, relation, status,
                 fact_fingerprint, canonical(basis), now))
            if inserted.rowcount:
                self._event('fact_evidence_linked',
                            dict(fact_id=fact_id, evidence_id=evidence_id,
                                 relation=relation, status=status),
                            task_id=task_id)
        return dict(fact_id=fact_id, evidence_id=evidence_id, relation=relation,
                    status=status, created=bool(inserted.rowcount),
                    fact_fingerprint=fact_fingerprint)

    # -- neutral read APIs (company-scoped; no trust/confirmation claims) --

    def get_source(self, source_id):
        row = self.db.execute('SELECT * FROM sources WHERE company_id=? AND source_id=?',
                              (self.company_id, source_id)).fetchone()
        return dict(row) if row else None

    def links_for_fact(self, fact_id):
        self._require_fact(fact_id)
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM fact_evidence_links WHERE company_id=? AND fact_id=? ORDER BY evidence_id,relation',
            (self.company_id, fact_id)).fetchall()]

    def evidence_for_fact(self, fact_id):
        self._require_fact(fact_id)
        return [dict(r) for r in self.db.execute('''SELECT e.* FROM source_evidence AS e
            JOIN fact_evidence_links AS l ON l.company_id=e.company_id AND l.evidence_id=e.evidence_id
            WHERE l.company_id=? AND l.fact_id=? ORDER BY e.evidence_id''',
            (self.company_id, fact_id)).fetchall()]

    def distinct_source_ids_for_fact(self, fact_id):
        self._require_fact(fact_id)
        return [r[0] for r in self.db.execute('''SELECT DISTINCT e.source_id
            FROM source_evidence AS e
            JOIN fact_evidence_links AS l ON l.company_id=e.company_id AND l.evidence_id=e.evidence_id
            WHERE l.company_id=? AND l.fact_id=? ORDER BY e.source_id''',
            (self.company_id, fact_id)).fetchall()]
