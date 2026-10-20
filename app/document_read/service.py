"""Single-identity, explicit bypass. Progress and facts share SQLite commits.

No implicit migration, gateway, parser invocation, code execution or business
classification. Transport has no retries: each counted call is one request.
"""
import json
import os
from copy import deepcopy
import math
from ..runtime_errors import EmployeeTransportError, transport_exception
from pathlib import Path

from .. import settings
from ..db.database import connect, validate_schema
from ..employees.model_tool import EmployeeModelTool
from ..employees.worker import EmployeeResult, WorkerUnavailable
from ..memory.company_facts.numeric_guard import validate
from ..memory.company_facts.l2_fact_verifier import verify_candidate
from ..memory.company_facts.l2_consolidation import ConsolidationService
from ..memory.company_facts.l2_projection import L2ProjectionService
from ..memory.company_facts.l1_brief import L1BriefService
from ..memory.company_facts.l0_guide import L0GuideService


class BudgetStopped(RuntimeError):
    pass


class _LeafWorker:
    """Pure forwarding adapter to the reader's charged model channel."""
    def __init__(self, service, state, limits):
        self.service, self.state, self.limits = service, state, limits

    def run(self, *, skill, text, company_id, **kwargs):
        payload = json.loads(text)
        action = payload['action']
        if skill != 'fact-leaf' or action not in ('plan', 'leaf'):
            raise ValueError('invalid leaf worker action')
        if company_id != self.state['company_id']:
            raise ValueError('leaf worker differs from host scope')
        return EmployeeResult([self.service._call(action, payload, self.state, self.limits)])


class DirectReadService:
    def __init__(self, db_path, *, config, dossier, memory, model=None):
        validate_schema(db_path)
        self.db_path, self.dossier, self.memory = db_path, dossier, memory
        self.model = model or EmployeeModelTool(config)
        self.skill_root = Path(config.skill_root)
        self.skill = (self.skill_root / 'file-processor/SKILL.md').read_text()
        self.facts = ConsolidationService(db_path)
        from threading import Lock
        self._execution_lock = Lock()

    def _save(self, conn, state):
        if getattr(self, '_lease', None) is not None:
            self._lease.check(conn)
        conn.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?) '
                     'ON CONFLICT(uri) DO UPDATE SET content=excluded.content',
                     (self.key, json.dumps(state, ensure_ascii=False), '{}'))

    def _call(self, action, payload, state, limits):
        phase = action if action in ('classify', 'attribute', 'plan', 'leaf', 'review', 'publish') else 'extract'
        if phase == 'review' and limits.get('review') is None:
            # Independent review budget was never configured: stop partial with
            # zero review calls; a later run with the sixth budget resumes here.
            raise BudgetStopped('review budget not configured; add an explicit review phase limit')
        if phase == 'publish' and limits.get('publish') is None:
            # Independent publication budget was never configured: stop partial
            # with zero publish calls; V1 (no publish phase) stays at stage B.
            raise BudgetStopped('publish budget not configured; add an explicit publish phase limit')
        state['calls'].setdefault(phase, 0)
        if state['calls'][phase] >= limits[phase]:
            raise BudgetStopped(phase)
        state['calls'][phase] += 1
        state['active_action'] = action
        # Charge before transport; a timeout, malformed response or crash counts.
        with connect(self.db_path) as conn:
            self._save(conn, state)
        skill_text = self.skill
        if payload.get('protocol') == 'mapped-md-v4':
            # Independent gate-open V4 reading skill; the closed-path shared
            # file-processor SKILL.md bytes are never touched by V4 routing.
            skill_text = (self.skill_root / 'file-processor/SKILL.v4.md').read_text()
        elif settings.SAM_FACTS_TREE and action in ('plan', 'leaf'):
            skill_text = (self.skill_root / 'fact-leaf/SKILL.md').read_text()
        if payload.get('protocol') in ('mapped-md-v3', 'mapped-md-v4'):
            skill_text = (f'CURRENT ACTION ONLY: {action}. Return only this action response_contract from the JSON request. '
                          'It overrides other-action and legacy examples in the skill. Do not execute other phases.\n\n' + skill_text)
        raw = self.model.execute(skill_text=skill_text,
            text=json.dumps({'action': action, **payload}, ensure_ascii=False), timeout=180)
        try:
            response = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise EmployeeTransportError('employee returned invalid JSON') from exc
        if not isinstance(response, dict) and phase != 'review':
            # Review responses are structurally validated (and bounded through
            # the per-note correction loop) by the stage B host: a non-object
            # review response counts as a malformed decision, not a transport
            # failure. Invalid JSON above is still a transport error.
            raise EmployeeTransportError('employee response must be an object')
        state.pop('stage_failure', None)
        return response

    def run(self, *, company_id, job_id, limits, lease_seconds=360, renew_deadline_seconds=None, compare_retries=3, correction_attempts=2, resume_unresolved=False, cancel=None, retry_request_id=None, note_review=True):
        if type(note_review) is not bool:
            raise ValueError('note_review must be an explicit bool; V4 defaults to post-read review')
        if not settings.SAM_FACTS_TREE:
            return self._run(company_id=company_id, job_id=job_id, limits=limits, legacy=False)
        from .execution import JobLease
        if renew_deadline_seconds is not None and (type(renew_deadline_seconds) is not int
                or not 1 <= renew_deadline_seconds <= 1800):
            raise ValueError('explicit deadline renewal must be 1..1800 seconds')
        if type(compare_retries) is not int or not 1 <= compare_retries <= 8:
            raise ValueError('compare retries must be 1..8')
        if type(correction_attempts) is not int or not 0 <= correction_attempts <= 2 or type(resume_unresolved) is not bool:
            raise ValueError('corrections must be 0..2; resume_unresolved must be explicit bool')
        if retry_request_id is not None and (not isinstance(retry_request_id,str) or not retry_request_id or len(retry_request_id)>128):
            raise ValueError('retry request ID must be a bounded nonempty string')
        from .execution import JobBusy
        if not self._execution_lock.acquire(blocking=False):
            raise JobBusy('a reader instance cannot serve concurrent file tasks')
        lease = None
        try:
            lease = JobLease(self.db_path, job_id, company_id, seconds=lease_seconds, cancel=cancel)
            self._lease = lease
            self._compare_retries = compare_retries
            self._correction_attempts = correction_attempts
            self._resume_unresolved = resume_unresolved
            self._retry_request_id = retry_request_id
            self._deadline_renewal = renew_deadline_seconds
            return self._run(company_id=company_id, job_id=job_id, limits=limits, legacy=False,
                             note_review=note_review)
        finally:
            try:
                if lease is not None: lease.release()
            finally:
                self._lease = None
                self._execution_lock.release()

    def run_legacy(self, *, company_id, job_id, limits):
        """Explicit v1 compatibility, never selected by the gate-open default."""
        return self._run(company_id=company_id, job_id=job_id, limits=limits, legacy=True)

    @staticmethod
    def _notes_protocol_selected():
        # Explicit per-process opt-in. Absent/anything else keeps V3 default;
        # V3 checkpoints are never auto-upgraded to the V4 notebook flow.
        return os.environ.get('SAM_MD_READ_PROTOCOL', '').strip().lower() in ('4', 'v4')

    def _run(self, *, company_id, job_id, limits, legacy, note_review=True):
        phases = ('classify', 'attribute', 'extract')
        if settings.SAM_FACTS_TREE:
            phases += ('plan', 'leaf')
        if settings.SAM_FACTS_TREE and not legacy and note_review and 'review' in limits:
            # The review is an independent sixth budget. Missing review budget is
            # legal at entry: review then stops partial (resumable), nothing crashes.
            phases += ('review',)
        if settings.SAM_FACTS_TREE and not legacy and 'publish' in limits:
            # Publication is an independent seventh budget, entered only when
            # configured; without it a V4 run still completes at stage B (the
            # V1 reading-notes behavior is frozen for existing callers).
            phases += ('publish',)
        if not company_id or any(type(limits.get(p)) is not int or limits[p] < 0
                                for p in phases):
            raise ValueError('host scope and explicit nonnegative phase limits required')
        if getattr(self.dossier, 'quality_fingerprint', None) != self.dossier.fingerprint:
            raise ValueError('document requires independent fidelity verification')
        self.key = f'sam://kv/rt/md-direct/{job_id}'
        fingerprint = self.dossier.fingerprint
        with connect(self.db_path) as conn:
            job = conn.execute('SELECT * FROM parse_jobs WHERE job_id=? AND company_id=? AND file_hash=?',
                               (job_id, company_id, self.dossier.file_hash)).fetchone()
            if job is None:
                raise ValueError('job is outside host scope')
            old = conn.execute('SELECT content FROM memory_kv WHERE uri=?', (self.key,)).fetchone()
        state = json.loads(old['content']) if old else {
            'company_id':company_id, 'fingerprint':fingerprint, 'calls':dict.fromkeys(limits, 0),
            'classification':None, 'ranges':None, 'complete':False, 'accepted':[],
            'fact_ids':[], 'unresolved':[], 'brief_done':False, 'guide_done':False}
        if state['company_id'] != company_id or state['fingerprint'] != fingerprint:
            raise ValueError('resume evidence or scope changed')
        if settings.SAM_FACTS_TREE and legacy and old is not None:
            # The explicit v1 compatibility entry can never consume a mapped
            # protocol checkpoint: both V4 notes and V3 mapped-MD progress must
            # resume through their own gate-open flow. Key presence decides, so
            # a malformed/null checkpoint cannot slip into the whole-document
            # flow (which would write L2 facts/leaves). Runs before any model
            # call or write.
            if 'mapped_notes' in state:
                raise ValueError('existing V4 notes checkpoint requires explicit SAM_MD_READ_PROTOCOL=v4; '
                                 'run_legacy whole-document flow can never consume or mix with it')
            if 'mapped_md' in state:
                raise ValueError('existing V3 mapped-MD checkpoint must resume through the gate-open mapped reader; '
                                 'run_legacy whole-document flow cannot consume or mix with it')
        notes_v4 = settings.SAM_FACTS_TREE and not legacy and self._notes_protocol_selected()
        if notes_v4 and 'mapped_notes' in state and not isinstance(state['mapped_notes'], dict):
            raise ValueError('existing V4 notes checkpoint malformed; preserve old audit and use a new isolated task')
        if settings.SAM_FACTS_TREE and not legacy and old:
            v4_progress = state.get('mapped_notes')
            v3_progress = state.get('mapped_md')
            if notes_v4:
                if v3_progress is not None:
                    raise ValueError('existing V3 mapped-MD checkpoint cannot auto-upgrade to V4 notes; preserve its audit and use a new isolated V4 task')
                if v4_progress is not None and v4_progress.get('protocol') != 4:
                    raise ValueError('reading protocol changed: preserve old audit/results; use a new isolated protocol-4 task, no automatic upgrade')
                if v4_progress is None and (any(state['calls'].values()) or state.get('accepted') or state.get('done')):
                    raise ValueError('reading protocol changed: preserve old audit/results; use a new isolated protocol-4 task, no automatic upgrade')
            else:
                if v4_progress is not None:
                    raise ValueError('existing V4 notes checkpoint requires explicit SAM_MD_READ_PROTOCOL=v4; it is never downgraded to V3')
                progress = v3_progress
                if progress and any('outcome' in r and (not isinstance(r.get('retention_reason'),str) or not r['retention_reason'].strip())
                                    for r in progress.get('records',[])):
                    raise ValueError('plan receipt contract changed; preserve old audit/results, use a new isolated task')
                if ((progress is not None and progress.get('protocol')!=3)
                        or (progress is None and (any(state['calls'].values()) or state.get('accepted') or state.get('done')))):
                    raise ValueError('reading protocol changed: preserve old audit/results; use a new isolated protocol-3 task, no automatic upgrade')
        if settings.SAM_FACTS_TREE and not legacy and notes_v4:
            if 'mapped_notes' in state:
                self._require_mapped_notes_identity(state['mapped_notes'], company_id)
            else:
                state['mapped_notes'] = dict(protocol=4, company_id=company_id,
                    file_hash=self.dossier.file_hash, fingerprint=fingerprint,
                    cursor=0, records=[], unresolved=[], reading_complete=False,
                    binding_identity=self._new_notes_shell_binding_identity(company_id))
        if settings.SAM_FACTS_TREE and not legacy and not notes_v4 and isinstance(state.get('mapped_md'), dict):
            # A pre-existing V3 checkpoint — even an unread shell — is old
            # state and must already carry the verified binding identity.
            # Fail closed here, before any resume/deadline progress write or
            # employee call; on rejection nothing is mutated or persisted.
            self._require_mapped_md_identity(state['mapped_md'], company_id)
        if settings.SAM_FACTS_TREE:
            for phase in ('plan', 'leaf'):
                state['calls'].setdefault(phase, 0)
        if (settings.SAM_FACTS_TREE and not legacy and self._resume_unresolved
                and (self._retry_request_id is None or state.get('last_md_retry_request_id') != self._retry_request_id)):
            retry_positions = []
            if notes_v4:
                # V1 explicit retry only reopens format-blocked/read-pending
                # windows. Windows with persisted note receipts keep their
                # append-only audit; they are never silently rewritten.
                progress = state.get('mapped_notes', {})
                for index, record in enumerate(progress.get('records', [])):
                    if record.get('note_format_blocked'):
                        record.setdefault('note_format_retry_history', []).append(record.get('note_format_attempts', 0))
                        record['note_format_attempts'] = 0
                        record.pop('note_format_blocked', None)
                        retry_positions.append(index)
                    blocked = {marker: record.pop(marker)
                               for marker in ('context_tool_blocked', 'extract_progress_blocked')
                               if record.get(marker)}
                    if blocked:
                        # Explicit unresolved retry clears stop markers and streak
                        # counters only; calls/deadline/accepted candidates survive.
                        record.setdefault('reading_block_retry_history', []).append(dict(
                            markers=blocked, stalls=record.get('context_tool_stalls', 0),
                            extract_streak=record.get('preparation', {}).get('extract_no_progress_streak', 0)))
                        record['context_tool_stalls'] = 0
                        preparation = record.get('preparation')
                        if isinstance(preparation, dict):
                            preparation['extract_no_progress_streak'] = 0
                            preparation['attribute_tool_rejections'] = 0
                            preparation['extract_tool_rejections'] = 0
                        retry_positions.append(index)
                    elif record.get('status') == 'read_pending':
                        retry_positions.append(index)
                reopen = bool(retry_positions)
                # Stage B reopen: blocked review notes get a new round with their
                # correction/stall counters cleared; reading state and decided
                # notes (append-only receipts) are never replayed or rewritten.
                review_progress = progress.get('review')
                if isinstance(review_progress, dict) and not review_progress.get('complete'):
                    review_positions = []
                    items = review_progress.get('items', {})
                    for index, review_note_id in enumerate(review_progress.get('order', [])):
                        item = items.get(review_note_id)
                        if isinstance(item, dict) and item.get('blocked'):
                            item.setdefault('review_retry_history', []).append(dict(
                                attempts=item.get('attempts', 0),
                                tool_rejections=item.get('tool_rejections', 0),
                                stalls=item.get('stalls', 0)))
                            item['attempts'] = 0
                            item['tool_rejections'] = 0
                            item['stalls'] = 0
                            # Reset to schema-complete defaults rather than
                            # deleting required keys: the review loop assumes
                            # every persisted item carries the full shape.
                            item['blocked'] = False
                            item['feedback'] = None
                            item['tool_feedback'] = None
                            review_positions.append(index)
                    if review_positions:
                        review_progress['cursor'] = min(review_positions)
                        review_progress['complete'] = False
                        progress['review_complete'] = False
                        progress['awaiting_publication'] = False
                        reopen = True
                # Stage C reopen: blocked assignments restart from scratch;
                # unresolved leaves become assigned again with cleared counters.
                # Submitted leaves are never replayed: their durable
                # md_leaf_operations/md_note_publications receipts make a
                # re-execution an idempotent no-op by construction.
                publication = progress.get('publication')
                if isinstance(publication, dict) and not publication.get('complete'):
                    publish_reopened = False
                    if publication.get('assignment_blocked'):
                        publication['assignment_attempts'] = 0
                        publication.pop('assignment_blocked', None)
                        publication.pop('assignment_unresolved', None)
                        publication['assignments'] = None
                        publication['items'] = {}
                        publication['order'] = []
                        publish_reopened = True
                    elif publication.get('assignment_unresolved') is not None:
                        publication.pop('assignment_unresolved', None)
                        publication['assignments'] = None
                        publication['items'] = {}
                        publication['order'] = []
                        publish_reopened = True
                    for pub_item in (publication.get('items') or {}).values():
                        if isinstance(pub_item, dict) and pub_item.get('status') == 'unresolved':
                            pub_item.setdefault('publish_retry_history', []).append(pub_item.get('reason'))
                            pub_item['status'] = 'assigned'
                            pub_item['correction_round'] = 0
                            for key in ('draft', 'base', 'old_block_ids', 'reason', 'correction_feedback'):
                                pub_item.pop(key, None)
                            publish_reopened = True
                    if publish_reopened:
                        publication['cursor'] = 0
                        publication['complete'] = False
                        state['done'] = False
            else:
                progress = state.get('mapped_md', {})
                reopen = False
                for index, record in enumerate(progress.get('records', [])):
                    if record.get('plan_format_blocked'):
                        record.setdefault('plan_format_retry_history',[]).append(record.get('plan_format_attempts',0))
                        record['plan_format_attempts'] = 0
                        record.pop('plan_format_blocked',None)
                        retry_positions.append(index)
                    if record['status']=='unresolved' and not record['assignments']:
                        record.setdefault('reading_retry_history', []).append(record.get('reason'))
                        record['status'] = 'read_pending'
                        retry_positions.append(index)
                    for assignment in record['assignments']:
                        if assignment['status']=='unresolved':
                            assignment.update(status='allocated', correction_round=0)
                            assignment.pop('draft', None)
                            record['status'] = 'allocated'
                            retry_positions.append(index)
            if self._retry_request_id is not None:
                state['last_md_retry_request_id'] = self._retry_request_id
            if retry_positions:
                progress.update(cursor=min(retry_positions), done=False)
                state['done'] = False
                if not notes_v4:
                    state['leaves_done'] = False
                if notes_v4 and reopen:
                    progress['reading_complete'] = False
                    progress['awaiting_publication'] = False
            elif notes_v4 and reopen:
                # Review-only explicit retry: reading stays complete and is never
                # replayed; only the review cursor reopens blocked notes.
                state['done'] = False
                progress['awaiting_publication'] = False
            with connect(self.db_path) as conn: self._save(conn,state)
        if state.get('done'):
            if settings.SAM_FACTS_TREE and not legacy and 'mapped_md' in state:
                try:
                    self._run_mapped_leaves(state, company_id, job_id, limits)
                except Exception as exc:
                    state['done'] = False
                    self._finish(state, job_id, 'partial', str(exc))
                    raise
            if settings.SAM_FACTS_TREE and not legacy and 'mapped_notes' in state:
                try:
                    self._run_mapped_notes(state, company_id, job_id, limits, note_review=note_review)
                    self._maybe_run_publish(state, company_id, job_id, limits)
                except Exception as exc:
                    state['done'] = False
                    self._finish(state, job_id, 'partial', str(exc))
                    raise
            return state
        if settings.SAM_FACTS_TREE and not legacy:
            from ..facts_tree.md_reader import ComparisonStopped
            if notes_v4:
                notes_stopped = ComparisonStopped
                progress_key, progress_defaults = 'mapped_notes', dict(protocol=4, company_id=company_id,
                    file_hash=self.dossier.file_hash, fingerprint=fingerprint,
                    cursor=0, records=[], unresolved=[], reading_complete=False)
            else:
                notes_stopped = ComparisonStopped
                progress_key, progress_defaults = 'mapped_md', dict(protocol=3, company_id=company_id,
                    file_hash=self.dossier.file_hash, fingerprint=fingerprint,
                    cursor=0, records=[], unresolved=[], affected=[])
            if (self._deadline_renewal is not None and (self._retry_request_id is None
                    or state.get('last_md_deadline_request_id') != self._retry_request_id)):
                import time
                if progress_key not in state:
                    state[progress_key] = dict(progress_defaults)
                    if not notes_v4:
                        # A new V3 shell records the verified binding identity
                        # before its first persistence; a pre-existing
                        # checkpoint was already validated above and never gets
                        # an identity minted for it.
                        state[progress_key]['binding_identity'] = self._new_shell_binding_identity(company_id)
                progress = state[progress_key]
                progress['deadline_at'] = time.time() + self._deadline_renewal
                progress['deadline_renewals'] = progress.get('deadline_renewals', 0) + 1
                if self._retry_request_id is not None: state['last_md_deadline_request_id'] = self._retry_request_id
                with connect(self.db_path) as conn:
                    self._save(conn, state)
            try:
                if notes_v4:
                    self._run_mapped_notes(state, company_id, job_id, limits, note_review=note_review)
                    progress = state['mapped_notes']
                    state['complete'] = progress['reading_complete']
                    published = self._maybe_run_publish(state, company_id, job_id, limits)
                    state['done'] = True
                    if published:
                        status, message = 'done', '映射MD整份阅读、笔记、读后复核与唯一归叶发布完成（未解决项见回执）'
                    elif progress.get('awaiting_publication'):
                        status, message = 'done', '映射MD整份阅读、笔记与读后复核完成；发布待显式 publish 阶段预算（未解决项见回执）'
                    elif note_review and progress.get('review_complete'):
                        status, message = 'done', '映射MD整份阅读、笔记与读后复核完成（未解决项见回执）'
                    else:
                        status, message = 'done', '映射MD整份阅读与笔记完成（本运行止于阶段A，未执行读后复核；未解决项见回执）'
                else:
                    self._run_mapped_leaves(state, company_id, job_id, limits)
                    state['complete'] = state['mapped_md']['coverage_complete']
                    state['done'] = True
                    status, message = 'done', '映射MD窗口处理完成（未解决项见回执）'
            except (BudgetStopped, notes_stopped):
                status, message = 'partial', '窗口处理暂停；预算、格式反馈或竞争待续（见窗口回执）'
            except Exception as exc:
                failure = transport_exception(exc)
                if failure is None and isinstance(exc, WorkerUnavailable):
                    failure = EmployeeTransportError(str(exc))
                if failure is None:
                    self._finish(state, job_id, 'partial', str(exc))
                    raise
                state['stage_failure'] = dict(action=state.get('active_action'),
                    kind=failure.error_kind, detail=str(failure), retryable=failure.retryable)
                status, message = 'partial', str(failure)
            self._finish(state, job_id, status, message)
            return state
        context = {'document':self.dossier.document['markdown'],
                   'line_count':len(self.dossier.lines),
                   'map':self.dossier.document['document_map']}
        try:
            if state['classification'] is None:
                result = self._call('classify', context, state, limits)
                if not isinstance(result.get('classification'), dict):
                    raise ValueError('missing document classification')
                state['classification'] = result['classification']
                with connect(self.db_path) as conn:
                    self._save(conn, state)
            while state['ranges'] is None:
                result = self._call('attribute', {**context, 'classification':state['classification'],
                    'format_feedback':state.get('attribution_feedback')}, state, limits)
                try:
                    ranges = result.get('ranges')
                    if not isinstance(ranges, list) or not ranges:
                        raise ValueError('missing attribution ranges')
                    for item in ranges:
                        if not isinstance(item, dict) or not isinstance(item.get('entity'), str) or not item['entity'].strip():
                            raise ValueError('invalid attribution')
                        self.dossier.range(item['start_line'], item['end_line'])
                except (ValueError, KeyError, TypeError) as exc:
                    if state.get('attribution_feedback'):
                        raise EmployeeTransportError('employee attribution format failed after one correction') from exc
                    state['attribution_feedback'] = {'error':str(exc), 'first_line':1,
                        'last_line':len(self.dossier.lines), 'previous_ranges':result.get('ranges')}
                    with connect(self.db_path) as conn:
                        self._save(conn, state)
                    continue  # exactly one counted structural correction, not a business retry
                state['ranges'] = ranges
                with connect(self.db_path) as conn:
                    self._save(conn, state)
            while not state['complete']:
                remaining = state.get('remaining_source_locs')
                evidence = context if remaining is None else {
                    'source_sections':[{'source_loc':loc, 'md':self.dossier.read(loc)} for loc in remaining],
                    'map':self.dossier.document['document_map']}
                delivered = [{k:item[k] for k in ('entity','attribute','period','value','unit','source_loc')}
                             for item in state['accepted']]
                module_options = {}
                if settings.SAM_FACTS_TREE:
                    from ..module_tree import MODULES
                    module_options['module_options'] = [dict(key=m.key, directory=m.directory, hidden=m.hidden) for m in MODULES]
                result = self._call('extract', {**module_options, **evidence, 'classification':state['classification'],
                    'ranges':state['ranges'], 'already_delivered':delivered,
                    'correction_feedback':[item for item in state.get('correction_feedback', [])
                        if item['source'].get('span') and (remaining is None or item['source']['span'] in remaining)]}, state, limits)
                if not isinstance(result.get('facts'), list) or type(result.get('complete')) is not bool:
                    raise ValueError('invalid fact delivery')
                pending = result.get('remaining_source_locs')
                if pending is not None:
                    if not isinstance(pending, list) or any(not isinstance(loc, str) for loc in pending):
                        raise EmployeeTransportError('invalid employee remaining coordinates')
                    if len(set(pending)) != len(pending):
                        raise EmployeeTransportError('duplicate employee remaining coordinates')
                    for loc in pending:
                        try: self.dossier.read(loc)
                        except KeyError as exc:
                            raise EmployeeTransportError('unknown employee remaining coordinate') from exc
                    if remaining is not None and not set(pending).issubset(remaining):
                        raise EmployeeTransportError('employee reopened completed coordinates')
                    if result['complete'] != (not pending):
                        raise EmployeeTransportError('completion differs from employee remaining coordinates')
                next_state = deepcopy(state)
                if pending is not None:
                    next_state['remaining_source_locs'] = pending
                candidates, unresolved = [], []
                for item in result['facts']:
                    try:
                        candidate = self._verify(item, state)
                        if candidate is None:
                            raise ValueError('quote is not present at declared coordinate')
                        candidates.append(candidate)
                        next_state['accepted'].append(item)
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        unresolved.append({'source':{'file_hash':self.dossier.file_hash,
                            'text':None, 'span':item.get('source_loc') if isinstance(item, dict) else None},
                            'reason':'invalid_candidate', 'blocking':str(exc), 'candidate':item})
                next_state['unresolved'].extend(unresolved)
                next_state['correction_feedback'] = unresolved
                next_state['complete'] = result['complete']
                with connect(self.db_path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    committed = self.facts.consolidate_candidates(candidates, unresolved=unresolved,
                                                    company_id=company_id, conn=conn)
                    next_state['fact_ids'] = sorted(set(state['fact_ids']) | set(committed.verified_fact_ids)
                                                    | set(committed.duplicate_fact_ids))
                    if not next_state['fact_ids']:
                        next_state['complete'] = False
                        next_state['remaining_source_locs'] = remaining
                    self._save(conn, next_state)
                    conn.execute('UPDATE parse_jobs SET l2_committed=?,projection_fact_ids=? WHERE job_id=?',
                                 (int(bool(next_state['fact_ids'])), json.dumps(next_state['fact_ids']), job_id))
                state = next_state
            if not settings.SAM_FACTS_TREE:
                L2ProjectionService(self.db_path, self.memory).publish(company_id=company_id)
            if settings.SAM_FACTS_TREE:
                self._run_leaves(state, company_id, job_id, limits)
            service = self
            class Reader:
                def run(self, *, skill, text, **kwargs):
                    action = 'brief' if skill == 'fact-brief' else 'guide'
                    request = json.loads(text)
                    # Supply source cards only, not internal paths/storage tags.
                    if action == 'brief':
                        request = {'facts':[{k:f[k] for k in ('id','entity','attribute','period','value','unit')}
                                            for f in request['facts']]}
                    else:
                        request = {'allowed_entry_ids':[i['entry_id'] for i in request['l1']['items']],
                                   'entries':[{k:i[k] for k in ('entry_id','topic','conclusion','verified_numbers')}
                                              for i in request['l1']['items']]}
                    response = service._call(action, request, state, limits)
                    items = response.get('items')
                    if not isinstance(items, list):
                        raise ValueError('missing reading summary')
                    checked = service._checked_summary(action, items, request, state)
                    if items and not checked:
                        response = service._call(action, {**request,
                            'format_feedback':state['summary_unresolved'][-len(items):]}, state, limits)
                        items = response.get('items')
                        if not isinstance(items, list):
                            raise EmployeeTransportError('missing corrected reading summary')
                        checked = service._checked_summary(action, items, request, state)
                    return EmployeeResult(checked)
            reader = Reader()
            if not settings.SAM_FACTS_TREE and state['fact_ids']:
                if not state['brief_done']:
                    brief = L1BriefService(self.db_path, self.memory, worker=reader).rebuild(company_id=company_id)
                    if not brief or brief.get('status') == 'rejected':
                        raise ValueError('brief source check failed')
                    state['brief_done'] = True
                    with connect(self.db_path) as conn:
                        self._save(conn, state)
                if not state['guide_done']:
                    guide = L0GuideService(self.memory, worker=reader, token_budget=2400).rebuild(company_id=company_id)
                    if not guide or guide.get('status') == 'rejected':
                        raise ValueError('guide source check failed')
                    state['guide_done'] = True
            state['done'] = True
            status, message = 'done', '文件处理完成'
        except BudgetStopped:
            status, message = 'partial', '预算已用尽；已核验事实保留，可提高预算续跑'
        except Exception as exc:
            failure = transport_exception(exc)
            if failure is None and settings.SAM_FACTS_TREE and state.get('active_action') in ('plan', 'leaf') and isinstance(exc, WorkerUnavailable):
                failure = EmployeeTransportError(str(exc))
            if failure is None and state.get('active_action') in ('brief', 'guide') and isinstance(exc, (ValueError, WorkerUnavailable)):
                failure = EmployeeTransportError(str(exc))
            if failure is None:
                self._finish(state, job_id, 'partial', str(exc))
                raise
            state['stage_failure'] = {'action':state.get('active_action'),
                                      'kind':failure.error_kind, 'detail':str(failure),
                                      'retryable':failure.retryable}
            status, message = 'partial', str(failure)
        self._finish(state, job_id, status, message)
        return state

    def _prepare_md_window(self, state, job_id, grant, record, company, call, save):
        """All employee phases see only this granted window, never the full MD."""
        from dataclasses import replace
        from ..facts_tree.md_reader import MDWindows
        from ..facts_tree.leaf_store import LeafRejected
        from ..module_tree import MODULES
        prepared = record.setdefault('preparation', dict(classification=None, ranges=None,
            complete=False, accepted=[], feedback=[], fact_ids=[]))
        payload = dict(protocol='mapped-md-v3', company=company, window=MDWindows.view(grant),
            context_grants=[MDWindows.view(g) for g in record.get('rolling_context',[])],
            retained_context_windows=record.get('input_retained_context_windows',[]),
            source_sections=[dict(source_loc=grant['source_loc'], md='\n'.join(grant['lines'].values()))],
            line_count=len(self.dossier.lines), map=dict(elements=[dict(source_loc=grant['source_loc'],
                start_line=a, end_line=b) for a,b in grant['ranges']]))
        if prepared['classification'] is None:
            response = call('classify', payload)
            if not isinstance(response.get('classification'), dict):
                raise LeafRejected('missing window classification')
            prepared['classification'] = response['classification']; save()
        format_attempts = 0
        while prepared['ranges'] is None:
            contract = dict(nonempty='ranges of explicit entity and granted absolute start_line/end_line; optional outcome=attributed',
                empty=dict(ranges=[],outcome='no_attribution',reason='nonempty employee explanation'),
                boundary='No entity is inferred. Empty attribution does not skip extract or independent read_assign/leaf narrative handling.')
            response = call('attribute', {**payload, 'classification':prepared['classification'],
                'attribute_contract':contract, 'format_feedback':prepared.get('attribution_feedback')})
            try:
                ranges = response.get('ranges')
                if not isinstance(ranges,list): raise ValueError('attribution ranges must be a list')
                if not ranges:
                    if (response.get('outcome')!='no_attribution' or not isinstance(response.get('reason'),str)
                            or not response['reason'].strip() or len(response['reason'])>2000):
                        raise ValueError('empty ranges require outcome=no_attribution and a nonempty reason (max 2000 characters)')
                    decision = dict(outcome='no_attribution',reason=response['reason'])
                else:
                    if response.get('outcome','attributed')!='attributed': raise ValueError('nonempty ranges require attributed outcome')
                    for r in ranges:
                        if (not isinstance(r,dict) or not isinstance(r.get('entity'),str) or not r['entity'].strip()
                                or type(r.get('start_line')) is not int or type(r.get('end_line')) is not int
                                or r['start_line']>r['end_line'] or r['end_line']-r['start_line']+1>len(grant['lines'])
                                or not all(str(i) in grant['lines'] for i in range(r['start_line'],r['end_line']+1))):
                            raise ValueError('attribution outside granted window')
                    decision = dict(outcome='attributed')
            except (ValueError,KeyError,TypeError) as exc:
                prepared.setdefault('attribution_rejections',[]).append(dict(response=deepcopy(response),reason=str(exc)))
                prepared['attribution_feedback'] = dict(error=str(exc),contract=contract)
                save()
                format_attempts += 1
                if format_attempts >= 2:
                    raise BudgetStopped('window attribution format correction exhausted; resume with remaining total budget')
                continue  # At most one charged format correction per invocation.
            prepared['ranges'] = ranges
            prepared['attribution_decision'] = decision
            prepared.pop('attribution_feedback',None)
            save()
        while not prepared['complete']:
            response = call('extract', {**payload, 'classification':prepared['classification'],
                'ranges':prepared['ranges'],
                'already_delivered':[{k:item[k] for k in ('entity','attribute','period','value','unit','source_loc')} for item in prepared['accepted']],
                # Invalid employee text is audit material, not a new read grant.
                'correction_feedback':[dict(reason=r['reason'],blocking=r['blocking'],candidate_index=r['candidate_index'],source_loc=grant['source_loc']) for r in prepared['feedback']],
                'module_options':[dict(key=m.key,directory=m.directory,hidden=m.hidden) for m in MODULES]})
            if not isinstance(response.get('facts'),list) or type(response.get('complete')) is not bool:
                prepared.setdefault('delivery_rejections',[]).append(dict(response=deepcopy(response),reason='invalid window fact delivery'))
                save()
                raise EmployeeTransportError('invalid window fact delivery')
            pending = response.get('remaining_source_locs')
            if pending is not None and (not isinstance(pending,list)
                    or pending not in ([],[grant['source_loc']]) or response['complete'] != (not pending)):
                raise EmployeeTransportError('window remaining coordinates differ from grant')
            candidates, accepted, rejected = [], [], []
            window_state = dict(company_id=state['company_id'], ranges=prepared['ranges'],
                                remaining_source_locs=[grant['source_loc']])
            for candidate_index, item in enumerate(response['facts']):
                try:
                    candidate = self._verify(item, window_state)
                    if candidate is None: raise ValueError('quote is not present at declared coordinate')
                    from ..facts_tree.reading_context import field_evidence
                    proofs = field_evidence({**item,'period':candidate.period,'unit':candidate.unit or '', 'entity':candidate.entity},grant,record.get('rolling_context',[]),state['company_id'])
                    # Locate the proven literal within the attributed/read lines only.
                    q = ''.join(item['quote'].split())
                    selected = ([prepared['ranges'][item['attribution_index']]] if item.get('attribution_index') is not None
                                else [r for r in prepared['ranges'] if r['entity']==item['entity']])
                    found = None
                    for r in selected:
                        normalized = ''.join(''.join(grant['lines'][str(i)].split()) for i in range(r['start_line'],r['end_line']+1))
                        offset = normalized.find(q)
                        if offset < 0: continue
                        cursor, indices = 0, []
                        for i in range(r['start_line'],r['end_line']+1):
                            length = len(''.join(grant['lines'][str(i)].split()))
                            if cursor < offset+len(q) and cursor+length > offset: indices.append(i)
                            cursor += length
                        if indices: found = [indices[0],indices[-1]]; break
                    if found is None: raise ValueError('quote outside attributed read window')
                    span = json.loads(candidate.source_span)
                    span.update(ranges=[found], mapped_fingerprint=grant['mapped_fingerprint'], reading_protocol=3,field_evidence=proofs)
                    handoff = deepcopy(candidate.handoff)
                    handoff['source']['span'] = json.dumps(span,ensure_ascii=False)
                    candidates.append(replace(candidate,source_span=json.dumps(span,ensure_ascii=False),handoff=handoff))
                    accepted.append(item)
                except (ValueError,KeyError,TypeError,OverflowError) as exc:
                    rejected.append(dict(source=dict(file_hash=self.dossier.file_hash,text=None,span=grant['source_loc']),
                        reason='invalid_candidate',blocking=str(exc),candidate_index=candidate_index,candidate=item))
            # Stage the entire checkpoint off the live object graph. A rollback
            # must not let outer _finish persist uncommitted IDs/completion.
            staged = deepcopy(state)
            staged_prepared = next(r for r in staged['mapped_md']['records']
                                   if r['window_id'] == record['window_id'])['preparation']
            with connect(self.db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                result = self.facts.consolidate_candidates(candidates,unresolved=rejected,
                    company_id=state['company_id'],conn=conn)
                ids = set(result.verified_fact_ids) | set(result.duplicate_fact_ids)
                staged_prepared['fact_ids'] = sorted(set(staged_prepared['fact_ids']) | ids)
                staged_prepared['accepted'].extend(accepted); staged_prepared['feedback'] = rejected
                staged_prepared['complete'] = response['complete'] and not (response['facts'] and not candidates)
                staged['accepted'].extend(accepted); staged['unresolved'].extend(rejected)
                staged['correction_feedback'] = rejected
                staged['fact_ids'] = sorted(set(staged['fact_ids']) | ids)
                self._save(conn,staged)
                conn.execute('UPDATE parse_jobs SET l2_committed=?,projection_fact_ids=? WHERE job_id=?',
                    (int(bool(staged['fact_ids'])),json.dumps(staged['fact_ids']),job_id))
            # SQLite context exit has committed successfully. Keep record/reader
            # references intact while publishing only the committed fields.
            prepared.clear(); prepared.update(staged_prepared)
            for key in ('accepted', 'unresolved', 'correction_feedback', 'fact_ids'):
                state[key] = staged[key]
            save()

    def _require_mapped_md_identity(self, progress, company_id):
        """Fail-closed binding-identity gate for a pre-existing V3 checkpoint."""
        from ..facts_tree.leaf_store import LeafPublishService
        from ..facts_tree.md_reader import require_existing_checkpoint_binding
        store = LeafPublishService(self.db_path, self.memory, company_id=company_id)
        try:
            require_existing_checkpoint_binding(store, company_id, progress)
        finally:
            store.close()

    def _require_mapped_notes_identity(self, progress, company_id):
        """V4 resume gate: old checkpoints must already pin the verified binding."""
        from ..facts_tree.md_reader import current_checkpoint_binding_identity, require_checkpoint_binding
        if not isinstance(progress, dict):
            raise ValueError('existing V4 notes checkpoint malformed; preserve old audit and use a new isolated task')
        current = current_checkpoint_binding_identity(self.db_path, company_id, checkpoint_name='V4 notes')
        require_checkpoint_binding(progress.get('binding_identity'), current, checkpoint_name='V4 notes')

    def _new_notes_shell_binding_identity(self, company_id):
        from ..facts_tree.md_reader import current_checkpoint_binding_identity
        return current_checkpoint_binding_identity(self.db_path, company_id, checkpoint_name='V4 notes')

    def _new_shell_binding_identity(self, company_id):
        """Verified binding identity minted only for a newly created V3 shell."""
        from ..facts_tree.leaf_store import LeafPublishService, LeafRejected
        from ..facts_tree.md_reader import checkpoint_binding_identity
        store = LeafPublishService(self.db_path, self.memory, company_id=company_id)
        try:
            binding = store.binding.get(company_id)
            if not binding:
                raise LeafRejected('company has no template binding')
            return checkpoint_binding_identity(binding)
        finally:
            store.close()

    def _run_mapped_leaves(self, state, company_id, job_id, limits):
        # New gate-open default. Legacy v1 remains an explicit compatibility API.
        from ..facts_tree.md_reader import MappedMDService
        import time
        def save():
            records = state.get('mapped_md', {}).get('records', [])
            assignments = [a for r in records for a in r['assignments']]
            state['leaves_built'] = [a['receipt'] for a in assignments if a.get('receipt', {}).get('changed')]
            state['leaves_empty'] = [a['receipt'] for a in assignments if a['status']=='no_content']
            state['leaves_failed'] = [dict(leaf_code=a['leaf_code'], reason=a['reason']) for a in assignments if a['status']=='unresolved']
            with connect(self.db_path) as conn:
                self._save(conn, state)
        def call(action, payload):
            from .contracts import response_contract
            payload = {**payload,'response_contract':response_contract(action)}
            from ..facts_tree.reading_context import MAX_READING_REQUEST_BYTES
            if len(json.dumps({'action':action,**payload},ensure_ascii=False).encode())>MAX_READING_REQUEST_BYTES:
                state['reading_limit'] = dict(action=action,reason='bounded reading request exceeds byte limit; nothing truncated or transmitted')
                raise BudgetStopped('mapped MD request byte limit')
            state.pop('reading_limit',None)
            progress = state['mapped_md']
            progress.setdefault('deadline_at', time.time() + 1800)
            if time.time() >= progress['deadline_at']:
                raise BudgetStopped('mapped MD deadline')
            return self._call(action, payload, state, limits)
        reader = MappedMDService(self.db_path, self.memory, self.dossier, call=call, save=save,
            fence=getattr(self, '_lease', None), compare_retries=getattr(self, '_compare_retries', 3),
            correction_attempts=getattr(self, '_correction_attempts', 2),
            prepare_window=lambda grant, record, company: self._prepare_md_window(state, job_id, grant, record, company, call, save))
        reader.run(company=company_id, job_id=job_id, state=state)
        from ..facts_tree.leaf_store import LeafPublishService
        from ..facts_tree.rollup import FactsTreeRollup
        store = LeafPublishService(self.db_path, self.memory, company_id=company_id)
        try:
            consumed = FactsTreeRollup(store).consume_notifications(company_id=company_id, fence=getattr(self, '_lease', None))
            if consumed['consumed'] or 'md_consumption' not in state:
                state['md_consumption'] = consumed
            save()
        finally: store.close()

    def _prepare_notes_window(self, state, job_id, grant, record, company, call, save, *,
                              context_tool=None, existing_notes=None, read_grant_index=None,
                              revisit_grants=None):
        """V4 window reading: classify/attribute/extract verify candidates only.

        Nothing reaches the L2 fact ledger: accepted candidates become notebook
        inputs for the note receipt, rejected items stay KV-only audit material.
        """
        from ..facts_tree.md_reader import MDWindows, ComparisonStopped
        from ..facts_tree.leaf_store import LeafRejected
        from ..facts_tree.reading_context import field_evidence
        from .notes import NoteRejected as V4NoteRejected
        prepared = record.setdefault('preparation', dict(classification=None, ranges=None,
            complete=False, candidates=[], rejected=[], accepted=[], feedback=[]))
        # Additive checkpoint fields; old partial checkpoints default to empty.
        prepared.setdefault('candidate_keys', [])
        prepared.setdefault('extract_no_progress_streak', 0)
        prepared.setdefault('context_tool_results', [])
        prepared.setdefault('attribute_tool_rejections', 0)
        prepared.setdefault('extract_tool_rejections', 0)

        def combined_grants():
            seen = {}
            sources = list(record.get('rolling_context', []))
            if revisit_grants is not None:
                sources = sources + list(revisit_grants())
            for g in sources:
                seen.setdefault(g['grant_id'], g)
            return list(seen.values())

        tool_contract = dict(
            receipt='mutually exclusive alternative to a normal delivery: exactly '
                    '{"outcome":"request_context","request_context_windows":["<grant_id>"],"reason":"<=2000 chars"}, '
                    'no ranges/facts/complete/other keys in that response',
            index='read_grant_index lists earlier truly-read grant coordinates (grant_id/source_loc/ranges); '
                  'coordinates are not text authorization',
            authorization='only grant IDs of earlier windows truly read in this same job/company/file are displayable; '
                          'future/unread/directory-only/foreign/fingerprint-mismatch IDs are refused with zero side effects',
            bounds='at most two distinct revisit grants per window; rolling+retained+revisit at most six grants and 128000 bytes',
            temporary='a revisit is a temporary per-window redisplay; it never enters retain_context_windows, '
                      'it is not retention, and the same window can be requested again from a later window',
            repeat='a repeated request that displays no new grant is a bounded stall; the second stall stops the window',
            result='granted originals return as context_tool_results on the next same-phase request; the request still spends phase budget')

        def parse_tool_receipt(response):
            windows = response.get('request_context_windows')
            reason = response.get('reason')
            if (not isinstance(windows, list) or not 1 <= len(windows) <= 2
                    or any(not isinstance(w, str) or not w for w in windows)
                    or len(set(windows)) != len(windows)):
                raise ValueError('request_context receipt requires 1..2 distinct request_context_windows grant ID strings')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError('request_context receipt requires a nonempty reason (max 2000 characters)')
            if set(response) != {'outcome', 'request_context_windows', 'reason'}:
                raise ValueError('request_context receipt is mutually exclusive with ranges/facts/complete and extra keys')
            return windows, reason

        def invoke_tool(phase, response, feedback_key):
            windows, reason = parse_tool_receipt(response)
            try:
                if context_tool is None:
                    raise V4NoteRejected('context tool is not available for this window')
                views = context_tool(windows, reason)
            except V4NoteRejected as exc:
                prepared[feedback_key] = dict(error=str(exc)[:1000], contract=tool_contract)
                prepared.setdefault('context_tool_rejections', []).append(
                    dict(phase=phase, requested=windows, reason=reason, error=str(exc)[:1000]))
                save()
                return False
            prepared.pop(feedback_key, None)
            entry = dict(phase=phase, request_context_windows=windows, reason=reason, granted_views=views)
            if views:
                entry['notice'] = 'newly granted earlier-window originals; temporary for this window, not retention'
                # A newly displayed grant is valid tool progress: the following
                # extract delivery must not count as the second no-progress strike.
                prepared['extract_no_progress_streak'] = 0
            else:
                entry['notice'] = 'no new grant displayed; do not repeat the same request_context_windows again'
            prepared['context_tool_results'].append(entry)
            save()
            return True

        payload = dict(protocol='mapped-md-v4', company=company, window=MDWindows.view(grant),
            context_grants=[MDWindows.view(g) for g in combined_grants()],
            retained_context_windows=record.get('input_retained_context_windows', []),
            source_sections=[dict(source_loc=grant['source_loc'], md='\n'.join(grant['lines'].values()))],
            line_count=len(self.dossier.lines), map=dict(elements=[dict(source_loc=grant['source_loc'],
                start_line=a, end_line=b) for a, b in grant['ranges']]))
        if prepared['classification'] is None:
            response = call('classify', payload)
            if not isinstance(response.get('classification'), dict):
                raise LeafRejected('missing window classification')
            prepared['classification'] = response['classification']; save()
        format_attempts = 0
        while prepared['ranges'] is None:
            payload['context_grants'] = [MDWindows.view(g) for g in combined_grants()]
            contract = dict(nonempty='ranges of explicit entity and granted absolute start_line/end_line; optional outcome=attributed',
                empty=dict(ranges=[], outcome='no_attribution', reason='nonempty employee explanation'),
                boundary='No entity is inferred. Empty attribution does not skip extract or the independent note receipt.')
            response = call('attribute', {**payload, 'classification': prepared['classification'],
                'attribute_contract': contract, 'format_feedback': prepared.get('attribution_feedback'),
                'existing_notes': existing_notes or [], 'read_grant_index': read_grant_index or [],
                'context_tool_contract': tool_contract,
                'context_tool_results': [r for r in prepared['context_tool_results'] if r['phase'] == 'attribute'],
                'context_tool_feedback': prepared.get('attribute_tool_feedback')})
            if isinstance(response, dict) and response.get('outcome') == 'request_context':
                # Tool receipts have their own independent correction counter:
                # a denied/malformed tool turn never spends an ordinary
                # attribution format-correction attempt and vice versa.
                try:
                    parse_tool_receipt(response)
                except ValueError as exc:
                    prepared['attribute_tool_feedback'] = dict(error=str(exc), contract=tool_contract)
                    prepared['attribute_tool_rejections'] += 1
                    save()
                    if prepared['attribute_tool_rejections'] >= 2:
                        raise BudgetStopped('window attribute context-tool correction exhausted; resume with remaining total budget')
                    continue
                if not invoke_tool('attribute', response, 'attribute_tool_feedback'):
                    prepared['attribute_tool_rejections'] += 1
                    if prepared['attribute_tool_rejections'] >= 2:
                        raise BudgetStopped('window attribute context-tool correction exhausted; resume with remaining total budget')
                continue
            try:
                ranges = response.get('ranges')
                if not isinstance(ranges, list): raise ValueError('attribution ranges must be a list')
                if not ranges:
                    if (response.get('outcome') != 'no_attribution' or not isinstance(response.get('reason'), str)
                            or not response['reason'].strip() or len(response['reason']) > 2000):
                        raise ValueError('empty ranges require outcome=no_attribution and a nonempty reason (max 2000 characters)')
                    prepared['attribution_decision'] = dict(outcome='no_attribution', reason=response['reason'])
                else:
                    if response.get('outcome', 'attributed') != 'attributed': raise ValueError('nonempty ranges require attributed outcome')
                    for r in ranges:
                        if (not isinstance(r, dict) or not isinstance(r.get('entity'), str) or not r['entity'].strip()
                                or type(r.get('start_line')) is not int or type(r.get('end_line')) is not int
                                or r['start_line'] > r['end_line'] or r['end_line'] - r['start_line'] + 1 > len(grant['lines'])
                                or not all(str(i) in grant['lines'] for i in range(r['start_line'], r['end_line'] + 1))):
                            raise ValueError('attribution outside granted window')
                    prepared['attribution_decision'] = dict(outcome='attributed')
            except (ValueError, KeyError, TypeError) as exc:
                prepared.setdefault('attribution_rejections', []).append(dict(response=deepcopy(response), reason=str(exc)))
                prepared['attribution_feedback'] = dict(error=str(exc), contract=contract)
                save()
                format_attempts += 1
                if format_attempts >= 2:
                    raise BudgetStopped('window attribution format correction exhausted; resume with remaining total budget')
                continue
            prepared['ranges'] = ranges
            prepared.pop('attribution_feedback', None)
            save()
        while not prepared['complete']:
            payload['context_grants'] = [MDWindows.view(g) for g in combined_grants()]
            response = call('extract', {**payload, 'classification': prepared['classification'],
                'ranges': prepared['ranges'],
                'already_delivered': [{k: item[k] for k in ('entity', 'attribute', 'period', 'value', 'unit', 'source_loc')}
                                      for item in prepared['accepted']],
                'correction_feedback': [dict(reason=r['reason'], blocking=r['blocking'],
                    candidate_index=r['candidate_index'], source_loc=grant['source_loc']) for r in prepared['feedback']],
                'existing_notes': existing_notes or [], 'read_grant_index': read_grant_index or [],
                'context_tool_contract': tool_contract,
                'context_tool_results': [r for r in prepared['context_tool_results'] if r['phase'] == 'extract'],
                'context_tool_feedback': prepared.get('extract_tool_feedback')})
            if isinstance(response, dict) and response.get('outcome') == 'request_context':
                try:
                    parse_tool_receipt(response)
                except ValueError as exc:
                    prepared['extract_tool_feedback'] = dict(error=str(exc), contract=tool_contract)
                    prepared['extract_tool_rejections'] += 1
                    save()
                    if prepared['extract_tool_rejections'] >= 2:
                        raise BudgetStopped('window extract context-tool correction exhausted; resume with remaining total budget')
                    continue
                if not invoke_tool('extract', response, 'extract_tool_feedback'):
                    prepared['extract_tool_rejections'] += 1
                    if prepared['extract_tool_rejections'] >= 2:
                        raise BudgetStopped('window extract context-tool correction exhausted; resume with remaining total budget')
                continue
            if not isinstance(response.get('facts'), list) or type(response.get('complete')) is not bool:
                prepared.setdefault('delivery_rejections', []).append(dict(response=deepcopy(response), reason='invalid window fact delivery'))
                save()
                raise EmployeeTransportError('invalid window fact delivery')
            pending = response.get('remaining_source_locs')
            if pending is not None and (not isinstance(pending, list)
                    or pending not in ([], [grant['source_loc']]) or response['complete'] != (not pending)):
                raise EmployeeTransportError('window remaining coordinates differ from grant')
            window_state = dict(protocol=4, company_id=state['company_id'], ranges=prepared['ranges'],
                                remaining_source_locs=[grant['source_loc']])
            accepted, rejected, duplicates = [], [], []
            known_keys = set(prepared['candidate_keys'])
            for candidate_index, item in enumerate(response['facts']):
                try:
                    candidate = self._verify(item, window_state)
                    if candidate is None: raise ValueError('quote is not present at declared coordinate')
                    proofs = field_evidence({**item, 'period': candidate.period, 'unit': candidate.unit or '',
                                             'entity': candidate.entity}, grant,
                                            [g for g in combined_grants() if g['grant_id'] != grant['grant_id']],
                                            state['company_id'])
                    q = ''.join(item['quote'].split())
                    selected = ([prepared['ranges'][item['attribution_index']]] if item.get('attribution_index') is not None
                                else [r for r in prepared['ranges'] if r['entity'] == item['entity']])
                    found = None
                    for r in selected:
                        normalized = ''.join(''.join(grant['lines'][str(i)].split())
                                             for i in range(r['start_line'], r['end_line'] + 1))
                        offset = normalized.find(q)
                        if offset < 0: continue
                        cursor, indices = 0, []
                        for i in range(r['start_line'], r['end_line'] + 1):
                            length = len(''.join(grant['lines'][str(i)].split()))
                            if cursor < offset + len(q) and cursor + length > offset: indices.append(i)
                            cursor += length
                        if indices: found = [indices[0], indices[-1]]; break
                    if found is None: raise ValueError('quote outside attributed read window')
                    structured = dict(entity=candidate.entity, attribute=candidate.metric,
                        period=candidate.period, value=candidate.value, unit=candidate.unit or '',
                        value_type=candidate.value_type,
                        quote=item['quote'], source_loc=grant['source_loc'],
                        start_line=found[0], end_line=found[1], grant_id=grant['grant_id'],
                        file_hash=grant['file_hash'], mapped_fingerprint=grant['mapped_fingerprint'],
                        original_name=grant['original_name'], field_evidence=proofs)
                    # Structural idempotency only: a byte-identical canonical
                    # candidate is the same delivery, never a second fact, and
                    # never new progress. This is not backend semantic dedup.
                    identity = json.dumps({k: structured[k] for k in (
                        'entity', 'attribute', 'period', 'value', 'unit', 'value_type',
                        'source_loc', 'start_line', 'end_line', 'grant_id')},
                        ensure_ascii=False, sort_keys=True)
                    if identity in known_keys:
                        duplicates.append(candidate_index)
                        continue
                    known_keys.add(identity)
                    prepared['candidate_keys'].append(identity)
                    accepted_item = dict(item)
                    # V4 defers taxonomy selection until publish assigns a
                    # single top1 leaf. A null from an in-flight older worker
                    # is inert metadata; do not carry even that field forward.
                    accepted_item.pop('module_key', None)
                    accepted.append(accepted_item)
                    prepared['candidates'].append(structured)
                except (ValueError, KeyError, TypeError, OverflowError) as exc:
                    rejected.append(dict(source=dict(file_hash=self.dossier.file_hash, text=None, span=grant['source_loc']),
                        reason='invalid_candidate', blocking=str(exc), candidate_index=candidate_index, candidate=item))
            prepared['accepted'].extend(accepted)
            prepared['feedback'] = rejected
            prepared['rejected'].extend(rejected)
            prepared['complete'] = response['complete'] and not (response['facts'] and not accepted)
            # A title-only window explicitly closed with facts=[]/complete=true is
            # progress. Otherwise two consecutive deliveries that add no valid
            # candidate and do not advance coverage stop the window partially.
            if accepted or response['complete']:
                prepared['extract_no_progress_streak'] = 0
                record.pop('extract_no_progress_drafts', None)
                record.pop('extract_no_progress_feedback', None)
            else:
                prepared['extract_no_progress_streak'] += 1
                drafts = record.get('extract_no_progress_drafts', [])
                drafts.append(dict(facts=deepcopy(response.get('facts')), rejected_indices=[r['candidate_index'] for r in rejected],
                    duplicate_indices=duplicates))
                record['extract_no_progress_drafts'] = drafts[-2:]
                feedback = [dict(candidate_index=r['candidate_index'], blocking=r['blocking'][:1000]) for r in rejected[:32]]
                if duplicates and not rejected:
                    feedback.append(dict(candidate_index=None,
                        blocking=f'candidate indices {duplicates} are structurally identical to already accepted '
                                   'deliveries; resending them is idempotent, not new progress; correct the rejected '
                                   'literal/field, request_context, or close a genuinely fact-free window with complete=true'))
                if not response['facts']:
                    feedback.append(dict(candidate_index=None,
                        blocking='delivery contained zero candidate facts and did not close the window; '
                                   'resend verbatim field/unit literals, request_context for an earlier read grant, '
                                   'or send complete=true only for a genuinely fact-free title window'))
                record['extract_no_progress_feedback'] = feedback
                save()
                if prepared['extract_no_progress_streak'] >= 2:
                    record['extract_progress_blocked'] = True
                    record['extract_block_reason'] = (
                        'two consecutive extract deliveries without a new valid candidate or coverage advance; '
                        'feedback names the rejected field/type/literal; explicit resume_unresolved required')
                    save()
                    raise ComparisonStopped(record['extract_block_reason'])
            save()
        # Stable per-window candidate indices for the note receipt.
        for index, structured in enumerate(prepared['candidates']):
            structured.setdefault('candidate_index', index)

    def _run_mapped_notes(self, state, company_id, job_id, limits, note_review=True):
        # Protocol V4: full-file reading + notebook (stage A) then the
        # quote-bounded post-read note review (stage B). No leaf publisher, no L2
        # consolidation, no rollup consumption is reachable from either stage.
        from .notes import NoteStore, MappedNotesService, NoteRejected as V4NoteRejected
        import time

        def save():
            with connect(self.db_path) as conn:
                self._save(conn, state)

        def call(action, payload):
            from .contracts import response_contract_v4
            payload = {**payload, 'response_contract': response_contract_v4(action)}
            from ..facts_tree.reading_context import MAX_READING_REQUEST_BYTES
            if len(json.dumps({'action': action, **payload}, ensure_ascii=False).encode()) > MAX_READING_REQUEST_BYTES:
                state['reading_limit'] = dict(action=action, reason='bounded reading request exceeds byte limit; nothing truncated or transmitted')
                raise BudgetStopped('mapped MD request byte limit')
            state.pop('reading_limit', None)
            progress = state['mapped_notes']  # created by MappedNotesService before first call
            progress.setdefault('deadline_at', time.time() + 1800)
            if time.time() >= progress['deadline_at']:
                raise BudgetStopped('mapped MD deadline')
            return self._call(action, payload, state, limits)

        with connect(self.db_path) as conn:
            row = conn.execute('SELECT original_name FROM parse_jobs '
                'WHERE company_id=? AND job_id=? AND file_hash=?',
                (company_id, job_id, self.dossier.file_hash)).fetchone()
            has_binding = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_tree_meta'").fetchone()
            binding = conn.execute('SELECT display_name FROM company_tree_meta WHERE company_id=?',
                                   (company_id,)).fetchone() if has_binding else None
        if row is None:
            raise ValueError('job source is outside host scope')
        display_name = binding['display_name'] if binding is not None else None
        notes = NoteStore(self.db_path, company_id=company_id, job_id=job_id,
            file_hash=self.dossier.file_hash, mapped_fingerprint=self.dossier.fingerprint,
            fence=getattr(self, '_lease', None))
        try:
            def review_apply(*, note, decision, expected_round, request_sha, grants):
                # Host-side verification boundary: the employee decision is never
                # trusted for scope/hash/fingerprint/coordinates. revise_fact runs
                # the same attribution-range + verify_candidate guards as reading,
                # rebuilt from the durable origin window; the ledger re-runs the
                # literal field guards inside one atomic transaction.
                action = decision['action']
                fact = proofs = None
                relations = decision.get('relations')
                if action == 'revise_fact':
                    origin_index = int(note['window_id'])
                    record = state['mapped_notes']['records'][origin_index]
                    preparation = record.get('preparation') or {}
                    primary = note['payload']['evidence']['primary']
                    if primary['grant_id'] != record['grant']['grant_id']:
                        raise V4NoteRejected('note origin grant differs from the persisted window')
                    window_state = dict(protocol=4, company_id=company_id,
                        remaining_source_locs=[primary['source_loc']],
                        ranges=preparation.get('ranges') or [])
                    item = {**decision['fact'], 'quote': primary['quote'],
                            'source_loc': primary['source_loc']}
                    try:
                        candidate = self._verify(item, window_state)
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        raise V4NoteRejected(str(exc)[:1000]) from exc
                    if candidate is None:
                        raise V4NoteRejected('quote is not present at the persisted origin coordinate')
                    fact = dict(entity=candidate.entity, attribute=candidate.metric,
                        period=candidate.period, value=candidate.value,
                        unit=candidate.unit or '', value_type=candidate.value_type)
                    proofs = decision.get('field_evidence')
                return notes.apply_review(note_id=note['payload']['note_id'], action=action,
                    reason=decision['reason'], expected_round=expected_round,
                    request_sha=request_sha, grants=grants, fact=fact, proofs=proofs,
                    relations=relations)

            reader = MappedNotesService(self.db_path, self.dossier, call=call, save=save, notes=notes,
                original_name=row['original_name'], display_name=display_name,
                fence=getattr(self, '_lease', None),
                correction_attempts=getattr(self, '_correction_attempts', 2),
                review_enabled=note_review, review_apply=review_apply,
                prepare_window=lambda grant, record, company_context, **kwargs:
                    self._prepare_notes_window(state, job_id, grant, record, company_context, call, save, **kwargs))
            reader.run(company=company_id, job_id=job_id, state=state)
        finally:
            notes.close()

    def _run_mapped_publish(self, state, company_id, job_id, limits):
        # Protocol V4 stage C: post-review note publication. One SQLite
        # connection, one short transaction per leaf; every model turn happens
        # outside any transaction; replay is driven by durable receipts.
        from ..facts_tree.notes_publish import NotesPublishService
        import time

        def save():
            with connect(self.db_path) as conn:
                self._save(conn, state)

        def call(action, payload):
            from .contracts import response_contract_v4
            payload = {**payload, 'response_contract': response_contract_v4(action)}
            from ..facts_tree.reading_context import MAX_READING_REQUEST_BYTES
            if len(json.dumps({'action': action, **payload}, ensure_ascii=False).encode()) > MAX_READING_REQUEST_BYTES:
                state['reading_limit'] = dict(action=action, reason='bounded reading request exceeds byte limit; nothing truncated or transmitted')
                raise BudgetStopped('mapped MD request byte limit')
            state.pop('reading_limit', None)
            progress = state['mapped_notes']
            progress.setdefault('deadline_at', time.time() + 1800)
            if time.time() >= progress['deadline_at']:
                raise BudgetStopped('mapped MD deadline')
            return self._call(action, payload, state, limits)

        with connect(self.db_path) as conn:
            row = conn.execute('SELECT original_name FROM parse_jobs '
                'WHERE company_id=? AND job_id=? AND file_hash=?',
                (company_id, job_id, self.dossier.file_hash)).fetchone()
            has_binding = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_tree_meta'").fetchone()
            binding = conn.execute('SELECT display_name FROM company_tree_meta WHERE company_id=?',
                                   (company_id,)).fetchone() if has_binding else None
        if row is None:
            raise ValueError('job source is outside host scope')
        display_name = binding['display_name'] if binding is not None else None
        reader = NotesPublishService(self.db_path, self.memory, self.dossier,
            call=call, save=save, original_name=row['original_name'], display_name=display_name,
            fence=getattr(self, '_lease', None),
            compare_retries=getattr(self, '_compare_retries', 3),
            correction_attempts=getattr(self, '_correction_attempts', 2))
        reader.run(company=company_id, job_id=job_id, state=state)

    def _maybe_run_publish(self, state, company_id, job_id, limits):
        """Enter V4 stage C only with an explicit publish budget.

        Returns True when publication is complete (ran now or earlier). Without
        a configured publish phase a V4 run still completes at stage B, so
        existing V1 reading-notes callers keep their frozen behavior.
        """
        if not self._notes_protocol_selected():
            return False
        progress = state.get('mapped_notes')
        # The publication gate is review_complete AND zero open notes. Do not
        # trust the awaiting_publication flag here: the reading stage rewrites
        # it on every run (including durable replays), while open_notes is
        # recomputed from the durable note ledger by the same run.
        if not isinstance(progress, dict) or not progress.get('review_complete') \
                or progress.get('open_notes', 1) != 0:
            return False
        publication = progress.get('publication')
        if isinstance(publication, dict) and publication.get('complete'):
            return True
        if 'publish' not in limits:
            return False
        self._run_mapped_publish(state, company_id, job_id, limits)
        publication = progress.get('publication')
        if not isinstance(publication, dict) or not publication.get('complete'):
            from ..facts_tree.md_reader import ComparisonStopped
            raise ComparisonStopped('publication incomplete; explicit resume_unresolved retries unresolved leaves')
        return True

    def _run_leaves(self, state, company_id, job_id, limits):
        if state.get('leaves_done'):
            return
        # Do not import or construct the opt-in publisher on the closed path.
        from ..facts_tree.leaf_plan import LeafPlanningService
        planner = LeafPlanningService(self.db_path, self.memory,
            worker=_LeafWorker(self, state, limits),
            documents={self.dossier.file_hash: self.dossier.document})

        def save():
            with connect(self.db_path) as conn:
                self._save(conn, state)

        try:
            if 'leaves_planned' not in state:
                planned, diag = planner.plan(company_id, job_id)
                state['leaves_planned'] = planned
                state['leaves_diagnostics'] = diag
                state['leaves_built'] = []
                state['leaves_failed'] = []
                state['leaves_empty'] = []
                save()
            state.setdefault('leaves_empty', [])
            processed = {entry['leaf_code'] for entry in state['leaves_built'] + state['leaves_failed'] + state['leaves_empty']}
            for code in state['leaves_planned']:
                if code in processed:
                    continue
                entry = planner.build_one(company_id, code, job_id)
                if entry is None:
                    state['leaves_failed'].append(planner.failed_leaves[-1])
                elif entry.get('empty') is True:
                    state['leaves_empty'].append(entry)
                else:
                    state['leaves_built'].append(entry)
                save()  # Checkpoint each terminal leaf before the next charged call.
            # Codes came from the validated plan; this is a structural trunk field,
            # not a classification of document content.
            trunks = [entry['leaf_code'].split('-')[1] for entry in state['leaves_built']]
            try:
                state['leaves_rollup'] = planner.finalize(company_id, trunks)
            except Exception as exc:
                if hasattr(exc, 'planning_result'):
                    state['leaves_rollup'] = exc.planning_result
                raise
            state['leaves_done'] = True
            save()
        finally:
            planner.close()

    @staticmethod
    def _checked_summary(action, items, request, state):
        """Quarantine one malformed/source-invalid reading item, not its siblings.

        Reuses the unchanged source guard. Never rewrites conclusions, source
        IDs, numbers or units to make an invalid employee item pass.
        """
        if action == 'brief':
            by_id = {str(f['id']):f for f in request['facts']}
            refs_key, kind = 'fact_ids', 'f'
        else:
            by_id = {i['entry_id']:i['verified_numbers'] for i in request['entries']}
            refs_key, kind = 'l1_entry_ids', 'l'
        accepted = []
        for item in items:
            errors = []
            if not isinstance(item, dict):
                errors = ['item must be an object']
            else:
                refs = item.get(refs_key)
                conclusion = item.get('conclusion')
                if (not isinstance(refs, list) or not refs
                        or any((type(ref) is not int if kind == 'f' else not isinstance(ref, str))
                               or str(ref) not in by_id for ref in refs)
                        or not isinstance(conclusion, str) or not conclusion.strip()):
                    errors = ['invalid source reference or conclusion']
                else:
                    errors = validate(conclusion, {str(ref):by_id[str(ref)] for ref in refs}, kind=kind)[1]
                    if kind == 'f':
                        topic = item.get('topic')
                        if not isinstance(topic, str) or not topic.strip():
                            errors.append('invalid topic')
                        else:
                            errors.extend(validate(topic, {}, kind='f')[1])
                    elif len(conclusion.splitlines()) != 1:
                        errors.append('guide must be one line')
            if errors:
                state.setdefault('summary_unresolved', []).append({'action':action, 'item':item, 'errors':errors})
            else:
                accepted.append(item)
        return accepted

    def _finish(self, state, job_id, status, message):
        state['status'] = status
        with connect(self.db_path) as conn:
            self._save(conn, state)
            conn.execute('UPDATE parse_jobs SET status=?,stage=?,message=? WHERE job_id=?',
                         (status, status, message, job_id))

    def _verify(self, item, state):
        if isinstance(item, dict):
            item = dict(item)
            # JSON scalar serialization only: no units, periods or meaning inferred.
            if type(item.get('value')) in (int, float) and math.isfinite(item['value']):
                item['value'] = str(item['value'])
            if type(item.get('period')) is int:
                item['period'] = str(item['period'])
        required = ('entity','attribute','period','value','unit','quote','source_loc')
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in required):
            raise ValueError('fact contract requires explicit strings and coordinates')
        if any(not item[k].strip() for k in required if k not in ('unit', 'period')):
            raise ValueError('empty fact field')
        if not math.isfinite(float(item.get('confidence', 1.0))):
            raise ValueError('confidence must be finite')
        if settings.SAM_FACTS_TREE and item.get('module_key') is not None:
            if state.get('protocol') == 4:
                raise ValueError('V4 extract cannot select module_key; choose one top1 leaf during publish')
            from ..module_tree import MODULE_KEYS
            if not isinstance(item['module_key'], str) or item['module_key'] not in MODULE_KEYS:
                raise ValueError('invalid module_key: expected a module code or null')
        pending = state.get('remaining_source_locs')
        if pending is not None and item['source_loc'] not in pending:
            raise ValueError('fact outside employee pending coordinates')
        self.dossier.read(item['source_loc'])
        elements = [e for e in self.dossier.elements if e['source_loc'] == item['source_loc']]
        index = item.get('attribution_index')
        if index is None:
            ranges = [r for r in state['ranges'] if r['entity'] == item['entity']]
        elif type(index) is int and 0 <= index < len(state['ranges']):
            ranges = [state['ranges'][index]]
        else:
            raise ValueError('invalid employee attribution index')
        windows = []
        for r in ranges:
            for e in elements:
                start, end = max(r['start_line'], e['start_line']), min(r['end_line'], e['end_line'])
                if start <= end:
                    windows.append(self.dossier.range(start, end))
        if not windows:
            raise ValueError('fact lies outside employee attributed range')
        value_type = item.get('value_type', 'string')
        if not isinstance(value_type, str) or not value_type.strip():
            raise ValueError('invalid optional value type')
        return verify_candidate({**item, 'value_type':value_type, 'metric':item['attribute']}, [{
            'content':content, 'subject':item['entity'], 'file_hash':self.dossier.file_hash,
            'source_span':json.dumps({'source_loc':item['source_loc'], 'quote':item['quote']}, ensure_ascii=False)}
            for content in windows], company_id=state['company_id'])
