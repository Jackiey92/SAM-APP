"""S1 explicit legacy file-credential backfill.

One company, one explicit call, one atomic batch. The scan replays that
company's full ``facts`` ledger *including superseded rows* and re-expresses
existing file coordinates through the generic source registry:

* verifiable legacy coordinates -> source + version + evidence +
  ``supports / legacy_backfilled`` fact-evidence links;
* anything that cannot be proven at ledger level -> per-batch quarantine;
* old ``facts`` rows are never mutated, and no credential is ever invented.

The status is deliberately ``legacy_backfilled``: S1 records evidence and
migration state only. It is not verified support, not two-point confirmation,
and feeds no trust/count/certification output (those decisions belong to S2).
"""
from datetime import datetime, timezone
import json

from .source_registry import (
    SourceRegistry,
    SourceRejected,
    canonical,
    digest,
)

REASON_MISSING_SOURCE = 'missing_source'
REASON_MISSING_SOURCE_FILE = 'missing_source_file'
REASON_OUTSIDE_COMPANY = 'source_outside_company'
REASON_BAD_COORDINATE = 'unverifiable_coordinate'
QUARANTINE_REASONS = (
    REASON_MISSING_SOURCE,
    REASON_MISSING_SOURCE_FILE,
    REASON_OUTSIDE_COMPANY,
    REASON_BAD_COORDINATE,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _valid_ranges(value):
    if not isinstance(value, list) or not value:
        return None
    ranges = []
    for pair in value:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or type(pair[0]) is not int or type(pair[1]) is not int
                or pair[0] < 0 or pair[1] < pair[0]):
            return None
        ranges.append([pair[0], pair[1]])
    return ranges


MAX_LOCATOR = 2048
MAX_QUOTE = 16000


def _primary_coordinate(span):
    """Return (locator, quote, coordinate) for the fact's own ledger coordinate."""
    if not isinstance(span, dict):
        return None, 'source_span is not an object'
    locator = span.get('source_loc')
    quote = span.get('quote')
    if not isinstance(locator, str) or not locator.strip():
        return None, 'source_span.source_loc missing'
    if len(locator) > MAX_LOCATOR:
        return None, 'source_span.source_loc too large'
    if not isinstance(quote, str) or not quote.strip():
        return None, 'source_span.quote missing'
    if len(quote) > MAX_QUOTE:
        return None, 'source_span.quote too large'
    fingerprint = span.get('mapped_fingerprint')
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return None, 'source_span.mapped_fingerprint missing'
    ranges = _valid_ranges(span.get('ranges'))
    if ranges is None:
        return None, 'source_span.ranges missing or malformed'
    coordinate = {
        'kind': 'fact_source_span',
        'ranges': ranges,
        'mapped_fingerprint': fingerprint,
    }
    if 'reading_protocol' in span:
        coordinate['reading_protocol'] = span['reading_protocol']
    return (locator, quote, coordinate), None


def _field_evidence(span, file_hash):
    """Well-formed field proofs only; malformed auxiliary proofs are skipped."""
    proofs = span.get('field_evidence')
    if not isinstance(proofs, list):
        return [], 0
    out = []
    skipped = 0
    for proof in proofs:
        if not isinstance(proof, dict):
            skipped += 1
            continue
        proof_hash = proof.get('file_hash')
        if proof_hash is not None and proof_hash != file_hash:
            # Never let a proof fragment from another file authorize this fact.
            skipped += 1
            continue
        locator = proof.get('source_loc')
        quote = proof.get('quote')
        fingerprint = proof.get('mapped_fingerprint')
        ranges = _valid_ranges(proof.get('ranges'))
        if (not isinstance(locator, str) or not locator.strip() or len(locator) > MAX_LOCATOR
                or not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE
                or not isinstance(fingerprint, str) or not fingerprint.strip()
                or ranges is None):
            skipped += 1
            continue
        coordinate = {
            'kind': 'field_evidence',
            'ranges': ranges,
            'mapped_fingerprint': fingerprint,
        }
        if proof.get('field') is not None:
            coordinate['field'] = proof['field']
        if proof.get('grant_id') is not None:
            coordinate['grant_id'] = proof['grant_id']
        out.append((locator, quote, coordinate))
    return out, skipped


class SourceBackfill:
    """Explicit, single-company, idempotent legacy-credential backfill."""

    def __init__(self, db_path, *, company_id):
        # Construction is the gate-local schema boundary: registry construction
        # calls require_enabled() and creates the runtime tables explicitly.
        self.registry = SourceRegistry(db_path, company_id=company_id)
        self.db = self.registry.db
        self.company_id = company_id

    def close(self):
        self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _strict_scope(self, file_hash, fact_id):
        """_sources-equivalent authorization, minus the scanned fact itself.

        Including ``facts WHERE id = <the fact under evaluation>`` would let a
        legacy row authorize its own hash circularly; the three visibility
        channels are otherwise identical to LeafPublishService._sources.
        """
        row = self.db.execute(
            'SELECT * FROM source_files WHERE file_hash=?', (file_hash,)).fetchone()
        if row is None:
            return REASON_MISSING_SOURCE_FILE, None
        scoped = self.db.execute('''SELECT 1 FROM file_classifications
                WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM parse_jobs WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM facts WHERE company_id=? AND source_file=? AND id != ?
            LIMIT 1''',
            (self.company_id, file_hash, self.company_id, file_hash,
             self.company_id, file_hash, fact_id)).fetchone()
        if scoped is None:
            return REASON_OUTSIDE_COMPANY, None
        return None, row

    def _quarantine(self, batch_id, fact, reason, detail):
        self.db.execute(
            'INSERT INTO source_backfill_quarantine'
            '(company_id,batch_id,fact_id,reason,detail,created_at) VALUES(?,?,?,?,?,?)',
            (self.company_id, batch_id, fact['id'], reason, canonical(detail), _now()))

    def _mark_resolved(self, fact_id, batch_id):
        return self.db.execute(
            'UPDATE source_backfill_quarantine SET resolved_at=?, resolved_batch_id=? '
            'WHERE company_id=? AND fact_id=? AND resolved_at IS NULL',
            (_now(), batch_id, self.company_id, fact_id)).rowcount

    def run(self, *, request_sha):
        if not isinstance(request_sha, str) or not request_sha.strip() or len(request_sha) > 256:
            raise SourceRejected('backfill request_sha must be a nonempty string of <=256 chars')
        batch_id = 'bf_' + digest([self.company_id, request_sha])[:32]

        prior = self.db.execute(
            'SELECT * FROM source_backfill_batches WHERE company_id=? AND batch_id=?',
            (self.company_id, batch_id)).fetchone()
        if prior is not None and prior['finished_at']:
            receipt = json.loads(prior['result'])
            receipt['replay'] = True
            return receipt

        counts = dict(scanned=0, linked_facts=0, quarantined=0, resolved=0,
                      sources_new=0, versions_new=0, evidence_new=0, links_new=0,
                      skipped_field_proofs=0, by_reason={reason: 0 for reason in QUARANTINE_REASONS})
        linked_fact_ids = []
        quarantined = []
        started_at = _now()
        db = self.db
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute(
                'INSERT OR REPLACE INTO source_backfill_batches'
                '(company_id,batch_id,request_sha,started_at,finished_at,counts,result) '
                'VALUES(?,?,?,?,NULL,NULL,NULL)',
                (self.company_id, batch_id, request_sha, started_at))
            facts = db.execute(
                'SELECT * FROM facts WHERE company_id=? ORDER BY id',
                (self.company_id,)).fetchall()
            for fact in facts:
                counts['scanned'] += 1
                fact_id = fact['id']
                file_hash = fact['source_file']
                identity = dict(fact_id=fact_id, entity=fact['entity'],
                                attribute=fact['attribute'], period=fact['period'],
                                status=fact['status'], superseded_by=fact['superseded_by'],
                                source_file=file_hash)

                if file_hash is None:
                    reason = REASON_MISSING_SOURCE
                else:
                    reason, _file_row = self._strict_scope(file_hash, fact_id)
                if reason is not None:
                    self._quarantine(batch_id, fact, reason, dict(identity, issue=reason))
                    counts['by_reason'][reason] += 1
                    counts['quarantined'] += 1
                    quarantined.append(dict(fact_id=fact_id, reason=reason))
                    continue

                try:
                    span = json.loads(fact['source_span']) if fact['source_span'] else None
                except (ValueError, TypeError):
                    span = None
                primary, issue = _primary_coordinate(span)
                if primary is None:
                    self._quarantine(batch_id, fact, REASON_BAD_COORDINATE,
                                     dict(identity, issue=issue))
                    counts['by_reason'][REASON_BAD_COORDINATE] += 1
                    counts['quarantined'] += 1
                    quarantined.append(dict(fact_id=fact_id, reason=REASON_BAD_COORDINATE))
                    continue

                registered = self.registry.register_file_source(file_hash=file_hash)
                counts['sources_new'] += int(registered['source_created'])
                counts['versions_new'] += int(registered['version_created'])

                records = [primary]
                aux, skipped = _field_evidence(span, file_hash)
                records.extend(aux)
                counts['skipped_field_proofs'] += skipped

                links_for_fact = 0
                for locator, quote, coordinate in records:
                    evidence = self.registry.add_evidence(
                        file_hash=file_hash, locator=locator, quote=quote,
                        coordinate=coordinate,
                        authorization={'mode': 'legacy_ledger', 'batch_id': batch_id})
                    counts['evidence_new'] += int(evidence['created'])
                    link = self.registry.link_fact(
                        fact_id=fact_id, evidence_id=evidence['evidence_id'],
                        relation='supports', status='legacy_backfilled',
                        basis={'batch_id': batch_id, 'mode': 'ledger_only',
                               'verified': False, 'two_point_confirmation': False})
                    counts['links_new'] += int(link['created'])
                    links_for_fact += 1

                if links_for_fact:
                    counts['resolved'] += self._mark_resolved(fact_id, batch_id)
                    counts['linked_facts'] += 1
                    linked_fact_ids.append(fact_id)

            finished_at = _now()
            receipt = dict(batch_id=batch_id, request_sha=request_sha,
                           company_id=self.company_id, started_at=started_at,
                           finished_at=finished_at, counts=counts,
                           linked_fact_ids=linked_fact_ids, quarantined=quarantined,
                           replay=False)
            db.execute(
                'UPDATE source_backfill_batches SET finished_at=?, counts=?, result=? '
                'WHERE company_id=? AND batch_id=?',
                (finished_at, canonical(counts), canonical(receipt),
                 self.company_id, batch_id))
            db.execute(
                'INSERT INTO source_events'
                '(company_id,event_id,action,subject,before_revision,after_revision,task_id,reason,created_at) '
                'VALUES(?,?,?,?,NULL,NULL,?,?,?)',
                (self.company_id,
                 'evt_' + digest([self.company_id, 'backfill_committed', batch_id])[:40],
                 'legacy_backfill_committed',
                 canonical(dict(batch_id=batch_id, counts=counts)),
                 batch_id, 'legacy file credential compatibility backfill', finished_at))
            db.commit()
            return receipt
        except Exception:
            db.rollback()
            raise

    def open_quarantine(self):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM source_backfill_quarantine '
            'WHERE company_id=? AND resolved_at IS NULL ORDER BY batch_id,fact_id',
            (self.company_id,)).fetchall()]

    def batch(self, batch_id):
        row = self.db.execute(
            'SELECT * FROM source_backfill_batches WHERE company_id=? AND batch_id=?',
            (self.company_id, batch_id)).fetchone()
        return dict(row) if row else None
