"""Bounded mapped-MD grants and persisted single-employee action loop."""
from copy import deepcopy
import json

from ..memory.company_facts.l2_consolidation import ConsolidationService
from .leaf_store import LeafPublishService, LeafRejected
from .md_publish import MDLeafPublisher, BaseChanged, digest, fact_in_grant
from .quote_sources import DossierQuoteResolver
from .grants import range_is_granted
from .reading_context import PROTOCOL, bounded_context, block_support_contract
from .template_packs import load_pack
from .tree_meta import require_enabled


class PlanFormatRejected(LeafRejected):
    pass


def allocation_contract():
    return dict(action='plan',
        json_schema=dict(type='object',required=['outcome','assignments','retain_context_windows','retention_reason'],
            properties=dict(outcome=dict(type='string',enum=['assigned','no_content','unresolved']),
                assignments=dict(type='array',items=dict(type='object',required=['leaf_code','ranges','context_windows'],
                    properties=dict(leaf_code=dict(type='string'),
                        ranges=dict(type='array',minItems=1,items=dict(type='array',minItems=2,maxItems=2,items=dict(type='integer'))),
                        context_windows=dict(type='array',maxItems=2,uniqueItems=True,items=dict(type='string'))),
                    **{'not':{'anyOf':[{'required':['retain_context_windows']},{'required':['retention_reason']}]}})),
                retain_context_windows=dict(type='array',maxItems=2,uniqueItems=True,items=dict(type='string')),
                retention_reason=dict(type='string',minLength=1,pattern=r'\S'),reason=dict(type='string')),
            **{'not':{'required':['context_windows']},
               'if':{'properties':{'outcome':{'enum':['no_content','unresolved']}}},
               'then':{'required':['reason']}}),
        required=['outcome','assignments','retain_context_windows','retention_reason'],
        outcomes=['assigned','no_content','unresolved'],
        top_level_context_windows='forbidden; use assignments[i].context_windows',
        retention='Every window explicitly select <=2 current/displayed grant IDs or []; nonempty retention_reason required. Retain applicable forward qualifiers even with no_content; employee judges applicability.',
        referential_qualifiers=('A window may carry a qualifier only by reference: wording such as "the same company/deadline/unit as the most recent record above" or "another record on the same basis, not a new period". '
            'For a field whose concrete literal value is absent from this window text, the carry-over statement alone cannot witness that field and must not replace the concrete earlier grant whose original text states the value. '
            'A mixed statement that literally writes out some field values may still evidence exactly those values under the existing literal/range guards; only the absent fields must be supported by the pointed-to concrete grant. Do not hard-classify a window by words such as "same"; check each field against the actual text. '
            'When the current window is such a bridging/carry-over statement and the concrete qualifier grant is displayed, keep both grants within the at-most-two slots: the bridging grant and the concrete qualifier grant it points back to, when later windows still need its literal fields. '
            'Releasing the concrete grant while keeping only the paraphrase must be backed by a stated reason in retention_reason. If a needed concrete qualifier is no longer displayed or the two slots cannot hold what later tables need, return outcome=unresolved with a nonempty reason; never let a reference stand in for a missing literal, and never ask the backend to choose or inherit a qualifier.'),
        assignment_required=['leaf_code','ranges','context_windows'],
        ranges='Nonempty [start_line,end_line] integer pairs from current window only; example line 1 is illustrative, use actual granted lines.',
        assignment_retention='forbidden; retain_context_windows and retention_reason belong at top level',
        example=dict(outcome='assigned',assignments=[dict(leaf_code='<template leaf_code>',ranges=[[1,1]],context_windows=['<displayed context grant ID>'])],
            retain_context_windows=['<current or displayed grant ID>'],retention_reason='Employee explains why these original qualifiers remain applicable to subsequent windows.'),
        empty_example=dict(outcome='no_content',assignments=[],reason='Employee finds no leaf content in this window.',
            retain_context_windows=[],retention_reason='Employee judges no displayed qualifier applies to subsequent content.'))


def comparison_contract():
    """Structural authority boundary, not a content/duplicate decision."""
    return dict(
        field_support=block_support_contract(),
        retained_blocks=dict(source='current.blocks',
            requirement='Copy every published block verbatim, recursively preserving every field, text and punctuation.',
            fields=['block_id','text','evidence.ref','evidence.grant_id','evidence.file_hash',
                    'evidence.original_name','evidence.source_loc','evidence.start_line','evidence.end_line','evidence.fact_ids'],
            authorization='Published evidence/IDs need not occur in this request grants/facts. The backend rechecks existing evidence; do not replace or regrant it.'),
        new_evidence=dict(applies_to=['new blocks absent from current.blocks','conflicts.new_block'],
            requirement='Only new evidence must use this request grants/facts. New block_id/ref must be distinct from retained ones.'),
        no_content=dict(requirement='When you judge no material belongs to this target leaf, return leaf_code, outcome=no_content, nonempty reason; no blocks/conflicts/assignments. Never use it to evade a structural/evidence error for real content.'),
        no_op=dict(requirement='If you judge this material only confirms existing content with no addition or conflict, copy blocks=current.blocks and return conflicts=[] with your note. Do not change old evidence merely to use the current source.'))


def correction_feedback(assignment):
    saved = assignment.get('correction_feedback')
    if not saved: return None
    # Old interrupted jobs may persist the ambiguous v0.5.0 requirement. Keep
    # their rejected-draft audit, but never retransmit that obsolete instruction.
    return dict(reason=saved['reason'], contract=comparison_contract())


class ComparisonStopped(RuntimeError):
    pass


def checkpoint_binding_identity(binding):
    """Immutable identity of the verified binding when a checkpoint is created."""
    return dict(binding_revision=binding['binding_revision'], pack_id=binding['template_pack'],
        pack_version=binding['pack_version'], manifest_sha256=binding['pack_digest'])


def current_checkpoint_binding_identity(database, company, checkpoint_name='mapped MD'):
    """Read and verify the current company binding without schema repair or writes."""
    from .company_binding import CompanyBindingStore
    store = None
    try:
        store = CompanyBindingStore(database, company_id=company, read_only=True)
        binding = store.get(company)
    except ValueError as exc:
        raise LeafRejected(f'{checkpoint_name} checkpoint binding failed verification: {exc}; '
            'preserve old checkpoint and use a new isolated task') from exc
    finally:
        if store is not None:
            store.close()
    if not binding:
        raise LeafRejected(f'{checkpoint_name} checkpoint binding no longer exists; '
            'preserve old checkpoint and use a new isolated task')
    return checkpoint_binding_identity(binding)


def require_checkpoint_binding(recorded, current, checkpoint_name='mapped MD'):
    """Fail closed unless the persisted identity exactly matches the current
    verified binding; never repair, backfill or upgrade an old checkpoint."""
    prefix = f'{checkpoint_name} checkpoint'
    if recorded is None:
        raise LeafRejected(f'{prefix} predates binding identity recording; '
            'preserve old checkpoint and use a new isolated task, no automatic resume upgrade')
    if (not isinstance(recorded, dict)
            or set(recorded) != {'binding_revision', 'pack_id', 'pack_version', 'manifest_sha256'}
            or type(recorded['binding_revision']) is not int
            or type(recorded['pack_version']) is not int
            or not isinstance(recorded['pack_id'], str)
            or not isinstance(recorded['manifest_sha256'], str)):
        raise LeafRejected(f'{prefix} binding identity malformed; '
            'preserve old checkpoint and use a new isolated task, no automatic resume upgrade')
    if recorded != current:
        raise LeafRejected(f'{prefix} binding identity mismatch; template binding changed after checkpoint creation; '
            'preserve old checkpoint and use a new isolated task, no automatic resume upgrade')


def require_existing_checkpoint_binding(store, company, progress):
    """Fail-closed identity gate for a pre-existing V3 mapped_md checkpoint.

    Any checkpoint that already exists when a run starts — including an
    unread shell — is old state and must already carry the verified binding
    identity; it is never repaired, backfilled or upgraded. Runs before any
    employee call, leaf write, progress write or deadline-renewal state
    change and raises without touching the checkpoint. Returns the verified
    current binding.
    """
    try:
        binding = store.binding.get(company)
    except ValueError as exc:
        raise LeafRejected(f'mapped MD checkpoint binding failed verification: {exc}; '
            'preserve old checkpoint and use a new isolated task') from exc
    if not binding:
        raise LeafRejected('mapped MD checkpoint binding no longer exists; '
            'preserve old checkpoint and use a new isolated task')
    require_checkpoint_binding(progress.get('binding_identity'), checkpoint_binding_identity(binding))
    return binding


class MDWindows:
    def __init__(self, dossier, *, max_lines=80, max_chars=10000):
        self.dossier, self.windows = dossier, []
        if max_lines < 3 or max_chars < 128: raise ValueError('invalid window bounds')
        # Only actual mapped ranges are addressable. No page numbers invented.
        covered = set()
        for element in sorted(dossier.elements, key=lambda e:(e['start_line'], e['end_line'])):
            start, end = element['start_line'], element['end_line']
            covered.update(range(start, end + 1))
            lines = dossier.lines[start-1:end]
            table = len(lines) >= 2 and lines[0].lstrip().startswith('|') and lines[1].lstrip().startswith('|')
            header = list(range(start, min(end+1, start+2))) if table else []
            cursor = start + len(header)
            if cursor > end: cursor = start; header = []
            while cursor <= end:
                stop = cursor-1
                length = sum(len(dossier.lines[i-1])+1 for i in header)
                while stop < end and stop-cursor+1 < max_lines-len(header):
                    extra = len(dossier.lines[stop])+1
                    if length+extra > max_chars: break
                    length += extra; stop += 1
                if stop < cursor:
                    self.windows.append(dict(window_id=str(len(self.windows)), source_loc=element['source_loc'],
                        start_line=cursor, end_line=cursor, ranges=[], unavailable='mapped line exceeds window bound'))
                    cursor += 1
                    continue
                ranges = [[header[0], header[-1]]] if header else []
                ranges.append([cursor, stop])
                self.windows.append(dict(window_id=str(len(self.windows)), source_loc=element['source_loc'],
                    start_line=cursor, end_line=stop, ranges=ranges))
                cursor = stop+1
        self.unmapped = [i for i,line in enumerate(dossier.lines,1) if line.strip() and i not in covered]

    def grant(self, window, original_name):
        lines = {str(i):self.dossier.lines[i-1] for a,b in window['ranges'] for i in range(a,b+1)}
        return dict(grant_id=window['window_id'], mapped_fingerprint=self.dossier.fingerprint, file_hash=self.dossier.file_hash,
            original_name=original_name, source_loc=window['source_loc'], ranges=window['ranges'], lines=lines,
            complete_coordinate=self.complete_coordinate(window['source_loc'],lines))

    def complete_coordinate(self, source_loc, lines):
        elements = [e for e in self.dossier.elements if e['source_loc']==source_loc]
        return bool(elements) and all(str(i) in lines for e in elements for i in range(e['start_line'],e['end_line']+1))

    @staticmethod
    def view(grant):
        return {**grant, 'md':'\n'.join(grant['lines'].values())}




class MappedMDService:
    def __init__(self, db_path, memory, dossier, *, call, save, prepare_window=None, fence=None, compare_retries=3, correction_attempts=2):
        require_enabled()
        self.db_path, self.memory, self.dossier = db_path, memory, dossier
        self.call, self.save = call, save
        self.prepare_window = prepare_window
        self.fence, self.compare_retries = fence, compare_retries
        self.correction_attempts = correction_attempts
        self.windows = MDWindows(dossier)

    def run(self, *, company, job_id, state):
        require_enabled()
        if self.dossier.quality_fingerprint != self.dossier.fingerprint:
            raise LeafRejected('mapped document fidelity fingerprint changed')
        fresh = 'mapped_md' not in state
        progress = state.setdefault('mapped_md', dict(protocol=PROTOCOL, company_id=company, file_hash=self.dossier.file_hash,
            fingerprint=self.dossier.fingerprint, cursor=0, records=[], unresolved=[], affected=[]))
        if progress['fingerprint'] != self.dossier.fingerprint or progress['file_hash'] != self.dossier.file_hash:
            raise LeafRejected('mapped MD changed; old cursor cannot resume')
        if progress.get('protocol')!=PROTOCOL or progress.get('company_id')!=company:
            raise LeafRejected('reading protocol/scope changed; preserve old audit and use a new isolated task, no automatic resume upgrade')
        if any('outcome' in r and (not isinstance(r.get('retention_reason'),str) or not r['retention_reason'].strip())
               for r in progress['records']):
            raise LeafRejected('plan receipt contract changed; preserve old audit/results and use a new isolated task')
        existing = not fresh
        try:
            store = LeafPublishService(self.db_path, self.memory, company_id=company,
                                      quote_resolver=DossierQuoteResolver({self.dossier.file_hash:self.dossier.document}))
        except ValueError as exc:
            if existing:
                raise LeafRejected(f'mapped MD checkpoint binding failed verification: {exc}; '
                    'preserve old checkpoint and use a new isolated task') from exc
            raise
        try:
            publisher = MDLeafPublisher(store, fence=self.fence, reading_protocol=PROTOCOL)
            if existing:
                # A pre-existing checkpoint — even an unread shell — is old
                # state and must already carry its verified binding identity.
                # The done short-circuit is only allowed past this gate.
                binding = require_existing_checkpoint_binding(store, company, progress)
                if progress.get('done'): return
            else:
                # The only place an identity is minted: this run created the
                # shell. Validate and record before any persistence.
                binding = store.binding.get(company)
                if not binding: raise LeafRejected('company has no template binding')
                progress['binding_identity'] = checkpoint_binding_identity(binding)
            pack = load_pack(binding['template_pack'])
            if pack.version != binding['pack_version']:
                raise LeafRejected('bound pack version unavailable')
            company_context = dict(company_id=company, display_name=binding['display_name'])
            codes = {l.code:l for l in pack.leaves}
            _, names = store._sources(company, [self.dossier.file_hash])
            original = next(iter(names))
            if not progress['records'] and self.windows.unmapped:
                progress['unresolved'].append(dict(reason='unmapped MD lines', lines=self.windows.unmapped))
            def retained_grants(selection, current, displayed):
                if (not isinstance(selection,list) or len(selection)>2
                        or any(not isinstance(i,str) for i in selection)
                        or len(set(selection))!=len(selection)):
                    raise LeafRejected('retain_context_windows requires at most two distinct string IDs')
                allowed = {g['grant_id']:g for g in displayed+[current]}
                if any(i not in allowed for i in selection):
                    raise LeafRejected('retained context was not displayed or currently read')
                return [deepcopy(allowed[i]) for i in selection]

            while progress['cursor'] < len(self.windows.windows):
                window = self.windows.windows[progress['cursor']]
                if len(progress['records']) <= progress['cursor']:
                    record = dict(window_id=window['window_id'], status='read_pending', assignments=[])
                    progress['records'].append(record)
                record = progress['records'][progress['cursor']]
                if window.get('unavailable'):
                    record.update(status='unresolved', reason=window['unavailable'])
                    progress['cursor'] += 1; self.save(); continue
                grant = self.windows.grant(window, original)
                grant['company_id'] = company
                # Rebuild employee retention from the completed prefix, never from
                # the current window's newly selected future-facing state.
                retained = []
                history = []
                for previous in progress['records'][:progress['cursor']]:
                    if 'grant' not in previous: continue
                    n = int(previous['window_id'])
                    if not 0 <= n < progress['cursor']: raise LeafRejected('context was not previously read')
                    expected = self.windows.grant(self.windows.windows[n], original)
                    expected['company_id'] = company
                    if previous['grant'] != expected: raise LeafRejected('persisted context scope/fingerprint/lines differ from read window')
                    displayed = list({g['grant_id']:g for g in history[-2:]+retained}.values())
                    if previous.get('rolling_context') != displayed:
                        raise LeafRejected('persisted rolling context differs from read grants')
                    if 'retain_context_windows' in previous:
                        retained = retained_grants(previous['retain_context_windows'], expected, displayed)
                        if previous.get('retained_context') != retained:
                            raise LeafRejected('persisted retained context differs from read grants')
                    history.append(expected)
                contexts = list({g['grant_id']:deepcopy(g) for g in history[-2:]+retained}.values())
                try: bounded_context([MDWindows.view(g) for g in contexts])
                except LeafRejected as exc:
                    record.update(status='unresolved',reason=str(exc))
                    progress['cursor'] += 1; self.save(); continue
                if record.get('rolling_context') is not None and record['rolling_context']!=contexts:
                    raise LeafRejected('persisted rolling context differs from read grants')
                input_retention = [g['grant_id'] for g in retained]
                if record.get('input_retained_context_windows',input_retention)!=input_retention:
                    raise LeafRejected('persisted input retention differs from completed prefix')
                record['input_retained_context_windows'] = input_retention
                record['rolling_context'] = contexts
                if 'retain_context_windows' in record:
                    if record.get('retained_context') != retained_grants(record['retain_context_windows'],grant,contexts):
                        raise LeafRejected('persisted retained context differs from read grants')
                if record['status'] == 'read_pending':
                    record['grant'] = grant
                    self.save()  # Read grant persists before transport; no write transaction held.
                    if self.prepare_window is not None:
                        self.prepare_window(grant, record, company_context)
                    request = dict(protocol='mapped-md-v3', company=company_context, operation='read_assign', file_hash=self.dossier.file_hash,
                        fingerprint=progress['fingerprint'], window=MDWindows.view(grant),
                        context_grants=[MDWindows.view(g) for g in contexts],
                        retained_context_windows=input_retention,
                        retention_contract=dict(field='retain_context_windows',max_grants=2,
                            allowed='current window or displayed context_grants IDs only',
                            required=True,reason='nonempty retention_reason required',empty_list='explicit release',
                            effective='future windows only; current input snapshot unchanged'),
                        response_contract=allocation_contract(),
                        directory=[dict(source_loc=e['source_loc'], start_line=e['start_line'], end_line=e['end_line']) for e in self.dossier.elements[:256]],
                        directory_count=len(self.dossier.elements),
                        read_windows=[dict(grant_id=g['grant_id'],source_loc=g['source_loc'],ranges=g['ranges']) for g in contexts],
                        template=[dict(leaf_code=l.code, trunk=l.trunk, title=l.title, shape=l.shape) for l in pack.leaves])
                    def context_shape(value, path):
                        if not isinstance(value,list):
                            raise PlanFormatRejected(f'{path}: expected array, actual {type(value).__name__}')
                        for index, item in enumerate(value):
                            if not isinstance(item,str):
                                raise PlanFormatRejected(f'{path}[{index}]: expected quoted string grant ID, actual {type(item).__name__} {repr(item)[:80]}; preserve supplied ID spelling, do not remove windows')
                        if len(set(value))!=len(value):
                            raise PlanFormatRejected(f'{path}: duplicate grant IDs; expected distinct strings')
                        if len(value)>2:
                            raise PlanFormatRejected(f'{path}: actual count {len(value)} exceeds maximum 2; employee must choose, host never trims')

                    def validate(response):
                        if not isinstance(response,dict): raise PlanFormatRejected('response must be a JSON object')
                        if 'context_windows' in response:
                            raise PlanFormatRejected('response.context_windows forbidden; use assignments[i].context_windows')
                        if 'retain_context_windows' not in response:
                            raise PlanFormatRejected('response.retain_context_windows required (array, including explicit [])')
                        if not isinstance(response.get('retention_reason'),str) or not response['retention_reason'].strip():
                            raise PlanFormatRejected('response.retention_reason required nonempty string')
                        assignments = response.get('assignments')
                        if not isinstance(assignments, list) or response.get('outcome') not in ('assigned','no_content','unresolved'):
                            raise LeafRejected('invalid MD allocation receipt')
                        seen, allocated = set(), []
                        for ordinal, a in enumerate(assignments):
                            if isinstance(a,dict) and ('retain_context_windows' in a or 'retention_reason' in a):
                                raise PlanFormatRejected(f'assignments[{ordinal}].retain_context_windows/retention_reason forbidden; use top-level retention fields')
                            if isinstance(a,dict) and 'context_windows' not in a:
                                raise PlanFormatRejected(f'assignments[{ordinal}].context_windows required (array, including [])')
                            if not isinstance(a, dict) or not isinstance(a.get('leaf_code'), str) or a['leaf_code'] not in codes or a['leaf_code'] in seen:
                                raise LeafRejected('invalid or duplicate assigned leaf')
                            seen.add(a['leaf_code'])
                            # Allocation records select actual subranges; not implicit whole-file grants.
                            ranges = a.get('ranges')
                            if not isinstance(ranges, list) or not ranges:
                                raise LeafRejected('allocation requires selected line ranges')
                            for pair in ranges:
                                if (not isinstance(pair,list) or len(pair)!=2 or any(type(i) is not int for i in pair)
                                        or not range_is_granted(grant['lines'],pair[0],pair[1])):
                                    raise LeafRejected('allocation outside read window')
                            selected = deepcopy(grant); selected['ranges'] = ranges
                            selected['lines'] = {str(i):grant['lines'][str(i)] for x,y in ranges for i in range(x,y+1)}
                            selected['complete_coordinate'] = self.windows.complete_coordinate(selected['source_loc'],selected['lines'])
                            requested_contexts = a.get('context_windows', [])
                            context_shape(requested_contexts, f'assignments[{ordinal}].context_windows')
                            prior = {g['grant_id']:g for g in record['rolling_context']}
                            if any(not isinstance(i,str) or i not in prior for i in requested_contexts):
                                raise LeafRejected('context window has not been read or is outside displayed bounded context')
                            allocated.append(dict(leaf_code=a['leaf_code'], grant=selected,
                                context_grants=[deepcopy(prior[i]) for i in requested_contexts], status='allocated'))
                        if (response['outcome']=='assigned') != bool(assignments):
                            raise LeafRejected('allocation outcome inconsistent')
                        if response['outcome'] != 'assigned' and (not isinstance(response.get('reason'),str) or not response['reason'].strip()):
                            raise PlanFormatRejected('response.reason required nonempty string for no_content/unresolved')
                        retention = response['retain_context_windows']
                        context_shape(retention, 'response.retain_context_windows')
                        next_retained = retained_grants(retention, grant, contexts)
                        bounded_context([MDWindows.view(g) for g in next_retained])
                        return allocated, retention, next_retained
                    while True:
                        if record.get('plan_format_attempts',0)>=2:
                            record['plan_format_blocked'] = True
                            self.save()
                            raise ComparisonStopped('plan format correction exhausted; explicit resume_unresolved required')
                        outgoing = deepcopy(request)
                        if record.get('plan_rejected_receipts'):
                            outgoing['format_feedback'] = deepcopy(record['plan_rejected_receipts'][-1]['feedback'])
                        response = self.call('plan', outgoing)
                        try:
                            allocated, retention, next_retained = validate(response)
                        except PlanFormatRejected as exc:
                            record.setdefault('plan_rejected_receipts',[]).append(dict(receipt=deepcopy(response),error=str(exc),
                                feedback=dict(error=str(exc),contract=allocation_contract(),
                                    instruction='Correct receipt shape using exactly the same current window and displayed context. Do not redo prior phases or change authorization.')))
                            record['plan_format_attempts'] = record.get('plan_format_attempts',0)+1
                            self.save()
                            continue
                        break
                    record['retention_reason'] = response['retention_reason']
                    record['retain_context_windows'] = retention
                    record['retained_context'] = next_retained
                    record['assignments'] = allocated
                    record['outcome'] = response['outcome']
                    record.update(status='allocated' if allocated else response['outcome'], reason=response.get('reason'))
                    self.save()
                for ordinal, assignment in enumerate(record['assignments']):
                    if assignment['status'] in ('submitted','no_content','unresolved'): continue
                    # Old job checkpoints may carry a copied whole-coordinate
                    # flag. Recompute for active drafts as well as new allocations.
                    for g in [assignment['grant']]+assignment.get('context_grants',[]):
                        g['complete_coordinate'] = self.windows.complete_coordinate(g['source_loc'],g['lines'])
                    attempts = 0
                    while assignment['status'] not in ('submitted', 'no_content', 'unresolved'):
                        code = assignment['leaf_code']
                        op = 'md-'+digest([company,job_id,progress['fingerprint'],window['window_id'],code])[:40]
                        # A saved draft is replayed unchanged, so lost post-commit receipts are idempotent.
                        if 'draft' not in assignment:
                            current = publisher.current(company, code)
                            if len(json.dumps(current, ensure_ascii=False).encode()) > 64000:
                                assignment.update(status='unresolved', reason='existing leaf exceeds U1 bounded comparison view')
                                self.save(); continue
                            allocated_grants = [assignment['grant']] + assignment.get('context_grants', [])
                            facts = ConsolidationService(self.db_path).list_verified_facts(company_id=company)
                            inputs = [dict(fact_id=f['id'], entity=f['entity'], attribute=f['attribute'], period=f['period'],
                                           value=f['value'], unit=f['unit'],field_evidence=json.loads(f['source_span']).get('field_evidence',[])) for f in facts
                                      if f['module_key']==codes[code].trunk and fact_in_grant(f,assignment['grant'],support_grants=allocated_grants,strict=True)]
                            assignment['base'] = dict(version=current['version'], content_sha=current['content_sha'])
                            self.save()
                            response = self.call('leaf', dict(protocol='mapped-md-v3', company=company_context, operation='compare_submit',
                                target=dict(leaf_code=code, trunk=codes[code].trunk, title=codes[code].title),
                                current=current, grants=[MDWindows.view(g) for g in allocated_grants], facts=inputs,
                                comparison_contract=comparison_contract(),
                                correction_feedback=correction_feedback(assignment),
                                declared_sources=[dict(file_hash=self.dossier.file_hash,original_name=original)]))
                            assignment['draft'] = response
                            self.save()
                        draft = assignment['draft']
                        try:
                            if draft.get('leaf_code') != code: raise LeafRejected('employee target mismatch')
                            if draft.get('outcome')=='no_content':
                                reason = draft.get('reason')
                                if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
                                    raise LeafRejected('leaf no_content requires a nonempty employee reason (max 2000 characters)')
                                if any(k in draft and draft[k] != [] for k in ('blocks','conflicts','assignments')):
                                    raise LeafRejected('leaf no_content cannot carry blocks, conflicts or assignments')
                                receipt = dict(operation_id=op,job_id=job_id,leaf_code=code,outcome='no_content',
                                    reason=reason,changed=False,todos=0,
                                    mapped_fingerprint=progress['fingerprint'],
                                    grant_ids=[g['grant_id'] for g in [assignment['grant']]+assignment.get('context_grants',[])])
                                assignment.update(status='no_content',receipt=receipt,technical_rejection=False)
                                self.save(); continue
                            if draft.get('outcome')=='unresolved':
                                if not isinstance(draft.get('reason'),str): raise LeafRejected('unresolved requires employee reason')
                                assignment.update(status='unresolved', reason=draft['reason']); self.save(); continue
                            base = assignment['base']
                            if draft.get('base_version')!=base['version'] or draft.get('base_sha')!=base['content_sha']:
                                raise LeafRejected('employee base differs from read version')
                            receipt = publisher.submit(company=company, code=code, operation_id=op,
                                base_version=base['version'], base_sha=base['content_sha'], blocks=draft.get('blocks'),
                                conflicts=draft.get('conflicts',[]), grants={g['grant_id']:g for g in [assignment['grant']]+assignment.get('context_grants',[])},
                                job_id=job_id, note=draft.get('note'))
                            assignment.update(status='submitted', receipt=receipt, technical_rejection=False)
                            if receipt['changed'] and code not in progress['affected']: progress['affected'].append(code)
                        except BaseChanged:
                            assignment.pop('draft',None)
                            assignment.update(status='allocated', reason='base changed; reread and compare required')
                            self.save()
                            attempts += 1
                            assignment['base_changes'] = assignment.get('base_changes', 0) + 1
                            self.save()
                            if attempts >= self.compare_retries:
                                raise ComparisonStopped('bounded same-leaf contention; resume to reread')
                            continue
                        except LeafRejected as exc:
                            assignment.setdefault('rejected_drafts', []).append(dict(draft=deepcopy(draft), reason=str(exc)))
                            assignment['correction_feedback'] = dict(reason=str(exc)[:1000], contract=comparison_contract())
                            assignment.pop('draft', None)
                            assignment['technical_rejection'] = True
                            count = assignment.get('correction_round', 0)
                            if count < self.correction_attempts:
                                assignment.update(status='allocated', correction_round=count+1)
                                self.save(); continue
                            assignment.update(status='unresolved', reason=str(exc))
                        self.save()
                if record['assignments']:
                    record['status'] = 'processed'
                progress['cursor'] += 1
                self.save()
            # Completion derives from persisted per-window/assignment receipts, not an employee complete flag.
            if len(progress['records']) != len(self.windows.windows) or any(r['status']=='read_pending' for r in progress['records']):
                raise LeafRejected('MD coverage incomplete')
            progress['coverage_complete'] = True
            progress['pending_count'] = len(progress['unresolved']) + sum(r['status']=='unresolved' for r in progress['records']) + sum(a['status']=='unresolved' or a.get('receipt',{}).get('todos',0)>0 for r in progress['records'] for a in r['assignments'])
            progress['technical_rejections'] = sum(a.get('technical_rejection',False) and a['status']=='unresolved' for r in progress['records'] for a in r['assignments'])
            progress['unresolved_readings'] = len(progress['unresolved']) + sum(r['status']=='unresolved' for r in progress['records']) + sum(a['status']=='unresolved' and not a.get('technical_rejection',False) for r in progress['records'] for a in r['assignments'])
            progress['pending_todos'] = sum(a.get('receipt',{}).get('todos',0) for r in progress['records'] for a in r['assignments'])
            progress['content_complete'] = progress['pending_count'] == 0
            progress['done'] = True
            state['leaves_done'] = True
            self.save()
        finally:
            store.close()
