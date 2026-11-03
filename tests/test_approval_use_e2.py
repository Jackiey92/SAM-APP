"""revD E2: real host authority, pure history and transactional Git admission.

Synthetic companies only. Host secrets stay in the fixture, never Git records.
The process drills use SIGKILL, real reopened SQLite connections and real Git.
"""
import ast
import builtins
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
import signal
import socket
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.facts_tree import broker_tree_gate, git_format
from app.facts_tree.broker_coordination import BrokerCoordination
from app.facts_tree.broker_final_gate import (
    AssembledAttempt, BrokerFinalGate, HostApprovalEvidence, VerifiedReentryFacts,
    approval_artifact_digests,
)
from app.facts_tree.git_repository import FactsGitRepository
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.record_schema import (
    SCHEMA_FIELDS, SCHEMA_COLUMNS, RecordSchemaClosureError,
    validate_record_schema, validate_record_schema_closure,
)
from app.facts_tree.record_schema._primitives import APPROVAL_USE_FIELDS
from app.facts_tree.record_tree import build_record_tree
from app.host_approval_consumptions import HostApprovalConsumptionService
from app.host_approval_decisions import HostApprovalDecisionService
from app.host_approval_reservations import HostApprovalReservationService
from app.host_authorization import HostAuthorizationService
from app.host_identity import HostIdentityService
from app.host_prepared_effects import HostPreparedEffectService
from app.host_task_credentials import HostTaskCredentialService
from tests.facts_tree_full_fixture import SCOPE, full_tree
from tests.test_facts_tree_record_schema_closure import binding_tree
from tests.test_host_prepared_effects import effect_kwargs, scope_kwargs
from tests.test_host_task_credentials import at

TIME = '2026-11-01T12:00:00+08:00'
EFFECT = {'opaque': ['effect', 1]}
DEPS = {'taxonomy_pack': 'generic-v1', 'plan': 'plan-v1'}
OP = git_format.record_path('operation', operation_id='operation-1')
EVENT = git_format.record_path('event', event_id='event-1')
META = git_format.record_path('repository_meta')
ROOT = Path(__file__).resolve().parents[1]


def reference(**overrides):
    ref = dict(schema_version=1, audit_ref_type='approval_use',
               reservation_id='reservation-test', approval_id='approval-test',
               operation_id='operation-1', task_id='task-test', run_id='run-test',
               installation_id='install-test', company_id=SCOPE['company_id'],
               principal_id='owner-test', member_revision=1, policy_id='policy-test',
               policy_revision=1, effect_digest='a' * 64,
               preparation_digest='b' * 64, action='read', target='uploads-inbox')
    ref.update(overrides)
    return ref


def raw_tree(tree):
    return {p: git_format.dumps_record(v) for p, v in tree.items()}


def records_for(head, opid='operation-1', refs=()):
    tree = full_tree()
    op = tree.pop(OP)
    op.update(operation_id=opid, base_commit=head,
              effect_digest=sha256(git_format.dumps_record(EFFECT)).hexdigest(),
              approval_use_refs=deepcopy(list(refs)))
    tree[git_format.record_path('operation', operation_id=opid)] = op
    tree[EVENT].update(operation_id=opid, base_commit=head)
    for payload in tree.values():
        if payload.get('record_type') == 'consumed_candidate':
            payload['operation_id'] = opid
    return raw_tree(tree)


def stack(db):
    identity = HostIdentityService(db)
    authorization = HostAuthorizationService(db, identity)
    credentials = HostTaskCredentialService(db, identity, authorization)
    prepared = HostPreparedEffectService(db, identity, authorization, credentials)
    decisions = HostApprovalDecisionService(db, identity, authorization, credentials, prepared)
    reservations = HostApprovalReservationService(db, identity, authorization, credentials,
                                                 prepared, decisions)
    return SimpleNamespace(identity=identity, authorization=authorization,
                           credentials=credentials, prepared=prepared,
                           decisions=decisions, reservations=reservations)


def make_scene(directory, *, manifest_override=None):
    directory.mkdir(parents=True, exist_ok=True)
    repo = FactsGitRepository.initialize(directory / 'facts.git', **SCOPE,
                                         installation_id='install-test',
                                         object_format=os.environ.get('GIT_TEST_DEFAULT_HASH', 'sha1'))
    db = sqlite3.connect(directory / 'work.sqlite')
    host = stack(db)
    host.identity.initialize_installation(installation_id='install-test',
        company_id=SCOPE['company_id'], owner_principal_id='owner-test',
        owner_role='owner', now=at(0))
    pairing = host.identity.issue_pairing_credential(now=at(10), lifetime_seconds=600)
    grant = host.identity.pair(pairing.secret, now=at(20), session_lifetime_seconds=3600)
    policy = host.authorization.create_policy(grant.secret, now=at(30), **scope_kwargs())
    cred = host.credentials.issue_task_credential(grant.secret, now=at(40),
        policy_id=policy.policy_id, expected_revision=1)
    head = repo.head_oid()
    baseline = {r.path: r.content for r in repo.read_snapshot_at(head).records}
    planned = {**baseline, **records_for(head)}
    manifest = dict(schema_version=1, repo_id=SCOPE['repo_id'], effect=EFFECT,
                    artifacts=approval_artifact_digests(planned, baseline, 'operation-1'))
    expected_artifacts = manifest['artifacts']
    if manifest_override is not None:
        manifest = deepcopy(manifest_override)
    prepared = host.prepared.prepare_effect(cred.secret, now=at(50),
        **effect_kwargs(base_commit=head, object_format=repo.object_format,
                        effect_manifest=manifest, dependency_versions=DEPS))
    decision = host.decisions.approve_effect(grant.secret, now=at(60),
        operation_id=prepared.operation_id, expected_effect_digest=prepared.effect_digest,
        expected_preparation_digest=prepared.preparation_digest,
        decision='approved', expires_at=at(300))
    reservation = host.reservations.reserve_approval(cred.secret, now=at(70),
        operation_id=prepared.operation_id, approval_id=decision.approval_id,
        expected_effect_digest=prepared.effect_digest,
        expected_preparation_digest=prepared.preparation_digest)
    consumers = HostApprovalConsumptionService(db, host.reservations, initialize=True)
    ref = {key: reservation.record[key] for key in APPROVAL_USE_FIELDS - {'schema_version', 'audit_ref_type'}}
    ref.update(schema_version=1, audit_ref_type='approval_use')
    records = {**baseline, **records_for(head, prepared.operation_id, [ref])}
    assert approval_artifact_digests(records, baseline, prepared.operation_id) == expected_artifacts
    store = OperationWorkStore(db)
    scope = (*SCOPE.values(), prepared.operation_id)
    with db:
        db.execute('BEGIN')
        store.record_operation(*scope, cred.task_id, 'key-test', TIME, {})
        store.mark_operation_prepared(*scope, 'work-prepared', TIME)
        store.mark_approval_required(*scope, 'work-required', TIME)
        store.mark_reserved(*scope, 'work-reserved', TIME)
    coord = BrokerCoordination(directory / 'coordination', SCOPE['repo_id'], db)
    lease = coord.claim_broker()
    clock = [at(80)]
    evidence = HostApprovalEvidence(consumers, cred.secret, now=lambda: clock[0])
    gate = BrokerFinalGate(repo, coord, store, approval_evidence=evidence)
    return SimpleNamespace(directory=directory, repo=repo, db=db, host=host,
        grant=grant, cred=cred, reservation=reservation, consumers=consumers,
        ref=ref, records=records, prepared=prepared, store=store, scope=scope,
        coord=coord, lease=lease, gate=gate, clock=clock, initial_head=head)


@pytest.fixture(autouse=True)
def no_network_attempts(monkeypatch):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(1)
        raise AssertionError('E2 attempted network IO')
    for name in ('socket', 'create_connection', 'getaddrinfo'):
        monkeypatch.setattr(socket, name, forbidden)
    yield
    assert attempts == []


@pytest.fixture
def scene(tmp_path):
    item = make_scene(tmp_path / 'scene')
    try:
        yield item
    finally:
        item.lease.close()
        item.db.close()


def assemble_scene(s):
    return lambda *_: AssembledAttempt(s.records, {'opaque': 'plan'}, deepcopy(EFFECT), deepcopy(DEPS))


def run(s, **kwargs):
    return s.gate.accept(lease=s.lease, operation_id=s.prepared.operation_id,
        effect=EFFECT, assemble=kwargs.pop('assemble', assemble_scene(s)), recorded_at=TIME, **kwargs)


def count(s, table='host_approval_consumptions'):
    return s.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]


def test_first_execution_joint_durability_and_expired_accepted_replay(scene, monkeypatch):
    s = scene
    calls = []
    real = s.repo.accept
    def accept(*args, **kwargs):
        # The post-intent lock is active; independent readers see BOTH facts.
        assert s.db.in_transaction
        with sqlite3.connect(s.directory / 'work.sqlite') as other:
            assert other.execute('SELECT count(*) FROM host_approval_consumptions').fetchone() == (1,)
            assert other.execute("SELECT state FROM fact_operation_attempt_events ORDER BY seq DESC LIMIT 1").fetchone() == ('accepting',)
        calls.append('CAS')
        return real(*args, **kwargs)
    monkeypatch.setattr(s.repo, 'accept', accept)
    result = run(s)
    assert result.status == 'accepted', result
    assert calls == ['CAS'] and count(s) == 1
    s.host.credentials.revoke_task_credential(s.grant.secret, now=at(100), task_id=s.cred.task_id)
    s.clock[0] = at(400)
    replay = run(s, assemble=lambda *_: pytest.fail('accepted replay must not assemble'))
    assert replay.status == 'idempotent_replay'
    assert replay.accepted_commit == result.accepted_commit and calls == ['CAS']
    s.store.verify_integrity()


@pytest.mark.parametrize('failure', ['effect', 'dependencies', 'base', 'artifacts', 'product'])
def test_independent_bridge_tampering_fails_before_cas(scene, monkeypatch, failure):
    s = scene
    assemble = assemble_scene(s)
    if failure == 'effect':
        # Keep the Broker's opaque digest legitimate for this supplied effect;
        # the independently prepared host effect must still disagree.
        changed = {'opaque': ['different', 2]}
        op_path = git_format.record_path('operation', operation_id=s.prepared.operation_id)
        op = git_format.loads_record(s.records[op_path])
        op['effect_digest'] = sha256(git_format.dumps_record(changed)).hexdigest()
        s.records[op_path] = git_format.dumps_record(op)
        assemble = lambda *_: AssembledAttempt(s.records, {}, changed, DEPS)
    elif failure == 'dependencies':
        assemble = lambda *_: AssembledAttempt(s.records, {}, EFFECT, {'plan': 'changed'})
    elif failure == 'artifacts':
        path = git_format.record_path('fact_revision', fact_uid='fact-1', revision=2)
        payload = git_format.loads_record(s.records[path])
        payload['canonical_value'] = {'secret': 'changed actual content'}
        s.records[path] = git_format.dumps_record(payload)
    elif failure == 'base':
        # A real legal intervening commit changes base while host approval stays bound.
        baseline = {r.path: r.content for r in s.repo.read_snapshot_at(s.initial_head).records}
        tree = s.repo.build_tree(baseline.items(), expected_paths=baseline)
        commit = s.repo.create_commit(tree, s.initial_head, message='intervening')
        assert s.repo.accept(s.initial_head, commit, expected_paths=baseline).accepted
        for path in (git_format.record_path('operation', operation_id=s.prepared.operation_id), EVENT):
            payload = git_format.loads_record(s.records[path]); payload['base_commit'] = commit
            s.records[path] = git_format.dumps_record(payload)
    elif failure == 'product':
        real = s.repo._validate_commit_tree
        def corrupted(*args, **kwargs):
            tree, records = real(*args, **kwargs)
            return tree, {**records, 'unexpected': b'changed'}
        monkeypatch.setattr(s.repo, '_validate_commit_tree', corrupted)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    effect = changed if failure == 'effect' else EFFECT
    result = s.gate.accept(lease=s.lease, operation_id=s.prepared.operation_id,
        effect=effect, assemble=assemble, recorded_at=TIME)
    assert result.status == 'rejected', result
    assert not calls and count(s) == 0
    assert count(s, 'fact_operation_attempts') == 0


@pytest.mark.parametrize('phase', ['before_joint_commit', 'after_joint_commit'])
def test_atomic_faults_and_irreversible_reservation(scene, monkeypatch, phase):
    s = scene
    marker = 'SECRET-DO-NOT-ECHO'
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    if phase == 'before_joint_commit':
        def fail(*a, **k):
            assert count(s) == 1 and s.db.in_transaction
            raise RuntimeError(marker)
        monkeypatch.setattr(s.store, 'admit_execution', fail)
    else:
        real = broker_tree_gate.validate_broker_tree
        rounds = []
        def validate(*a, **k):
            rounds.append(1)
            if len(rounds) == 2:
                raise RuntimeError(marker)
            return real(*a, **k)
        monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validate)
    result = run(s)
    assert result.status == ('rejected' if phase == 'before_joint_commit' else 'unknown'), result
    assert marker not in repr(result) and not calls
    assert count(s) == (0 if phase == 'before_joint_commit' else 1)
    assert count(s, 'fact_operation_attempts') == (0 if phase == 'before_joint_commit' else 1)
    assert count(s, 'host_approval_reservations') == 1
    assert s.repo.head_oid() == s.initial_head and not s.db.in_transaction


@pytest.mark.parametrize('failure', ['expired', 'revoked'])
def test_post_intent_current_snapshot_rejects_authorization_change(scene, monkeypatch, failure):
    s = scene
    real = broker_tree_gate.validate_broker_tree
    rounds = []
    def validate(*a, **k):
        result = real(*a, **k)
        rounds.append(1)
        if len(rounds) == 2:
            if failure == 'expired':
                s.clock[0] = at(301)
            else:
                # Real independent connection commits revocation before terminal check.
                other = sqlite3.connect(s.directory / 'work.sqlite')
                try:
                    stack(other).credentials.revoke_task_credential(s.grant.secret,
                        now=at(85), task_id=s.cred.task_id)
                finally:
                    other.close()
        return result
    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validate)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'unknown', result
    assert len(rounds) == 2 and not calls and count(s) == 1
    assert count(s, 'host_approval_reservations') == 1


def test_external_mapping_mutation_does_not_pollute_captured_commit(scene, monkeypatch):
    s = scene
    original = dict(s.records)
    real = broker_tree_gate.validate_broker_tree
    rounds = []
    def validate(*a, **k):
        rounds.append(1)
        if len(rounds) == 2:
            s.records.clear(); s.records['caller-only'] = b'corrupt'
        return real(*a, **k)
    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validate)
    result = run(s)
    assert result.status == 'accepted', result
    assert {r.path: r.content for r in s.repo.read_snapshot_at(result.accepted_commit).records} == original


def test_disabled_capability_refuses_new_refs_but_structure_accepts(scene):
    s = scene
    tree = build_record_tree(s.records.items(), **SCOPE, commit_oid='c' * s.repo.oid_length,
                             head_oid=s.initial_head, object_format=s.repo.object_format)
    assert tree.record_count == len(s.records)
    s.gate = BrokerFinalGate(s.repo, s.coord, s.store)
    result = run(s)
    assert result.status == 'rejected' and result.reason == 'approval_evidence_unavailable'
    assert count(s) == 0 and s.repo.head_oid() == s.initial_head


@pytest.mark.parametrize('bad', [[{}], ['opaque'], [{'approval_id': 'only'}], {}, None, ['actor'], [1]])
@pytest.mark.parametrize('family', ['operation', 'event', 'placement_revision', 'source_lifecycle'])
def test_old_malformed_refs_stay_rejected(family, bad):
    tree, paths, _ = binding_tree() if family == 'event' else (full_tree(), (), None)
    path = next(p for p,v in tree.items() if v.get('record_type') == family)
    tree[path]['approval_use_refs'] = deepcopy(bad)
    with pytest.raises(RecordSchemaClosureError):
        validate_record_schema(path, tree[path], **SCOPE)


@pytest.mark.parametrize('field,value', [('member_revision', True), ('policy_revision', 0),
    ('effect_digest', None), ('preparation_digest', ''), ('audit_ref_type', 'user_actor'),
    ('schema_version', True), ('target', ''), ('task_id', None)])
def test_exact_composite_type_and_digest_discipline(field, value):
    tree = full_tree(); tree[OP]['approval_use_refs'] = [reference(**{field: value})]
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema(OP, tree[OP], **SCOPE)
    assert caught.value.code == 'unsupported_approval_audit_ref'


def test_exact_keys_scope_duplicate_and_ordinary_event_negative():
    tree = full_tree()
    cases = [([reference(secret='never-echo')], 'unsupported_approval_audit_ref'),
             ([reference(company_id='wrong')], 'approval_use_scope'),
             ([reference(installation_id='wrong')], 'approval_use_scope'),
             ([reference(), reference()], 'duplicate_approval_use_ref')]
    for refs, code in cases:
        tree[OP]['approval_use_refs'] = refs
        with pytest.raises(RecordSchemaClosureError) as caught:
            validate_record_schema(OP, tree[OP], **SCOPE)
        assert caught.value.code == code and 'never-echo' not in str(caught.value)
    tree[EVENT]['approval_use_refs'] = [reference()]
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema(EVENT, tree[EVENT], **SCOPE)
    assert caught.value.code == 'unknown_field'
    assert len(SCHEMA_FIELDS) == len(SCHEMA_COLUMNS) == 16
    assert set(SCHEMA_FIELDS) == set(SCHEMA_COLUMNS)


def binding_refs():
    tree, paths, _ = binding_tree()
    tree[paths[1]]['approval_use_refs'] = [reference()]
    tree[paths[3]]['approval_use_refs'] = [reference()]
    return tree, paths


def test_binding_restatement_allowed_and_structural_acceptance_grants_no_rights(monkeypatch):
    """结构接受不授权：任意历史有效性状态都不触发宿主查询。"""
    tree, _ = binding_refs()
    def forbidden(*a, **k):
        pytest.fail('pure structure tried host/IO')
    for target, name in [(sqlite3, 'connect'), (socket, 'socket'),
                         (subprocess, 'run'), (subprocess, 'Popen'), (builtins, 'open'),
                         (HostApprovalConsumptionService, 'get_current_status')]:
        monkeypatch.setattr(target, name, forbidden)
    validate_record_schema_closure(tree, **SCOPE)
    records = raw_tree(tree)
    built = build_record_tree(records.items(), **SCOPE, commit_oid='c'*40, head_oid='d'*40)
    broker_tree_gate.validate_broker_tree(records, **SCOPE, commit_oid='c'*40, head_oid='d'*40)
    assert built.record_count == len(tree)


@pytest.mark.parametrize('field,value', [('task_id', 'another-task'), ('effect_digest', 'c'*64),
    ('preparation_digest', 'd'*64), ('target', 'another-target'), ('approval_id', 'another-approval')])
def test_binding_restatement_must_exactly_repeat_operation(field, value):
    tree, paths = binding_refs()
    tree[paths[3]]['approval_use_refs'][0][field] = value
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema_closure(tree, **SCOPE)
    assert caught.value.code == 'approval_use_restatement'


def test_tree_ownership_conflict_and_missing_operation():
    tree, paths = binding_refs()
    second = deepcopy(tree[paths[1]])
    second.update(operation_id='operation-2', event_ids=[], approval_use_refs=[reference(operation_id='operation-2')])
    tree[git_format.record_path('operation', operation_id='operation-2')] = second
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema_closure(tree, **SCOPE)
    assert caught.value.code == 'approval_use_owner_conflict'
    del tree[git_format.record_path('operation', operation_id='operation-2')]
    tree[paths[3]]['approval_use_refs'][0]['operation_id'] = 'missing'
    with pytest.raises(RecordSchemaClosureError) as caught:
        validate_record_schema_closure(tree, **SCOPE)
    assert caught.value.code == 'approval_use_operation_binding'


def test_pure_layer_ast_forbids_io_but_allows_existing_registration():
    modules = list((ROOT / 'app/facts_tree/record_schema').glob('*.py'))
    modules += [ROOT / 'app/facts_tree/record_tree.py', ROOT / 'app/facts_tree/broker_tree_gate.py']
    forbidden_modules = {'sqlite3', 'socket', 'subprocess', 'requests', 'urllib', 'http', 'os', 'pathlib'}
    for path in modules:
        syntax = ast.parse(path.read_text())
        for node in ast.walk(syntax):
            if isinstance(node, ast.Import):
                assert not {a.name.split('.')[0] for a in node.names} & forbidden_modules, path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or '').split('.')[0] not in forbidden_modules, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {'open', 'exec', 'eval', '__import__'}, path
    assert 'from importlib import import_module' in (ROOT / 'app/facts_tree/record_schema/__init__.py').read_text()


def test_no_production_instantiations():
    banned = {'HostApprovalConsumptionService', 'HostApprovalReservationService',
              'HostApprovalDecisionService', 'HostPreparedEffectService',
              'BrokerFinalGate', 'OperationWorkStore', 'HostApprovalEvidence'}
    calls = []
    for path in (ROOT / 'app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else '')
                if name in banned:
                    calls.append((str(path), node.lineno, name))
    assert calls == []


def test_four_families_nonempty_and_same_operation_restatements():
    tree, paths = binding_refs()
    business = full_tree()
    for path, payload in business.items():
        if payload.get('record_type') not in {'operation', 'event', 'consumed_candidate', 'input_candidate'}:
            tree[path] = payload
    owned = []
    for path, payload in tree.items():
        if payload.get('record_type') in {'placement_revision', 'source_lifecycle'}:
            payload['approval_use_refs'] = [reference()]
            family = payload['record_type']
            owned.append(dict(record_type=family,
                stable_id=payload['fact_uid'] if family == 'placement_revision' else payload['source_id'],
                revision=payload['revision']))
    extra = deepcopy(business[EVENT])
    extra.update(event_id='event-2', ordinal=1, changed_refs=owned,
                 affected_refs=deepcopy(owned), before=[None, None], after=[1, 1])
    tree[git_format.record_path('event', event_id='event-2')] = extra
    tree[paths[1]]['event_ids'].append('event-2')
    validate_record_schema_closure(tree, **SCOPE)
    for family in ('placement_revision', 'source_lifecycle'):
        path = next(p for p,v in tree.items() if v.get('record_type') == family)
        original = deepcopy(tree[path]['approval_use_refs'])
        for field, value in [('task_id', 'another'), ('target', 'another'),
                             ('effect_digest', 'e'*64), ('approval_id', 'new')]:
            tree[path]['approval_use_refs'] = [reference(**{field: value})]
            with pytest.raises(RecordSchemaClosureError) as caught:
                validate_record_schema_closure(tree, **SCOPE)
            assert caught.value.code == 'approval_use_restatement'
        tree[path]['approval_use_refs'] = original


@pytest.mark.parametrize('enabled', [False, True])
def test_three_projection_entrances_read_expired_revoked_history_without_host_io(scene, tmp_path, monkeypatch, enabled):
    from app.facts_tree.projection_advance import build_standard_generation_for_commit
    from app.facts_tree.projection_incremental import build_replayed_generation
    from app.facts_tree.projection_decode import decode_generation_records, decode_rows
    s = scene
    accepted = run(s)
    assert accepted.status == 'accepted'
    s.clock[0] = at(400)
    s.host.credentials.revoke_task_credential(s.grant.secret, now=at(100), task_id=s.cred.task_id)
    def forbidden(*a, **k):
        pytest.fail('history read current host authority')
    for obj, method in [(s.consumers, '_current'), (s.consumers, 'get_current_status'),
                        (HostApprovalEvidence, '_verify')]:
        monkeypatch.setattr(obj, method, forbidden)
    expected = dict(expected_repo_id=SCOPE['repo_id'], expected_company_id=SCOPE['company_id'])
    single = build_standard_generation_for_commit(s.repo, accepted.accepted_commit, tmp_path/'single', **expected)
    replay = build_replayed_generation(s.repo, s.repo.zero_oid, accepted.accepted_commit, tmp_path/'replay', **expected)
    assert single.render_sha256 == replay.render_sha256
    with sqlite3.connect(single.db_path) as conn:
        decoded = decode_generation_records(conn, authz_digest='history-read')
        op = next(r for r in decoded.records if r.family == 'operation')
        assert op.payload['approval_use_refs'] == [s.ref]
        rows = conn.execute('SELECT path,family,byte_length,sha256 FROM record_canonical').fetchall()
        assert decode_rows(conn, rows, authz_digest='history-read') == decoded
    # A new unrelated operation has no live approval requirement. Old refs do
    # not consult the injected expired/revoked host chain.
    if not enabled:
        s.gate = BrokerFinalGate(s.repo, s.coord, s.store)
    new_id = 'unrelated-operation'
    scope = (*SCOPE.values(), new_id)
    with s.db:
        s.db.execute('BEGIN')
        s.store.record_operation(*scope, 'unrelated-task', 'unrelated-key', TIME, {})
        s.store.mark_operation_prepared(*scope, 'unrelated-prepared', TIME)
    def assemble(head, snapshot):
        records = {r.path:r.content for r in snapshot.records}
        op = git_format.loads_record(records[git_format.record_path('operation', operation_id=s.prepared.operation_id)])
        op.update(operation_id=new_id, base_commit=head, candidate_refs=[],
                  disposition_result=[], formal_fact_refs=[], event_ids=[], approval_use_refs=[])
        records[git_format.record_path('operation', operation_id=new_id)] = git_format.dumps_record(op)
        return AssembledAttempt(records, {}, EFFECT, {})
    result = s.gate.accept(lease=s.lease, operation_id=new_id, effect=EFFECT, assemble=assemble, recorded_at=TIME)
    assert result.status == 'accepted', result
    assert count(s) == 1


@pytest.mark.parametrize('phase,readmit', [('before_cas', 'unproved'),
    ('before_cas', 'isolated'), ('before_cas', 'expired'),
    ('before_cas', 'dependencies'), ('before_cas', 'stale'),
    ('before_cas', 'unisolated'), ('before_cas', 'proof_error'),
    ('before_cas', 'missing_refs'), ('after_cas', 'unproved')])
def test_real_sigkill_windows_reopen_and_reconcile(tmp_path, monkeypatch, phase, readmit):
    import json
    import select
    directory = tmp_path/'crash'
    child = subprocess.Popen([sys.executable, '-m', 'tests.disaster_drills.approval_use_e2_worker',
        str(directory), phase], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        assert select.select([child.stdout], [], [], 30)[0], 'worker did not reach kill window'
        ready = child.stdout.readline().strip()
        if not ready:
            pytest.fail('worker failed: ' + child.stderr.read())
        assert ready == 'READY ' + phase
        # The actual writer and ALL possible descendants lose execution/ref
        # ability together; no PID-only timeout proof is used on re-admission.
        os.killpg(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
        # bwrap's supervisor can be reaped before its SIGKILLed child has
        # disappeared from /proc. A zombie has no execution or open files;
        # require no LIVE member, rather than mistake a zombie for a writer.
        import time
        def live_group():
            live = []
            for stat in Path('/proc').glob('[0-9]*/stat'):
                try:
                    fields = stat.read_text().rsplit(')', 1)[1].split()
                except (OSError, IndexError):
                    continue
                if int(fields[2]) == child.pid and fields[0] != 'Z':
                    live.append(stat.parent.name)
            return live
        for _ in range(100):
            if not live_group():
                break
            time.sleep(0.01)
        assert not live_group()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=10)
    data = json.loads((directory/'transfer.json').read_text())
    assert data['worker_group'] == child.pid
    assert data['worker_pid'] != child.pid  # real worker inside bwrap
    db = sqlite3.connect(directory/'work.sqlite')
    lease = None
    try:
        host = stack(db)
        consumers = HostApprovalConsumptionService(db, host.reservations)
        repo = FactsGitRepository.open(directory/'facts.git', **SCOPE, installation_id='install-test')
        store = OperationWorkStore(db)
        coord = BrokerCoordination(directory/'coordination', SCOPE['repo_id'], db)
        lease = coord.claim_broker(takeover=True)
        evidence = HostApprovalEvidence(consumers, data['task_secret'],
            now=lambda: at(400 if readmit == 'expired' else 90))
        gate = BrokerFinalGate(repo, coord, store, approval_evidence=evidence)
        scope = (*SCOPE.values(), data['operation_id'])
        assert store.get_operation(*scope)['current_state'] == 'in_flight'
        assert db.execute('SELECT count(*) FROM host_approval_consumptions').fetchone()[0] == 1
        assert len(store.list_attempts(*scope)) == 1
        calls = []
        real = repo.accept
        def accept(*a, **k):
            calls.append(1); return real(*a, **k)
        monkeypatch.setattr(repo, 'accept', accept)
        records = {p:b.encode() for p,b in data['records'].items()}
        if readmit == 'missing_refs':
            path = git_format.record_path('operation', operation_id=data['operation_id'])
            op = git_format.loads_record(records[path]); op['approval_use_refs'] = []
            records[path] = git_format.dumps_record(op)
        assemble = lambda *_: AssembledAttempt(records, {}, EFFECT, DEPS)
        kwargs = dict(lease=lease, operation_id=data['operation_id'], effect=EFFECT,
                      assemble=assemble, recorded_at=TIME)
        if phase == 'before_cas':
            assert repo.head_oid() == data['initial_head']
            assert gate.accept(**kwargs).status == 'rejected'
        def isolation(_):
            if readmit == 'proof_error':
                raise RuntimeError('SECRET-ISOLATION-PROOF-ERROR')
            return (readmit != 'unisolated' and not live_group()
                    and child.returncode == -signal.SIGKILL and lease._pid == os.getpid())
        facts = VerifiedReentryFacts(repo.zero_oid if readmit == 'stale' else repo.head_oid(),
                                    True, readmit != 'dependencies')
        result = gate.reconcile_unknown(**kwargs,
            prove_executor_isolated=None if readmit == 'unproved' else isolation,
            reentry_facts=facts)
        assert 'SECRET-ISOLATION-PROOF-ERROR' not in repr(result)
        if phase == 'after_cas':
            assert result.status == 'accepted', result
            assert not calls
            assert gate.reconcile_unknown(**kwargs) == result
        elif readmit == 'isolated':
            assert result.status == 'accepted', result
            assert calls == [1]
        else:
            assert result.status in {'rejected', 'unknown'}, result
            assert store.get_operation(*scope)['current_state'] == 'unknown'
            assert not calls and repo.head_oid() == data['initial_head']
        assert len(store.list_attempts(*scope)) == 1
        assert db.execute('SELECT count(*) FROM host_approval_consumptions').fetchone()[0] == 1
        store.verify_integrity()
    finally:
        if lease is not None: lease.close()
        db.close()


def test_expiry_after_real_sqlite_write_lock_wait(scene, monkeypatch):
    import threading
    s = scene
    real = broker_tree_gate.validate_broker_tree
    rounds = []
    locked = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    errors = []
    def holder():
        try:
            other = sqlite3.connect(s.directory/'work.sqlite')
            other.execute('BEGIN IMMEDIATE')
            locked.set()
            assert release.wait(10)
            # Clock changes while the gate is waiting to acquire its DB lock.
            s.clock[0] = at(301)
            other.commit(); other.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()
    def validate(*a, **k):
        result = real(*a, **k)
        rounds.append(1)
        if len(rounds) == 2:
            threading.Thread(target=holder).start()
            assert locked.wait(5)
        return result
    # Trace runs immediately before the real BEGIN attempts the held lock.
    # Release asynchronously while SQLite's real busy wait is in progress.
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE' and locked.is_set() and not completed.is_set():
            threading.Timer(0.1, release.set).start()
    s.db.set_trace_callback(trace)
    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validate)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    try:
        result = run(s)
        assert completed.wait(5) and not errors
    finally:
        release.set(); s.db.set_trace_callback(None)
    assert result.status == 'unknown', result
    assert not calls and count(s) == 1


def test_real_joint_sqlite_commit_failure_rolls_back_consumption_and_intent(scene, monkeypatch):
    s = scene
    s.db.execute('PRAGMA foreign_keys=ON')
    s.db.executescript('''
        CREATE TABLE e2_fault_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE e2_fault_child(parent_id INTEGER REFERENCES e2_fault_parent(id)
                                   DEFERRABLE INITIALLY DEFERRED);
        CREATE TRIGGER e2_defer_commit_failure AFTER INSERT ON fact_operation_attempt_events
        WHEN NEW.state='accepting'
        BEGIN INSERT INTO e2_fault_child VALUES (12345); END;
    ''')
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'rejected' and result.reason == 'approval_evidence_invalid', result
    assert not calls and not s.db.in_transaction
    assert count(s) == count(s, 'fact_operation_attempts') == count(s, 'e2_fault_child') == 0
    assert count(s, 'host_approval_reservations') == 1
    assert s.repo.head_oid() == s.initial_head


@pytest.mark.parametrize('failure', ['missing', 'view', 'guard', 'clock', 'reference', 'task_secret'])
def test_authority_unavailable_binding_conflict_and_secret_errors_fail_closed(scene, monkeypatch, failure):
    s = scene
    secret = 'SECRET-HOST-ERROR-SHOULD-NOT-ESCAPE'
    if failure in {'missing', 'view'}:
        s.db.execute('DROP TABLE host_approval_consumptions')
        if failure == 'view':
            s.db.execute('CREATE VIEW host_approval_consumptions AS SELECT 1')
    elif failure == 'guard':
        name = s.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='host_approval_consumptions'").fetchone()[0]
        s.db.execute('DROP TRIGGER "' + name + '"')
    elif failure == 'clock':
        def fail():
            raise RuntimeError(secret)
        s.gate.approval_evidence._now = fail
    elif failure == 'task_secret':
        s.gate.approval_evidence._task_secret = secret
    else:
        path = git_format.record_path('operation', operation_id=s.prepared.operation_id)
        op = git_format.loads_record(s.records[path])
        op['approval_use_refs'][0]['approval_id'] = 'another-operation-approval'
        s.records[path] = git_format.dumps_record(op)
    s.db.commit()
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'rejected', result
    assert result.reason.startswith('approval_') and secret not in repr(result)
    assert not calls and s.repo.head_oid() == s.initial_head
    assert count(s, 'fact_operation_attempts') == 0


def test_consumed_row_without_intent_is_never_permission(scene, monkeypatch):
    s = scene
    s.consumers.record_admission(s.cred.secret, now=at(75),
        operation_id=s.prepared.operation_id, reservation_id=s.reservation.reservation_id,
        approval_id=s.reservation.approval_id, expected_effect_digest=s.prepared.effect_digest,
        expected_preparation_digest=s.prepared.preparation_digest)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'rejected' and result.reason == 'approval_already_consumed'
    assert not calls and count(s) == 1 and count(s, 'fact_operation_attempts') == 0


def test_lost_cas_new_base_cannot_reuse_original_approval(scene, monkeypatch):
    s = scene
    real = s.repo.accept
    calls = []
    def racing(head, commit, **kwargs):
        calls.append(1)
        records = {r.path:r.content for r in s.repo.read_snapshot_at(head).records}
        tree = s.repo.build_tree(records.items(), expected_paths=records)
        competitor = s.repo.create_commit(tree, head, message='advance base')
        assert real(head, competitor, expected_paths=records).accepted
        return real(head, commit, **kwargs)
    monkeypatch.setattr(s.repo, 'accept', racing)
    def assemble(head, snapshot):
        records = dict(s.records)
        for path in (git_format.record_path('operation', operation_id=s.prepared.operation_id), EVENT):
            payload = git_format.loads_record(records[path]); payload['base_commit'] = head
            records[path] = git_format.dumps_record(payload)
        return AssembledAttempt(records, {}, EFFECT, DEPS)
    result = run(s, assemble=assemble)
    assert result.status == 'rejected', result
    assert calls == [1] and count(s) == 1
    assert len(s.store.list_attempts(*s.scope)) == 1
    assert s.store.get_operation(*s.scope)['current_state'] != 'accepted'


def test_real_post_intent_git_product_corruption_rejected(scene, monkeypatch):
    s = scene
    real = broker_tree_gate.validate_broker_tree
    rounds = []
    def validate(*args, **kwargs):
        result = real(*args, **kwargs)
        rounds.append(1)
        if len(rounds) == 2:
            # Corrupt the actual already-written commit object, preserving the
            # caller's captured records. This is distinct from external map
            # mutation and must be caught by terminal product inspection.
            oid = kwargs['commit_oid']
            obj = s.directory/'facts.git'/'objects'/oid[:2]/oid[2:]
            obj.chmod(0o600)
            obj.write_bytes(b'not a zlib Git object')
        return result
    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validate)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'unknown', result
    assert len(rounds) == 2 and not calls and count(s) == 1
    assert s.repo.head_oid() == s.initial_head


def test_artifact_contract_preserves_opaque_nested_business_fields():
    path = git_format.record_path('taxonomy_leaf', pack_version='p', leaf_id='l')
    before = {path:git_format.dumps_record({'operation_id':'operation-1', 'approval_use_refs':['business']})}
    after = {path:git_format.dumps_record({'operation_id':'other', 'approval_use_refs':['changed']})}
    assert approval_artifact_digests(before, {}, 'operation-1') != approval_artifact_digests(after, {}, 'operation-1')


def test_artifact_contract_binds_removed_mutable_paths():
    path = git_format.record_path('entity_current', entity_uid='removed')
    baseline = {path:git_format.dumps_record({'revision':1})}
    assert approval_artifact_digests({}, baseline, 'operation-1') == {path:None}
    assert approval_artifact_digests(baseline, baseline, 'operation-1') == {}


@pytest.mark.parametrize('enabled', [False, True])
def test_another_new_operation_cannot_smuggle_refs_as_history(scene, enabled):
    s = scene
    own_path = git_format.record_path('operation', operation_id=s.prepared.operation_id)
    own = git_format.loads_record(s.records[own_path]); own['approval_use_refs'] = []
    s.records[own_path] = git_format.dumps_record(own)
    extra = deepcopy(own)
    extra.update(operation_id='smuggled-operation', candidate_refs=[], disposition_result=[],
                 formal_fact_refs=[], event_ids=[], approval_use_refs=[reference(operation_id='smuggled-operation')])
    s.records[git_format.record_path('operation', operation_id='smuggled-operation')] = git_format.dumps_record(extra)
    if not enabled:
        s.gate = BrokerFinalGate(s.repo, s.coord, s.store)
    result = run(s)
    assert result.status == 'rejected', result
    assert result.reason == ('approval_reference_mismatch' if enabled else 'approval_evidence_unavailable')
    assert s.repo.head_oid() == s.initial_head and count(s, 'fact_operation_attempts') == 0


def test_four_family_contract_does_not_interpret_opaque_taxonomy_fields():
    tree = full_tree()
    tree[META] = dict(SCOPE, schema_version=1, format_version=1, installation_id='install-test')
    tree[git_format.record_path('taxonomy_leaf', pack_version='pack', leaf_id='leaf')] = {
        'code':'leaf', 'approval_use_refs':'opaque taxonomy business field'}
    validate_record_schema_closure(tree, **SCOPE)


def test_required_approval_cannot_be_bypassed_by_erasing_refs(scene, monkeypatch):
    s = scene
    path = git_format.record_path('operation', operation_id=s.prepared.operation_id)
    op = git_format.loads_record(s.records[path]); op['approval_use_refs'] = []
    s.records[path] = git_format.dumps_record(op)
    calls = []
    monkeypatch.setattr(s.repo, 'accept', lambda *a, **k: calls.append(1))
    result = run(s)
    assert result.status == 'rejected' and result.reason == 'approval_reference_mismatch'
    assert not calls and count(s) == count(s, 'fact_operation_attempts') == 0


def test_real_host_prepared_opaque_manifest_is_unexplained_and_refused(tmp_path):
    s = make_scene(tmp_path/'opaque-host', manifest_override={'opaque': ['unexplained', 1]})
    try:
        result = run(s)
        assert result.status == 'rejected' and result.reason == 'approval_effect_unexplained'
        assert s.repo.head_oid() == s.initial_head
        assert count(s) == count(s, 'fact_operation_attempts') == 0
        assert count(s, 'host_approval_reservations') == 1
    finally:
        s.lease.close(); s.db.close()
