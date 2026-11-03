"""E3a: offline, read-only approval history and Git/W reconciliation.

This report grants no rights and performs no recovery decision, merge, repair,
phase transition or credential authentication. The trusted host supplies C's
already-installed bare, C's admission evidence, a pinned current R5 anchor and
D's physically independent handoff. Old W cannot manufacture that context.

Current ledger connections have a reserved input slot only: E3a does NOT verify
independence/currentness and never upgrades any input to current non-consumption.
B is a pending candidate on declared, complete backup evidence; old packages
retain the independent-current route but never derive B from a missing table.

All Git execution goes through the existing repository's read-only methods.
The sole direct repository command is a literal, no-includes local-config READ;
no process API, configurable command, original-package import or projection is
introduced. Immutable evidence retains raw row/record/commit digest coordinates;
portable D evidence includes every existing IndependentEvidence field and key.
"""
from dataclasses import dataclass
import hashlib
import os

from app.facts_tree import git_format
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.git_append_only import validate_append_only_retention
from app.facts_tree.git_repository import FactsGitRepository, _VERIFY_ENV
from app.facts_tree.operation_acceptance import locate_operation_acceptance
from app.facts_tree.record_tree import build_record_tree
from app.facts_tree.recovery_epoch import (
    EvidenceKey, EvidenceState, IndependentEvidence, RecoveryPhase,
)
from app.facts_tree.recovery_materializer import AdmissionEvidence, barrier_digest, _package_proof, _proof
from app.facts_tree.recovery_reauthorize import (
    CleanReport, ReauthorizationReceipt, _PROOF_KIND_EVIDENCE, _PROOF_KIND_REAUTH,
    _assert_roots_isolated, _evaluate_credentials, _evidence_payload, _load_plan,
    _pin_root, _read_completed_receipt, _reauth_payload, _receipt_document,
    _reports_from_view, _valid_anchor,
)
from app.facts_tree.recovery_repository import _assert_local_config_clean, _assert_no_offline_bypasses
from app.facts_tree.recovery_state_store import RecoveryStateStore
from app.facts_tree.recovery_verifier import (
    _approval_id, _file, _hash, _json, _root, _sqlite,
    verify_recovery_package,
)

_RESTRICTIVE = frozenset({'reserved','consumed','revoked','policy_restricted','unknown'})
_FAMILIES = frozenset({'operation','event','placement_revision','source_lifecycle'})


class ApprovalAuditError(ValueError):
    """Fixed code; no confidential input or dependency exception chain."""


@dataclass(frozen=True)
class ApprovalOrigin:
    kind: str
    digest: str
    role: str = ''
    table: str = ''
    record_path: str = ''
    commit_oid: str = ''
    row_key: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalMarker:
    category: str
    key: tuple[str, str, str] | None
    states: tuple[str, ...] = ()
    origins: tuple[ApprovalOrigin, ...] = ()
    reasons: tuple[str, ...] = ()
    requires_independent_current: bool = False


@dataclass(frozen=True)
class ApprovalAuditReport:
    case_id: str
    core_digest: str
    handoff_digest: str
    journal_anchor: tuple[int, str]
    head_oid: str
    commit_oid: str
    declared: bool
    markers: tuple[ApprovalMarker, ...]
    evidence_inputs: tuple[IndependentEvidence, ...]
    original_clean_claim: tuple
    prior_receipt_digest: str | None
    blockers: tuple[str, ...]
    current_evidence: str = 'not_proven'
    current_ledger_route_available: bool = True
    package_evidence: tuple = ()

    def to_handoff_items(self):
        """Fresh plain D item payloads with their shared EvidenceKey.

        The caller may serialize these with D's existing physical bundle
        machinery later. This adapter writes no item, receipt or journal row.
        These restrictive history observations never assert current UNUSED.
        """
        return tuple({'key': {'task_id': item.key.task_id, 'approval_id': item.key.approval_id,
                              'operation_id': item.key.operation_id},
            'evidence': {'source_id':item.source_id, 'scope':item.scope, 'ledger_id':item.ledger_id,
                'revocation_seq':item.revocation_seq, 'event_id':item.event_id,
                'event_digest':item.event_digest, 'state':item.state.value,
                'session_id':item.session_id, 'policy_revision':item.policy_revision}}
            for item in self.evidence_inputs)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _key(record):
    value = tuple(record.get(k) for k in ('task_id','approval_id','operation_id'))
    try:
        for item in value:
            _approval_id(item)
    except Exception:
        return None
    return value


class _JournalReader:
    # Reuse R5's COMPLETE schema/row/chain/watermark/epoch replay without its
    # constructor (which can create a file and execute DDL). No write method
    # exists on this reader, and its connection is kernel/SQLite read-only.
    replay = RecoveryStateStore._replay

    def __init__(self, db):
        self._db = db


def _read_context(state_fd, case_id, anchor):
    if not _valid_anchor(anchor):
        raise ApprovalAuditError('approval_case_context_invalid')
    for suffix in ('-wal','-journal'):
        try:
            fd = _file(state_fd, 'recovery-state.sqlite3'+suffix)
        except FileNotFoundError:
            continue
        else:
            os.close(fd)
            raise ApprovalAuditError('approval_case_context_invalid')
    fd = _file(state_fd, 'recovery-state.sqlite3')
    try:
        before = _hash(fd)
        db = _sqlite(fd)
        try:
            states, actual = _JournalReader(db).replay()
        finally:
            db.close()
        if actual != anchor or _hash(fd) != before:
            raise ApprovalAuditError('approval_case_context_invalid')
        view = states[case_id]
        if not view.is_current_epoch:
            raise ApprovalAuditError('approval_case_context_invalid')
        return view
    finally:
        os.close(fd)


def _verify_receipt(plan, view, receipt, receipt_fd, anchor):
    if type(receipt) is not ReauthorizationReceipt or receipt.case_id != plan.case_id:
        raise ApprovalAuditError('approval_receipt_invalid')
    if (receipt.phase != RecoveryPhase.REAUTHORIZED.value
            or receipt.clean != tuple(CleanReport(b.key,b.backup,b.claim_digest) for b in plan.clean_blocks)
            or view.phase != RecoveryPhase.REAUTHORIZED or view.authentication != plan.authentication
            or receipt.manifest_digest != plan.manifest_digest or receipt.journal_anchor_after != anchor):
        raise ApprovalAuditError('approval_receipt_invalid')
    reports = _reports_from_view(plan, view)
    clean = tuple(CleanReport(b.key,b.backup,b.claim_digest) for b in plan.clean_blocks)
    credentials = _evaluate_credentials(plan, view)
    first = _proof(_PROOF_KIND_EVIDENCE, _evidence_payload(plan,reports))
    second = _proof(_PROOF_KIND_REAUTH, _reauth_payload(plan,first,credentials))
    proofs = (('evidence_merged',first),('current_authorization',second))
    if (receipt.proofs != proofs or receipt.merges != reports or receipt.credentials != credentials
            or any(dict(view.proofs).get(k) != v for k,v in proofs)):
        raise ApprovalAuditError('approval_receipt_invalid')
    def rebuild(before, after):
        return _receipt_document(plan,view,reports,clean,credentials,proofs,before,after)
    def checked_rebuild(before,after):
        if before != receipt.journal_anchor_before:
            raise ApprovalAuditError('approval_receipt_invalid')
        return rebuild(before,after)
    raw = _read_completed_receipt(receipt_fd,os.path.basename(receipt.receipt_path),checked_rebuild,anchor)
    if 'sha256:'+_sha(raw) != receipt.receipt_sha256:
        raise ApprovalAuditError('approval_receipt_invalid')
    return receipt.receipt_sha256


def _offline_repository(repository, bare_fd):
    # These controlled repository methods never invoke an arbitrary command.
    # --no-includes is crucial: reject an include without following its path.
    _assert_no_offline_bypasses(bare_fd)
    text = repository._git_text(['config','--local','--no-includes','--list'], env_extra={
        **_VERIFY_ENV, 'GIT_CONFIG_COUNT':'2', 'GIT_CONFIG_KEY_0':'core.hooksPath',
        'GIT_CONFIG_VALUE_0':'/dev/null', 'GIT_CONFIG_KEY_1':'protocol.allow',
        'GIT_CONFIG_VALUE_1':'never'})
    _assert_local_config_clean(text)
    # C emits only these inert local keys. Fail closed on executable helpers,
    # aliases and future configuration instead of expanding execution ability.
    allowed = {'core.repositoryformatversion','core.filemode','core.bare',
               'core.fsync','core.fsyncmethod','extensions.objectformat'}
    if any(line.partition('=')[0].strip().lower() not in allowed for line in text.splitlines()):
        raise ApprovalAuditError('approval_git_configuration_invalid')


def _git_refs(repository, bare_fd, head, identity, blockers):
    """Return (all four-family appearances, acceptance-proven references).

    Full physical snapshots, business closure, immutable retention and new-op
    parent binding are verified on the WHOLE genesis..C interval. The shared
    locator proves first acceptance; a dangling commit is never a result.
    """
    appearances, proven = [], []
    try:
        _offline_repository(repository,bare_fd)
        repository.verify_integrity(expected_head=head)
        snapshot = repository.read_snapshot_at(head)
        if snapshot.head_oid != head:
            raise ApprovalAuditError('approval_git_unavailable')
        raw = {r.path:git_format.loads_record(r.content) for r in snapshot.records}
        for path, payload in sorted(raw.items()):
            if git_format.canonical_record_type(path) not in _FAMILIES:
                continue
            refs = payload.get('approval_use_refs',[])
            if type(refs) is list:
                for ref in refs:
                    appearances.append((path,ref,_sha(git_format.dumps_record(payload))))
        previous, first = {}, {}
        for commit in repository.list_commit_chain(repository.zero_oid,head):
            snap = repository.read_snapshot_at(commit)
            if snap.head_oid != head:
                raise ApprovalAuditError('approval_git_unavailable')
            tree = build_record_tree(snap.records, repo_id=identity['repo_id'], company_id=identity['company_id'],
                commit_oid=commit,head_oid=head,object_format=repository.object_format)
            current = {r.path:r.content for r in snap.records}
            validate_append_only_retention(
                ((p,_sha(v)) for p,v in previous.items()), ((p,_sha(v)) for p,v in current.items()))
            parents = repository._commit_parents(commit,_VERIFY_ENV)
            for path in tree.families.get('operation',()):
                if path not in previous:
                    op = tree.ordered[path]
                    if op['base_commit'] != (parents[0] if parents else repository.zero_oid):
                        raise ApprovalAuditError('approval_git_structure_invalid')
                    first[op['operation_id']] = commit
            previous = current
        origins = {}
        for path, ref, digest in appearances:
            if type(ref) is not dict:
                raise ApprovalAuditError('approval_git_structure_invalid')
            op = ref['operation_id']
            if op not in origins:
                origin = locate_operation_acceptance(repository,operation_id=op,commit_oid=head)
                if (origin.head_oid != head or origin.accepted_commit_oid != first.get(op)
                        or origin.accepted_commit_oid is None):
                    raise ApprovalAuditError('approval_git_structure_invalid')
                origins[op] = origin.accepted_commit_oid
            proven.append((path,ref,digest,origins[op]))
        if repository.head_oid() != head:
            raise ApprovalAuditError('approval_git_unavailable')
    except Exception:
        # Whatever was already visible is merely C; no partial proof becomes A2.
        blockers.add('approval_git_unverified')
        proven = []
    return appearances, proven


def audit_recovery_approvals(*, package_dir, repository, handoff_dir, state_root,
        case_id, trusted_anchor, expected_identity, material_evidence,
        prior_receipt=None, original_clean_claim=None, independent_ledger_connection=None):
    """Read-only E3a report. The connection slot is UNVERIFIED, including None.

    D context failures raise one fixed error without dependency chains. W/Git
    failures return blockers WITH independently established A retained. No
    missing old source is reinterpreted as UNUSED or used to erase old A.
    """
    result, failure = None, None
    try:
        result = _audit(package_dir,repository,handoff_dir,state_root,case_id,trusted_anchor,
                        expected_identity,material_evidence,prior_receipt,original_clean_claim,
                        independent_ledger_connection)
    except ApprovalAuditError as exc:
        failure = str(exc)
    except Exception:
        failure = 'approval_audit_context_invalid'
    if failure is not None:
        raise ApprovalAuditError(failure)
    return result


def _audit(package_dir,repository,handoff_dir,state_root,case_id,anchor,identity,material,
           receipt,clean_claim,current_connection):
    if (type(repository) is not FactsGitRepository or type(material) is not AdmissionEvidence
            or type(identity) is not dict or set(identity) != {'repo_id','installation_id','company_id'}
            or repository._expected_meta != dict(identity,schema_version=1,format_version=1)):
        raise ApprovalAuditError('approval_audit_input_invalid')
    roots = []
    try:
        for kind,path in (('state',state_root),('handoff',handoff_dir),('bare',repository.path)):
            fd,_ = _pin_root(path,kind)
            roots.append((kind,fd))
        state_fd,input_fd,bare_fd = (fd for _,fd in roots)
        if receipt is not None:
            if type(receipt) is not ReauthorizationReceipt:
                raise ApprovalAuditError('approval_receipt_invalid')
            receipt_fd,_ = _pin_root(os.path.dirname(receipt.receipt_path),'receipt')
            roots.append(('receipt',receipt_fd))
        try:
            _assert_roots_isolated(tuple(roots))
            plan = _load_plan(input_fd)
        except Exception:
            raise ApprovalAuditError('approval_handoff_invalid')
        view = _read_context(state_fd,case_id,anchor)
        if (plan.case_id != case_id or view.backup_id != plan.backup_id or view.barrier_digest != plan.barrier_digest
                or not plan.authentication.matches(view.epoch)
                or (view.authentication is not None and view.authentication != plan.authentication)):
            raise ApprovalAuditError('approval_case_binding_mismatch')
        clean = tuple((b.key.task_id,b.key.approval_id,b.key.operation_id,
                       tuple(s.value for s in b.backup),b.claim_digest) for b in plan.clean_blocks)
        if clean_claim is not None and clean_claim != clean:
            raise ApprovalAuditError('approval_original_claim_mismatch')
        receipt_digest = None
        if receipt is not None:
            receipt_digest = _verify_receipt(plan,view,receipt,receipt_fd,anchor)
        blockers = set()
        if receipt is None and view.phase == RecoveryPhase.REAUTHORIZED:
            blockers.add('approval_prior_receipt_missing')
        records, audit, core, package_evidence = [], {}, {}, ()
        try:
            package_fd = _root(package_dir)
        except Exception:
            blockers.add('approval_package_unavailable')
        else:
            roots.append(('package',package_fd))
            try:
                _assert_roots_isolated(tuple(roots))
                manifest_fd = _file(package_fd,'manifest.json')
                try:
                    core = _json(manifest_fd)['core']
                finally:
                    os.close(manifest_fd)
                verdict = verify_recovery_package(package_dir,expected_identity={
                    **identity,'scope':{'kind':'company','installation_id':identity['installation_id'],
                                       'company_id':identity['company_id']}})
                # Existing packages carry the same identity with scope. A
                # bound package may also use the older three-ID spelling.
                if verdict.identity == identity:
                    verdict = verify_recovery_package(package_dir,expected_identity=identity)
                package_evidence = verdict.approval_audit
                audit = dict(package_evidence)
                if not verdict.recoverable:
                    blockers.add('approval_package_invalid')
                records = [{**dict(row),'record':dict(dict(row)['record'])} for row in audit.get('records',())]
            except Exception:
                blockers.add('approval_package_unavailable')
                audit,records = {},[]
        bound = False
        try:
            if (material.verdict != 'admitted' or material.core_digest != audit['core_digest']
                    or compute_core_digest(core) != audit['core_digest']
                    or core['backup_id'] != view.backup_id or barrier_digest(core['barrier_id']) != view.barrier_digest
                    or dict(view.proofs).get('package_verified') != _package_proof(case_id,material.core_digest,material,
                        core['identity'],core)):
                raise ValueError('binding')
            bound = True
        except Exception:
            blockers.add('approval_material_binding_invalid')
        if not bound:
            records,package_evidence = [],()
        by_category = {k:{} for k in 'ABCD'}
        evidence = []

        def mark(category,key,origin,reason='',states=(),revision=None):
            entry = by_category[category].setdefault(key,{'states':set(),'origins':set(),'reasons':set()})
            entry['states'].update(states)
            if origin is not None: entry['origins'].add(origin)
            if reason: entry['reasons'].add(reason)
            if category != 'A': return
            if key is None:
                blockers.add('approval_evidence_key_unrepresentable')
                return
            try:
                dkey = EvidenceKey(*key)
                for state in states:
                    mapped = 'policy_restricted' if state=='denied' else state
                    if mapped not in _RESTRICTIVE:
                        continue
                    stamp = _sha(git_format.dumps_record({'key':list(key),'state':mapped,
                        'kind':origin.kind,'digest':origin.digest,'role':origin.role,'table':origin.table,
                        'record_path':origin.record_path,'commit_oid':origin.commit_oid,'row_key':list(origin.row_key)}))
                    evidence.append(IndependentEvidence('recovery-history','approval-history','e3a-'+stamp,0,
                        'event-'+stamp,stamp,dkey,EvidenceState(mapped),
                        policy_revision=revision if mapped=='policy_restricted' else None))
            except Exception:
                blockers.add('approval_evidence_key_unrepresentable')

        # R5 and independent D items are retained even when W/Git later fail.
        for merge in view.merges:
            key = (merge.key.task_id,merge.key.approval_id,merge.key.operation_id)
            origin = ApprovalOrigin('R5',anchor[1],row_key=key)
            revision = next((e.policy_revision for e in merge.evidence if e.policy_revision is not None),None)
            mark('A',key,origin,states=tuple(s.value for s in merge.restrictions),revision=revision)
            evidence.extend(e for e in merge.evidence if e.state.value in _RESTRICTIVE)
        for block in plan.merge_blocks:
            key = (block.key.task_id,block.key.approval_id,block.key.operation_id)
            backed = tuple(s.value for s in block.backup if s.value in _RESTRICTIVE)
            if backed:
                mark('A',key,ApprovalOrigin('D',plan.manifest_digest,record_path='handoff.json',row_key=key),
                     states=backed)
            for item, ref in zip(block.evidence,block.items):
                if item.state.value in _RESTRICTIVE:
                    origin = ApprovalOrigin('D',ref.sha256,record_path='items/'+ref.name,row_key=key)
                    mark('A',key,origin,states=(item.state.value,),revision=item.policy_revision)
                    evidence.append(item)
        by_table = {name:{} for name in ('host_prepared_effects','host_approval_decisions',
                                         'host_approval_reservations','host_approval_consumptions')}
        for row in records:
            by_table[row['table']][row['record']['operation_id']] = row
        for row in records:
            record = row['record']; key = _key(record)
            if key is None:
                linked = by_table['host_approval_decisions'].get(record['operation_id'])
                if linked is not None:
                    key = _key(linked['record'])
            origin = ApprovalOrigin('W',row['row_sha256'],row['role'],row['table'],
                                    row_key=tuple(v for v in key or () if v is not None))
            if row['table']=='host_approval_reservations':
                mark('A',key,origin,states=('reserved',))
            elif row['table']=='host_approval_decisions' and record['decision']=='denied':
                mark('A',key,origin,states=('denied',),revision=record['policy_revision'])
            elif row['table']=='host_approval_consumptions' and record['operation_id'] in audit.get('consumption_operations',()):
                mark('A',key,origin,states=('consumed',))
        for frozen in audit.get('restrictive_rows',()) if bound else ():
            restriction = dict(frozen)
            matching = {_key(row['record']) for row in records
                if row['record']['task_id']==restriction['task_id']
                and (restriction['operation_id'] is None or row['record']['operation_id']==restriction['operation_id'])}
            matching.discard(None)
            origin = ApprovalOrigin('W',restriction['row_sha256'],restriction['role'],restriction['table'],
                row_key=tuple(v for v in (restriction['task_id'],restriction['operation_id']) if v is not None))
            for key in sorted(matching) if matching else (None,):
                mark('A',key,origin,states=(restriction['state'],))
        if audit.get('failures'):
            blockers.add('approval_w_evidence_invalid')
        head = core.get('authority',{}).get('commit_oid','') if bound else ''
        if bound:
            appearances, proven = _git_refs(repository,bare_fd,head,identity,blockers)
        else:
            appearances,proven = [],[]
        if not bound or 'approval_git_unverified' in blockers:
            for key in plan.domain:
                mark('C',(key.task_id,key.approval_id,key.operation_id),None,'approval_git_unverified')
        proven_paths = {(path,git_format.dumps_record(ref)) for path,ref,_,_ in proven}
        for path,ref,digest in appearances:
            if (path,git_format.dumps_record(ref)) not in proven_paths:
                key = _key(ref) if type(ref) is dict else None
                mark('C',key,ApprovalOrigin('Git',digest,record_path=path,commit_oid=head),
                     'approval_git_reference_unproven')
                if key is None or not any(_key(row['record'])==key for row in records):
                    mark('D',key,ApprovalOrigin('Git',digest,record_path=path,commit_oid=head),
                         'approval_host_record_missing')
        seen = set()
        w_consumed = by_table['host_approval_consumptions']
        w_links = [row for table in by_table.values() for row in table.values() if 'approval_id' in row['record']]
        for path,ref,digest,accepted in proven:
            key = _key(ref)
            origin = ApprovalOrigin('Git',digest,record_path=path,commit_oid=accepted,row_key=key or ())
            seen.add(key)
            conflict = False
            for row in w_links:
                stored = row['record']
                if (stored['approval_id']==ref['approval_id'] or stored.get('reservation_id')==ref['reservation_id']
                        or stored['operation_id']==ref['operation_id']):
                    for field in ref:
                        if field not in {'schema_version','audit_ref_type'} and field in stored and stored[field]!=ref[field]:
                            conflict = True
                    if stored.get('decision') == 'denied':
                        conflict = True
            prepared = by_table['host_prepared_effects'].get(ref['operation_id'])
            if prepared is not None:
                op_path = git_format.record_path('operation',operation_id=ref['operation_id'])
                snap = repository.read_snapshot_at(accepted)
                op = next(git_format.loads_record(r.content) for r in snap.records if r.path==op_path)
                if (op['base_commit']!=prepared['record']['base_commit']
                        or prepared['record']['object_format']!=repository.object_format):
                    conflict = True
            if conflict:
                blockers.add('approval_git_w_conflict')
                mark('C',key,origin,'approval_git_w_conflict')
                continue
            if ref['operation_id'] not in audit.get('consumption_operations',()):
                mark('A',key,origin,'approval_git_accepted_w_missing',states=('consumed',))
                mark('C',key,origin,'approval_w_consumption_missing')
                mark('D',key,origin,'approval_host_underreported')
        for op,row in w_consumed.items():
            if op not in audit.get('consumption_operations',()): continue
            key = _key(row['record'])
            if key not in seen:
                mark('D',key,ApprovalOrigin('W',row['row_sha256'],row['role'],row['table'],row_key=key or ()),
                     'approval_consumption_without_git')
        for key in by_category['A']:
            if key is not None and any((b.key.task_id,b.key.approval_id,b.key.operation_id)==key for b in plan.clean_blocks):
                mark('D',key,None,'approval_clean_claim_underreported')
            if key is not None and not any((k.task_id,k.approval_id,k.operation_id)==key for k in plan.domain):
                blockers.add('approval_handoff_coverage_missing')
                mark('D',key,None,'approval_handoff_coverage_missing')
        # A declared complete backup can produce only a pending B candidate.
        # Legacy/missing carrier/current connection never proves unused.
        if (audit.get('declared') and not audit.get('failures') and bound
                and not blockers):
            for op,row in by_table['host_approval_decisions'].items():
                record = row['record']; key = _key(record)
                if (record['decision']=='approved' and op in audit.get('chain_operations',())
                        and op not in by_table['host_approval_reservations']
                        and key not in by_category['A'] and key not in by_category['C']):
                    mark('B',key,ApprovalOrigin('W',row['row_sha256'],row['role'],row['table'],row_key=key or ()),
                         'approval_independent_current_required')
        markers = []
        for category, entries in by_category.items():
            for key,data in sorted(entries.items(),key=lambda pair:pair[0] or ('','','')):
                markers.append(ApprovalMarker(category,key,tuple(sorted(data['states'])),
                    tuple(sorted(data['origins'],key=lambda x:(x.kind,x.digest,x.role,x.table,x.record_path,x.commit_oid,x.row_key))),
                    tuple(sorted(data['reasons'])),category=='B'))
        return ApprovalAuditReport(case_id,audit.get('core_digest',material.core_digest),plan.manifest_digest,
            anchor,head,head,bool(audit.get('declared')),tuple(markers),tuple(dict.fromkeys(evidence)),
            clean,receipt_digest,tuple(sorted(blockers)),
            'not_proven' if current_connection is None else 'unverified_connection_or_snapshot',
            package_evidence=package_evidence)
    finally:
        for _,fd in roots:
            os.close(fd)
