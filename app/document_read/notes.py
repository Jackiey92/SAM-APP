"""Protocol V4 reading notes: persisted structured drafts, never formal facts.

A note is one employee-split atomic fact captured while reading a mapped MD
window. V1 only reads and notes: nothing is written to the L2 fact ledger or to
any leaf table. Notes are revisable, but every revision is append-only audit.

Leaf selection is intentionally absent from this module: notes carry no target
leaf. The V2 publication phase will consume verified notes and enforce exactly
one target leaf per note_id.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from ..db.database import connect
from ..facts_tree.grants import range_is_granted
from ..facts_tree.leaf_store import LeafRejected
from ..facts_tree.md_reader import MDWindows, ComparisonStopped
from ..facts_tree.reading_context import (
    MAX_CONTEXT_BYTES, UNKNOWN_PERIODS, bounded_context, field_evidence)
from ..facts_tree.tree_meta import require_enabled
from ..memory import paths

NOTES_PROTOCOL = 4
NOTES_PROTOCOL_NAME = 'mapped-md-v4'

NOTE_STATUSES = ('verified', 'needs_check', 'rejected')
CREATE_STATUSES = ('verified', 'needs_check')
REVISION_ACTIONS = ('update_status', 'relate', 'question')
RELATION_KINDS = ('same_fact', 'same_metric_diff_period', 'conflicts_with', 'supports', 'related')
# Stage B: one mutually exclusive per-note review decision.
REVIEW_ACTIONS = ('confirmed', 'revise_fact', 'relate', 'mark_needs_check', 'reject')
REVIEW_DECISION_KEYS = frozenset(('note_id', 'action', 'reason', 'fact', 'field_evidence', 'relations'))
REVIEW_FACT_KEYS = ('entity', 'attribute', 'period', 'value', 'unit', 'value_type')

# Durable per-note review item shape. Checkpoints persisted before stage B
# finalization (and entries reset by an explicit retry) may lack individual
# keys; they are backfilled additively, never replacing recorded attempts,
# rejections, revisit audits or receipts.
def review_entry_defaults():
    return dict(attempts=0, tool_rejections=0, stalls=0,
        tool_requests=[], revisit_windows=[], revisit_views=[], rejected_decisions=[],
        feedback=None, blocked=False, round=None, receipt=None, decision=None)


def review_item(items, note_id):
    entry = items.setdefault(note_id, review_entry_defaults())
    if isinstance(entry, dict):
        for key, value in review_entry_defaults().items():
            entry.setdefault(key, deepcopy(value))
    return entry

SCHEMA = '''
CREATE TABLE IF NOT EXISTS md_read_notes (
 company_id TEXT NOT NULL, job_id TEXT NOT NULL, note_id TEXT NOT NULL,
 file_hash TEXT NOT NULL, mapped_fingerprint TEXT NOT NULL,
 window_id TEXT NOT NULL, status TEXT NOT NULL, rev INTEGER NOT NULL,
 request_sha TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(company_id,job_id,note_id));
CREATE TABLE IF NOT EXISTS md_read_note_revisions (
 company_id TEXT NOT NULL, job_id TEXT NOT NULL, note_id TEXT NOT NULL,
 rev INTEGER NOT NULL, action TEXT NOT NULL, request_sha TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,job_id,note_id,rev));
CREATE TABLE IF NOT EXISTS md_read_window_receipts (
 company_id TEXT NOT NULL, job_id TEXT NOT NULL, window_id TEXT NOT NULL,
 request_sha TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,job_id,window_id));
CREATE TABLE IF NOT EXISTS md_read_note_review_receipts (
 company_id TEXT NOT NULL, job_id TEXT NOT NULL, note_id TEXT NOT NULL,
 round INTEGER NOT NULL, request_sha TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,job_id,note_id,round));
CREATE TRIGGER IF NOT EXISTS md_note_revisions_no_update BEFORE UPDATE ON md_read_note_revisions
 BEGIN SELECT RAISE(ABORT,'append-only note revision audit'); END;
CREATE TRIGGER IF NOT EXISTS md_note_revisions_no_delete BEFORE DELETE ON md_read_note_revisions
 BEGIN SELECT RAISE(ABORT,'append-only note revision audit'); END;
CREATE TRIGGER IF NOT EXISTS md_note_review_receipts_no_update BEFORE UPDATE ON md_read_note_review_receipts
 BEGIN SELECT RAISE(ABORT,'append-only note review receipt audit'); END;
CREATE TRIGGER IF NOT EXISTS md_note_review_receipts_no_delete BEFORE DELETE ON md_read_note_review_receipts
 BEGIN SELECT RAISE(ABORT,'append-only note review receipt audit'); END;
'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class NoteRejected(LeafRejected):
    pass


def _bounded_reason(value, *, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise NoteRejected('note operation requires a nonempty employee reason (max 2000 characters)')
    return value


def _fact_from_candidate(candidate):
    fact = {}
    for key in ('entity', 'attribute', 'period', 'value', 'unit', 'value_type'):
        fact[key] = candidate.get(key)
    fact['quote'] = candidate['quote']
    return fact


def _proof_inputs(candidate, grants):
    """Mechanically convert host-verified normalized proofs back into the input
    shape of the literal guard, without trusting their contents.

    Reading produced normalized proofs (ranges=[[start,end]]); the note ledger
    re-runs the same strict guard on the reconstructed inputs, so malformed
    ranges, foreign grants/companies/fingerprints and forged quotes are rejected
    here rather than preserved. The host never fills in missing qualifiers.
    """
    proofs = candidate.get('field_evidence', [])
    if proofs is None:
        return []
    if not isinstance(proofs, list) or len(proofs) > 3:
        raise NoteRejected('candidate field_evidence must contain at most three normalized proofs')
    inputs = []
    for proof in proofs:
        if not isinstance(proof, dict):
            raise NoteRejected('candidate field evidence must be normalized proof objects')
        ranges = proof.get('ranges')
        if (not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], list)
                or len(ranges[0]) != 2 or not all(type(i) is int for i in ranges[0])):
            raise NoteRejected('candidate field evidence must carry exactly one verified [start_line,end_line] range')
        start, end = ranges[0]
        grant_id = proof.get('grant_id')
        if grant_id not in grants:
            raise NoteRejected('candidate field evidence grant is outside read grants')
        inputs.append(dict(field=proof.get('field'), grant_id=grant_id,
            company_id=proof.get('company_id'), file_hash=proof.get('file_hash'),
            mapped_fingerprint=proof.get('mapped_fingerprint'), source_loc=proof.get('source_loc'),
            start_line=start, end_line=end, quote=proof.get('quote')))
    return inputs


class NoteStore:
    """Gate-local note ledger for one reading job. No leaf/fact tables touched."""

    def __init__(self, db_path, *, company_id, job_id, file_hash, mapped_fingerprint, fence=None):
        require_enabled()
        self.db_path = db_path
        self.company_id = company_id
        self.job_id = job_id
        self.file_hash = file_hash
        self.mapped_fingerprint = mapped_fingerprint
        self.fence = fence
        self.db = connect(db_path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def list_compact(self):
        """Employee notebook view: fact fields/coordinates/relations, never requoting originals.

        Original lines stay authorized only through displayed grants; notes must
        not smuggle the full mapped MD back through the notebook.
        """
        rows = self.db.execute(
            'SELECT note_id,status,payload FROM md_read_notes WHERE company_id=? AND job_id=? ORDER BY rowid',
            (self.company_id, self.job_id)).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row['payload'])
            # The notebook review never requotes originals: fact literals stay
            # reachable only through grants the employee actually read.
            fact = {k: v for k, v in payload['fact'].items() if k != 'quote'}
            items.append(dict(note_id=row['note_id'], status=row['status'], fact=fact,
                source_loc=payload['evidence']['primary']['source_loc'],
                topic_hint=payload.get('topic_hint'),
                relations=payload.get('relations', []),
                open_questions=payload.get('open_questions', [])))
        return items

    def note_count(self, status=None):
        sql = 'SELECT count(*) FROM md_read_notes WHERE company_id=? AND job_id=?'
        args = [self.company_id, self.job_id]
        if status is not None:
            sql += ' AND status=?'; args.append(status)
        return self.db.execute(sql, args).fetchone()[0]

    def note_ids(self):
        """Stable job-wide review order, frozen before the review loop."""
        return [r[0] for r in self.db.execute(
            'SELECT note_id FROM md_read_notes WHERE company_id=? AND job_id=? ORDER BY rowid',
            (self.company_id, self.job_id)).fetchall()]

    def note_payload(self, note_id):
        row = self.db.execute(
            'SELECT status,rev,payload,window_id FROM md_read_notes WHERE company_id=? AND job_id=? AND note_id=?',
            (self.company_id, self.job_id, note_id)).fetchone()
        if row is None:
            raise NoteRejected(f'review target {note_id} does not exist in this job')
        return dict(status=row['status'], rev=row['rev'], payload=json.loads(row['payload']),
                    window_id=row['window_id'])

    def note_status(self, note_id):
        row = self.db.execute('SELECT status FROM md_read_notes WHERE company_id=? AND job_id=? AND note_id=?',
            (self.company_id, self.job_id, note_id)).fetchone()
        if row is None:
            raise NoteRejected(f'review target {note_id} does not exist in this job')
        return row['status']

    def review_round(self, note_id):
        """Latest persisted review round for a note, or None when never reviewed."""
        row = self.db.execute('SELECT MAX(round) FROM md_read_note_review_receipts '
            'WHERE company_id=? AND job_id=? AND note_id=?',
            (self.company_id, self.job_id, note_id)).fetchone()
        return row[0]

    def review_receipt(self, note_id):
        row = self.db.execute('SELECT receipt FROM md_read_note_review_receipts '
            'WHERE company_id=? AND job_id=? AND note_id=? ORDER BY round DESC LIMIT 1',
            (self.company_id, self.job_id, note_id)).fetchone()
        return json.loads(row['receipt']) if row is not None else None

    def list_review_index(self):
        """Post-read notebook index with proof metadata but never any quote/md.

        The body of a primary/field witness stays reachable only through an
        explicitly authorized context grant during review; this index carries
        coordinates (field/grant_id/source_loc/ranges), not text.
        """
        rows = self.db.execute(
            'SELECT note_id,status,rev,window_id,payload FROM md_read_notes '
            'WHERE company_id=? AND job_id=? ORDER BY rowid',
            (self.company_id, self.job_id)).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row['payload'])
            evidence = payload['evidence']
            primary = evidence['primary']
            items.append(dict(
                note_id=row['note_id'], status=row['status'], rev=row['rev'], window_id=row['window_id'],
                fact={k: v for k, v in payload['fact'].items() if k != 'quote'},
                primary_ref=dict(grant_id=primary['grant_id'], source_loc=primary['source_loc'],
                    start_line=primary['start_line'], end_line=primary['end_line']),
                field_refs=[dict(field=p['field'], grant_id=p['grant_id'],
                    source_loc=p['source_loc'], ranges=p['ranges'])
                    for p in evidence.get('field_evidence', [])],
                topic_hint=payload.get('topic_hint'),
                relations=payload.get('relations', []),
                open_questions=payload.get('open_questions', [])))
        return items

    def _candidate_for(self, create, candidates, grants):
        index = create.get('candidate_index')
        if type(index) is not int or not 0 <= index < len(candidates):
            raise NoteRejected('create requires a valid candidate_index from this window extract')
        # Fact fields are taken from the host-verified candidate only; the note
        # receipt binds an index and never edits literals/coordinates.
        for key in ('entity', 'attribute', 'period', 'value', 'unit', 'value_type'):
            if key in create:
                raise NoteRejected(f'fact field {key} is host-verified on the candidate; do not resend it in the note receipt')
        candidate = candidates[index]
        grant = grants.get(candidate.get('grant_id'))
        if grant is None:
            raise NoteRejected('candidate grant is outside this window')
        return candidate, grant

    def _build_payload(self, create, candidate, grant, grants):
        note_id = create.get('note_id')
        try:
            paths.identifier(note_id)
        except ValueError as exc:
            raise NoteRejected(str(exc)) from exc
        if self.db.execute('SELECT 1 FROM md_read_notes WHERE company_id=? AND job_id=? AND note_id=?',
                           (self.company_id, self.job_id, note_id)).fetchone():
            raise NoteRejected(f'note_id {note_id} already exists; revise it instead of recreating')
        # Notes never carry leaf decisions in V1.
        for forbidden in ('target_leaf_code', 'leaf_code', 'leaf', 'assignment'):
            if forbidden in create:
                raise NoteRejected(f'notes must not select leaves in the reading phase: {forbidden} forbidden')
        status = create.get('status', 'verified')
        if status not in CREATE_STATUSES:
            raise NoteRejected(f'new note status must be one of {CREATE_STATUSES}')
        topic_hint = create.get('topic_hint')
        if topic_hint is not None and (not isinstance(topic_hint, str) or not topic_hint.strip() or len(topic_hint) > 2000):
            raise NoteRejected('topic_hint must be a nonempty bounded employee string when present')
        open_questions = self._questions(create.get('open_questions', []))
        item = _fact_from_candidate(candidate)
        # Re-verify the reading-stage normalized proofs through the same literal
        # guard rather than copying them blindly; missing qualifiers still reject.
        item['field_evidence'] = _proof_inputs(candidate, grants)
        try:
            proofs = field_evidence(item, grant, [g for gid, g in grants.items() if gid != grant['grant_id']],
                                    self.company_id)
        except ValueError as exc:
            raise NoteRejected(str(exc)[:1000]) from exc
        primary = dict(grant_id=grant['grant_id'], file_hash=grant['file_hash'],
            mapped_fingerprint=grant['mapped_fingerprint'], original_name=grant['original_name'],
            source_loc=candidate['source_loc'], start_line=candidate['start_line'],
            end_line=candidate['end_line'], quote=candidate['quote'])
        # Defense in depth: the reading service already verified all of this;
        # re-check coordinates and literals against the actual granted lines.
        self._check_primary(primary, grant)
        payload = dict(note_id=note_id, status=status,
            fact={k: candidate[k] for k in ('entity', 'attribute', 'period', 'value', 'unit', 'value_type')},
            evidence=dict(primary=primary, field_evidence=proofs),
            relations=[], open_questions=open_questions)
        if topic_hint is not None:
            payload['topic_hint'] = topic_hint
        return note_id, status, payload

    @staticmethod
    def _check_primary(primary, grant):
        for key in ('file_hash', 'mapped_fingerprint', 'source_loc', 'original_name'):
            if primary.get(key) != grant[key]:
                raise NoteRejected(f'note evidence {key} differs from the read grant')
        start, end = primary.get('start_line'), primary.get('end_line')
        if not range_is_granted(grant['lines'], start, end):
            raise NoteRejected('note evidence line range outside read grant')
        quote = primary.get('quote')
        if not isinstance(quote, str) or not quote.strip():
            raise NoteRejected('note requires a nonempty verbatim quote')
        text = ''.join('\n'.join(grant['lines'][str(i)] for i in range(start, end + 1)).split())
        if ''.join(quote.split()) not in text:
            raise NoteRejected('note quote is not present verbatim at the declared granted coordinate')

    @staticmethod
    def _questions(value):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 32 or any(not isinstance(q, str) or not q.strip() or len(q) > 2000 for q in value):
            raise NoteRejected('open_questions must be a list of at most 32 nonempty bounded strings')
        return list(value)

    def _validate_relations(self, relations, available_ids):
        if not isinstance(relations, list) or len(relations) > 32:
            raise NoteRejected('relations must be a list of at most 32 entries')
        seen = set()
        result = []
        for relation in relations:
            if not isinstance(relation, dict):
                raise NoteRejected('relation must be an object')
            kind = relation.get('rel')
            to_note_id = relation.get('to_note_id')
            reason = relation.get('reason')
            if kind not in RELATION_KINDS:
                raise NoteRejected(f'relation rel must be one of {RELATION_KINDS}')
            try:
                paths.identifier(to_note_id)
            except (ValueError, TypeError) as exc:
                raise NoteRejected('relation target must be an existing note identifier') from exc
            if to_note_id not in available_ids:
                raise NoteRejected(f'relation target {to_note_id} is not an existing note of this job')
            _bounded_reason(reason)
            key = (kind, to_note_id)
            if key in seen:
                raise NoteRejected('duplicate relation in the same note operation')
            seen.add(key)
            result.append(dict(rel=kind, to_note_id=to_note_id, reason=reason))
        return result

    def apply_window(self, *, window_id, outcome, creates, revisions, grants, candidates, request_sha):
        """Atomically persist one window note receipt.

        Same request_sha replay returns the stored receipt and appends nothing.
        Leaf/fact tables are never touched here.
        """
        if outcome not in ('noted', 'no_content', 'unresolved'):
            raise NoteRejected('window outcome must be noted, no_content or unresolved')
        if not isinstance(request_sha, str) or not request_sha:
            raise NoteRejected('note receipt requires a stable request idempotency key')
        prior = self.db.execute('SELECT request_sha,receipt FROM md_read_window_receipts '
            'WHERE company_id=? AND job_id=? AND window_id=?',
            (self.company_id, self.job_id, window_id)).fetchone()
        if prior is not None:
            if prior['request_sha'] != request_sha:
                raise NoteRejected('window note receipt already persisted under another request; resume via explicit unresolved retry')
            return json.loads(prior['receipt'])
        for grant in grants.values():
            if grant.get('company_id') != self.company_id or grant.get('file_hash') != self.file_hash:
                raise NoteRejected('note grant is outside the host company/file scope')
        creates = creates if isinstance(creates, list) else None
        revisions = revisions if isinstance(revisions, list) else None
        if creates is None or revisions is None:
            raise NoteRejected('creates and revisions must be arrays')
        if outcome == 'no_content' and (creates or revisions):
            raise NoteRejected('no_content window cannot create or revise notes')
        if outcome == 'noted' and not creates and not revisions:
            raise NoteRejected('noted window requires note creates or revisions; use no_content with a reason otherwise')
        db = self.db
        db.execute('BEGIN IMMEDIATE')
        try:
            if self.fence is not None:
                self.fence.check(db)
            now = datetime.now(timezone.utc).isoformat()
            staged = []
            created_ids = []
            for create in creates:
                if not isinstance(create, dict):
                    raise NoteRejected('note create must be an object')
                candidate, grant = self._candidate_for(create, candidates, grants)
                note_id, status, payload = self._build_payload(create, candidate, grant, grants)
                staged.append((note_id, status, payload, create.get('relations', [])))
                created_ids.append(note_id)
            available = {r['note_id'] for r in db.execute(
                'SELECT note_id FROM md_read_notes WHERE company_id=? AND job_id=?',
                (self.company_id, self.job_id)).fetchall()} | set(created_ids)
            revised_ids = []
            for revision in revisions:
                if not isinstance(revision, dict):
                    raise NoteRejected('note revision must be an object')
                note_id = revision.get('note_id')
                action = revision.get('action')
                try:
                    paths.identifier(note_id)
                except (ValueError, TypeError) as exc:
                    raise NoteRejected('revision requires an existing note_id') from exc
                if action not in REVISION_ACTIONS:
                    raise NoteRejected(f'revision action must be one of {REVISION_ACTIONS}')
                if note_id in created_ids:
                    raise NoteRejected('revise a note in a later window receipt, not in its create receipt')
                row = db.execute('SELECT status,rev,payload FROM md_read_notes WHERE company_id=? AND job_id=? AND note_id=?',
                    (self.company_id, self.job_id, note_id)).fetchone()
                if row is None:
                    raise NoteRejected(f'revision target {note_id} does not exist')
                if row['status'] == 'rejected':
                    # D: an employee-rejected note is a terminal discard record;
                    # it is append-only audit and can never masquerade as verified.
                    raise NoteRejected(f'rejected note {note_id} is terminal; create a new note instead')
                payload = json.loads(row['payload'])
                next_rev = row['rev'] + 1
                reason = _bounded_reason(revision.get('reason'))
                if action == 'update_status':
                    status = revision.get('status')
                    if status not in NOTE_STATUSES:
                        raise NoteRejected(f'status must be one of {NOTE_STATUSES}')
                    payload['status'] = status
                elif action == 'relate':
                    available_for_note = available  # relation targets validated job-wide
                    payload.setdefault('relations', [])
                    payload['relations'].extend(self._validate_relations(revision.get('relations', []), available_for_note))
                else:
                    payload['open_questions'] = payload.get('open_questions', []) + self._questions(revision.get('open_questions', []))
                db.execute('UPDATE md_read_notes SET status=?, rev=?, payload=?, request_sha=?, updated_at=? '
                    'WHERE company_id=? AND job_id=? AND note_id=?',
                    (payload['status'], next_rev, canonical(payload), request_sha, now,
                     self.company_id, self.job_id, note_id))
                db.execute('INSERT INTO md_read_note_revisions VALUES(?,?,?,?,?,?,?,?)',
                    (self.company_id, self.job_id, note_id, next_rev, action, request_sha,
                     canonical(dict(reason=reason, **{k: v for k, v in revision.items()
                                                      if k not in ('note_id', 'action', 'reason')})), now))
                revised_ids.append(note_id)
            for note_id, status, payload, relations in staged:
                # An unresolved window cannot dress its notes up as verified.
                if outcome == 'unresolved':
                    status = 'needs_check'
                    payload['status'] = status
                payload['relations'] = self._validate_relations(relations, available - {note_id})
                db.execute('INSERT INTO md_read_notes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (self.company_id, self.job_id, note_id, self.file_hash, self.mapped_fingerprint,
                     window_id, status, 1, request_sha, canonical(payload), now))
                db.execute('INSERT INTO md_read_note_revisions VALUES(?,?,?,?,?,?,?,?)',
                    (self.company_id, self.job_id, note_id, 1, 'create', request_sha,
                     canonical(deepcopy(payload)), now))
            receipt = dict(window_id=window_id, outcome=outcome, created=created_ids, revised=revised_ids,
                           mapped_fingerprint=self.mapped_fingerprint)
            db.execute('INSERT INTO md_read_window_receipts VALUES(?,?,?,?,?,?)',
                (self.company_id, self.job_id, window_id, request_sha, canonical(receipt), now))
            if self.fence is not None:
                self.fence.check(db)
            db.commit()
            return receipt
        except Exception:
            db.rollback()
            raise


    def _review_relations(self, payload, relations):
        available = {r['note_id'] for r in self.db.execute(
            'SELECT note_id FROM md_read_notes WHERE company_id=? AND job_id=?',
            (self.company_id, self.job_id)).fetchall()} - {payload['note_id']}
        validated = self._validate_relations(relations, available)
        existing = {(r['rel'], r['to_note_id']) for r in payload.get('relations', [])}
        if any((r['rel'], r['to_note_id']) in existing for r in validated):
            raise NoteRejected('review relation already exists; host does not add duplicate edges')
        return validated

    def apply_review(self, *, note_id, action, reason, expected_round, request_sha,
                     grants, fact=None, proofs=None, relations=None):
        """Atomically persist one post-read review decision for one note.

        The employee fact/evidence is never trusted because it was in a prior
        note: the origin grant is rebuilt by the host and every literal is
        re-verified here. The revision (full old/new payload snapshot) and the
        per-round review receipt commit in one transaction or not at all.
        """
        if action not in REVIEW_ACTIONS:
            raise NoteRejected(f'review action must be one of {REVIEW_ACTIONS}')
        if not isinstance(request_sha, str) or not request_sha:
            raise NoteRejected('review decision requires a stable request idempotency key')
        if type(expected_round) is not int or expected_round < 1:
            raise NoteRejected('review round must be a positive integer')
        if not isinstance(grants, dict) or not grants:
            raise NoteRejected('review decision requires the grants displayed this review')
        for grant in grants.values():
            if not isinstance(grant, dict) or grant.get('company_id') != self.company_id \
                    or grant.get('file_hash') != self.file_hash:
                raise NoteRejected('review grant is outside the host company/file scope')
        if proofs is not None and not isinstance(proofs, list):
            raise NoteRejected('field_evidence must be a list of at most three field witnesses')
        reason = _bounded_reason(reason)
        db = self.db
        db.execute('BEGIN IMMEDIATE')
        try:
            if self.fence is not None:
                self.fence.check(db)
            row = db.execute(
                'SELECT status,rev,payload FROM md_read_notes WHERE company_id=? AND job_id=? AND note_id=?',
                (self.company_id, self.job_id, note_id)).fetchone()
            if row is None:
                raise NoteRejected(f'review target {note_id} does not exist')
            prior = db.execute(
                'SELECT round,request_sha FROM md_read_note_review_receipts '
                'WHERE company_id=? AND job_id=? AND note_id=? ORDER BY round DESC LIMIT 1',
                (self.company_id, self.job_id, note_id)).fetchone()
            if row['status'] == 'rejected':
                # Terminality outranks round handling: a discarded note can never
                # gain another review receipt, replay or new round.
                raise NoteRejected(f'rejected note {note_id} is terminal; create a new note instead')
            last_round = prior['round'] if prior is not None else 0
            if expected_round != last_round + 1:
                if prior is not None and expected_round == prior['round']:
                    if prior['request_sha'] == request_sha:
                        # Exact same-round replay returns the stored receipt.
                        stored = db.execute(
                            'SELECT receipt FROM md_read_note_review_receipts '
                            'WHERE company_id=? AND job_id=? AND note_id=? AND round=?',
                            (self.company_id, self.job_id, note_id, expected_round)).fetchone()
                        db.rollback()
                        return json.loads(stored['receipt'])
                    raise NoteRejected('note review round already persisted under another request; '
                                       'resume via explicit unresolved retry')
                raise NoteRejected('review round mismatch; explicit unresolved retry opens a new round')
            payload = json.loads(row['payload'])
            old_payload = deepcopy(payload)
            now = datetime.now(timezone.utc).isoformat()
            next_rev = row['rev'] + 1
            revision_action = None
            audit_payload = None
            new_status = row['status']
            if action == 'confirmed':
                if row['status'] != 'verified':
                    # A bare confirmation cannot quietly upgrade an open note.
                    raise NoteRejected('needs_check note cannot be confirmed without new verified '
                                       'evidence; use revise_fact with literal field_evidence, '
                                       'mark_needs_check, or reject')
            elif action == 'relate':
                if not isinstance(relations, list) or not relations:
                    raise NoteRejected('relate review requires a nonempty relations array')
                added = self._review_relations(payload, relations)
                payload.setdefault('relations', []).extend(added)
                revision_action = 'relate'
                audit_payload = canonical(dict(reason=reason, relations=added))
            elif action == 'mark_needs_check':
                new_status = 'needs_check'
                payload['status'] = new_status
                revision_action = 'update_status'
                audit_payload = canonical(dict(reason=reason, action=action, status=new_status))
            elif action == 'reject':
                new_status = 'rejected'
                payload['status'] = new_status
                revision_action = 'update_status'
                audit_payload = canonical(dict(reason=reason, action=action, status=new_status))
            else:  # revise_fact
                if not isinstance(fact, dict) or set(fact) != set(REVIEW_FACT_KEYS):
                    raise NoteRejected('revise_fact requires complete replacement fact fields')
                if any(not isinstance(fact[k], str) or not fact[k].strip()
                       for k in REVIEW_FACT_KEYS if k != 'unit'):
                    raise NoteRejected('revise_fact fields require explicit nonempty strings')
                if not isinstance(fact['unit'], str):
                    raise NoteRejected('revise_fact unit must be a string (use "" for genuinely unitless)')
                primary = payload['evidence']['primary']
                origin = next((g for g in grants.values()
                               if g.get('grant_id') == primary['grant_id']), None)
                if origin is None:
                    raise NoteRejected('revise_fact origin grant was not displayed this review')
                self._check_primary(primary, origin)
                # Re-run the same literal guard on employee input-shape proofs;
                # field_evidence rebuilds each normalized proof and refuses
                # non-verbatim evidence, so employee scope fields are not trusted.
                item = {**{k: fact[k] for k in REVIEW_FACT_KEYS},
                        'quote': primary['quote'],
                        'field_evidence': proofs if proofs is not None else []}
                try:
                    verified_proofs = field_evidence(
                        item, origin,
                        [g for g in grants.values() if g is not origin],
                        self.company_id)
                except ValueError as exc:
                    raise NoteRejected(str(exc)[:1000]) from exc
                if fact['period'] in UNKNOWN_PERIODS:
                    raise NoteRejected('a verified revised fact cannot retain an unknown period; '
                                       'supply verbatim period field_evidence or mark_needs_check')
                added_relations = []
                if relations:
                    added_relations = self._review_relations(payload, relations)
                    payload.setdefault('relations', []).extend(added_relations)
                payload['fact'] = {k: fact[k] for k in REVIEW_FACT_KEYS}
                payload['evidence']['field_evidence'] = verified_proofs
                new_status = 'verified'
                payload['status'] = new_status
                revision_action = 'revise_fact'
                # Full before/after payload snapshots plus employee reason, per note.
                audit_payload = canonical(dict(reason=reason, old=old_payload, new=deepcopy(payload)))
            if revision_action is not None:
                # Optimistic CAS on the rev loaded at the top of this transaction.
                changed = db.execute('UPDATE md_read_notes SET status=?, rev=?, payload=?, request_sha=?, updated_at=? '
                    'WHERE company_id=? AND job_id=? AND note_id=? AND rev=?',
                    (new_status, next_rev, canonical(payload), request_sha, now,
                     self.company_id, self.job_id, note_id, row['rev']))
                if changed.rowcount != 1:
                    raise NoteRejected('note revision CAS lost; reload the current note before review')
                db.execute('INSERT INTO md_read_note_revisions VALUES(?,?,?,?,?,?,?,?)',
                    (self.company_id, self.job_id, note_id, next_rev, revision_action,
                     request_sha, audit_payload, now))
            receipt = dict(note_id=note_id, round=expected_round, action=action,
                rev=next_rev if revision_action is not None else row['rev'],
                status=new_status, mapped_fingerprint=self.mapped_fingerprint)
            db.execute('INSERT INTO md_read_note_review_receipts VALUES(?,?,?,?,?,?,?)',
                (self.company_id, self.job_id, note_id, expected_round,
                 request_sha, canonical(receipt), now))
            if self.fence is not None:
                self.fence.check(db)
            db.commit()
            return receipt
        except Exception:
            db.rollback()
            raise


def note_receipt_contract():
    return dict(action='plan', operation='note_receipt', protocol=NOTES_PROTOCOL_NAME,
        json_schema=dict(type='object',
            required=['outcome', 'creates', 'revisions', 'retain_context_windows', 'retention_reason'],
            properties=dict(
                outcome=dict(type='string', enum=['noted', 'no_content', 'unresolved']),
                creates=dict(type='array', items=dict(type='object',
                    required=['note_id', 'candidate_index'],
                    properties=dict(
                        note_id=dict(type='string', pattern=r'^[A-Za-z0-9-]{1,128}$'),
                        candidate_index=dict(type='integer', minimum=0),
                        status=dict(type='string', enum=list(CREATE_STATUSES)),
                        topic_hint=dict(type='string', maxLength=2000),
                        relations=dict(type='array', maxItems=32),
                        open_questions=dict(type='array', maxItems=32)),
                    additionalProperties=False)),
                revisions=dict(type='array', items=dict(type='object',
                    required=['note_id', 'action', 'reason'],
                    properties=dict(action=dict(type='string', enum=list(REVISION_ACTIONS))))),
                retain_context_windows=dict(type='array', maxItems=2, uniqueItems=True,
                    items=dict(type='string')),
                retention_reason=dict(type='string', minLength=1, pattern=r'\S'),
                reason=dict(type='string', maxLength=2000))),
        outcomes=['noted', 'no_content', 'unresolved'],
        retention=('Every window explicitly select <=2 current or rolling/retained context grant IDs, or []; '
                   'request_context revisit IDs are temporary per-window and must not be retained. '
                   'Nonempty retention_reason required. Employee judges applicability; host never auto-selects qualifiers.'),
        candidate_binding='Each create binds candidate_index to THIS window candidates; fact fields are taken from the host-verified candidate and must not be edited. Splitting/dedup is the employee call: later evidence of the same fact becomes a revision/relation to the existing note, never a second note for another leaf.',
        no_leaf='Reading phase: no target_leaf_code, no blocks, no leaf assignment. Top1 leaf selection happens only in the later publication phase over the complete notebook.',
        revisions=dict(update_status='status verified|needs_check|rejected + explicit nonempty reason; a rejected note is a terminal append-only discard that can never be revised back or publish as verified; rejected notes do not block file completion and are counted separately',
            relate='relations: [{rel, to_note_id, reason}], rel one of ' + ','.join(RELATION_KINDS),
            question='open_questions strings + reason; all revisions append audit, nothing is silently overwritten'),
        example=dict(outcome='noted',
            creates=[dict(note_id='n-0001', candidate_index=0, status='verified',
                topic_hint='员工可选的主题提示，不是叶子选择', relations=[], open_questions=[])],
            revisions=[], retain_context_windows=[], retention_reason='员工判断无需为后续窗保留本窗原文'),
        empty_example=dict(outcome='no_content', creates=[], revisions=[], reason='本窗只有提示性文字，无原子事实',
            retain_context_windows=[], retention_reason='员工判断本窗限定不约束后续材料'),
        unresolved_example=dict(outcome='unresolved', creates=[], revisions=[], reason='依据不足，无法判定，保留未解决',
            retain_context_windows=[], retention_reason='员工显式保留或释放并说明'))


def note_review_contract():
    return dict(action='review', operation='note_review', protocol=NOTES_PROTOCOL_NAME,
        scope=('Review exactly the single current note after the whole mapped MD was read. '
               'The review_index carries coordinates and fact fields but never quotes; body text is '
               'available only through the origin window plus explicitly granted request_context revisits.'),
        json_schema=dict(type='object',
            required=['note_id', 'action', 'reason'],
            properties=dict(
                note_id=dict(type='string', description='must equal the current note_id, one note per turn'),
                action=dict(type='string', enum=list(REVIEW_ACTIONS)),
                reason=dict(type='string', minLength=1, maxLength=2000),
                fact=dict(type='object', description='revise_fact only: complete replacement fact fields',
                    required=list(REVIEW_FACT_KEYS),
                    properties=dict(entity=dict(type='string'), attribute=dict(type='string'),
                        period=dict(type='string'), value=dict(type='string'),
                        unit=dict(type='string', description='"" for genuinely unitless; null is a type error'),
                        value_type=dict(type='string')),
                    additionalProperties=False),
                field_evidence=dict(type='array', maxItems=3,
                    description='revise_fact only: verbatim field witnesses, input shape start_line/end_line',
                    items=dict(type='object',
                        required=['field', 'grant_id', 'company_id', 'file_hash',
                                  'mapped_fingerprint', 'source_loc', 'start_line', 'end_line', 'quote'])),
                relations=dict(type='array', maxItems=32,
                    description='relate or revise_fact only; employee-declared edges, host adds no reverse edge'))),
        actions=dict(
            confirmed='note already carries correct host-verified fact and evidence; writes only a review receipt, '
                      'never upgrades a needs_check note without new evidence',
            revise_fact='complete replacement fact + reason + per-field verbatim field_evidence from the origin '
                        'or a granted revisit; host re-runs attribution range/verify_candidate/field_evidence; '
                        'no date reformat, unit inference, arithmetic or conflict adjudication; unknown period / '
                        'empty unit stays needs_check, it can never be confirmed as verified',
            relate='add employee-declared relations [{rel,to_note_id,reason}] to existing notes only; '
                   'no duplicate edge, no auto reverse edge, no host comparability judgment; '
                   'conflicts_with (e.g. same metric different period with differing values) requires this explicit '
                   'submission with both values preserved',
            mark_needs_check='keep the note open with reason; open notes block any future publication',
            reject='discard this note with reason; terminal append-only audit, never publishes and never reverts'),
        mutex=('Exactly one action for exactly the current note_id. No batches, no unknown keys, no multiple actions. '
               'fact/field_evidence accompany revise_fact only; relations accompany relate or revise_fact only.'),
        request_context_alternative=dict(
            receipt=dict(outcome='request_context', request_context_windows=['<grant_id from read_grant_index>'],
                         reason='nonempty reason, max 2000 characters'),
            mutex='a request_context response contains exactly those three keys and no note_id/action/fact/reason decision',
            bounds='at most two distinct earlier-read grants per note, origin window plus revisits within 128000 bytes; '
                   'temporary for this note only, never carried to another note, never truncated',
            repeat='repeating an already-displayed ID without a new grant is a stall; the second stall stops the review partially'),
        unknown_vs_null=('Unknown period is one of "", "unknown", "unspecified", "未知" and empty unit is an explicit '
                         'empty string; null/wrong-type fields are hard errors. A note can leave review as '
                         'needs_check only through an explicit mark_needs_check or a failed literal补证; it can never '
                         'remain unknown-period yet verified.'),
        example=dict(note_id='n-0001', action='confirmed',
            reason='员工复核：主证据与字段旁证逐字一致，结论保持不变'),
        revise_example=dict(note_id='n-0003', action='revise_fact',
            reason='员工在授权回看的grant 4逐字看到截止日期与单位，补证并修订，不做格式推断',
            fact=dict(entity='验收公司', attribute='员工总数', period='2023年12月31日',
                      value='90', unit='人', value_type='number'),
            field_evidence=[dict(field='period', grant_id='4', company_id='<host company>',
                file_hash='<mapped file hash>', mapped_fingerprint='<mapped fingerprint>',
                source_loc='docx:body/3/paragraph', start_line=13, end_line=13,
                quote='截至2023年12月31日，以下记录验收公司的员工总数，单位为人。')],
            relations=[dict(rel='conflicts_with', to_note_id='n-0002',
                reason='同一公司同一指标不同截止日期数值不同，员工显式标记冲突，两个数值均保留')]),
        needs_check_example=dict(note_id='n-0003', action='mark_needs_check',
            reason='回看授权内仍无逐字期间/单位依据，保持待核，不推断日期'))


class NotesFormatRejected(NoteRejected):
    pass


def request_key(company, job_id, fingerprint, window_id):
    return hashlib.sha256(canonical([company, job_id, fingerprint, window_id]).encode()).hexdigest()


def review_request_key(company, job_id, fingerprint, note_id, round_no):
    return hashlib.sha256(
        canonical([company, job_id, fingerprint, note_id, round_no]).encode()).hexdigest()


class MappedNotesService:
    """Protocol V4: read every window and keep a notebook; never publish leaves."""

    def __init__(self, db_path, dossier, *, call, save, notes, original_name,
                 display_name=None, prepare_window=None, fence=None, correction_attempts=2,
                 review_enabled=True, review_apply=None):
        require_enabled()
        self.db_path = db_path
        self.dossier = dossier
        self.call = call
        self.save = save
        self.notes = notes
        self.original_name = original_name
        self.display_name = display_name
        self.prepare_window = prepare_window
        self.fence = fence
        self.correction_attempts = correction_attempts
        # Stage B is part of the V4 product default; callers stop at stage A only
        # through an explicit test/acceptance switch, never an environment default.
        self.review_enabled = review_enabled
        self.review_apply = review_apply
        self.windows = MDWindows(dossier)

    def run(self, *, company, job_id, state):
        require_enabled()
        if self.dossier.quality_fingerprint != self.dossier.fingerprint:
            raise NoteRejected('mapped document fidelity fingerprint changed')
        from ..facts_tree.md_reader import current_checkpoint_binding_identity, require_checkpoint_binding
        if 'mapped_notes' in state:
            progress = state['mapped_notes']
            if not isinstance(progress, dict):
                raise NoteRejected('existing V4 notes checkpoint malformed; preserve old audit and use a new isolated task')
            try:
                current = current_checkpoint_binding_identity(self.db_path, company, checkpoint_name='V4 notes')
                require_checkpoint_binding(progress.get('binding_identity'), current, checkpoint_name='V4 notes')
            except LeafRejected as exc:
                if isinstance(exc, NoteRejected):
                    raise
                raise NoteRejected(str(exc)) from exc
        else:
            try:
                identity = current_checkpoint_binding_identity(self.db_path, company, checkpoint_name='V4 notes')
            except LeafRejected as exc:
                raise NoteRejected(str(exc)) from exc
            progress = dict(protocol=NOTES_PROTOCOL, company_id=company,
                file_hash=self.dossier.file_hash, fingerprint=self.dossier.fingerprint,
                cursor=0, records=[], unresolved=[], reading_complete=False,
                binding_identity=identity)
            state['mapped_notes'] = progress
        if progress['protocol'] != NOTES_PROTOCOL or progress.get('company_id') != company:
            raise NoteRejected('reading protocol/scope changed; preserve old audit and use a new isolated task')
        if progress['fingerprint'] != self.dossier.fingerprint or progress['file_hash'] != self.dossier.file_hash:
            raise NoteRejected('mapped MD changed; old cursor cannot resume')
        company_context = dict(company_id=company)
        if self.display_name:
            company_context['display_name'] = self.display_name
        if progress.get('reading_complete'):
            # Reading and review are independent durable states: a checkpoint that
            # finished reading still enters (or resumes with zero new reading calls)
            # the post-read review.
            if self.review_enabled and not progress.get('review', {}).get('complete'):
                self._run_review(progress, company, job_id, company_context)
            return
        if not progress['records'] and self.windows.unmapped:
            progress['unresolved'].append(dict(reason='unmapped MD lines', lines=self.windows.unmapped))

        def retained_grants(selection, current, displayed):
            if (not isinstance(selection, list) or len(selection) > 2
                    or any(not isinstance(i, str) for i in selection)
                    or len(set(selection)) != len(selection)):
                raise NotesFormatRejected('retain_context_windows requires at most two distinct string IDs')
            allowed = {g['grant_id']: g for g in displayed + [current]}
            if any(i not in allowed for i in selection):
                raise NoteRejected('retained context was not displayed or currently read')
            return [deepcopy(allowed[i]) for i in selection]

        while progress['cursor'] < len(self.windows.windows):
            window = self.windows.windows[progress['cursor']]
            if len(progress['records']) <= progress['cursor']:
                progress['records'].append(dict(window_id=window['window_id'], status='read_pending'))
            record = progress['records'][progress['cursor']]
            if window.get('unavailable'):
                record.update(status='unresolved', reason=window['unavailable'])
                progress['cursor'] += 1
                self.save()
                continue
            grant = self.windows.grant(window, self.original_name)
            grant['company_id'] = company
            retained, history = [], []
            for previous in progress['records'][:progress['cursor']]:
                if 'grant' not in previous:
                    continue
                n = int(previous['window_id'])
                if not 0 <= n < progress['cursor']:
                    raise NoteRejected('context was not previously read')
                expected = self.windows.grant(self.windows.windows[n], self.original_name)
                expected['company_id'] = company
                if previous['grant'] != expected:
                    raise NoteRejected('persisted context scope/fingerprint/lines differ from read window')
                displayed = list({g['grant_id']: g for g in history[-2:] + retained}.values())
                if previous.get('rolling_context') != displayed:
                    raise NoteRejected('persisted rolling context differs from read grants')
                if 'retain_context_windows' in previous:
                    retained = retained_grants(previous['retain_context_windows'], expected, displayed)
                    if previous.get('retained_context') != retained:
                        raise NoteRejected('persisted retained context differs from read grants')
                history.append(expected)
            contexts = list({g['grant_id']: deepcopy(g) for g in history[-2:] + retained}.values())
            try:
                bounded_context([MDWindows.view(g) for g in contexts])
            except NoteRejected as exc:
                record.update(status='unresolved', reason=str(exc))
                progress['cursor'] += 1
                self.save()
                continue
            record['input_retained_context_windows'] = [g['grant_id'] for g in retained]
            record['rolling_context'] = contexts
            if record['status'] == 'read_pending':
                record['grant'] = grant
                if record.get('context_tool_blocked') or record.get('extract_progress_blocked'):
                    # Blocked windows stay stopped on ordinary resume with zero
                    # model calls; only explicit resume_unresolved clears markers.
                    raise ComparisonStopped('window blocked; explicit resume_unresolved required to retry')
                # Explicit per-window revisit grants are re-derived from persisted
                # completed-prefix grants on every run, so a tampered checkpoint
                # cannot authorize future/unread/foreign windows.
                revisit_grants = [self._revisit_grant(progress, company, int(gid))
                                  for gid in record.get('revisit_context_windows', [])]
                stored = record.get('revisit_context_grants')
                if stored is not None and stored != [MDWindows.view(g) for g in revisit_grants]:
                    raise NoteRejected('persisted revisit context differs from read grants')

                def request_context(ids, reason):
                    """Mechanical context tool: redisplay earlier-read grants."""
                    if (not isinstance(ids, list) or not ids or len(ids) > 2
                            or any(not isinstance(i, str) for i in ids)
                            or len(set(ids)) != len(ids)):
                        raise NotesFormatRejected('request_context_windows requires 1..2 distinct string grant IDs')
                    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                        raise NotesFormatRejected('request_context requires a nonempty bounded reason')
                    try:
                        parsed = [int(i) for i in ids]
                    except ValueError as exc:
                        raise NotesFormatRejected('request_context_windows must quote earlier read grant IDs') from exc
                    owned = [str(i) for i in record.get('revisit_context_windows', [])]
                    canonical = [str(n) for n in parsed]
                    if len(set(canonical)) != len(canonical):
                        raise NotesFormatRejected('request_context_windows requires distinct grant IDs')
                    fresh = [n for n, gid in zip(parsed, canonical) if gid not in owned]
                    audit = dict(requested=list(ids), reason=reason)
                    if not fresh:
                        # A repeated request without a new grant cannot loop forever.
                        record['context_tool_stalls'] = record.get('context_tool_stalls', 0) + 1
                        audit.update(granted=[], stalls=record['context_tool_stalls'])
                        record.setdefault('context_tool_requests', []).append(audit)
                        self.save()
                        if record['context_tool_stalls'] >= 2:
                            record['context_tool_blocked'] = True
                            self.save()
                            raise ComparisonStopped('context tool repeated without new grant; explicit resume_unresolved required')
                        return []
                    if len(owned) + len(fresh) > 2:
                        raise NoteRejected('at most two distinct revisit grants per window')
                    new_grants = [self._revisit_grant(progress, company, n) for n in fresh]
                    combined = list({g['grant_id']: g for g in contexts + revisit_grants + new_grants}.values())
                    if len(combined) > 6:
                        raise NoteRejected('combined reading context exceeds six grants')
                    if len(json.dumps([MDWindows.view(g) for g in combined], ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
                        raise NoteRejected('combined reading context exceeds byte bound; no truncated grant displayed')
                    revisit_grants.extend(new_grants)
                    record['revisit_context_windows'] = [g['grant_id'] for g in revisit_grants]
                    record['revisit_context_grants'] = [MDWindows.view(g) for g in revisit_grants]
                    record['context_tool_stalls'] = 0
                    # Independent request/actual-grant-view audit per call.
                    audit['granted'] = [g['grant_id'] for g in new_grants]
                    audit['granted_views'] = [MDWindows.view(g) for g in new_grants]
                    record.setdefault('context_tool_requests', []).append(audit)
                    self.save()
                    return audit['granted_views']

                self.save()  # Read grant persists before transport; no write transaction held.
                existing_notes = self.notes.list_compact()
                read_grant_index = [dict(grant_id=previous['grant']['grant_id'],
                    source_loc=previous['grant']['source_loc'], ranges=previous['grant']['ranges'])
                    for previous in progress['records'][:progress['cursor']] if 'grant' in previous]
                if self.prepare_window is not None:
                    self.prepare_window(grant, record, company_context,
                        context_tool=request_context, existing_notes=existing_notes,
                        read_grant_index=read_grant_index,
                        revisit_grants=lambda: [deepcopy(g) for g in revisit_grants])
                candidates = record.get('preparation', {}).get('candidates', [])
                # Evidence seen by the employee includes the explicit revisit grants;
                # the note ledger re-verifies against exactly this same set.
                context_grants = list({g['grant_id']: deepcopy(g)
                                       for g in contexts + revisit_grants}.values())
                request = dict(protocol=NOTES_PROTOCOL_NAME, company=company_context,
                    operation='note_receipt', file_hash=self.dossier.file_hash,
                    fingerprint=progress['fingerprint'], window=MDWindows.view(grant),
                    context_grants=[MDWindows.view(g) for g in context_grants],
                    retained_context_windows=record['input_retained_context_windows'],
                    retention_contract=dict(field='retain_context_windows', max_grants=2,
                        allowed='current window or rolling/retained context grant IDs only; '
                                'request_context revisit IDs are temporary and are refused here',
                        required=True, reason='nonempty retention_reason required', empty_list='explicit release'),
                    candidates=candidates,
                    existing_notes=existing_notes,
                    response_contract=note_receipt_contract(),
                    directory=[dict(source_loc=e['source_loc'], start_line=e['start_line'], end_line=e['end_line'])
                              for e in self.dossier.elements[:256]],
                    directory_count=len(self.dossier.elements),
                    read_windows=[dict(grant_id=g['grant_id'], source_loc=g['source_loc'], ranges=g['ranges'])
                                  for g in context_grants])

                def validate(response):
                    if not isinstance(response, dict):
                        raise NotesFormatRejected('response must be a JSON object')
                    if not isinstance(response.get('retention_reason'), str) or not response['retention_reason'].strip():
                        raise NotesFormatRejected('response.retention_reason required nonempty string')
                    outcome = response.get('outcome')
                    if outcome not in ('noted', 'no_content', 'unresolved'):
                        raise NotesFormatRejected('response.outcome required: noted|no_content|unresolved')
                    creates = response.get('creates')
                    revisions = response.get('revisions')
                    if not isinstance(creates, list) or not isinstance(revisions, list):
                        raise NotesFormatRejected('creates and revisions must be arrays')
                    if outcome in ('no_content', 'unresolved'):
                        if not isinstance(response.get('reason'), str) or not response['reason'].strip():
                            raise NotesFormatRejected(f'{outcome} requires a nonempty reason')
                        if outcome == 'no_content' and (creates or revisions):
                            raise NotesFormatRejected('no_content cannot create or revise notes')
                    if outcome == 'noted' and not creates and not revisions:
                        raise NotesFormatRejected('noted requires creates or revisions')
                    retention = response.get('retain_context_windows')
                    next_retained = retained_grants(retention, grant, contexts)
                    bounded_context([MDWindows.view(g) for g in next_retained])
                    return outcome, creates, revisions, retention, next_retained, response.get('reason')

                while True:
                    if record.get('note_format_attempts', 0) >= 2:
                        record['note_format_blocked'] = True
                        self.save()
                        raise ComparisonStopped('note format correction exhausted; explicit resume_unresolved required')
                    outgoing = deepcopy(request)
                    if record.get('note_rejected_receipts'):
                        outgoing['format_feedback'] = deepcopy(record['note_rejected_receipts'][-1]['feedback'])
                    response = self.call('plan', outgoing)
                    try:
                        outcome, creates, revisions, retention, next_retained, reason = validate(response)
                    except NotesFormatRejected as exc:
                        record.setdefault('note_rejected_receipts', []).append(dict(receipt=deepcopy(response),
                            error=str(exc), feedback=dict(error=str(exc), contract=note_receipt_contract(),
                                instruction='Correct the note receipt shape for the same current window and candidates. Do not redo prior phases or change authorization.')))
                        record['note_format_attempts'] = record.get('note_format_attempts', 0) + 1
                        self.save()
                        continue
                    break
                grants = {g['grant_id']: g for g in [grant] + context_grants}
                try:
                    receipt = self.notes.apply_window(window_id=window['window_id'], outcome=outcome,
                        creates=creates, revisions=revisions, grants=grants, candidates=candidates,
                        request_sha=request_key(company, job_id, progress['fingerprint'], window['window_id']))
                except NoteRejected as exc:
                    # Structural/authorization failure: the note transaction rolled
                    # back (zero side effects). Keep audit and mark the window.
                    record.setdefault('rejected_note_receipts', []).append(dict(receipt=deepcopy(response), reason=str(exc)))
                    record.update(status='unresolved', reason=str(exc), technical_rejection=True,
                        retention_reason=response['retention_reason'],
                        retain_context_windows=retention, retained_context=next_retained)
                    progress['cursor'] += 1
                    self.save()
                    continue
                record['retention_reason'] = response['retention_reason']
                record['retain_context_windows'] = retention
                record['retained_context'] = next_retained
                record['note_receipt'] = receipt
                record['note_ids'] = sorted(set(record.get('note_ids', [])) | set(receipt['created']) | set(receipt['revised']))
                record['outcome'] = outcome
                record.update(status='noted' if outcome == 'noted' else outcome, reason=reason)
                self.save()
            progress['cursor'] += 1
            self.save()
        if len(progress['records']) != len(self.windows.windows) or any(r['status'] == 'read_pending' for r in progress['records']):
            raise NoteRejected('MD coverage incomplete')
        unresolved_windows = sum(r['status'] == 'unresolved' for r in progress['records'])
        technical = sum(bool(r.get('technical_rejection')) and r['status'] == 'unresolved' for r in progress['records'])
        progress['reading_complete'] = True
        progress['window_count'] = len(progress['records'])
        progress['verified_notes'] = self.notes.note_count('verified')
        # D: open_notes counts needs_check only. Employee-rejected notes are an
        # append-only discard stream; they never publish and never block file
        # completion, but stay visible as discarded_notes.
        progress['open_notes'] = self.notes.note_count('needs_check')
        progress['discarded_notes'] = self.notes.note_count('rejected')
        progress['unresolved_readings'] = len(progress['unresolved']) + unresolved_windows
        progress['technical_rejections'] = technical
        # Publication gate is owned by stage B: reading completion alone is never
        # publishable. The review recomputes this once every note is decided.
        progress['awaiting_publication'] = False
        self.save()
        if self.review_enabled:
            self._run_review(progress, company, job_id, company_context)

    def _revisit_grant(self, progress, company, n):
        """Rebuild an earlier truly-read grant from the dossier, verifying the
        persisted record byte-for-byte (company/hash/fingerprint/coordinate/ranges).
        Directory entries, future/unread and foreign-scope windows are refused."""
        if not isinstance(n, int) or not 0 <= n < progress['cursor']:
            raise NotesFormatRejected('request_context may only target earlier read window numbers')
        previous = progress['records'][n]
        if 'grant' not in previous or previous.get('status') == 'read_pending':
            raise NotesFormatRejected('request_context target window was not read')
        expected = self.windows.grant(self.windows.windows[n], self.original_name)
        expected['company_id'] = company
        if previous['grant'] != expected:
            raise NoteRejected('persisted grant differs from dossier rebuild')
        return deepcopy(expected)

    def _run_review(self, progress, company, job_id, company_context):
        """Stage B: review every note once reading the whole mapped MD is durable.

        Reading and review are independent states and budgets: the reading prefix
        is never replayed, decided notes cost zero model calls on resume, and each
        note decision is one mutually exclusive host-verified receipt.
        """
        if not progress.get('reading_complete'):
            raise NoteRejected('post-read review starts only after the whole mapped MD was read')
        review = progress.setdefault('review', dict(order=[], items=dict(), cursor=0, complete=False))
        if review.get('complete'):
            return
        window_count = len(progress['records'])
        # Re-verify every persisted read grant against a dossier rebuild before
        # review may quote any earlier window again.
        for n, previous in enumerate(progress['records']):
            if 'grant' not in previous:
                continue
            expected = self.windows.grant(self.windows.windows[n], self.original_name)
            expected['company_id'] = company
            if previous['grant'] != expected:
                raise NoteRejected('persisted read grant differs from dossier rebuild')
        if not review['order']:
            # Stable job-wide order frozen before the first review turn. Notes
            # already rejected during reading carry a terminal audited decision
            # and cannot change, so they are not re-litigated here.
            review['order'] = [nid for nid in self.notes.note_ids()
                               if self.notes.note_status(nid) != 'rejected']
            self.save()
        order = review['order']
        review_index = self.notes.list_review_index()
        index_by_id = {entry['note_id']: entry for entry in review_index}
        read_grant_index = [dict(grant_id=r['grant']['grant_id'], source_loc=r['grant']['source_loc'],
                                 ranges=r['grant']['ranges'])
                            for r in progress['records'] if 'grant' in r]
        tool_contract = dict(
            receipt='mutually exclusive alternative to a review decision: exactly '
                    '{"outcome":"request_context","request_context_windows":["<grant_id>"],"reason":"<=2000 chars"}, '
                    'no note_id/action/fact/field_evidence keys in that response',
            index='read_grant_index lists grant coordinates (grant_id/source_loc/ranges) of every truly-read window; '
                  'coordinates are not text authorization',
            authorization='only grant IDs of windows truly read in this same job/company/file are displayable; '
                          'future/unread/directory-only/foreign/fingerprint-mismatch IDs are refused with zero side effects',
            bounds='at most two distinct revisit grants per note; origin window plus revisits stays within four grants '
                   'and 128000 bytes; nothing is truncated or concatenated',
            temporary='a review revisit is temporary for THIS note only; it never carries to another note and never '
                      'becomes retained context',
            repeat='repeating an already-displayed ID without a new grant is a bounded stall; the second stall stops the review',
            result='granted originals return as context_tool_results on the next review request; the turn still spends review budget')

        while review['cursor'] < len(order):
            note_id = order[review['cursor']]
            entry = review_item(review['items'], note_id)
            if entry['blocked']:
                raise ComparisonStopped('note review blocked; explicit resume_unresolved required to retry')
            last_round = self.notes.review_round(note_id)
            if last_round is not None:
                # SQLite receipt committed but the KV cursor did not: resume with
                # zero model calls and zero state change.
                entry['round'] = last_round
                if entry.get('receipt') is None:
                    entry['receipt'] = self.notes.review_receipt(note_id)
                review['cursor'] += 1
                self.save()
                continue
            expected_round = (last_round or 0) + 1
            entry['round'] = expected_round
            note = self.notes.note_payload(note_id)
            origin_window = int(note['window_id'])
            origin = self._revisit_grant(progress, company, origin_window)
            revisits = []
            if entry['revisit_windows']:
                revisits = [self._revisit_grant(progress, company, n) for n in entry['revisit_windows']]
                if [MDWindows.view(g) for g in revisits] != entry['revisit_views']:
                    raise NoteRejected('persisted review revisit views differ from dossier rebuild')

            def request_context(ids, reason):
                if (not isinstance(ids, list) or not 1 <= len(ids) <= 2
                        or any(not isinstance(i, str) for i in ids)
                        or len(set(ids)) != len(ids)):
                    raise NotesFormatRejected('request_context_windows requires 1..2 distinct grant ID strings')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                    raise NotesFormatRejected('request_context requires a nonempty reason (max 2000 characters)')
                try:
                    parsed = [int(i) for i in ids]
                except ValueError as exc:
                    raise NotesFormatRejected('request_context_windows must quote earlier read grant IDs') from exc
                if any(not 0 <= n < window_count for n in parsed) or len(set(parsed)) != len(parsed):
                    raise NotesFormatRejected('request_context targets only earlier read windows of this file')
                owned = list(entry['revisit_windows'])
                fresh = [n for n in parsed if n != origin_window and n not in owned]
                audit = dict(requested=list(ids), reason=reason)
                if not fresh:
                    entry['stalls'] += 1
                    audit.update(granted=[], stalls=entry['stalls'])
                    entry['tool_requests'].append(audit)
                    self.save()
                    if entry['stalls'] >= 2:
                        entry['blocked'] = True
                        self.save()
                        raise ComparisonStopped('review context tool stalled twice without a new grant; '
                                                'explicit resume_unresolved required')
                    return []
                if len(owned) + len(fresh) > 2:
                    raise NoteRejected('at most two distinct revisit grants per review note')
                new_grants = [self._revisit_grant(progress, company, n) for n in fresh]
                combined = list({g['grant_id']: g for g in [origin] + revisits + new_grants}.values())
                combined_views = [MDWindows.view(g) for g in combined]
                try:
                    bounded_context(combined_views)
                    # Mirror the reading stage: the byte bound is the imported
                    # notes-module constant, so nothing is concatenated/truncated.
                    if len(json.dumps(combined_views, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
                        raise LeafRejected('review context exceeds byte bound; no truncated grant displayed')
                except LeafRejected as exc:
                    raise NoteRejected(str(exc)) from exc
                revisits.extend(new_grants)
                entry['revisit_windows'] = [g['grant_id'] for g in revisits]
                entry['revisit_views'] = [MDWindows.view(g) for g in revisits]
                entry['stalls'] = 0
                audit['granted'] = [g['grant_id'] for g in new_grants]
                audit['granted_views'] = [MDWindows.view(g) for g in new_grants]
                entry['tool_requests'].append(audit)
                self.save()
                return audit['granted_views']

            def validate_decision(response):
                if not isinstance(response, dict):
                    raise NotesFormatRejected('review response must be a JSON object')
                extra = set(response) - REVIEW_DECISION_KEYS
                if extra:
                    raise NotesFormatRejected(f'unknown review response keys: {sorted(extra)}')
                if response.get('note_id') != note_id:
                    raise NotesFormatRejected('review one specific current note_id per turn; batches are forbidden')
                action = response.get('action')
                if action not in REVIEW_ACTIONS:
                    raise NotesFormatRejected(f'review action must be one of {REVIEW_ACTIONS}')
                try:
                    reason = _bounded_reason(response.get('reason'))
                except NoteRejected as exc:
                    raise NotesFormatRejected(str(exc)) from exc
                has_fact, has_proofs, has_relations = ('fact' in response, 'field_evidence' in response,
                                                       'relations' in response)
                if action == 'confirmed':
                    if has_fact or has_proofs or has_relations:
                        raise NotesFormatRejected('confirmed carries only note_id/action/reason')
                elif action == 'relate':
                    if has_fact or has_proofs:
                        raise NotesFormatRejected('relate cannot carry fact or field_evidence')
                    relations = response.get('relations')
                    if not isinstance(relations, list) or not relations:
                        raise NotesFormatRejected('relate requires a nonempty relations array')
                elif action == 'revise_fact':
                    fact = response.get('fact')
                    if not isinstance(fact, dict) or set(fact) != set(REVIEW_FACT_KEYS):
                        raise NotesFormatRejected('revise_fact requires the complete replacement fact with exactly '
                                                  f'{list(REVIEW_FACT_KEYS)}')
                    proofs = response.get('field_evidence')
                    if not isinstance(proofs, list) or len(proofs) > 3:
                        raise NotesFormatRejected('revise_fact field_evidence must be a list of at most three witnesses')
                    if has_relations and not isinstance(response.get('relations'), list):
                        raise NotesFormatRejected('relations must be an array when present')
                elif has_fact or has_proofs or has_relations:
                    raise NotesFormatRejected(f'{action} carries only note_id/action/reason')
                return dict(note_id=note_id, action=action, reason=reason,
                            fact=response.get('fact'), field_evidence=response.get('field_evidence'),
                            relations=response.get('relations'))

            request = dict(protocol=NOTES_PROTOCOL_NAME, company=company_context, operation='note_review',
                file_hash=self.dossier.file_hash, fingerprint=progress['fingerprint'],
                note=index_by_id[note_id], review_index=review_index,
                origin_window=MDWindows.view(origin),
                context_grants=[MDWindows.view(g) for g in revisits],
                read_grant_index=read_grant_index,
                context_tool_contract=tool_contract,
                context_tool_results=entry['tool_requests'],
                context_tool_feedback=entry.get('tool_feedback'),
                response_contract=note_review_contract())

            while True:
                if entry['attempts'] >= 2:
                    entry['blocked'] = True
                    self.save()
                    raise ComparisonStopped('review correction exhausted; explicit resume_unresolved required')
                outgoing = deepcopy(request)
                outgoing['context_grants'] = [MDWindows.view(g) for g in revisits]
                outgoing['context_tool_results'] = deepcopy(entry['tool_requests'])
                outgoing['context_tool_feedback'] = entry.get('tool_feedback')
                if entry.get('feedback') is not None:
                    outgoing['review_feedback'] = entry['feedback']
                response = self.call('review', outgoing)
                if isinstance(response, dict) and response.get('outcome') == 'request_context':
                    try:
                        if set(response) != {'outcome', 'request_context_windows', 'reason'}:
                            raise NotesFormatRejected('request_context receipt is mutually exclusive with a review decision')
                        views = request_context(response['request_context_windows'], response['reason'])
                    except NoteRejected as exc:
                        entry['tool_rejections'] += 1
                        entry['tool_feedback'] = dict(error=str(exc)[:1000], contract=tool_contract)
                        entry['tool_requests'].append(dict(receipt=deepcopy(response), error=str(exc)[:1000]))
                        self.save()
                        if entry['tool_rejections'] >= 2:
                            entry['blocked'] = True
                            self.save()
                            raise ComparisonStopped('review context-tool correction exhausted; '
                                                    'explicit resume_unresolved required') from exc
                        continue
                    entry.pop('tool_feedback', None)
                    if not views:
                        entry['feedback'] = dict(
                            error='request_context displayed no new grant; do not repeat the same ID',
                            contract=note_review_contract())
                    else:
                        entry['feedback'] = None
                    # Next charged turn carries the granted views via context_grants/results.
                    self.save()
                    continue
                try:
                    decision = validate_decision(response)
                except NotesFormatRejected as exc:
                    entry['attempts'] += 1
                    entry['feedback'] = dict(error=str(exc)[:1000], contract=note_review_contract(),
                        instruction='Correct this single-note review decision. Authorization and earlier decisions do not change.')
                    entry['rejected_decisions'].append(dict(receipt=deepcopy(response), error=str(exc)[:1000]))
                    self.save()
                    continue
                grants = {g['grant_id']: g for g in [origin] + revisits}
                request_sha = review_request_key(company, job_id, progress['fingerprint'],
                                                 note_id, expected_round)
                if self.review_apply is None:
                    raise NoteRejected('review host verifier is not wired; refusing an unverified decision')
                try:
                    receipt = self.review_apply(note=note, decision=decision,
                        expected_round=expected_round, request_sha=request_sha, grants=grants)
                except NoteRejected as exc:
                    # Illegal/forged evidence: the ledger transaction rolled back
                    # with zero state change. The diagnostic echoes no unauthorized
                    # text; feed it back for one bounded correction.
                    entry['attempts'] += 1
                    entry['feedback'] = dict(error=str(exc)[:1000], contract=note_review_contract(),
                        instruction='Revise with verbatim evidence from the displayed grants only, '
                                    'mark_needs_check, or reject. Host never reformats dates/units/numbers.')
                    entry['rejected_decisions'].append(dict(decision=deepcopy(decision), error=str(exc)[:1000]))
                    self.save()
                    continue
                entry['receipt'] = receipt
                entry['decision'] = dict(action=decision['action'], reason=decision['reason'])
                entry['feedback'] = None
                review['cursor'] += 1
                self.save()
                break

        review['complete'] = True
        progress['review_complete'] = True
        progress['reviewed_notes'] = len(order)
        progress['verified_notes'] = self.notes.note_count('verified')
        progress['open_notes'] = self.notes.note_count('needs_check')
        progress['discarded_notes'] = self.notes.note_count('rejected')
        # A future publication phase may start only when review is complete AND no
        # note remains open; discarded (rejected) notes never block.
        progress['awaiting_publication'] = progress['open_notes'] == 0
        self.save()
