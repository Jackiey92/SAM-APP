"""V2 structured evidence publication; no employee decisions are inferred."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import yaml

from ..memory import paths
from ..memory.company_facts.l1_brief import read_optional
from .grants import range_is_granted
from .reading_context import fields_in_grants, field_support_diagnostic
from .leaf_store import LeafRejected, check_union, FOOTNOTE, DEFINITION, parse_markdown, _ends_footnote
from .tree_meta import require_enabled
from .uris import leaf_uri as pack_leaf_uri

SCHEMA = '''
CREATE TABLE IF NOT EXISTS md_leaf_bodies (
 company_id TEXT NOT NULL, leaf_code TEXT NOT NULL, blocks TEXT NOT NULL,
 PRIMARY KEY(company_id,leaf_code));
CREATE TABLE IF NOT EXISTS md_leaf_operations (
 company_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_sha TEXT NOT NULL,
 receipt TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(company_id,operation_id));
CREATE TABLE IF NOT EXISTS md_leaf_todos (
 company_id TEXT NOT NULL, operation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 leaf_code TEXT NOT NULL, base_version INTEGER NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(company_id,operation_id,ordinal));
CREATE TABLE IF NOT EXISTS md_leaf_notifications (
 company_id TEXT NOT NULL, operation_id TEXT NOT NULL, leaf_code TEXT NOT NULL,
 version INTEGER NOT NULL, PRIMARY KEY(company_id,operation_id));
CREATE TABLE IF NOT EXISTS md_leaf_rewrites (
 company_id TEXT NOT NULL, operation_id TEXT NOT NULL, leaf_code TEXT NOT NULL,
 base_version INTEGER NOT NULL, base_sha TEXT NOT NULL,
 old_markdown TEXT NOT NULL, new_markdown TEXT NOT NULL,
 old_blocks TEXT NOT NULL, new_blocks TEXT NOT NULL,
 coverage TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(company_id,operation_id));
CREATE TRIGGER IF NOT EXISTS md_operations_no_update BEFORE UPDATE ON md_leaf_operations
 BEGIN SELECT RAISE(ABORT,'append-only operation audit'); END;
CREATE TRIGGER IF NOT EXISTS md_operations_no_delete BEFORE DELETE ON md_leaf_operations
 BEGIN SELECT RAISE(ABORT,'append-only operation audit'); END;
CREATE TRIGGER IF NOT EXISTS md_rewrites_no_update BEFORE UPDATE ON md_leaf_rewrites
 BEGIN SELECT RAISE(ABORT,'append-only rewrite audit'); END;
CREATE TRIGGER IF NOT EXISTS md_rewrites_no_delete BEFORE DELETE ON md_leaf_rewrites
 BEGIN SELECT RAISE(ABORT,'append-only rewrite audit'); END;
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class BaseChanged(LeafRejected):
    pass


def fact_in_grant(fact, grant, *, support_grants=None, strict=False):
    if fact['source_file'] != grant['file_hash']: return False
    try:
        span = json.loads(fact['source_span'])
    except (ValueError, TypeError): return False
    if (not isinstance(span,dict) or span.get('source_loc')!=grant['source_loc']
            or not isinstance(span.get('quote'),str) or not span['quote'].strip()
            or ''.join(span['quote'].split()) not in ''.join('\n'.join(grant['lines'].values()).split())):
        return False
    if not fields_in_grants(fact,span,support_grants or [grant],strict=strict): return False
    if 'ranges' not in span:
        # Legacy unpositioned facts are usable only when the ENTIRE coordinate was read.
        return grant.get('complete_coordinate') is True
    return (span.get('mapped_fingerprint')==grant['mapped_fingerprint']
            and isinstance(span['ranges'],list) and bool(span['ranges'])
            and all(isinstance(r,list) and len(r)==2 and all(type(i) is int for i in r)
                    and range_is_granted(grant['lines'],r[0],r[1])
                    for r in span['ranges']))


class MDLeafPublisher:
    def __init__(self, store, *, fence=None, reading_protocol=2):
        require_enabled()
        store._require_writable()
        self.store = store
        self.fence = fence
        self.reading_protocol = reading_protocol
        # Gate-local extension, never an automatic migration of old artifacts.
        store.db.executescript(SCHEMA)

    def current(self, company, code):
        # Ledger and provider use distinct readers. Hold only a short SQLite
        # writer reservation so a concurrent publisher cannot advance the
        # revision between ledger SELECTs and projection read. No model call.
        db = self.store.db
        if db.in_transaction:
            return self._current(company, code)
        db.execute('BEGIN IMMEDIATE')
        try:
            return self._current(company, code)
        finally:
            db.rollback()

    def _current(self, company, code):
        self.store._leaf(company, code)
        db = self.store.db
        index = db.execute('SELECT uri FROM facts_tree_leaf_index WHERE company_id=? AND leaf_code=?', (company, code)).fetchone()
        latest = db.execute('SELECT version,content_sha,fact_ids FROM leaf_revisions WHERE company_id=? AND leaf_code=? ORDER BY version DESC LIMIT 1', (company, code)).fetchone()
        body = db.execute('SELECT blocks FROM md_leaf_bodies WHERE company_id=? AND leaf_code=?', (company, code)).fetchone()
        content = read_optional(self.store.memory, index['uri']) if index else None
        if latest and (content is None or hashlib.sha256(content.encode()).hexdigest() != latest['content_sha']):
            raise LeafRejected('previous projection differs from revision')
        if body and (content is None or parse_markdown(content)[0].get('blocks_sha') != digest(json.loads(body['blocks']))):
            raise LeafRejected('structured body differs from projection')
        return dict(leaf_code=code, version=latest['version'] if latest else 0,
                    content_sha=latest['content_sha'] if latest else '', markdown=content,
                    blocks=json.loads(body['blocks']) if body else [], fact_ids=json.loads(latest['fact_ids']) if latest else [], legacy=bool(latest and not body))

    def _v4_source_match(self, fact, span, specific, grants, note_id):
        """Match one member witness of a protocol-4 multi-source fact.

        The member id is host-supplied (never an employee fact id); its field
        witnesses may use other explicitly granted read windows. V3 facts retain
        the single-source path below unchanged.
        """
        if self.reading_protocol != 4 or note_id is None:
            return False
        sources = span.get('sources') if isinstance(span, dict) else None
        if not isinstance(sources, list):
            return False
        source = next((item for item in sources if isinstance(item, dict)
                       and item.get('note_id') == note_id), None)
        if source is None:
            return False
        witness = {key: source.get(key) for key in
                   ('source_loc', 'quote', 'ranges', 'mapped_fingerprint', 'field_evidence')}
        support = [specific]
        for proof in witness.get('field_evidence') or []:
            if isinstance(proof, dict):
                grant = grants.get(proof.get('grant_id'))
                if grant is not None:
                    support.append(grant)
        candidate = dict(fact)
        candidate['source_span'] = json.dumps(witness, ensure_ascii=False)
        return fact_in_grant(candidate, specific, support_grants=support, strict=True)

    def _carried_witness_quote(self, fact, entry):
        """Persisted original quote backing one carried old-evidence entry.

        The fact ledger is the only authority for a carry: file hash, mapped
        coordinate, line range and the persisted original quote must match the
        cited member witness (V4 entries with a note id) or the consistent
        historical primary span (old V3 evidence without one). Old leaf body
        text is never a quote source; an unverifiable carry fails closed.
        """
        if fact['source_file'] != entry['file_hash']:
            raise LeafRejected('carried evidence file differs from the fact ledger')
        try:
            span = json.loads(fact['source_span'])
        except (ValueError, TypeError) as exc:
            raise LeafRejected('carried fact provenance unavailable') from exc
        if not isinstance(span, dict):
            raise LeafRejected('carried fact provenance unavailable')
        note_id = entry.get('note_id')
        if note_id is not None:
            sources = span.get('sources')
            witness = next((item for item in sources if isinstance(item, dict)
                            and item.get('note_id') == note_id), None) \
                if isinstance(sources, list) else None
            if witness is None:
                raise LeafRejected('carried note id is not a member witness of the fact')
        else:
            # Old V3 evidence may only fall back to the consistent primary span;
            # it can never mint or reshape evidence the ledger does not carry.
            witness = span
        if witness.get('source_loc') != entry['source_loc']:
            raise LeafRejected('carried coordinate differs from the fact ledger')
        ranges = witness.get('ranges')
        if (not isinstance(ranges, list)
                or [entry.get('start_line'), entry.get('end_line')] not in ranges):
            raise LeafRejected('carried line range differs from the fact ledger')
        quote = witness.get('quote')
        if not isinstance(quote, str) or not quote.strip():
            raise LeafRejected('carried persisted quote unavailable')
        return quote

    def _merge_block(self, company, leaf, pack, merge, old, grants):
        """Validate one V2-2 same-leaf merge item against the base blocks.

        The employee supplies only block_id/text/covers/carry ref mappings and
        an optional new_evidence list; carried evidence content is copied
        verbatim from the validated structured ledger (only the ref label is
        renamed) and re-verified against the fact ledger above. Every old
        evidence ref of every covered block maps exactly once and every old
        fact id is fully retained in the merged output.
        """
        if (not {'block_id', 'text', 'covers', 'carry', 'reason'} <= set(merge)
                or set(merge) - {'block_id', 'text', 'covers', 'carry', 'new_evidence', 'reason'}):
            raise LeafRejected('merge requires block_id/text/covers/carry/reason and optional new_evidence')
        block_id = merge['block_id']
        if block_id in old:
            raise LeafRejected('merge output requires a fresh block id')
        reason = merge['reason']
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise LeafRejected('merge requires a nonempty employee reason (max 2000 characters)')
        covers = merge['covers']
        carry = merge['carry']
        if not isinstance(carry, list):
            raise LeafRejected('merge carry must be a list')
        carried_entries = []
        seen = set()
        for item in carry:
            if not isinstance(item, dict) or set(item) != {'from_block_id', 'from_ref', 'to_ref'}:
                raise LeafRejected('carry entries require exactly from_block_id/from_ref/to_ref; '
                                   'carried evidence content is host-copied, never employee-supplied')
            source = old.get(item['from_block_id'])
            if source is None or item['from_block_id'] not in covers:
                raise LeafRejected('carry source outside the covered blocks')
            entry = next((e for e in source['evidence'] if e.get('ref') == item['from_ref']), None)
            if entry is None:
                raise LeafRejected('carry references unknown old evidence')
            key = (item['from_block_id'], item['from_ref'])
            if key in seen:
                raise LeafRejected('old evidence carried more than once')
            seen.add(key)
            try:
                to_ref = paths.identifier(item.get('to_ref'))
            except ValueError as exc:
                raise LeafRejected(str(exc)) from exc
            carried_entries.append(dict(entry, ref=to_ref))
        expected = {(covered_id, e['ref']) for covered_id in covers for e in old[covered_id]['evidence']}
        if seen != expected:
            raise LeafRejected('every old evidence ref of covered blocks must be carried exactly once')
        carried = {}
        for entry in carried_entries:
            if entry['ref'] in carried:
                raise LeafRejected('duplicate carried evidence label')
            facts = self.store._facts(company, leaf, pack, entry['fact_ids'])
            self.store._sources(company, [entry['file_hash']])
            quotes = [self._carried_witness_quote(fact, entry) for fact in facts.values()]
            carried[entry['ref']] = (facts, quotes, entry.get('note_id'))
        # Omitted means no new evidence; once provided it must be a JSON array
        # (empty allowed). Falsy non-array values (null/{}/""/0/false) fail
        # closed instead of being silently coerced to an empty list.
        new_evidence = merge.get('new_evidence', [])
        if not isinstance(new_evidence, list):
            raise LeafRejected('merge new_evidence must be a list when provided')
        evidence = carried_entries + new_evidence
        old_fact_ids = {ref for covered_id in covers for e in old[covered_id]['evidence']
                        for ref in e['fact_ids']}
        if not old_fact_ids <= {ref for e in evidence for ref in e['fact_ids']}:
            raise LeafRejected('merge output must retain every old fact id')
        return self._blocks(company, leaf, pack,
                            [dict(block_id=block_id, text=merge['text'], evidence=evidence)],
                            grants, carried=carried)[0]

    def _blocks(self, company, leaf, pack, blocks, grants, carried=None):
        if not isinstance(blocks, list):
            raise LeafRejected('blocks must be a list')
        ids, refs = set(), set()
        for block in blocks:
            if not isinstance(block, dict): raise LeafRejected('invalid block')
            try:
                paths.identifier(block.get('block_id'))
            except ValueError as exc:
                raise LeafRejected(str(exc)) from exc
            if block['block_id'] in ids: raise LeafRejected('duplicate block id')
            ids.add(block['block_id'])
            text = block.get('text')
            evidence = block.get('evidence')
            if (not isinstance(text, str) or not text.strip() or len(text) > 16000
                    or not isinstance(evidence, list) or not evidence):
                raise LeafRejected('block requires bounded content and evidence')
            # Structure restrictions only, not a content/summary classifier.
            if any(line.startswith(('#', '```', '~~~', '---')) for line in text.splitlines()):
                raise LeafRejected('block must not add headings/frontmatter/fences')
            local = {}
            for evidence_index, e in enumerate(evidence):
                if not isinstance(e, dict): raise LeafRejected('invalid evidence')
                if (not isinstance(e.get('source_loc'), str) or ':' not in e['source_loc']
                        or not isinstance(e.get('original_name'), str)
                        or any(c in e['original_name'] for c in '\r\n')):
                    raise LeafRejected('invalid evidence coordinate/name')
                fact_ids = e.get('fact_ids')
                location = f"block_id={block['block_id'][:64]}, evidence[{evidence_index}]"
                if not isinstance(fact_ids,list):
                    raise LeafRejected(f'invalid fact ids: {location}.fact_ids expected array, actual {type(fact_ids).__name__}')
                for fact_index, fact_id in enumerate(fact_ids):
                    if type(fact_id) is not int or fact_id < 1:
                        raise LeafRejected(f'invalid fact ids: {location}.fact_ids[{fact_index}] expected positive JSON integer, actual {type(fact_id).__name__} {repr(fact_id)[:80]}; use the actual input fact ID without quotes, no automatic conversion')
                try:
                    ref = paths.identifier(e.get('ref'))
                except ValueError as exc:
                    raise LeafRejected(str(exc)) from exc
                if ref in refs: raise LeafRejected('duplicate evidence label')
                refs.add(ref)
                if carried is not None and ref in carried:
                    # Host-copied old evidence: re-verified against the fact
                    # ledger at carry time; old read grants are not re-required
                    # and old V3 entries legitimately carry no note_id.
                    facts, carried_quotes, carried_note = carried[ref]
                    local[ref] = (facts, carried_quotes, None, carried_note)
                    continue
                if not isinstance(e.get('grant_id'), str): raise LeafRejected('grant id must be a string')
                if self.reading_protocol == 4 and not isinstance(e.get('note_id'), str):
                    raise LeafRejected('protocol-4 evidence requires its host-resolved note_id')
                grant = grants.get(e['grant_id'])
                if grant is None: raise LeafRejected('evidence outside read grants')
                for k in ('file_hash', 'original_name', 'source_loc'):
                    if e.get(k) != grant[k]: raise LeafRejected('evidence differs from read grant')
                start, end = e.get('start_line'), e.get('end_line')
                if not range_is_granted(grant['lines'],start,end):
                    raise LeafRejected('evidence line range outside read grant')
                quote = '\n'.join(grant['lines'][str(i)] for i in range(start, end + 1))
                facts = self.store._facts(company, leaf, pack, e.get('fact_ids', []))
                specific = {**grant, 'ranges':[[start,end]],
                    'lines':{str(i):grant['lines'][str(i)] for i in range(start,end+1)},
                    'complete_coordinate':grant.get('complete_coordinate') is True and len(grant['lines'])==end-start+1}
                # Exact mapped coordinate must still resolve, independently of grant.
                if self.store.quote_resolver.resolve(file_hash=e['file_hash'], loc=e['source_loc']) is None:
                    raise LeafRejected('coordinate unavailable in task evidence')
                self.store._sources(company, [e['file_hash']])
                local[ref] = (facts, [quote], specific, e.get('note_id'))
            supports = [entry[2] for entry in local.values() if entry[2] is not None]
            for facts, quote, specific, note_id in local.values():
                if specific is None:
                    # Carried evidence was verified against the fact ledger at
                    # carry time; no current-grant provenance check applies.
                    continue
                for fact in facts.values():
                    try:
                        span = json.loads(fact['source_span'])
                    except (ValueError, TypeError):
                        span = None
                    if self._v4_source_match(fact, span, specific, grants, note_id): continue
                    if self.reading_protocol == 4:
                        # Protocol-4 facts carry per-note member witnesses; the
                        # cited note_id must resolve to one of them and that
                        # witness must verify strictly. There is deliberately
                        # no lenient single-source fallback for V4.
                        raise LeafRejected(f"protocol-4 fact provenance requires the cited note member witness verified in read grants: block_id={block['block_id'][:64]}, fact_id={fact['id']}, note_id={note_id!r}"[:1000])
                    if fact_in_grant(fact,specific,support_grants=supports,strict=self.reading_protocol==3): continue
                    try:
                        span = json.loads(fact['source_span'])
                        primary = dict(fact)
                        primary['source_span'] = json.dumps({**span,'field_evidence':[]})
                        detail = ('primary source is outside this evidence range; cite the fact primary source in this block'
                            if not fact_in_grant(primary,specific) else
                            field_support_diagnostic(fact,span,supports,list(grants.values()),strict=self.reading_protocol==3))
                    except (ValueError,TypeError,KeyError):
                        detail = 'primary source or field evidence metadata invalid; no additional authorization'
                    raise LeafRejected(f"fact provenance not in read window or block field-context evidence: block_id={block['block_id'][:64]}, fact_id={fact['id']}; {detail}"[:1000])
            # Every paragraph or table data row gets its own evidence union.
            used = set()
            for paragraph_number, paragraph in enumerate(text.split('\n\n'),1):
                paragraph = paragraph.strip()
                if not paragraph: continue
                rows = paragraph.splitlines() if paragraph.startswith('|') else [paragraph]
                for row_number,row in enumerate(rows,1):
                    if row.startswith('|') and (row == rows[0] or set(row.replace('|', '').strip()) <= {'-', ':', ' '}):
                        # Header is checked together with EACH data row below.
                        continue
                    labels = FOOTNOTE.findall(row)
                    if not labels or not set(labels) <= local.keys():
                        raise LeafRejected('content lacks matching evidence index')
                    if not row.startswith('|') and not _ends_footnote(row):
                        raise LeafRejected('paragraph lacks terminal footnote')
                    used.update(labels)
                    facts = {i:f for label in labels for i,f in local[label][0].items()}
                    quotes = [quote for label in labels for quote in local[label][1]]
                    try:
                        check_union(row, facts, quotes)
                    except LeafRejected as exc:
                        raise LeafRejected(f"block {block['block_id'][:128]}, paragraph {paragraph_number}, row {row_number}: {exc}") from exc
                    if row.startswith('|'):
                        try: check_union(rows[0], facts, quotes)
                        except LeafRejected as exc:
                            raise LeafRejected(f"block {block['block_id'][:128]}, paragraph {paragraph_number}, header row 1 with data row {row_number}: {exc}") from exc
            if used != local.keys(): raise LeafRejected('dangling or unused evidence index')
        return deepcopy(blocks)

    def submit(self, *, company, code, operation_id, base_version, base_sha,
               blocks, conflicts, grants, job_id, note, join_transaction=False, merges=None):
        require_enabled()
        paths.identifier(operation_id)
        if not isinstance(note, str) or not isinstance(conflicts, list): raise LeafRejected('invalid submission')
        if type(base_version) is not int or base_version < 0 or not isinstance(base_sha, str):
            raise LeafRejected('invalid base version')
        if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
            raise LeafRejected('blocks must be a list of objects')
        for block in blocks:
            try:
                paths.identifier(block.get('block_id'))
            except ValueError as exc:
                raise LeafRejected(str(exc)) from exc
        if len({b['block_id'] for b in blocks}) != len(blocks):
            raise LeafRejected('duplicate block id')
        if merges is not None:
            if not isinstance(merges, list) or any(not isinstance(m, dict) for m in merges):
                raise LeafRejected('merges must be a list of objects')
            for merge in merges:
                try:
                    paths.identifier(merge.get('block_id'))
                except ValueError as exc:
                    raise LeafRejected(str(exc)) from exc
            if len({m['block_id'] for m in merges}) != len(merges):
                raise LeafRejected('duplicate merge block id')
        merges = merges or []
        if merges and self.reading_protocol != 4:
            # V2-2 is protocol-4 only; V2/V3 submissions without merges keep
            # their existing behavior byte-for-byte.
            raise LeafRejected('same-leaf body merge requires reading protocol 4')
        leaf, pack = self.store._leaf(company, code)
        request = dict(code=code, base_version=base_version, base_sha=base_sha,
                       blocks=blocks, conflicts=conflicts, job_id=job_id, note=note)
        if merges:
            # V2-1 digest compatibility: an absent or empty merge list never
            # changes the request fingerprint of an old-style receipt.
            request['merges'] = merges
        request_sha = digest(request)
        db = self.store.db
        if join_transaction:
            if not db.in_transaction:
                raise LeafRejected('joined publication requires an active caller transaction')
            owns_transaction = False
        else:
            if db.in_transaction:
                raise LeafRejected('publisher cannot join an existing transaction without protocol-4 join')
            db.execute('BEGIN IMMEDIATE')
            owns_transaction = True
        try:
            if self.fence is not None: self.fence.check(db)
            prior = db.execute('SELECT request_sha,receipt FROM md_leaf_operations WHERE company_id=? AND operation_id=?', (company, operation_id)).fetchone()
            if prior:
                if prior['request_sha'] != request_sha: raise LeafRejected('operation replay differs')
                if owns_transaction:
                    db.rollback()
                return json.loads(prior['receipt'])
            current = self.current(company, code)
            if current['version'] != base_version or current['content_sha'] != base_sha:
                raise BaseChanged('leaf base changed; reread and compare again')
            if merges and current['legacy']:
                raise LeafRejected('legacy leaf cannot be merged')
            if current['legacy'] and (blocks or not conflicts):
                raise LeafRejected('legacy leaf requires explicit format migration; conflict-only allowed')
            old = {b['block_id']:b for b in current['blocks']}
            if current['legacy']:
                meta, body, _, _ = parse_markdown(current['markdown'])
                sources, names = self.store._sources(company, meta['sources'])
                definitions = self.store._footnotes(body, sources, names)
                previous_facts = db.execute('SELECT fact_ids FROM leaf_revisions WHERE company_id=? AND leaf_code=? AND version=?', (company,code,base_version)).fetchone()
                old['legacy'] = dict(block_id='legacy', markdown=current['markdown'], sources=sources,
                    fact_ids=json.loads(previous_facts['fact_ids']), evidence=[dict(ref=ref,file_hash=sha,original_name=db.execute('SELECT original_name FROM source_files WHERE file_hash=?',(sha,)).fetchone()[0],source_loc=loc) for ref,(sha,loc) in definitions.items()])
            # Retain old evidence exactly; employee chooses new nonconflicting additions.
            new = {b.get('block_id'):b for b in blocks} if isinstance(blocks, list) else {}
            # V2-2: every existing block takes exactly one fate — verbatim
            # retention, coverage by exactly one merge, or conflict old side
            # (which is also retained). A covered block never reappears in
            # blocks and a conflict old side can never be merged away.
            covered = {}
            for merge_index, merge in enumerate(merges):
                covers = merge.get('covers')
                if not isinstance(covers, list) or not covers or len(set(covers)) != len(covers):
                    raise LeafRejected('merge requires distinct covered block ids')
                for covered_id in covers:
                    if covered_id not in old:
                        raise LeafRejected('merge covers an unknown or cross-leaf block')
                    if covered_id in covered:
                        raise LeafRejected('old block covered by more than one merge')
                    covered[covered_id] = merge_index
            conflicted = {c.get('old_block_id') for c in conflicts if isinstance(c, dict)}
            if not current['legacy']:
                for block_id, block in old.items():
                    if block_id in covered:
                        if block_id in new:
                            raise LeafRejected('covered block must not be resubmitted verbatim')
                        if block_id in conflicted:
                            raise LeafRejected('conflict old block cannot be merged away')
                    elif new.get(block_id) != block:
                        raise LeafRejected('existing content/index must be retained')
            retained = [] if current['legacy'] else [b for b in old.values() if b['block_id'] not in covered]
            for block in retained:
                for evidence in block['evidence']:
                    self.store._facts(company, leaf, pack, evidence['fact_ids'])
                    self.store._sources(company, [evidence['file_hash']])
            merged = [self._merge_block(company, leaf, pack, merge, old, grants) for merge in merges]
            additions = [b for b in blocks if b['block_id'] not in old]
            additions = self._blocks(company, leaf, pack, additions, grants)
            # Deterministic output order: walk the base blocks; uncovered stay
            # in place, the first covered position hosts its merge output
            # (merges order on ties), plain additions append in request order.
            validated = []
            placed = set()
            for block in current['blocks']:
                block_id = block['block_id']
                if block_id not in covered:
                    validated.append(old[block_id])
                    continue
                merge_index = covered[block_id]
                if merge_index not in placed:
                    placed.add(merge_index)
                    validated.append(merged[merge_index])
            validated += additions
            if len({b['block_id'] for b in validated}) != len(validated): raise LeafRejected('duplicate block id')
            labels = [e['ref'] for b in validated for e in b['evidence']]
            if len(labels) != len(set(labels)): raise LeafRejected('duplicate evidence label')
            for i, conflict in enumerate(conflicts):
                if (not isinstance(conflict, dict) or conflict.get('old_block_id') not in old
                        or not isinstance(conflict.get('reason'), str) or not conflict['reason'].strip()):
                    raise LeafRejected('conflict requires old position and employee reason')
                candidate = self._blocks(company, leaf, pack, [conflict.get('new_block')], grants)[0]
                if candidate['block_id'] in new: raise LeafRejected('conflicting candidate cannot enter normal leaf')
                payload = dict(target_leaf=code, position=conflict['old_block_id'], base_version=base_version,
                    base_sha=base_sha, old=old[conflict['old_block_id']], new=candidate,
                    reason=conflict['reason'], job_id=job_id)
                db.execute('INSERT INTO md_leaf_todos(company_id,operation_id,ordinal,leaf_code,base_version,payload) VALUES(?,?,?,?,?,?)',
                           (company, operation_id, i, code, base_version, encode(payload)))
            changed = validated != current['blocks']
            version = base_version + int(changed)
            uri = pack_leaf_uri(company, pack, leaf)
            now = datetime.now(timezone.utc).isoformat()
            if changed:
                sources = sorted({e['file_hash'] for b in validated for e in b['evidence']})
                fact_ids = sorted({i for b in validated for e in b['evidence'] for i in e['fact_ids']})
                first = db.execute('SELECT created_at FROM leaf_revisions WHERE company_id=? AND leaf_code=? ORDER BY version LIMIT 1', (company,code)).fetchone()
                meta = dict(schema_version=2, blocks_sha=digest(validated), leaf_code=code, trunk=leaf.trunk, title=leaf.title,
                    company_id=company, status='active', latest_period='unspecified', sources=sources,
                    source_file_count=len(sources), version=version, created_at=first['created_at'] if first else now, updated_at=now, owner_skill='fact-leaf')
                content = '\n\n'.join(f'<!-- block:{b["block_id"]} -->\n{b["text"]}' for b in validated)
                index = '\n'.join(f'[^'+e['ref']+']: '+e['original_name']+' → '+e['source_loc']+
                    f' (lines {e["start_line"]}-{e["end_line"]}; block {b["block_id"]}; sha {e["file_hash"]}; facts '+','.join(map(str,e['fact_ids']))+')'
                    for b in validated for e in b['evidence'])
                markdown = '---\n'+yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)+'---\n## 内容\n'+content+'\n\n## 索引\n'+index+'\n'
                sha = hashlib.sha256(markdown.encode()).hexdigest()
                db.execute('INSERT INTO facts_tree_leaf_index VALUES(?,?,?) ON CONFLICT(company_id,leaf_code) DO NOTHING', (company, code, uri))
                db.execute('''INSERT INTO leaf_revisions(company_id,leaf_code,version,content_sha,source_files,fact_ids,owner_skill,change_note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (company, code, version, sha, encode(sources), encode(fact_ids), 'fact-leaf', note, now))
                db.execute('INSERT INTO md_leaf_bodies VALUES(?,?,?) ON CONFLICT(company_id,leaf_code) DO UPDATE SET blocks=excluded.blocks', (company, code, encode(validated)))
                if merges:
                    # V2-2 rewrite audit: the full old/new markdown and
                    # structured blocks plus the coverage/carry mapping and the
                    # employee reason land in the SAME transaction, one
                    # append-only row per leaf operation.
                    coverage = [dict(block_id=m['block_id'], covers=list(m['covers']),
                        carry=[dict(from_block_id=c['from_block_id'], from_ref=c['from_ref'],
                                    to_ref=c['to_ref']) for c in m['carry']],
                        reason=m['reason']) for m in merges]
                    db.execute('INSERT INTO md_leaf_rewrites VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (company, operation_id, code, base_version, base_sha,
                                current['markdown'] or '', markdown, encode(current['blocks']),
                                encode(validated), encode(coverage), now))
                for ref in fact_ids:
                    fact = db.execute('SELECT * FROM facts WHERE id=?', (ref,)).fetchone()
                    db.execute('''INSERT INTO fact_leaf_links(company_id,fact_id,trunk,leaf_code,source_file,job_id,created_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,fact_id,leaf_code) DO NOTHING''',
                        (company, ref, leaf.trunk, code, fact['source_file'], job_id, now))
                self.store._publish_documents(company, {uri:markdown}, metadata={'company_id':company, 'owner_skill':'fact-leaf'})
                db.execute('INSERT INTO md_leaf_notifications VALUES(?,?,?,?)', (company, operation_id, code, version))
            receipt = dict(operation_id=operation_id, job_id=job_id, owner_skill='fact-leaf', note=note,
                           mapped_fingerprints=sorted({g['mapped_fingerprint'] for g in grants.values()}),
                           leaf_code=code, version=version, changed=changed,
                           uri=uri if current['version'] or changed else None, todos=len(conflicts))
            db.execute('INSERT INTO md_leaf_operations VALUES(?,?,?,?,?)', (company, operation_id, request_sha, encode(receipt), now))
            if self.fence is not None: self.fence.check(db)
            if owns_transaction:
                db.commit()
            return receipt
        except Exception:
            if owns_transaction:
                db.rollback()
            raise
