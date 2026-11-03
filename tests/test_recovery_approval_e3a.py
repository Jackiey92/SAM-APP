"""E3a: real immutable host W rows, digest-bound packages, read-only recovery."""
import ast
from dataclasses import asdict, fields, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys

import pytest

from app.facts_tree import bindings_current, template_packs, git_format, recovery_verifier as r4
from app.host_approval_consumptions import HostApprovalConsumptionService
from tests.test_approval_use_e2 import stack
from tests.test_host_prepared_effects import at, effect_kwargs, scope_kwargs
from tests.test_facts_tree_backup_assembly import world
from tests.test_facts_tree_recovery_verifier import package, _manifest, _refresh, _seal, _a3_fingerprint

ROLE = 'w_items'
WPATH = 'payload/w/w_items.sqlite'


def host_history(path, identity, base, *, state='consumed', object_format='sha1'):
    db = sqlite3.connect(path)
    host = stack(db)
    host.identity.initialize_installation(installation_id=identity['installation_id'],
        company_id=identity['company_id'], owner_principal_id='owner-e3a', owner_role='owner', now=at(0))
    pairing = host.identity.issue_pairing_credential(now=at(10), lifetime_seconds=600)
    grant = host.identity.pair(pairing.secret, now=at(20), session_lifetime_seconds=3600)
    policy = host.authorization.create_policy(grant.secret, now=at(30), **scope_kwargs())
    cred = host.credentials.issue_task_credential(grant.secret, now=at(40),
        policy_id=policy.policy_id, expected_revision=1)
    effect = host.prepared.prepare_effect(cred.secret, now=at(50),
        **effect_kwargs(base_commit=base, object_format=object_format))
    decision = host.decisions.approve_effect(grant.secret, now=at(60),
        operation_id=effect.operation_id, expected_effect_digest=effect.effect_digest,
        expected_preparation_digest=effect.preparation_digest,
        decision='denied' if state=='denied' else 'approved', expires_at=at(300))
    consumption = HostApprovalConsumptionService(db, host.reservations, initialize=True)
    reservation = None
    if state in {'consumed', 'reserved', 'revoked', 'unknown'}:
        reservation = host.reservations.reserve_approval(cred.secret, now=at(70),
            operation_id=effect.operation_id, approval_id=decision.approval_id,
            expected_effect_digest=effect.effect_digest, expected_preparation_digest=effect.preparation_digest)
    if state == 'consumed':
        consumption.record_admission(cred.secret, now=at(80), operation_id=effect.operation_id,
            reservation_id=reservation.reservation_id, approval_id=decision.approval_id,
            expected_effect_digest=effect.effect_digest, expected_preparation_digest=effect.preparation_digest)
    if state == 'revoked':
        host.credentials.revoke_task_credential(grant.secret, now=at(90), task_id=cred.task_id)
    return dict(db=db, host=host, credential=cred, grant=grant, effect=effect,
                decision=decision, reservation=reservation, consumption=consumption)


def copy_host(package, history, *, declared=True):
    core = _manifest(package)['core']
    path = package/WPATH
    path.unlink()
    target = sqlite3.connect(path)
    history['db'].backup(target)
    core['databases'][0]['schema']['tables'] = [r[0] for r in target.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    target.close()
    if declared:
        core['recovery_capabilities'] = {'approval_consumptions': {'version':1,'roles':[ROLE]}}
    else:
        core.pop('recovery_capabilities', None)
    _refresh(package, core, WPATH)
    return core


@pytest.fixture
def approval_package(package, tmp_path):
    core = _manifest(package)['core']
    history = host_history(tmp_path/'host.sqlite', core['identity'], core['authority']['commit_oid'],
                           object_format=core['authority']['object_format'])
    copy_host(package, history)
    try:
        yield package, history
    finally:
        history['db'].close()


def report(verdict):
    value = dict(verdict.approval_audit)
    value['records'] = [dict(row) for row in value['records']]
    for row in value['records']:
        row['record'] = dict(row['record'])
    return value


def test_r4_declared_real_consumption_chain_and_immutable_compatible_verdict(approval_package):
    package, history = approval_package
    before = _a3_fingerprint(package)
    verdict = r4.verify_recovery_package(package)
    assert verdict.recoverable, asdict(verdict)
    audit = report(verdict)
    assert audit['declared'] and audit['consumption_operations'] == (history['effect'].operation_id,)
    assert audit['failures'] == ()
    assert tuple(f.name for f in fields(verdict)) == (
        'recoverable','package_format','self_contained','backup_id','barrier_id','identity',
        'bundle_head_oid','object_format','checks','blockers','advisories','verified_at','closure_r_list_digest')
    assert set(asdict(verdict)) == {f.name for f in fields(verdict)}
    rebuilt = r4.RecoveryVerdict(**asdict(verdict))
    assert rebuilt.approval_audit == ()  # no statement of non-consumption
    assert isinstance(verdict.approval_audit, tuple)
    with pytest.raises(TypeError):
        verdict.approval_audit[0] = ('forged', True)
    for secret in (history['credential'].secret, history['grant'].secret):
        assert secret not in repr(verdict) + repr(verdict.approval_audit) + json.dumps(asdict(verdict))
    assert _a3_fingerprint(package) == before


@pytest.mark.parametrize('attack', ['missing', 'view', 'column', 'guard', 'replace_reservation',
    'replace_approval', 'replace_operation', 'hash', 'canonical', 'scope', 'orphan', 'binding', 'time', 'schema_bool', 'index', 'duplicate'])
def test_r4_declared_damage_enters_stage_blockers_not_just_advisory(approval_package, attack):
    package, _ = approval_package
    db = sqlite3.connect(package/WPATH)
    table = 'host_approval_consumptions'
    if attack in {'missing','view','column'}:
        db.execute('DROP TABLE '+table)
        if attack == 'view': db.execute('CREATE VIEW '+table+' AS SELECT 1')
        if attack == 'column': db.execute('CREATE TABLE '+table+' (reservation_id TEXT)')
    elif attack in {'guard','replace_reservation','replace_approval','replace_operation'}:
        if attack == 'guard':
            db.execute('DROP TRIGGER host_approval_consumptions_no_update')
        else:
            # A guard protecting only TWO unique keys must never count as v1.
            ignored = attack.split('_',1)[1]+'_id'
            db.execute('DROP TRIGGER host_approval_consumptions_no_replace')
            terms = [k+'=NEW.'+k for k in ('reservation_id','approval_id','operation_id') if k != ignored]
            db.execute('CREATE TRIGGER host_approval_consumptions_no_replace BEFORE INSERT ON '+table+
                ' WHEN EXISTS(SELECT 1 FROM '+table+' WHERE '+' OR '.join(terms)+") BEGIN SELECT RAISE(ABORT,'approval_consumption_append_only'); END")
    elif attack == 'duplicate':
        other = package/'payload/w/w_ledger.sqlite'
        other.unlink()
        dst = sqlite3.connect(other); db.backup(dst); dst.close()
    else:
        # Preserve all original guards after changing actual row bytes.
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='host_approval_consumptions_no_update'").fetchone()[0]
        db.execute('DROP TRIGGER host_approval_consumptions_no_update')
        blob, sha = db.execute('SELECT record_blob,row_sha256 FROM '+table).fetchone()
        payload = git_format.loads_record(blob)
        if attack == 'hash': sha = 'e'*64
        elif attack == 'canonical': blob = json.dumps(payload).encode()
        elif attack == 'scope': payload['company_id'] = 'wrong-company'
        elif attack == 'orphan': payload['operation_id'] = 'orphan-operation'
        elif attack == 'binding': payload['preparation_digest'] = 'f'*64
        elif attack == 'time': payload['created_at'] = '2026-02-30T12:00:00.000000+00:00'
        elif attack == 'schema_bool': payload['schema_version'] = True
        if attack not in {'hash','canonical','duplicate'}:
            blob = git_format.dumps_record(payload)
            sha = hashlib.sha256(b'sam-host-approval-consumption row-v1\0'+blob).hexdigest()
        indexed=[k for k in r4._APPROVAL_COLUMNS[table] if k not in {'record_blob','row_sha256'}]
        values=[payload[k] for k in indexed]
        if attack=='index': values[indexed.index('operation_id')]='wrong-indexed-operation'
        db.execute('UPDATE '+table+' SET '+','.join(k+'=?' for k in indexed)+',record_blob=?,row_sha256=?', (*values,blob,sha))
        db.execute(ddl)
    db.commit(); db.close()
    core = _manifest(package)['core']
    for entry in core['databases']:
        conn=sqlite3.connect(package/'payload/w'/(entry['role']+'.sqlite'))
        entry['schema']['tables']=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        conn.close()
    if attack == 'duplicate':
        core['recovery_capabilities']['approval_consumptions']['roles'].append('w_ledger')
        _refresh(package, core, 'payload/w/w_ledger.sqlite')
        core = _manifest(package)['core']
    _refresh(package, core, WPATH)
    before = _a3_fingerprint(package)
    verdict = r4.verify_recovery_package(package)
    assert not verdict.recoverable, asdict(verdict)
    stage = next(c for c in verdict.checks if c.name=='E.approvals')
    assert not stage.passed and stage.reasons
    assert set(stage.reasons) <= set(verdict.blockers)
    assert all(code.startswith('approval_') for code in stage.reasons)
    if attack == 'duplicate':
        assert 'approval_consumption_duplicate' in stage.reasons
    if attack in {'orphan','binding'}:
        assert 'approval_consumption_chain_invalid' in stage.reasons
    assert _a3_fingerprint(package) == before


def test_no_declaration_is_legacy_and_never_proves_unused(package):
    verdict = r4.verify_recovery_package(package)
    assert verdict.recoverable
    assert not report(verdict)['declared']
    assert report(verdict)['consumption_operations'] == ()
    assert not any(c.name=='E.approvals' for c in verdict.checks)


# -- A real E2 admission exported into an A2 package and installed by slice C.
from copy import deepcopy
from app.facts_tree.backup_manifest import compute_core_digest
from app.facts_tree.broker_coordination import BrokerCoordination
from app.facts_tree.broker_final_gate import AssembledAttempt, BrokerFinalGate, HostApprovalEvidence, approval_artifact_digests
from app.facts_tree.git_repository import FactsGitRepository
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.recovery_approval_audit import ApprovalAuditError, audit_recovery_approvals
from app.facts_tree.recovery_epoch import RecoveryEpoch, EvidenceKey, EvidenceState
from app.facts_tree.recovery_materializer import AdmissionEvidence, barrier_digest, install_materials
from app.facts_tree.recovery_state_store import RecoveryStateStore
from tests.facts_tree_full_fixture import full_tree
from tests.test_facts_tree_record_schema_closure import binding_tree
from tests.test_facts_tree_git_repository import _bc_manifest_record
from tests.test_facts_tree_recovery_materialize import _make_layout, _roles
from tests.test_facts_tree_recovery_reauthorize import _evidence, _write_item, _green_bundle, _key as dkey, _credential, _run_d, _state_view, CASE

TIME = '2026-11-01T12:00:00+08:00'
EFFECT = {'opaque':['effect',1]}
DEPS = {'taxonomy_pack':'generic-v1','plan':'plan-v1'}


def scoped_records(identity, head, operation='operation-1', refs=(), *, all_families=False):
    tree=full_tree()
    if all_families:
        binding,paths,current_path=binding_tree()
        tree={p:v for p,v in tree.items() if v.get('record_type') not in {'operation','event','input_candidate','consumed_candidate'}}
        tree.update(binding)
        manifest=_bc_manifest_record(pack='generic',version=1)
        manifest['manifest_sha256']=template_packs.manifest_digest({k:v for k,v in manifest.items() if k not in {'schema_version','format_version','manifest_sha256'}})
        tree[bindings_current.derive_manifest_path('opaque-pack-id')]=manifest
        tree[current_path]['manifest_sha256']=manifest['manifest_sha256']
        tree[paths[3]]['after']['manifest_sha256']=manifest['manifest_sha256']
        tree[paths[3]]['approval_use_refs']=deepcopy(list(refs))
        owned=[]
        for payload in tree.values():
            if payload.get('record_type') in {'placement_revision','source_lifecycle'}:
                payload['approval_use_refs']=deepcopy(list(refs))
                family=payload['record_type']
                owned.append(dict(record_type=family,stable_id=payload['fact_uid'] if family=='placement_revision' else payload['source_id'],revision=payload['revision']))
        extra=deepcopy(next(v for v in full_tree().values() if v.get('record_type')=='event'))
        extra.update(event_id='event-2',ordinal=1,changed_refs=owned,affected_refs=deepcopy(owned),before=[None,None],after=[1,1])
        tree[git_format.record_path('event',event_id='event-2')]=extra
        tree[paths[1]]['event_ids'].append('event-2')
    def scope(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in identity: value[key]=identity[key]
                else: scope(item)
        elif isinstance(value,list):
            for item in value: scope(item)
    scope(tree)
    source = next(v for v in tree.values() if v.get('record_type')=='source_version')
    evidence = next(v for v in tree.values() if v.get('record_type')=='evidence')
    evidence['quote_digest']=hashlib.sha256(git_format.dumps_record(source)).hexdigest()
    issue = next(v for v in tree.values() if v.get('record_type')=='issue_event')
    op_path=git_format.record_path('operation',operation_id='operation-1')
    op=tree.pop(op_path)
    op.update(operation_id=operation,base_commit=head,approval_use_refs=deepcopy(list(refs)),
              effect_digest=hashlib.sha256(git_format.dumps_record(EFFECT)).hexdigest())
    tree[git_format.record_path('operation',operation_id=operation)]=op
    for event in tree.values():
        if event.get('record_type')=='event':
            event.update(operation_id=operation,base_commit=head)
            if not all_families:
                event['after']=[hashlib.sha256(git_format.dumps_record(issue)).hexdigest()]
    for payload in tree.values():
        if payload.get('record_type')=='consumed_candidate': payload['operation_id']=operation
    return {p:git_format.dumps_record(v) for p,v in tree.items()}


def export_repo(package, repo, core):
    head=repo.head_oid()
    repo.create_maintenance_pin(core['backup_id'],core['barrier_id'],head)
    output=package.parent/'export.bundle'
    exported=repo.export_maintenance_pin_bundle(core['backup_id'],core['barrier_id'],output)
    shutil.copyfile(output,package/'authority.bundle')
    verified=repo.verify_integrity()
    core['authority'].update(commit_oid=head,genesis_oid=verified.genesis_oid,
        object_format=repo.object_format,commit_count=verified.commit_count)
    core['bundle'].update(size_bytes=exported.size_bytes,sha256_digest='sha256:'+exported.sha256_digest)
    # This fixture is a legitimate hand-composed historical v2 package.
    # E coverage is a SEPARATE digest-bound capability, never inferred from v2.
    core['closure']['canonical_record']['commit_oid']=head
    for entry in core['closure']['canonical_record']['entries']:
        for root in entry['referenced_by']:
            if root['kind']=='H': root['root_id']=head
    digest=compute_core_digest(core['closure']['canonical_record'])
    core['closure']['r_list_digest']=digest
    core['payload']['r']['r_list_digest']=digest
    _seal(package,core)


def make_audit_scene(package, tmp_path, *, w_state='consumed', declared=True, accepted=True, transform=None, all_families=False):
    core=_manifest(package)['core']; identity={k:core['identity'][k] for k in ('repo_id','installation_id','company_id')}
    repo=FactsGitRepository.initialize(tmp_path/'source.git',**identity,
        object_format=os.environ.get('GIT_TEST_DEFAULT_HASH','sha1'))
    head=repo.head_oid(); baseline={r.path:r.content for r in repo.read_snapshot_at(head).records}
    planned={**baseline,**scoped_records(identity,head,all_families=all_families)}
    artifacts=approval_artifact_digests(planned,baseline,'operation-1')
    # Build real host envelopes with the exact E2 explanation contract.
    db=sqlite3.connect(tmp_path/'source-host.sqlite'); host=stack(db)
    host.identity.initialize_installation(installation_id=identity['installation_id'],company_id=identity['company_id'],
        owner_principal_id='owner-e3a',owner_role='owner',now=at(0))
    ticket=host.identity.issue_pairing_credential(now=at(10),lifetime_seconds=600)
    grant=host.identity.pair(ticket.secret,now=at(20),session_lifetime_seconds=3600)
    policy=host.authorization.create_policy(grant.secret,now=at(30),**scope_kwargs())
    cred=host.credentials.issue_task_credential(grant.secret,now=at(40),policy_id=policy.policy_id,expected_revision=1)
    effect=host.prepared.prepare_effect(cred.secret,now=at(50),**effect_kwargs(base_commit=head,
        object_format=repo.object_format,dependency_versions=DEPS,effect_manifest=dict(
            schema_version=1,repo_id=identity['repo_id'],effect=EFFECT,artifacts=artifacts)))
    decision=host.decisions.approve_effect(grant.secret,now=at(60),operation_id=effect.operation_id,
        expected_effect_digest=effect.effect_digest,expected_preparation_digest=effect.preparation_digest,
        decision='approved',expires_at=at(300))
    reservation=host.reservations.reserve_approval(cred.secret,now=at(70),operation_id=effect.operation_id,
        approval_id=decision.approval_id,expected_effect_digest=effect.effect_digest,
        expected_preparation_digest=effect.preparation_digest)
    consumers=HostApprovalConsumptionService(db,host.reservations,initialize=True)
    history=dict(db=db,host=host,credential=cred,grant=grant,effect=effect,decision=decision,reservation=reservation,consumption=consumers)
    if w_state=='missing':
        # Actual pre-intent snapshot; no deleted/rehashed consumption fiction.
        copy_host(package,history,declared=declared)
    ref={k:v for k,v in reservation.to_dict().items() if k not in {'reservation_state','created_at','expires_at'}}
    ref.update(schema_version=1,audit_ref_type='approval_use')
    records={**baseline,**scoped_records(identity,head,effect.operation_id,[ref],all_families=all_families)}
    work=OperationWorkStore(db); scope=(identity['repo_id'],identity['company_id'],effect.operation_id)
    with db:
        db.execute('BEGIN')
        work.record_operation(*scope,cred.task_id,'key-e3a',TIME,{})
        work.mark_operation_prepared(*scope,'work-prepared',TIME)
        work.mark_approval_required(*scope,'work-required',TIME)
        work.mark_reserved(*scope,'work-reserved',TIME)
    coord=BrokerCoordination(tmp_path/'source-coord',identity['repo_id'],db); lease=coord.claim_broker()
    if accepted:
        if accepted=='unknown':
            def failed_cas(*args,**kwargs):
                raise RuntimeError('test-only-storage-boundary-failure')
            repo.accept=failed_cas
        gate=BrokerFinalGate(repo,coord,work,approval_evidence=HostApprovalEvidence(consumers,cred.secret,now=lambda:at(80)))
        result=gate.accept(lease=lease,operation_id=effect.operation_id,effect=EFFECT,
            assemble=lambda *_:AssembledAttempt(records,{},EFFECT,DEPS),recorded_at=TIME)
        assert result.status==('unknown' if accepted=='unknown' else 'accepted'),result
    else:
        consumers.record_admission(cred.secret,now=at(80),operation_id=effect.operation_id,
            reservation_id=reservation.reservation_id,approval_id=decision.approval_id,
            expected_effect_digest=effect.effect_digest,expected_preparation_digest=effect.preparation_digest)
    lease.close()
    if w_state!='missing': copy_host(package,history,declared=declared)
    key=(cred.task_id,decision.approval_id,effect.operation_id)
    if transform is not None:
        key=transform(package,history,repo,records) or key
    core=_manifest(package)['core']; export_repo(package,repo,core)
    db.close()
    core=_manifest(package)['core']
    layout,paths,base=_make_layout(tmp_path,_roles(core),base_name='e3a-recovery')
    with RecoveryStateStore(paths['state']) as store:
        store.create_case(CASE,RecoveryEpoch('recovery-c','epoch-c-1',1),backup_id=core['backup_id'],
            barrier_digest=barrier_digest(core['barrier_id']),prior_epochs=(),at='2026-11-02T00:00:00.000000+00:00')
    admission=AdmissionEvidence('admitted',compute_core_digest(core),(
        ('envelope_version','2'),('payload_schema',core['payload']['schema']),
        ('payload_version',str(core['payload']['version'])),('object_format',repo.object_format)))
    checked=r4.verify_recovery_package(package,expected_identity=identity)
    assert checked.recoverable,[(c.name,c.reasons) for c in checked.checks if not c.passed]
    installed=install_materials(package_dir=package,layout=layout,case_id=CASE,identity=identity,
        head_oid=repo.head_oid(),evidence=admission,git_executable='/usr/bin/git',at='2026-11-03T00:00:00.000000+00:00')
    bare=FactsGitRepository.open(installed.bare.path,**identity)
    handoff=tmp_path/'handoff'
    _green_bundle(handoff,backup_id=core['backup_id'],barrier=barrier_digest(core['barrier_id']),
        merge_specs=[],clean_specs=[{'key':dkey(*key),'backup':['unused']}],
        credentials=[_credential('credential-descriptor',no_operations=True)])
    return dict(package=package,identity=identity,repository=bare,paths=paths,admission=admission,
                handoff=handoff,anchor=installed.journal_anchor,key=key,history=history,records=records,
                source_repo=repo,core=core)


@pytest.fixture
def audit_scene(package,tmp_path):
    return make_audit_scene(package,tmp_path)


def audit_scene_run(s,**overrides):
    kwargs=dict(package_dir=s['package'],repository=s['repository'],handoff_dir=s['handoff'],
        state_root=s['paths']['state'],case_id=CASE,trusted_anchor=s['anchor'],
        expected_identity=s['identity'],material_evidence=s['admission'])
    kwargs.update(overrides)
    return audit_recovery_approvals(**kwargs)


def categories(report,key):
    return {m.category:m for m in report.markers if m.key==key}


def test_both_sides_consistent_consumption_and_original_d_clean_claim_audit_only(audit_scene):
    s=audit_scene
    before={name:_a3_fingerprint(path) for name,path in (
        ('package',s['package']),('bare',s['repository'].path),('state',s['paths']['state']),('handoff',s['handoff']))}
    result=audit_scene_run(s)
    assert not result.blockers,result
    assert categories(result,s['key'])['A'].states==('consumed','reserved')
    assert set(categories(result,s['key']))=={'A','D'}
    assert result.current_evidence=='not_proven'
    assert result.evidence_inputs and all(i.state in {EvidenceState.CONSUMED,EvidenceState.RESERVED} for i in result.evidence_inputs)
    for name,path in (('package',s['package']),('bare',s['repository'].path),('state',s['paths']['state']),('handoff',s['handoff'])):
        assert _a3_fingerprint(path)==before[name]
    assert json.loads(json.dumps(asdict(result)))['case_id']==CASE


def test_git_accepted_missing_w_consumption_forms_a2_c_d(package,tmp_path):
    s=make_audit_scene(package,tmp_path,w_state='missing')
    result=audit_scene_run(s)
    marks=categories(result,s['key'])
    assert set(marks)=={'A','C','D'},result
    assert marks['A'].states==('consumed','reserved')
    assert 'approval_git_accepted_w_missing' in marks['A'].reasons
    assert marks['A'].origins and any(o.commit_oid==s['repository'].head_oid() for o in marks['A'].origins)


def test_w_consumption_without_git_reference_retains_a_and_audit(package,tmp_path):
    s=make_audit_scene(package,tmp_path,accepted=False)
    result=audit_scene_run(s)
    marks=categories(result,s['key'])
    assert set(marks)=={'A','D'},result
    assert marks['A'].states==('consumed','reserved')
    assert 'approval_consumption_without_git' in marks['D'].reasons


def replace_history(tmp_path, *, state, declared):
    def transform(package, old, repo, records):
        identity=_manifest(package)['core']['identity']
        history=host_history(tmp_path/'alternate-host.sqlite',identity,repo.head_oid(),
                             state=state,object_format=repo.object_format)
        copy_host(package,history,declared=declared)
        key=(history['credential'].task_id,history['decision'].approval_id,history['effect'].operation_id)
        history['db'].close()
        return key
    return transform


@pytest.mark.parametrize('declared',[False,True])
@pytest.mark.parametrize('state',['reserved','denied','revoked'])
def test_old_and_new_packages_keep_independent_restrictive_host_facts(package,tmp_path,declared,state):
    s=make_audit_scene(package,tmp_path,accepted=False,declared=declared,
        transform=replace_history(tmp_path,state=state,declared=declared))
    result=audit_scene_run(s)
    marks=categories(result,s['key'])
    assert 'B' not in marks and 'A' in marks
    expected={'reserved'} if state=='reserved' else {'denied'} if state=='denied' else {'reserved','revoked'}
    assert set(marks['A'].states)==expected,result
    assert result.current_ledger_route_available
    assert result.current_evidence=='not_proven'
    assert not result.blockers,result
    if state=='denied':
        assert any(e.state==EvidenceState.POLICY_RESTRICTED and e.policy_revision==1 for e in result.evidence_inputs)


@pytest.mark.parametrize('declared',[False,True])
def test_unreserved_approval_is_at_most_pending_b_and_legacy_keeps_current_route(package,tmp_path,declared):
    s=make_audit_scene(package,tmp_path,accepted=False,declared=declared,
        transform=replace_history(tmp_path,state='approved',declared=declared))
    result=audit_scene_run(s)
    marks=categories(result,s['key'])
    assert 'A' not in marks and not result.blockers,result
    assert ('B' in marks)==declared
    if declared:
        assert marks['B'].requires_independent_current
        assert marks['B'].reasons==('approval_independent_current_required',)
    assert result.current_evidence=='not_proven' and result.current_ledger_route_available


@pytest.mark.parametrize('source',['empty','package_copy','old_snapshot','replay','path','self_sha'])
def test_non_independent_current_inputs_never_prove_unused(package,tmp_path,source):
    s=make_audit_scene(package,tmp_path,accepted=False,
        transform=replace_history(tmp_path,state='approved',declared=True))
    if source=='empty':
        supplied=sqlite3.connect(':memory:'); supplied.execute('CREATE TABLE host_approval_consumptions (id TEXT)')
    elif source=='package_copy': supplied=sqlite3.connect(s['package']/WPATH)
    elif source=='old_snapshot': supplied=sqlite3.connect(tmp_path/'alternate-host.sqlite')
    elif source=='replay': supplied={'current':True,'snapshot':'replayed','sha256':'a'*64}
    elif source=='path': supplied=str(s['package']/WPATH)
    else: supplied={'path':str(s['package']/WPATH),'sha256':hashlib.sha256((s['package']/WPATH).read_bytes()).hexdigest()}
    queries=[]
    if isinstance(supplied,sqlite3.Connection): supplied.set_trace_callback(queries.append)
    try:
        result=audit_scene_run(s,independent_ledger_connection=supplied)
        assert result.current_evidence=='unverified_connection_or_snapshot'
        assert categories(result,s['key'])['B'].requires_independent_current
        assert not queries
    finally:
        if isinstance(supplied,sqlite3.Connection): supplied.close()


def test_legacy_git_accepted_is_a2_even_without_consumption_table(package,tmp_path):
    def legacy(package,history,repo,records):
        db=sqlite3.connect(package/WPATH)
        db.execute('DROP TABLE host_approval_consumptions'); db.close()
        core=_manifest(package)['core']
        core['recovery_capabilities']={'other_capability':{'version':2}}
        core['databases'][0]['schema']['tables'].remove('host_approval_consumptions')
        _refresh(package,core,WPATH)
    s=make_audit_scene(package,tmp_path,w_state='missing',declared=False,transform=legacy)
    result=audit_scene_run(s)
    assert not result.declared
    marks=categories(result,s['key'])
    assert set(marks)=={'A','C','D'} and marks['A'].states==('consumed','reserved')
    assert not result.blockers,result


def test_git_accepted_with_host_zero_records_keeps_a2_not_only_d(package,tmp_path):
    def empty(package,history,repo,records):
        path=package/WPATH; path.unlink()
        conn=sqlite3.connect(path); conn.execute('CREATE TABLE legacy_marker (id TEXT)'); conn.close()
        core=_manifest(package)['core']; core.pop('recovery_capabilities',None)
        core['databases'][0]['schema']['tables']=['legacy_marker']; _refresh(package,core,WPATH)
    s=make_audit_scene(package,tmp_path,transform=empty)
    result=audit_scene_run(s)
    marks=categories(result,s['key'])
    assert set(marks)=={'A','C','D'} and marks['A'].states==('consumed',)
    assert not result.blockers,result


def test_valid_w_chain_rebound_to_another_operation_is_hard_conflict(package,tmp_path):
    def rebind(package,history,repo,records):
        db=sqlite3.connect(package/WPATH)
        tables=r4._APPROVAL_TABLES
        for table in tables:
            triggers=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall()
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall():
                db.execute('DROP TRIGGER '+row[0])
            blob=db.execute('SELECT record_blob FROM '+table).fetchone()[0]
            record=git_format.loads_record(blob); record['operation_id']='different-operation'
            blob=git_format.dumps_record(record)
            domains={'host_prepared_effects':b'sam-host-prepared-effect row-v1\0',
                'host_approval_decisions':b'sam-host-approval-decision row-v1\0',
                'host_approval_reservations':b'sam-host-approval-reservation row-v1\0',
                'host_approval_consumptions':b'sam-host-approval-consumption row-v1\0'}
            db.execute('UPDATE '+table+' SET operation_id=?,record_blob=?,row_sha256=?',
                ('different-operation',blob,hashlib.sha256(domains[table]+blob).hexdigest()))
            for ddl, in triggers: db.execute(ddl)
        db.commit(); db.close()
        core=_manifest(package)['core']; _refresh(package,core,WPATH)
    s=make_audit_scene(package,tmp_path,transform=rebind)
    # Include both true identities in the original D handoff; no fabricated task.
    old=s['key']; other=(old[0],old[1],'different-operation')
    shutil.rmtree(s['handoff'])
    _green_bundle(s['handoff'],backup_id=s['core']['backup_id'],barrier=barrier_digest(s['core']['barrier_id']),
        merge_specs=[],clean_specs=[{'key':dkey(*key),'backup':['unused']} for key in (old,other)],
        credentials=[_credential('descriptor',no_operations=True)])
    result=audit_scene_run(s)
    assert 'approval_git_w_conflict' in result.blockers,result
    assert categories(result,old)['C'].reasons==('approval_git_w_conflict',)
    assert 'A' not in categories(result,old)  # conflict never forges A2
    assert categories(result,other)['A'].states==('consumed','reserved')
    assert not any(m.category=='B' for m in result.markers)


def restrictive_handoff(s, *, states=('consumed','revoked')):
    shutil.rmtree(s['handoff'])
    _green_bundle(s['handoff'],backup_id=s['core']['backup_id'],barrier=barrier_digest(s['core']['barrier_id']),
        merge_specs=[{'key':dkey(*s['key']),'backup':['approved'],
            'items':[(state+'.json',_evidence(state=state,source='source-'+state,ledger='ledger-'+state)) for state in states]}],
        clean_specs=[],credentials=[_credential('descriptor',no_operations=True)])


@pytest.mark.parametrize('declared',[False,True])
def test_execution_unknown_is_permanent_even_in_legacy_packages(package,tmp_path,declared):
    s=make_audit_scene(package,tmp_path,accepted='unknown',declared=declared)
    result=audit_scene_run(s)
    assert categories(result,s['key'])['A'].states==('consumed','reserved','unknown'),result
    assert 'B' not in categories(result,s['key'])


def test_d_evidence_and_existing_r5_restrictions_survive_missing_w(audit_scene,tmp_path):
    s=audit_scene; restrictive_handoff(s)
    first=audit_scene_run(s)
    assert categories(first,s['key'])['A'].states==('consumed','reserved','revoked')
    assert 'unknown' not in categories(first,s['key'])['A'].states
    receipts=tmp_path/'receipt'; receipts.mkdir()
    receipt=_run_d(s['paths'],s['handoff'],receipts,CASE,s['anchor'])
    s['anchor']=receipt.journal_anchor_after
    before=_a3_fingerprint(s['paths']['state'])
    # Package disappearance must not recalculate prior consumed/revoked as C.
    result=audit_scene_run(s,package_dir=tmp_path/'missing-old-w',prior_receipt=receipt)
    marks=categories(result,s['key'])
    assert marks['A'].states==('consumed','revoked'),result
    assert 'C' in marks and 'B' not in marks
    assert result.prior_receipt_digest==receipt.receipt_sha256
    assert result.blockers and _a3_fingerprint(s['paths']['state'])==before


def test_original_receipt_and_clean_claim_are_read_back_and_preserved(audit_scene,tmp_path):
    s=audit_scene; receipts=tmp_path/'receipt'; receipts.mkdir()
    receipt=_run_d(s['paths'],s['handoff'],receipts,CASE,s['anchor'])
    s['anchor']=receipt.journal_anchor_after
    before=_a3_fingerprint(receipts)
    result=audit_scene_run(s,prior_receipt=receipt)
    assert result.prior_receipt_digest==receipt.receipt_sha256
    assert result.original_clean_claim[0][:4]==(*s['key'],('unused',))
    assert categories(result,s['key'])['A'].states==('consumed','reserved')
    assert _a3_fingerprint(receipts)==before
    repeated=audit_scene_run(s,prior_receipt=receipt,original_clean_claim=result.original_clean_claim)
    assert repeated==result
    with pytest.raises(ApprovalAuditError,match='^approval_original_claim_mismatch$'):
        audit_scene_run(s,prior_receipt=receipt,original_clean_claim=())
    changed=json.loads(Path(receipt.receipt_path).read_bytes()); changed['case_id']='secret-do-not-echo'
    Path(receipt.receipt_path).write_text(json.dumps(changed))
    with pytest.raises(ApprovalAuditError) as error:
        audit_scene_run(s,prior_receipt=receipt)
    assert str(error.value)=='approval_audit_context_invalid'
    assert 'secret-do-not-echo' not in repr(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize('attack',['top_extra','top_missing','case','auth','credential','evidence','anchor','claim'])
def test_d_context_exact_keysets_case_auth_credentials_are_required_without_writes(audit_scene,attack):
    s=audit_scene
    manifest=json.loads((s['handoff']/'handoff.json').read_bytes())
    secret='secret-do-not-echo'
    overrides={}
    if attack=='top_extra': manifest['token']=secret
    elif attack=='top_missing': del manifest['authentication']
    elif attack=='case': manifest['case_id']='other-case'
    elif attack in {'auth','credential'}:
        ref=manifest['authentication'] if attack=='auth' else manifest['credentials'][0]
        payload=json.loads((s['handoff']/'items'/ref['name']).read_bytes()); payload['token']=secret
        fixed=_write_item(s['handoff']/'items',ref['name'],payload)
        if attack=='auth': manifest['authentication']=fixed
        else: manifest['credentials'][0]=fixed
    elif attack=='evidence':
        restrictive_handoff(s,states=('revoked',))
        manifest=json.loads((s['handoff']/'handoff.json').read_bytes())
        ref=manifest['merge_keys'][0]['items'][0]
        payload=json.loads((s['handoff']/'items'/ref['name']).read_bytes()); payload['token']=secret
        manifest['merge_keys'][0]['items'][0]=_write_item(s['handoff']/'items',ref['name'],payload)
    elif attack=='anchor': overrides['trusted_anchor']=(s['anchor'][0],'a'*64)
    else: overrides['original_clean_claim']=(('fabricated-task',),)
    (s['handoff']/'handoff.json').write_text(json.dumps(manifest))
    before={name:_a3_fingerprint(path) for name,path in (
        ('package',s['package']),('bare',s['repository'].path),('state',s['paths']['state']),('handoff',s['handoff']))}
    with pytest.raises(ApprovalAuditError) as error: audit_scene_run(s,**overrides)
    assert secret not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert str(error.value) in {'approval_handoff_invalid','approval_case_binding_mismatch',
                              'approval_case_context_invalid','approval_original_claim_mismatch'}
    for name,path in (('package',s['package']),('bare',s['repository'].path),('state',s['paths']['state']),('handoff',s['handoff'])):
        assert _a3_fingerprint(path)==before[name]


def test_a_evidence_exports_full_d_item_shape_and_union_not_unknown(audit_scene,tmp_path):
    s=audit_scene; restrictive_handoff(s)
    result=audit_scene_run(s)
    assert categories(result,s['key'])['A'].states==('consumed','reserved','revoked')
    items=result.to_handoff_items()
    assert items and all(set(v)=={'key','evidence'} for v in items)
    assert all(set(v['evidence'])=={'source_id','scope','ledger_id','revocation_seq','event_id','event_digest',
        'state','session_id','policy_revision'} and v['key']==dkey(*s['key']) for v in items)
    transfer=tmp_path/'future-e3b-handoff'
    _green_bundle(transfer,backup_id=s['core']['backup_id'],barrier=barrier_digest(s['core']['barrier_id']),
        merge_specs=[{'key':dkey(*s['key']),'backup':['approved'],
            'items':[(str(i)+'.json',v['evidence']) for i,v in enumerate(items)]}],
        clean_specs=[],credentials=[_credential('descriptor',no_operations=True)])
    from app.facts_tree.recovery_reauthorize import _load_plan, _pin_root
    fd,_=_pin_root(transfer,'handoff')
    try:
        loaded=_load_plan(fd)
        assert {v.state.value for v in loaded.merge_blocks[0].evidence}=={'consumed','reserved','revoked'}
    finally: os.close(fd)
    items[0]['evidence']['state']='unused'
    assert result.to_handoff_items()[0]['evidence']['state']!='unused'


@pytest.mark.parametrize('attack',['missing_objects','promisor','include','alias','hooks','fsmonitor','alternate'])
def test_real_bare_missing_objects_and_foreign_execution_config_are_offline(audit_scene,tmp_path,monkeypatch,attack):
    s=audit_scene; bare=Path(s['repository'].path)
    marker=tmp_path/'must-never-execute'
    canary=tmp_path/'git-remote-e3acanary'
    canary.write_text('#!/bin/sh\nprintf executed > '+str(marker)+'\nexit 91\n'); canary.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    malicious=tmp_path/'foreign.gitconfig'
    malicious.write_text('[core]\n\tfsmonitor = '+str(canary)+'\n[alias]\n\tfsck = !'+str(canary)+'\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(malicious))
    monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0',str(tmp_path))
    if attack in {'missing_objects','promisor'}:
        objects=bare/'objects'
        removed=[]
        for path in objects.rglob('*'):
            if path.is_file() and path.suffix not in {'.idx','.rev'}:
                removed.append(path); path.unlink()
        assert removed
    if attack=='promisor':
        with (bare/'config').open('a') as stream:
            stream.write('[remote "origin"]\n\turl = e3acanary::offline\n\tpromisor = true\n[extensions]\n\tpartialClone = origin\n')
    elif attack=='alternate':
        (bare/'objects/info/alternates').write_text(str(tmp_path/'not-an-object-store')+'\n')
    elif attack!='missing_objects':
        extra={'include':'[include]\n\tpath = '+str(malicious)+'\n',
            'alias':'[alias]\n\tfsck = !'+str(canary)+'\n',
            'hooks':'[core]\n\thooksPath = '+str(tmp_path)+'\n',
            'fsmonitor':'[core]\n\tfsmonitor = '+str(canary)+'\n'}[attack]
        with (bare/'config').open('a') as stream: stream.write(extra)
    attempts=[]; real_run=subprocess.run
    def observed_run(argv,*args,**kwargs):
        attempts.append((tuple(argv),dict(kwargs.get('env',{}))))
        assert Path(argv[0]).name=='git'
        assert not any(v in {'fetch','fetch-pack','remote-e3acanary','clone'} for v in argv)
        return real_run(argv,*args,**kwargs)
    def network_forbidden(*args,**kwargs):
        pytest.fail('new adapter attempted network access')
    monkeypatch.setattr(subprocess,'run',observed_run)
    monkeypatch.setattr(socket,'socket',network_forbidden)
    monkeypatch.setattr(socket,'create_connection',network_forbidden)
    before=_a3_fingerprint(bare)
    result=audit_scene_run(s)
    assert 'approval_git_unverified' in result.blockers,result
    assert categories(result,s['key'])['A'].states==('consumed','reserved')
    assert 'B' not in categories(result,s['key']) and 'C' in categories(result,s['key'])
    assert not marker.exists() and _a3_fingerprint(bare)==before
    bare_calls=[(argv,env) for argv,env in attempts if str(bare) in argv]
    if attack=='alternate':
        assert bare_calls==[]  # sentinel rejects before any bare Git invocation
    else:
        assert bare_calls
    for argv,env in bare_calls:
        if env.get('GIT_NO_LAZY_FETCH')!='1':
            assert argv[-3:]==('rev-parse','--verify','refs/heads/facts')
        else:
            assert env.get('GIT_NO_REPLACE_OBJECTS')=='1'
    assert not any('e3acanary' in ' '.join(argv) for argv,_ in attempts)


def test_external_git_configuration_is_ignored_on_valid_bare(audit_scene,tmp_path,monkeypatch):
    s=audit_scene; canary=tmp_path/'must-never-execute'
    config=tmp_path/'external.config'
    config.write_text('[alias]\n\tfsck = !touch '+str(canary)+'\n[core]\n\thooksPath = /no-such-secret-dir\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(config))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM',str(config))
    monkeypatch.setenv('GIT_DIR',str(tmp_path/'foreign.git'))
    result=audit_scene_run(s)
    assert not result.blockers,result
    assert not canary.exists()


def test_all_four_families_have_traceable_acceptance_proof_and_exact_restatement(package,tmp_path):
    s=make_audit_scene(package,tmp_path,all_families=True,w_state='missing')
    result=audit_scene_run(s)
    assert not result.blockers,result
    origin=categories(result,s['key'])['A'].origins
    git_origins=[v for v in origin if v.kind=='Git']
    assert {git_format.canonical_record_type(v.record_path) for v in git_origins}=={
        'operation','event','placement_revision','source_lifecycle'}
    assert all(v.commit_oid==s['repository'].head_oid() and len(v.digest)==64 for v in git_origins)
    assert categories(result,s['key'])['A'].states==('consumed','reserved')


def test_appearance_on_physically_valid_git_chain_without_business_binding_is_only_c(package,tmp_path):
    def unproven(package,history,repo,records):
        head=repo.head_oid()
        cumulative={r.path:r.content for r in repo.read_snapshot_at(head).records}
        # No mocked locator: low-level physical repository accepts an additional
        # ordinary event carrying refs; E2 business closure must then reject it.
        event=deepcopy(next(git_format.loads_record(v) for v in records.values()
                           if git_format.loads_record(v).get('record_type')=='event'))
        event.update(event_id='malformed-event',operation_id='appearance-operation',base_commit=head,ordinal=0,
            approval_use_refs=[{'approval_id':'secret-only-approval'}])
        op=deepcopy(next(git_format.loads_record(v) for v in records.values() if git_format.loads_record(v).get('record_type')=='operation'))
        op.update(operation_id='appearance-operation',base_commit=head,event_ids=['malformed-event'],
            candidate_refs=[],disposition_result=[],formal_fact_refs=[],approval_use_refs=[])
        cumulative[git_format.record_path('operation',operation_id='appearance-operation')]=git_format.dumps_record(op)
        path=git_format.record_path('event',event_id='malformed-event')
        cumulative[path]=git_format.dumps_record(event)
        tree=repo.build_tree(cumulative.items(),expected_paths=sorted(cumulative))
        commit=repo.create_commit(tree,head)
        repo.accept(head,commit,expected_paths=sorted(cumulative))
        # A plain legacy database has no independent host hard evidence.
        wpath=package/WPATH; wpath.unlink()
        db=sqlite3.connect(wpath); db.execute('CREATE TABLE legacy_marker (id TEXT)'); db.close()
        core=_manifest(package)['core']; core.pop('recovery_capabilities',None)
        core['databases'][0]['schema']['tables']=['legacy_marker']; _refresh(package,core,WPATH)
    s=make_audit_scene(package,tmp_path,transform=unproven)
    result=audit_scene_run(s)
    assert 'approval_git_unverified' in result.blockers
    assert not any(m.category in {'A','B'} for m in result.markers),result
    assert any(m.category=='C' and m.key is None for m in result.markers)
    assert all('secret-only-approval' not in reason for reason in result.blockers)


@pytest.mark.parametrize('declaration',[None,{}, {'approval_consumptions':None},
    {'approval_consumptions':{'version':True,'roles':['w_items']}},
    {'approval_consumptions':{'version':2,'roles':['w_items']}},
    {'approval_consumptions':{'version':1,'roles':[]}},
    {'approval_consumptions':{'version':1,'roles':['w_items','w_items']}},
    {'approval_consumptions':{'version':1,'roles':['../secret']}}])
def test_capability_declaration_must_be_explicit_versioned_and_digest_bound(approval_package,declaration):
    package,_=approval_package; core=_manifest(package)['core']
    core['recovery_capabilities']=declaration; _seal(package,core)
    verdict=r4.verify_recovery_package(package)
    if declaration=={}:
        assert verdict.recoverable and not report(verdict)['declared']
    else:
        assert not verdict.recoverable
        assert 'approval_capability_invalid' in verdict.blockers


def test_declaration_outside_bound_core_cannot_upgrade_legacy(package):
    manifest=_manifest(package)
    manifest['recovery_capabilities']={'approval_consumptions':{'version':1,'roles':['w_items']}}
    (package/'manifest.json').write_text(json.dumps(manifest))
    verdict=r4.verify_recovery_package(package)
    assert not dict(verdict.approval_audit).get('declared',False)
    assert not any(c.name=='E.approvals' for c in verdict.checks)


def test_corrupt_consumption_path_does_not_erase_valid_reservation_path(approval_package):
    package,history=approval_package; db=sqlite3.connect(package/WPATH)
    ddl=db.execute("SELECT sql FROM sqlite_master WHERE name='host_approval_consumptions_no_update'").fetchone()[0]
    db.execute('DROP TRIGGER host_approval_consumptions_no_update')
    db.execute("UPDATE host_approval_consumptions SET row_sha256=?",('e'*64,)); db.execute(ddl); db.commit(); db.close()
    _refresh(package,_manifest(package)['core'],WPATH)
    verdict=r4.verify_recovery_package(package)
    assert not verdict.recoverable
    snapshot=report(verdict)
    assert not snapshot['consumption_operations']
    assert any(r['table']=='host_approval_reservations' and r['record']['operation_id']==history['effect'].operation_id for r in snapshot['records'])


def test_new_adapter_ast_surface_and_existing_verifier_imports_are_frozen():
    import app.facts_tree.recovery_approval_audit as adapter
    from tests.test_facts_tree_recovery_verifier import test_import_surface_and_read_only_ast
    from tests.test_facts_tree_recovery_reauthorize import test_d92_production_and_test_ast_surface
    from tests.test_approval_use_e2 import test_no_production_instantiations
    test_import_surface_and_read_only_ast()
    test_d92_production_and_test_ast_surface()
    test_no_production_instantiations()
    tree=ast.parse(Path(adapter.__file__).read_text())
    modules=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): modules.update(v.name for v in node.names)
        elif isinstance(node,ast.ImportFrom): modules.add(node.module)
    assert modules=={'dataclasses','hashlib','os','app.facts_tree',
        'app.facts_tree.backup_manifest','app.facts_tree.git_append_only','app.facts_tree.git_repository',
        'app.facts_tree.operation_acceptance','app.facts_tree.record_tree','app.facts_tree.recovery_epoch',
        'app.facts_tree.recovery_materializer','app.facts_tree.recovery_reauthorize',
        'app.facts_tree.recovery_repository','app.facts_tree.recovery_state_store','app.facts_tree.recovery_verifier'}
    banned={'socket','subprocess','Popen','system','popen','eval','exec','__import__','pickle','requests',
        'HostApprovalConsumptionService','HostApprovalReservationService','HostApprovalDecisionService',
        'HostPreparedEffectService','BrokerFinalGate','OperationWorkStore'}
    assert not {n.id for n in ast.walk(tree) if isinstance(n,ast.Name)} & banned
    writes={'write','write_text','write_bytes','mkdir','unlink','rename','replace','commit','rollback',
        'transition','authenticate','merge','open_gate','create_case','executescript'}
    assert not {n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)} & writes
    assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='RecoveryStateStore' for n in ast.walk(tree))
    assert not any(isinstance(n,ast.ImportFrom) and n.module.startswith('app.host_') for n in ast.walk(tree))
    allowed_sql={'SELECT','PRAGMA'}
    for n in ast.walk(tree):
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute':
            assert isinstance(n.args[0],ast.Constant) and n.args[0].value.split()[0] in allowed_sql
    git_calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='_git_text']
    assert len(git_calls)==1
    assert ast.literal_eval(git_calls[0].args[0])==['config','--local','--no-includes','--list']


def test_report_is_deeply_immutable_and_package_coordinates_are_complete(audit_scene):
    s=audit_scene; result=audit_scene_run(s)
    from dataclasses import is_dataclass
    def immutable(value):
        if is_dataclass(value):
            assert type(value).__dataclass_params__.frozen
            for field in fields(value): immutable(getattr(value,field.name))
        elif type(value) is tuple:
            for item in value: immutable(item)
        else: assert value is None or type(value) in {str,int,bool} or isinstance(value,EvidenceState)
    immutable(result)
    snapshot=dict(result.package_evidence)
    assert snapshot['core_digest']==result.core_digest and snapshot['sources']
    rows=[dict(row) for row in snapshot['records']]
    for origin in categories(result,s['key'])['A'].origins:
        if origin.kind=='W':
            assert any(row['role']==origin.role and row['table']==origin.table and row['row_sha256']==origin.digest for row in rows)
    secrets=(s['history']['credential'].secret,s['history']['grant'].secret)
    rendered=repr(result)+json.dumps(asdict(result))
    assert all(secret not in rendered for secret in secrets)
