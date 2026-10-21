"""Protocol V4 stage C: post-review note publication (V2-1).

After stage A (reading) and stage B (review) the host mechanically lands the
persisted verified notes as formal facts, source evidence and leaf content:

* the employee assigns exactly one top1 leaf per publishable atomic fact group
  and submits leaf blocks; the host never infers merges, never picks winners
  and never rewrites existing blocks on its own. V2-2 same-leaf body merging
  IS implemented: the employee may propose merges synthesizing covered base
  blocks into one new block; the host copies carried old evidence verbatim
  (only renaming the ref labels), re-verifies carried facts/sources and the
  persisted quote/coordinates against the fact ledger, keeps every old fact
  id, re-verifies every carried old fact's active identity, current revision
  row and current leaf placement (trunk/leaf plus the exact current
  pack/version/manifest/binding revision) against the versioned attribution
  ledger, rejects merges on legacy leaves and on protocols 2/3, and appends
  one full old/new markdown+blocks+coverage rewrite-audit row inside the same
  commit transaction;
* one ordinary note is one formal fact; only employee-declared ``same_fact``
  groups with byte-identical fact fields merge into one fact carrying every
  member note witness;
* facts + source/version/evidence/active supports links + fact-leaf link +
  fact identity + versioned first placement + leaf revision/body/index/
  projection/notifications + the note->fact->leaf publication receipt commit
  in ONE SQLite connection, ONE short transaction per leaf; every model turn
  happens outside any transaction;
* replay/crash recovery is driven by md_leaf_operations plus the durable
  md_note_publications rows, never by checkpoint counters.

V3 semantics (guards, CAS, conflict todos) are reused unchanged through
MDLeafPublisher with reading_protocol=4.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from ..memory import paths
from .grants import range_is_granted
from .leaf_store import LeafPublishService, LeafRejected
from .md_publish import MDLeafPublisher, BaseChanged, digest
from .md_reader import MDWindows, ComparisonStopped
from .fact_identity import FactIdentityStore
from .fact_placements import FactPlacementStore
from .quote_sources import DossierQuoteResolver
from .rollup import FactsTreeRollup
from .source_registry import SourceRegistry
from .template_packs import load_pack
from .tree_meta import require_enabled

PUBLISH_PROTOCOL = 4
PUBLISH_PROTOCOL_NAME = 'mapped-md-v4'
FACT_KEYS = ('entity', 'attribute', 'period', 'value', 'unit', 'value_type')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS md_note_publications (
 company_id TEXT NOT NULL, job_id TEXT NOT NULL, note_id TEXT NOT NULL,
 fact_id INTEGER NOT NULL, leaf_code TEXT NOT NULL, operation_id TEXT NOT NULL,
 group_id TEXT NOT NULL, request_sha TEXT NOT NULL, receipt TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,job_id,note_id),
 FOREIGN KEY(fact_id) REFERENCES facts(id));
CREATE TRIGGER IF NOT EXISTS md_note_pub_no_update BEFORE UPDATE ON md_note_publications
 BEGIN SELECT RAISE(ABORT,'append-only note publication audit'); END;
CREATE TRIGGER IF NOT EXISTS md_note_pub_no_delete BEFORE DELETE ON md_note_publications
 BEGIN SELECT RAISE(ABORT,'append-only note publication audit'); END;
'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class PublishRejected(LeafRejected):
    """Host/state integrity failure during publication; never employee-fixable."""


class PublishFormatRejected(PublishRejected):
    """Employee publish receipt failure; one bounded correction loop applies."""


def group_id_for(company, job_id, note_ids):
    return 'grp_' + hashlib.sha256(canonical([company, job_id, sorted(note_ids)]).encode()).hexdigest()[:32]


def operation_id_for(company, job_id, fingerprint, leaf_code):
    return 'md-' + digest([company, job_id, fingerprint, 'notes-publish', leaf_code])[:40]


def publish_contract():
    """Stage C receipt shapes; no business inference, no leaf classification."""
    return dict(action='publish', protocol=PUBLISH_PROTOCOL_NAME,
        assign_leaves=dict(
            required=['assignments'],
            assignment_required=['group_ref', 'note_ids', 'leaf_code', 'reason'],
            group_ref='unique employee string id for this fact group; cite it as fact_refs in compare_submit',
            note_ids='one note = one fact; a group of >1 requires persisted employee same_fact relations '
                     'connecting every member AND byte-identical six fact fields; the host never infers merges',
            leaf_code='exactly one legal template top1 leaf per group; a note lands in one leaf only; '
                      'second assignment, cross-leaf replay and unhandled verified notes are hard rejections',
            coverage='every verified note exactly once; needs_check notes block the gate; rejected notes never publish',
            unresolved=dict(outcome='unresolved', reason='nonempty employee reason (max 2000 characters); '
                            'the file stays partial while any verified note is unassigned')),
        compare_submit=dict(
            required=['leaf_code', 'base_version', 'base_sha', 'blocks', 'conflicts', 'note'],
            block_required=['block_id', 'text', 'evidence'],
            evidence_required=['ref', 'grant_id', 'file_hash', 'original_name', 'source_loc',
                               'start_line', 'end_line', 'fact_refs', 'note_id'],
            fact_refs='array: group_ref strings of THIS operation groups, or JSON integers of facts already '
                      'published by this job; the host mints formal fact ids inside the publication transaction',
            note_id='host-supplied member note id; every cited fact must carry that note as a member witness',
            retention='copy every base block NOT covered by a merge verbatim into blocks; covered blocks must '
                      'not reappear in blocks; the host never rewrites, reorders or deletes uncovered blocks; '
                      'a conflict old side always stays in the leaf and can never be merged away',
            merges='optional array of same-leaf body merges (V2-2); each item: block_id (a FRESH id, never an '
                   'existing base block id), text, covers (one or more base block ids), carry (an entry '
                   '{from_block_id,from_ref,to_ref} mapping EVERY old evidence ref of the covered blocks exactly '
                   'once; the host copies the old evidence verbatim and only renames the ref — never supply '
                   'evidence fields yourself), optional new_evidence (same shape as block evidence below), '
                   'reason (nonempty, max 2000 characters); merged numbers are supported only by carried fact '
                   'fields or their persisted original quotes in the fact ledger, never by old leaf body text; '
                   'keep different periods/units in separate blocks and use conflicts for real contradictions',
            base='base_version JSON integer and base_sha copied exactly from current; a changed base is '
                 'rejected and the leaf must be reread before resubmission; old covers/carry are void after '
                 'a base change and must be rebuilt against the new base',
            conflicts='conflicts only create todos with both sides evidence; the host never adjudicates',
            unresolved=dict(outcome='unresolved', reason='nonempty employee reason (max 2000 characters); '
                            'unhandled notes keep the file partial; explicit resume retries')))


class NotesPublishService:
    """V4 publication loop over the complete reviewed notebook."""

    def __init__(self, db_path, memory, dossier, *, call, save, original_name,
                 display_name=None, fence=None, compare_retries=3, correction_attempts=2):
        require_enabled()
        self.db_path = db_path
        self.memory = memory
        self.dossier = dossier
        self.call = call
        self.save = save
        self.original_name = original_name
        self.display_name = display_name
        self.fence = fence
        self.compare_retries = compare_retries
        self.correction_attempts = correction_attempts
        self.windows = MDWindows(dossier)

    # -- DB current-state reads (checkpoint counters are never trusted) --

    @staticmethod
    def _load_notes(db, company, job_id):
        rows = db.execute(
            'SELECT note_id,status,window_id,payload FROM md_read_notes '
            'WHERE company_id=? AND job_id=? ORDER BY rowid', (company, job_id)).fetchall()
        return {row['note_id']: (row['status'], row['window_id'], json.loads(row['payload'])) for row in rows}

    @staticmethod
    def _load_publications(db, company, job_id):
        rows = db.execute(
            'SELECT note_id,fact_id,leaf_code,operation_id FROM md_note_publications '
            'WHERE company_id=? AND job_id=? ORDER BY note_id', (company, job_id)).fetchall()
        return {row['note_id']: dict(fact_id=row['fact_id'], leaf_code=row['leaf_code'],
                                     operation_id=row['operation_id']) for row in rows}

    # -- grant rebuild and witness re-verification against read authorizations --

    def _grant(self, progress, company, window_id, *, context):
        try:
            n = int(window_id)
        except (TypeError, ValueError) as exc:
            raise PublishRejected(f'{context} references an invalid window id') from exc
        records = progress['records']
        if not 0 <= n < len(records) or 'grant' not in records[n]:
            raise PublishRejected(f'{context} references a window that was never read in this job')
        grant = self.windows.grant(self.windows.windows[n], self.original_name)
        grant['company_id'] = company
        if records[n]['grant'] != grant:
            raise PublishRejected('persisted read grant differs from dossier rebuild')
        return grant

    def _leaf_grants(self, progress, company, groups, notes):
        """Rebuild every grant a leaf publication may cite and re-verify the
        persisted primary quote and per-field witnesses against them."""
        grants = {}
        for group in groups:
            for nid in group['note_ids']:
                _, _, payload = notes[nid]
                origin = self._grant(progress, company, notes[nid][1], context=f'note {nid} origin')
                primary = payload['evidence']['primary']
                if primary['grant_id'] != origin['grant_id']:
                    raise PublishRejected(f'note {nid} origin grant differs from the persisted window')
                for key in ('file_hash', 'mapped_fingerprint', 'source_loc', 'original_name'):
                    if primary.get(key) != origin[key]:
                        raise PublishRejected(f'note {nid} primary {key} differs from the rebuilt read grant')
                start, end = primary['start_line'], primary['end_line']
                if not range_is_granted(origin['lines'], start, end):
                    raise PublishRejected(f'note {nid} primary range outside the rebuilt read grant')
                text = ''.join('\n'.join(origin['lines'][str(i)] for i in range(start, end + 1)).split())
                quote = primary.get('quote')
                if not isinstance(quote, str) or not quote.strip() or ''.join(quote.split()) not in text:
                    raise PublishRejected(f'note {nid} primary quote absent from the rebuilt granted lines')
                grants[origin['grant_id']] = origin
                for proof in payload['evidence'].get('field_evidence', []):
                    grant = self._grant(progress, company, proof.get('grant_id'),
                                        context=f'note {nid} field witness')
                    for key in ('file_hash', 'mapped_fingerprint', 'source_loc', 'original_name'):
                        if proof.get(key) != grant[key]:
                            raise PublishRejected(f'note {nid} field witness {key} differs from the rebuilt read grant')
                    grants[grant['grant_id']] = grant
        return grants

    # -- assignment receipt validation --

    @staticmethod
    def _validate_assignment_response(response):
        if not isinstance(response, dict):
            raise PublishFormatRejected('publish assignment response must be a JSON object')
        extra = set(response) - {'assignments', 'outcome', 'reason'}
        if extra:
            raise PublishFormatRejected(f'unknown publish assignment keys: {sorted(extra)}')
        if response.get('outcome') == 'unresolved':
            reason = response.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise PublishFormatRejected('unresolved requires a nonempty employee reason (max 2000 characters)')
            return None
        if response.get('outcome') is not None:
            raise PublishFormatRejected('assignment outcome must be omitted or unresolved')
        assignments = response.get('assignments')
        if not isinstance(assignments, list) or not assignments:
            raise PublishFormatRejected('assignments must be a nonempty array covering every verified note')
        seen_refs, result = set(), []
        for index, item in enumerate(assignments):
            where = f'assignments[{index}]'
            if not isinstance(item, dict) or set(item) != {'group_ref', 'note_ids', 'leaf_code', 'reason'}:
                raise PublishFormatRejected(f'{where} requires exactly group_ref/note_ids/leaf_code/reason')
            try:
                paths.identifier(item['group_ref'])
            except (ValueError, TypeError) as exc:
                raise PublishFormatRejected(f'{where}.group_ref must be a bounded identifier') from exc
            if item['group_ref'] in seen_refs:
                raise PublishFormatRejected(f'duplicate group_ref {item["group_ref"]}')
            seen_refs.add(item['group_ref'])
            note_ids = item['note_ids']
            if (not isinstance(note_ids, list) or not note_ids
                    or any(not isinstance(nid, str) for nid in note_ids)
                    or len(set(note_ids)) != len(note_ids)):
                raise PublishFormatRejected(f'{where}.note_ids must be distinct note id strings')
            for nid in note_ids:
                try:
                    paths.identifier(nid)
                except ValueError as exc:
                    raise PublishFormatRejected(f'{where} carries an invalid note id') from exc
            if not isinstance(item['leaf_code'], str):
                raise PublishFormatRejected(f'{where}.leaf_code must be a template leaf code string')
            reason = item['reason']
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise PublishFormatRejected(f'{where}.reason must be a nonempty employee reason (max 2000 characters)')
            result.append(dict(group_ref=item['group_ref'], note_ids=list(note_ids),
                               leaf_code=item['leaf_code'], reason=reason))
        return result

    @staticmethod
    def _check_assignments(assignments, notes, published, codes, *, tolerate_published=False):
        """Semantic checks against CURRENT database state; actionable errors.

        ``tolerate_published`` is only for re-validating a persisted assignment
        on resume: a note already published to its assigned leaf by THIS
        assignment flow (crash between commit and checkpoint save) is expected;
        any other published note stays a hard rejection.
        """
        verified = {nid for nid, (status, _, _) in notes.items() if status == 'verified'}
        covered = set()
        for item in assignments:
            if item['leaf_code'] not in codes:
                raise PublishFormatRejected(
                    f"group {item['group_ref']}: unknown leaf code {item['leaf_code']!r}; "
                    'choose exactly one template top1 leaf')
            members = item['note_ids']
            for nid in members:
                entry = notes.get(nid)
                if entry is None:
                    raise PublishFormatRejected(f'note {nid} does not exist in this job; assign only persisted notes')
                status = entry[0]
                if status == 'rejected':
                    raise PublishFormatRejected(
                        f'rejected note {nid} is a terminal discard and never publishes; keep it out of assignments')
                if status != 'verified':
                    raise PublishFormatRejected(
                        f'note {nid} is {status}; only verified notes publish and open notes block the gate')
                if nid in published and not (tolerate_published
                                             and published[nid]['leaf_code'] == item['leaf_code']):
                    raise PublishFormatRejected(
                        f"note {nid} already published to {published[nid]['leaf_code']}; "
                        'second assignment or cross-leaf replay is forbidden')
                if nid in covered:
                    raise PublishFormatRejected(
                        f'note {nid} assigned twice; each note lands in exactly one top1 leaf')
                covered.add(nid)
            if len(members) > 1:
                fields = []
                for nid in members:
                    fact = notes[nid][2].get('fact')
                    if not isinstance(fact, dict):
                        raise PublishFormatRejected(f'note {nid} carries no fact fields')
                    fields.append({key: fact.get(key) for key in FACT_KEYS})
                if any(candidate != fields[0] for candidate in fields[1:]):
                    raise PublishFormatRejected(
                        f"group {item['group_ref']}: same_fact members carry different fact fields; "
                        'publish them as separate one-note groups or revise the notes first')
                adjacent = {nid: set() for nid in members}
                member_set = set(members)
                for nid in members:
                    for relation in notes[nid][2].get('relations', []):
                        if (isinstance(relation, dict) and relation.get('rel') == 'same_fact'
                                and relation.get('to_note_id') in member_set):
                            adjacent[nid].add(relation['to_note_id'])
                            adjacent[relation['to_note_id']].add(nid)
                reached = {members[0]}
                frontier = [members[0]]
                while frontier:
                    current = frontier.pop()
                    for other in adjacent[current] - reached:
                        reached.add(other)
                        frontier.append(other)
                if reached != member_set:
                    raise PublishFormatRejected(
                        f"group {item['group_ref']}: merging requires explicit persisted same_fact relations "
                        'connecting every member; the host never infers merges')
        missing = sorted(verified - covered)
        if missing:
            raise PublishFormatRejected(
                'unhandled verified notes: ' + ','.join(missing[:16]) +
                '; every verified note needs exactly one top1 leaf assignment')

    # -- compare_submit receipt validation and mechanical fact_refs rewrite --

    @staticmethod
    def _check_draft(draft, code, base):
        if not isinstance(draft, dict):
            raise PublishFormatRejected('publish compare_submit response must be a JSON object')
        extra = set(draft) - {'leaf_code', 'base_version', 'base_sha', 'blocks', 'conflicts', 'note',
                              'outcome', 'reason', 'merges'}
        if extra:
            raise PublishFormatRejected(f'unknown publish response keys: {sorted(extra)}')
        if draft.get('outcome') == 'unresolved':
            reason = draft.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise PublishFormatRejected('unresolved requires a nonempty employee reason (max 2000 characters)')
            if any(draft.get(key) for key in ('blocks', 'conflicts', 'merges')):
                raise PublishFormatRejected('unresolved cannot carry blocks, conflicts or merges')
            return
        if draft.get('outcome') is not None:
            raise PublishFormatRejected('publish outcome must be omitted or unresolved')
        if draft.get('leaf_code') != code:
            raise PublishFormatRejected('employee target leaf differs from the assigned top1 leaf')
        if (type(draft.get('base_version')) is not int or draft.get('base_version') != base['version']
                or draft.get('base_sha') != base['content_sha']):
            raise PublishFormatRejected('employee base differs from the read leaf version; '
                                        'reread current and copy base_version/base_sha exactly')
        if not isinstance(draft.get('blocks'), list):
            raise PublishFormatRejected('publication blocks must be an array')
        if not draft['blocks'] and not draft.get('merges'):
            raise PublishFormatRejected('publication requires new blocks or merges citing every assigned group')
        if draft.get('merges') is not None and not isinstance(draft['merges'], list):
            raise PublishFormatRejected('merges must be an array')
        if not isinstance(draft.get('conflicts', []), list):
            raise PublishFormatRejected('conflicts must be an array')

    def _rewrite_blocks(self, blocks, minted, member_pairs, published_pairs, known_notes,
                        old_block_ids, *, context):
        if not isinstance(blocks, list):
            raise PublishFormatRejected(f'{context} must be a list of block objects')
        rewritten = []
        for index, block in enumerate(blocks):
            where = f'{context}[{index}]'
            if not isinstance(block, dict) or set(block) != {'block_id', 'text', 'evidence'}:
                raise PublishFormatRejected(f'{where} requires exactly block_id/text/evidence')
            evidence = block['evidence']
            if not isinstance(evidence, list):
                raise PublishFormatRejected(f'{where}.evidence must be a list')
            out_evidence = []
            for evidence_index, entry in enumerate(evidence):
                ew = f'{where}.evidence[{evidence_index}]'
                if isinstance(entry, dict) and 'fact_ids' in entry and 'fact_refs' not in entry:
                    # Verbatim retained evidence copied from current.blocks; the
                    # publisher byte-compares retained blocks against the ledger
                    # inside the publication transaction. Only base block ids may
                    # use this shape; new blocks must cite fact_refs. Retained
                    # entries predate protocol 4, so note_id stays optional here
                    # (byte-equality with the ledger is the actual guard).
                    if block['block_id'] not in old_block_ids:
                        raise PublishFormatRejected(
                            f'{ew}: new blocks must cite fact_refs (group_ref strings or this-job fact ids), '
                            'not fact_ids')
                    if (not {'ref', 'grant_id', 'file_hash', 'original_name', 'source_loc',
                             'start_line', 'end_line', 'fact_ids'} <= set(entry)
                            <= {'ref', 'grant_id', 'file_hash', 'original_name', 'source_loc',
                                'start_line', 'end_line', 'fact_ids', 'note_id'}
                            or not isinstance(entry['fact_ids'], list)
                            or any(type(i) is not int or i < 1 for i in entry['fact_ids'])):
                        raise PublishFormatRejected(
                            f'{ew}: retained evidence must be copied verbatim from current.blocks')
                    out_evidence.append(dict(entry))
                    continue
                if (not isinstance(entry, dict)
                        or set(entry) != {'ref', 'grant_id', 'file_hash', 'original_name', 'source_loc',
                                          'start_line', 'end_line', 'fact_refs', 'note_id'}):
                    raise PublishFormatRejected(
                        f'{ew} requires exactly ref/grant_id/file_hash/original_name/source_loc/'
                        'start_line/end_line/fact_refs/note_id')
                refs = entry['fact_refs']
                if not isinstance(refs, list):
                    raise PublishFormatRejected(f'{ew}.fact_refs must be an array')
                fact_ids = []
                for ref_index, ref in enumerate(refs):
                    if type(ref) is int:
                        if ref < 1 or ref not in published_pairs:
                            raise PublishFormatRejected(
                                f'{ew}.fact_refs[{ref_index}] integer {ref!r} is not a fact published by '
                                'this job; cite this operation groups by group_ref')
                        fact_ids.append(ref)
                    elif isinstance(ref, str):
                        if ref not in minted:
                            raise PublishFormatRejected(
                                f'{ew}.fact_refs[{ref_index}] unknown group_ref {ref!r}; '
                                'use only this operation group refs')
                        fact_ids.append(minted[ref])
                    else:
                        raise PublishFormatRejected(
                            f'{ew}.fact_refs[{ref_index}] must be a JSON integer fact id or a group_ref string')
                note_id = entry['note_id']
                if not isinstance(note_id, str) or note_id not in known_notes:
                    raise PublishFormatRejected(f'{ew}.note_id must be a host-supplied note id of this job')
                for fact_id in fact_ids:
                    members = member_pairs.get(fact_id) or published_pairs.get(fact_id) or set()
                    if note_id not in members:
                        raise PublishFormatRejected(
                            f'{ew}: note {note_id} is not a member witness of fact {fact_id}; '
                            'copy the host-supplied member note id')
                out_evidence.append(dict(ref=entry['ref'], grant_id=entry['grant_id'],
                    file_hash=entry['file_hash'], original_name=entry['original_name'],
                    source_loc=entry['source_loc'], start_line=entry['start_line'],
                    end_line=entry['end_line'], fact_ids=fact_ids, note_id=note_id))
            rewritten.append(dict(block_id=block['block_id'], text=block['text'], evidence=out_evidence))
        return rewritten

    def _submit_request(self, code, base, draft, minted, db, company, job_id, groups, old_block_ids):
        """Build the exact publisher request (also used for replay digests)."""
        published_pairs = {}
        for row in db.execute(
                'SELECT note_id,fact_id FROM md_note_publications WHERE company_id=? AND job_id=?',
                (company, job_id)).fetchall():
            published_pairs.setdefault(row['fact_id'], set()).add(row['note_id'])
        member_pairs = {minted[group['group_ref']]: set(group['note_ids']) for group in groups}
        # note_id is host-supplied across the whole job; the member-witness
        # check below is the actual guard against cross-note citation.
        known_notes = {row['note_id'] for row in db.execute(
            'SELECT note_id FROM md_read_notes WHERE company_id=? AND job_id=?',
            (company, job_id)).fetchall()}
        blocks = self._rewrite_blocks(draft.get('blocks'), minted, member_pairs, published_pairs,
                                      known_notes, old_block_ids, context='blocks')
        conflicts = []
        for index, conflict in enumerate(draft.get('conflicts') or []):
            where = f'conflicts[{index}]'
            if not isinstance(conflict, dict) or set(conflict) != {'old_block_id', 'new_block', 'reason'}:
                raise PublishFormatRejected(f'{where} requires exactly old_block_id/new_block/reason')
            new_block = self._rewrite_blocks([conflict['new_block']], minted, member_pairs,
                                             published_pairs, known_notes, set(), context=where + '.new_block')[0]
            conflicts.append(dict(old_block_id=conflict['old_block_id'], new_block=new_block,
                                  reason=conflict['reason']))
        merges = []
        for index, merge in enumerate(draft.get('merges') or []):
            where = f'merges[{index}]'
            if (not isinstance(merge, dict)
                    or not {'block_id', 'text', 'covers', 'carry', 'reason'} <= set(merge)
                    or set(merge) - {'block_id', 'text', 'covers', 'carry', 'new_evidence', 'reason'}):
                raise PublishFormatRejected(
                    f'{where} requires block_id/text/covers/carry/reason and optional new_evidence')
            try:
                paths.identifier(merge['block_id'])
            except (ValueError, TypeError) as exc:
                raise PublishFormatRejected(f'{where}.block_id must be a bounded identifier') from exc
            if merge['block_id'] in old_block_ids:
                raise PublishFormatRejected(
                    f'{where}.block_id must be a fresh id, never an existing base block id')
            if not isinstance(merge['text'], str) or not merge['text'].strip():
                raise PublishFormatRejected(f'{where}.text must be nonempty merged content')
            covers = merge['covers']
            if (not isinstance(covers, list) or not covers
                    or any(not isinstance(bid, str) for bid in covers)
                    or len(set(covers)) != len(covers)):
                raise PublishFormatRejected(f'{where}.covers must be distinct base block ids')
            for bid in covers:
                if bid not in old_block_ids:
                    raise PublishFormatRejected(
                        f'{where}.covers references {bid!r} outside the current base blocks; '
                        'reread current and cover only existing base block ids')
            carry = merge['carry']
            if not isinstance(carry, list):
                raise PublishFormatRejected(f'{where}.carry must be an array')
            out_carry = []
            for carry_index, item in enumerate(carry):
                carry_where = f'{where}.carry[{carry_index}]'
                if not isinstance(item, dict) or set(item) != {'from_block_id', 'from_ref', 'to_ref'}:
                    raise PublishFormatRejected(
                        f'{carry_where} requires exactly from_block_id/from_ref/to_ref; carried '
                        'evidence content is host-copied from the base leaf, never employee-supplied')
                if item['from_block_id'] not in covers:
                    raise PublishFormatRejected(f'{carry_where}.from_block_id must be one of the covered blocks')
                for key in ('from_ref', 'to_ref'):
                    try:
                        paths.identifier(item[key])
                    except (ValueError, TypeError) as exc:
                        raise PublishFormatRejected(f'{carry_where}.{key} must be a bounded identifier') from exc
                out_carry.append(dict(from_block_id=item['from_block_id'],
                                      from_ref=item['from_ref'], to_ref=item['to_ref']))
            reason = merge['reason']
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise PublishFormatRejected(
                    f'{where}.reason must be a nonempty employee reason (max 2000 characters)')
            # Omitted means no new evidence; once provided it must be a JSON
            # array (empty allowed). Falsy non-array values (null/{}/""/0/false)
            # fail closed instead of being silently coerced to an empty array.
            new_evidence = merge.get('new_evidence', [])
            if not isinstance(new_evidence, list):
                raise PublishFormatRejected(f'{where}.new_evidence must be an array of evidence entries')
            if new_evidence:
                new_evidence = self._rewrite_blocks(
                    [dict(block_id=merge['block_id'], text=merge['text'], evidence=new_evidence)],
                    minted, member_pairs, published_pairs, known_notes, set(),
                    context=where + '.new_evidence')[0]['evidence']
            merges.append(dict(block_id=merge['block_id'], text=merge['text'], covers=list(covers),
                               carry=out_carry, new_evidence=new_evidence, reason=reason))
        cited = {fact_id for block in blocks for entry in block['evidence'] for fact_id in entry['fact_ids']}
        cited |= {fact_id for merge in merges for entry in merge['new_evidence']
                  for fact_id in entry['fact_ids']}
        uncited = sorted(fact_id for fact_id in minted.values() if fact_id not in cited)
        if uncited:
            raise PublishFormatRejected(
                f'every group fact must be cited by at least one new block evidence; uncited fact ids: {uncited}')
        note = draft.get('note')
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise PublishFormatRejected('publish submission requires a nonempty employee note (max 4000 characters)')
        request = dict(code=code, base_version=base['version'], base_sha=base['content_sha'],
                       blocks=blocks, conflicts=conflicts, job_id=job_id, note=note)
        if merges:
            # V2-1 replay compatibility: an absent or empty merge list never
            # enters the operation request fingerprint.
            request['merges'] = merges
        return request

    # -- V2-2 carried-fact attribution verification (CHG-036) --

    @staticmethod
    def _carried_fact_ids(request, base_blocks):
        """Distinct fact ids of the old evidence every merge carry maps in.

        Blocks or refs missing from the base are left to the publisher's own
        carry guards (BaseChanged and the LeafRejected correction loop); this
        helper never reclassifies them.
        """
        base_by_id = {block['block_id']: block for block in base_blocks}
        fact_ids = set()
        for merge in request.get('merges') or []:
            for item in merge['carry']:
                block = base_by_id.get(item['from_block_id'])
                if block is None:
                    continue
                entry = next((e for e in block['evidence']
                              if e.get('ref') == item['from_ref']), None)
                if entry is None:
                    continue
                for fact_id in entry.get('fact_ids') or []:
                    if type(fact_id) is int and fact_id >= 1:
                        fact_ids.add(fact_id)
        return sorted(fact_ids)

    @staticmethod
    def _verify_carried_placements(store, identity_store, placement_store,
                                   *, company, trunk, code, fact_ids):
        """Fail closed unless every carried old fact is still attributed here.

        Each carried fact must have an active identity whose current revision
        IS the cited row, plus a current placement on this exact trunk/leaf
        under the company's exact current binding (pack, version, manifest
        digest and binding revision). facts.module_key and fact_leaf_links
        are never consulted; nothing is backfilled, reassigned or migrated.
        """
        if not fact_ids:
            return
        binding = store.binding.get(company)
        if not binding:
            raise PublishRejected('company has no template binding')
        target = (binding['template_pack'], binding['pack_version'],
                  binding['pack_digest'], binding['binding_revision'])
        for fact_id in fact_ids:
            where = f'carried fact {fact_id}'
            try:
                mapping = identity_store.identity_for_row(company, fact_id)
            except ValueError as exc:
                raise PublishRejected(
                    f'{where}: identity ledger refused the cited row: {exc}') from exc
            if mapping is None:
                raise PublishRejected(
                    f'{where} has no fact identity; a merge never carries unmapped legacy facts')
            try:
                resolved = placement_store.resolve_current_identity(company, mapping['fact_uid'])
            except ValueError as exc:
                raise PublishRejected(
                    f'{where}: attribution ledger verification failed: {exc}') from exc
            if resolved is None:
                raise PublishRejected(
                    f'{where} resolves to an unknown identity; the ledger is inconsistent')
            if resolved['fact_row_id'] != fact_id:
                raise PublishRejected(
                    f'{where} is not the current revision of its identity; '
                    'stale fact rows never merge')
            placement = resolved['placement']
            if placement is None:
                raise PublishRejected(
                    f'{where} has no current leaf placement; a merge never carries it')
            if placement['trunk_code'] != trunk or placement['leaf_code'] != code:
                raise PublishRejected(
                    f"{where} is currently placed on {placement['trunk_code']}/"
                    f"{placement['leaf_code']}, not the merge target {trunk}/{code}")
            placed_binding = (placement['pack_id'], placement['pack_version'],
                              placement['manifest_sha256'], placement['binding_revision'])
            if placed_binding != target:
                raise PublishRejected(
                    f'{where} placement binding differs from the current company binding')

    # -- fact minting and source registry landing (inside the leaf transaction) --

    def _mint_fact(self, db, registry, company, job_id, trunk, group, notes):
        members = sorted(group['note_ids'])
        first_fact = notes[members[0]][2]['fact']
        fields = {key: first_fact.get(key) for key in FACT_KEYS}
        witnesses = []
        for nid in members:
            payload = notes[nid][2]
            fact = payload.get('fact')
            if not isinstance(fact, dict) or {key: fact.get(key) for key in FACT_KEYS} != fields:
                raise PublishRejected(f'same_fact group members carry different fact fields: {nid}')
            evidence = payload.get('evidence') or {}
            primary = evidence.get('primary') or {}
            quote = primary.get('quote')
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 16000:
                raise PublishRejected(f'note {nid} primary quote missing or over the evidence bound')
            proofs = evidence.get('field_evidence') or []
            for proof in proofs:
                ranges = proof.get('ranges')
                if (not isinstance(proof, dict) or proof.get('field') not in ('period', 'unit', 'entity')
                        or not isinstance(ranges, list) or len(ranges) != 1
                        or not isinstance(ranges[0], list) or len(ranges[0]) != 2
                        or any(type(i) is not int for i in ranges[0])
                        or not isinstance(proof.get('quote'), str) or not proof['quote'].strip()
                        or len(proof['quote']) > 16000
                        or not isinstance(proof.get('source_loc'), str) or not proof['source_loc'].strip()
                        or len(proof['source_loc']) > 2048
                        or not isinstance(proof.get('grant_id'), str)
                        or not isinstance(proof.get('mapped_fingerprint'), str)):
                    raise PublishRejected(f'note {nid} field witness is malformed; publication refuses it')
            witnesses.append(dict(note_id=nid, source_loc=primary['source_loc'], quote=quote,
                ranges=[[primary['start_line'], primary['end_line']]],
                mapped_fingerprint=primary['mapped_fingerprint'], grant_id=primary['grant_id'],
                field_evidence=proofs))
        top = witnesses[0]
        span = dict(source_loc=top['source_loc'], quote=top['quote'], ranges=top['ranges'],
            mapped_fingerprint=top['mapped_fingerprint'], reading_protocol=PUBLISH_PROTOCOL,
            field_evidence=top['field_evidence'], sources=witnesses)
        now = datetime.now(timezone.utc).isoformat()
        # Decision 13A: a V4 fact enters its unique leaf as 'claimed'. Source
        # links stay supports/active on their own axis, and confirm_mode='auto'
        # only records automatic processing; source count, mechanical quote or
        # number checks and automatic handling never upgrade it to 'verified'.
        cursor = db.execute(
            '''INSERT INTO facts(company_id,entity,attribute,period,value,value_type,unit,
                source_file,source_page,source_span,valid_from,confidence,status,confirm_mode,created_at,module_key)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (company, fields['entity'], fields['attribute'], fields['period'], fields['value'],
             fields['value_type'], fields['unit'], self.dossier.file_hash, None,
             json.dumps(span, ensure_ascii=False), now, 1.0, 'claimed', 'auto', now, trunk))
        fact_id = int(cursor.lastrowid)
        registry.register_file_source(file_hash=self.dossier.file_hash,
                                      original_name=self.original_name, task_id=job_id)
        for witness in witnesses:
            primary_evidence = registry.add_evidence(file_hash=self.dossier.file_hash,
                locator=witness['source_loc'], quote=witness['quote'],
                coordinate=dict(kind='fact_source_span', ranges=witness['ranges'],
                    mapped_fingerprint=witness['mapped_fingerprint'],
                    reading_protocol=PUBLISH_PROTOCOL, note_id=witness['note_id']),
                authorization=dict(mode='mapped_md_v4_note', job_id=job_id,
                    note_id=witness['note_id'], grant_id=witness['grant_id']),
                task_id=job_id)
            registry.link_fact(fact_id=fact_id, evidence_id=primary_evidence['evidence_id'],
                relation='supports', status='active',
                basis=dict(mode='v4_note_publish', note_id=witness['note_id'], witness='primary',
                           verified=True, two_point_confirmation=False),
                task_id=job_id)
            for proof in witness['field_evidence']:
                field_evidence = registry.add_evidence(file_hash=self.dossier.file_hash,
                    locator=proof['source_loc'], quote=proof['quote'],
                    coordinate=dict(kind='field_evidence', field=proof['field'], ranges=proof['ranges'],
                        mapped_fingerprint=proof['mapped_fingerprint'], grant_id=proof['grant_id'],
                        note_id=witness['note_id']),
                    authorization=dict(mode='mapped_md_v4_note', job_id=job_id,
                        note_id=witness['note_id'], grant_id=proof['grant_id'], field=proof['field']),
                    task_id=job_id)
                registry.link_fact(fact_id=fact_id, evidence_id=field_evidence['evidence_id'],
                    relation='supports', status='active',
                    basis=dict(mode='v4_note_publish', note_id=witness['note_id'], witness='field',
                               field=proof['field'], verified=True, two_point_confirmation=False),
                    task_id=job_id)
        return fact_id

    # -- the one-connection, one-transaction leaf commit --

    def _commit_leaf(self, store, publisher, registry, identity_store, placement_store,
                     *, company, job_id, code, trunk, groups, grants, draft, base,
                     fingerprint, old_block_ids):
        db = store.db
        operation_id = operation_id_for(company, job_id, fingerprint, code)
        prior = db.execute(
            'SELECT request_sha,receipt FROM md_leaf_operations WHERE company_id=? AND operation_id=?',
            (company, operation_id)).fetchone()
        if prior is not None:
            # Post-commit crash or replay: rebuild the minted mapping from the
            # durable publication rows; an identical rewritten request returns
            # the stored receipt with zero writes and zero model calls.
            rows = db.execute(
                'SELECT fact_id,receipt FROM md_note_publications '
                'WHERE company_id=? AND job_id=? AND operation_id=?',
                (company, job_id, operation_id)).fetchall()
            if not rows:
                raise PublishRejected('leaf operation receipt exists without note publication rows')
            minted = {}
            for row in rows:
                entry = json.loads(row['receipt'])
                group_ref = entry['group_ref']
                if group_ref in minted and minted[group_ref] != row['fact_id']:
                    raise PublishRejected('conflicting publication rows for one operation')
                minted[group_ref] = row['fact_id']
            request = self._submit_request(code, base, draft, minted, db, company, job_id,
                                           groups, old_block_ids)
            if digest(request) != prior['request_sha']:
                raise PublishRejected('publication operation replay differs')
            return json.loads(prior['receipt'])
        db.execute('BEGIN IMMEDIATE')
        try:
            if self.fence is not None:
                self.fence.check(db)
            notes = self._load_notes(db, company, job_id)
            if any(status == 'needs_check' for status, _, _ in notes.values()):
                raise PublishRejected('open needs_check notes block publication; return to review')
            published = self._load_publications(db, company, job_id)
            for group in groups:
                for nid in group['note_ids']:
                    if nid in published:
                        raise PublishRejected(
                            f'note {nid} already has a publication row; '
                            'second assignment or cross-leaf replay is forbidden')
                    entry = notes.get(nid)
                    if entry is None or entry[0] != 'verified':
                        raise PublishRejected(f'note {nid} is not a verified note of this job')
            minted = {}
            for group in groups:
                fact_id = self._mint_fact(
                    db, registry, company, job_id, trunk, group, notes)
                identity_store.register_initial_in_transaction(
                    company, fact_id, "system:v4-publish",
                    f"V4 publication {operation_id} created the fact identity")
                placement_store.place_initial_for_publication_in_transaction(
                    company, fact_id, trunk, code, "system:v4-publish",
                    f"V4 publication {operation_id} assigned top1 leaf {code}")
                minted[group['group_ref']] = fact_id
            request = self._submit_request(code, base, draft, minted, db, company, job_id,
                                           groups, old_block_ids)
            if request.get('merges'):
                # CHG-036: carried old evidence keeps its old fact ids, so
                # every carried fact must still belong to THIS leaf under the
                # exact current company binding. Verified against the identity
                # and placement ledgers inside this transaction; a base change
                # skips the lookup and lets submit raise BaseChanged below.
                current = publisher.current(company, code)
                if (current['version'] == base['version']
                        and current['content_sha'] == base['content_sha']):
                    self._verify_carried_placements(store, identity_store,
                        placement_store, company=company, trunk=trunk, code=code,
                        fact_ids=self._carried_fact_ids(request, current['blocks']))
            receipt = publisher.submit(company=company, code=code, operation_id=operation_id,
                base_version=base['version'], base_sha=base['content_sha'],
                blocks=request['blocks'], conflicts=request['conflicts'],
                grants=grants, job_id=job_id, note=request['note'], join_transaction=True,
                merges=request.get('merges'))
            request_sha = digest(request)
            row = db.execute('SELECT projection_fact_ids FROM parse_jobs WHERE company_id=? AND job_id=?',
                             (company, job_id)).fetchone()
            fact_ids = sorted(set(json.loads(row['projection_fact_ids'] or '[]')) | set(minted.values()))
            db.execute('UPDATE parse_jobs SET l2_committed=1,projection_fact_ids=? '
                       'WHERE company_id=? AND job_id=?',
                       (json.dumps(fact_ids), company, job_id))
            now = datetime.now(timezone.utc).isoformat()
            for group in groups:
                group_id = group_id_for(company, job_id, group['note_ids'])
                for nid in group['note_ids']:
                    db.execute('INSERT INTO md_note_publications VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (company, job_id, nid, minted[group['group_ref']], code, operation_id,
                         group_id, request_sha,
                         canonical(dict(note_id=nid, group_ref=group['group_ref'],
                                        fact_id=minted[group['group_ref']], leaf_code=code,
                                        operation_id=operation_id)), now))
            if self.fence is not None:
                self.fence.check(db)
            db.commit()
            return receipt
        except Exception:
            db.rollback()
            raise

    # -- employee payloads --

    def _note_view(self, nid, entry):
        status, _, payload = entry
        primary = payload['evidence']['primary']
        return dict(note_id=nid, status=status,
            fact={key: payload['fact'].get(key) for key in FACT_KEYS},
            primary_ref=dict(grant_id=primary['grant_id'], source_loc=primary['source_loc'],
                start_line=primary['start_line'], end_line=primary['end_line']),
            field_refs=[dict(field=proof['field'], grant_id=proof['grant_id'],
                source_loc=proof['source_loc'], ranges=proof['ranges'])
                for proof in payload['evidence'].get('field_evidence', [])],
            relations=payload.get('relations', []))

    def _assign_payload(self, company_context, notes, published, pack, progress):
        return dict(protocol=PUBLISH_PROTOCOL_NAME, company=company_context,
            operation='assign_leaves', file_hash=self.dossier.file_hash,
            fingerprint=progress['fingerprint'],
            notes=[self._note_view(nid, entry) for nid, entry in notes.items()],
            published=[dict(note_id=nid, **entry) for nid, entry in published.items()],
            template=[dict(leaf_code=leaf.code, trunk=leaf.trunk,
                           trunk_name=pack.trunk_directory(leaf.trunk),
                           title=leaf.title, shape=leaf.shape, scope=leaf.scope)
                      for leaf in pack.leaves])

    def _compare_payload(self, company_context, code, leaf, pack, current, groups,
                         notes, grants, published, item, progress, job_id):
        group_views = []
        for group in groups:
            members = []
            for nid in group['note_ids']:
                view = self._note_view(nid, notes[nid])
                members.append(dict(note_id=nid, primary_ref=view['primary_ref'],
                                    field_refs=view['field_refs']))
            fact = notes[group['note_ids'][0]][2]['fact']
            group_views.append(dict(group_ref=group['group_ref'],
                fact={key: fact.get(key) for key in FACT_KEYS}, members=members))
        payload = dict(protocol=PUBLISH_PROTOCOL_NAME, company=company_context,
            operation='compare_submit', file_hash=self.dossier.file_hash,
            fingerprint=progress['fingerprint'],
            target=dict(leaf_code=code, trunk=leaf.trunk,
                        trunk_name=pack.trunk_directory(leaf.trunk),
                        title=leaf.title, scope=leaf.scope),
            current=current, groups=group_views,
            published=[dict(note_id=nid, **entry) for nid, entry in published.items()],
            grants=[MDWindows.view(grant) for grant in grants.values()])
        if item.get('correction_feedback') is not None:
            payload['correction_feedback'] = item['correction_feedback']
        return payload

    # -- the publication loop --

    def run(self, *, company, job_id, state):
        require_enabled()
        if self.dossier.quality_fingerprint != self.dossier.fingerprint:
            raise PublishRejected('mapped document fidelity fingerprint changed')
        progress = state.get('mapped_notes')
        if not isinstance(progress, dict) or progress.get('protocol') != PUBLISH_PROTOCOL \
                or progress.get('company_id') != company:
            raise PublishRejected('publication requires the V4 notes checkpoint of this company')
        from .md_reader import current_checkpoint_binding_identity, require_checkpoint_binding
        try:
            current = current_checkpoint_binding_identity(self.db_path, company, checkpoint_name='V4 notes')
            require_checkpoint_binding(progress.get('binding_identity'), current, checkpoint_name='V4 notes')
        except LeafRejected as exc:
            if isinstance(exc, PublishRejected):
                raise
            raise PublishRejected(str(exc)) from exc
        if progress['fingerprint'] != self.dossier.fingerprint or progress['file_hash'] != self.dossier.file_hash:
            raise PublishRejected('mapped MD changed; old publication cursor cannot resume')
        # The publication gate is review completion plus zero open notes. The
        # checkpoint awaiting_publication flag is informational only: the
        # reading stage rewrites it on every run, so the authoritative open-note
        # check below re-queries the durable note ledger instead.
        if not progress.get('reading_complete') or not progress.get('review_complete'):
            raise PublishRejected('publication requires completed reading and review')
        publication = progress.setdefault('publication', dict(assignments=None,
            assignment_attempts=0, assignment_rejected=[], items={}, order=[], cursor=0, complete=False))
        publication.setdefault('assignments', None)
        publication.setdefault('assignment_attempts', 0)
        publication.setdefault('assignment_rejected', [])
        publication.setdefault('items', {})
        publication.setdefault('order', [])
        publication.setdefault('cursor', 0)
        if publication.get('complete'):
            return
        # Rebuild and byte-compare every persisted read grant before any quote
        # is reused for publication (never trust checkpoint counters/state).
        for n, record in enumerate(progress['records']):
            if 'grant' not in record:
                continue
            expected = self.windows.grant(self.windows.windows[n], self.original_name)
            expected['company_id'] = company
            if record['grant'] != expected:
                raise PublishRejected('persisted read grant differs from dossier rebuild')
        company_context = dict(company_id=company)
        if self.display_name:
            company_context['display_name'] = self.display_name
        store = LeafPublishService(self.db_path, self.memory, company_id=company,
            quote_resolver=DossierQuoteResolver({self.dossier.file_hash: self.dossier.document}))
        try:
            binding = store.binding.get(company)
            if not binding:
                raise PublishRejected('company has no template binding')
            pack = load_pack(binding['template_pack'])
            if pack.version != binding['pack_version']:
                raise PublishRejected('bound pack version unavailable')
            codes = {leaf.code: leaf for leaf in pack.leaves}
            publisher = MDLeafPublisher(store, fence=self.fence, reading_protocol=PUBLISH_PROTOCOL)
            # Gate-local tables are ensured strictly outside any transaction;
            # the registry joins the caller connection and never owns it.
            store.db.executescript(SCHEMA)
            store.db.commit()
            # These stores are constructed only while idle. During each leaf
            # commit their SAVEPOINT APIs join the publisher's outer transaction.
            identity_store = FactIdentityStore(store.db, company_id=company)
            placement_store = FactPlacementStore(store.db, company_id=company)
            registry = SourceRegistry(self.db_path, company_id=company, connection=store.db)
            notes = self._load_notes(store.db, company, job_id)
            if any(status == 'needs_check' for status, _, _ in notes.values()):
                raise PublishRejected('open needs_check notes block publication; return to review')
            published = self._load_publications(store.db, company, job_id)
            verified = sorted(nid for nid, (status, _, _) in notes.items() if status == 'verified')
            if publication['assignments'] is None:
                if not verified:
                    # Nothing publishable (e.g. every note was rejected): the
                    # gate passes with zero facts and zero model calls.
                    publication['assignments'] = []
                    publication['order'] = []
                    publication['items'] = {}
                    self.save()
                else:
                    request = self._assign_payload(company_context, notes, published, pack, progress)
                    while True:
                        if publication['assignment_attempts'] >= 2:
                            publication['assignment_blocked'] = True
                            self.save()
                            raise ComparisonStopped('publish assignment correction exhausted; '
                                                    'explicit resume_unresolved required')
                        outgoing = deepcopy(request)
                        if publication['assignment_rejected']:
                            outgoing['format_feedback'] = deepcopy(
                                publication['assignment_rejected'][-1]['feedback'])
                        response = self.call('publish', outgoing)
                        try:
                            assignments = self._validate_assignment_response(response)
                            if assignments is None:
                                publication['assignment_unresolved'] = response['reason']
                                self.save()
                                raise ComparisonStopped('employee left publication unassigned: '
                                                        + response['reason'][:200])
                            self._check_assignments(assignments, notes, published, codes)
                        except PublishFormatRejected as exc:
                            publication['assignment_rejected'].append(dict(receipt=deepcopy(response),
                                error=str(exc), feedback=dict(error=str(exc)[:1000],
                                    contract=publish_contract(),
                                    instruction='Correct the assignment receipt: every verified note exactly '
                                                'one top1 leaf; multi-note groups only with persisted same_fact '
                                                'relations and identical fact fields.')))
                            publication['assignment_attempts'] += 1
                            self.save()
                            continue
                        publication['assignments'] = assignments
                        publication['order'] = sorted({item['leaf_code'] for item in assignments})
                        publication['items'] = {code: dict(status='assigned')
                                                for code in publication['order']}
                        publication['cursor'] = 0
                        self.save()
                        break
            else:
                # Persisted assignments are re-validated against current DB
                # state on every resume; a mismatch is a hard stop, not a loop.
                # Notes already published to their assigned leaf by THIS flow
                # (crash between commit and checkpoint save) are expected.
                self._check_assignments(publication['assignments'], notes, published, codes,
                                        tolerate_published=True)
            while publication['cursor'] < len(publication['order']):
                code = publication['order'][publication['cursor']]
                item = publication['items'][code]
                if item.get('status') in ('submitted', 'unresolved'):
                    publication['cursor'] += 1
                    self.save()
                    continue
                groups = [a for a in publication['assignments'] if a['leaf_code'] == code]
                notes = self._load_notes(store.db, company, job_id)
                grants = self._leaf_grants(progress, company, groups, notes)
                if 'draft' not in item:
                    current = publisher.current(company, code)
                    if len(json.dumps(current, ensure_ascii=False).encode()) > 64000:
                        item.update(status='unresolved',
                                    reason='existing leaf exceeds bounded comparison view')
                        publication['cursor'] += 1
                        self.save()
                        continue
                    item['base'] = dict(version=current['version'], content_sha=current['content_sha'])
                    item['old_block_ids'] = [block['block_id'] for block in current['blocks']]
                    self.save()
                    published = self._load_publications(store.db, company, job_id)
                    item['draft'] = self.call('publish', self._compare_payload(
                        company_context, code, codes[code], pack, current, groups, notes,
                        grants, published, item, progress, job_id))
                    self.save()
                draft = item['draft']
                try:
                    self._check_draft(draft, code, item['base'])
                    if draft.get('outcome') == 'unresolved':
                        item.update(status='unresolved', reason=draft['reason'])
                        item.pop('draft', None)
                        publication['cursor'] += 1
                        self.save()
                        continue
                    receipt = self._commit_leaf(store, publisher, registry, identity_store,
                        placement_store, company=company,
                        job_id=job_id, code=code, trunk=codes[code].trunk, groups=groups,
                        grants=grants, draft=draft, base=item['base'],
                        fingerprint=progress['fingerprint'],
                        old_block_ids=item.get('old_block_ids', []))
                    item.update(status='submitted', receipt=receipt)
                    item.pop('correction_feedback', None)
                except BaseChanged:
                    item.pop('draft', None)
                    item['status'] = 'assigned'
                    item['base_changes'] = item.get('base_changes', 0) + 1
                    self.save()
                    if item['base_changes'] >= self.compare_retries:
                        raise ComparisonStopped('bounded same-leaf contention; resume to reread')
                    continue
                except PublishFormatRejected as exc:
                    item.setdefault('rejected_drafts', []).append(dict(draft=deepcopy(draft), reason=str(exc)))
                    item['correction_feedback'] = dict(reason=str(exc)[:1000], contract=publish_contract())
                    item.pop('draft', None)
                    count = item.get('correction_round', 0)
                    if count < self.correction_attempts:
                        item.update(status='assigned', correction_round=count + 1)
                        self.save()
                        continue
                    item.update(status='unresolved', reason=str(exc))
                    self.save()
                except PublishRejected:
                    raise
                except LeafRejected as exc:
                    item.setdefault('rejected_drafts', []).append(dict(draft=deepcopy(draft), reason=str(exc)))
                    item['correction_feedback'] = dict(reason=str(exc)[:1000], contract=publish_contract())
                    item.pop('draft', None)
                    count = item.get('correction_round', 0)
                    if count < self.correction_attempts:
                        item.update(status='assigned', correction_round=count + 1)
                        self.save()
                        continue
                    item.update(status='unresolved', reason=str(exc))
                    self.save()
                publication['cursor'] += 1
                self.save()
            # Final coverage gate from the database, never from counters.
            notes = self._load_notes(store.db, company, job_id)
            published = self._load_publications(store.db, company, job_id)
            unhandled = sorted(nid for nid, (status, _, _) in notes.items()
                               if status == 'verified' and nid not in published)
            if unhandled:
                raise ComparisonStopped('publication incomplete; unhandled verified notes: '
                                        + ','.join(unhandled[:8]))
            publication['complete'] = True
            self.save()
            consumed = FactsTreeRollup(store).consume_notifications(company_id=company, fence=self.fence)
            publication['md_consumption'] = consumed
            self.save()
        finally:
            store.close()
