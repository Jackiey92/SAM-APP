"""B4-1 integration against real offline Git, SQLite and coordination locks."""
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.facts_tree import broker_tree_gate, git_format, operation_acceptance
from app.facts_tree.broker_coordination import BrokerCoordination, BrokerOccupied
from app.facts_tree.broker_final_gate import AssembledAttempt, BrokerFinalGate
from app.facts_tree.git_repository import FactsGitRepository, CasResult, GitRepositoryError
from app.facts_tree.operation_work_store import OperationWorkStore
from tests.facts_tree_full_fixture import full_tree, SCOPE


TIME = '2026-11-01T12:00:00+08:00'
EFFECT = {'opaque': ['effect', 1]}
DIGEST = sha256(git_format.dumps_record(EFFECT)).hexdigest()
OP = git_format.record_path('operation', operation_id='operation-1')
EVENT = git_format.record_path('event', event_id='event-1')
SCOPE_ARGS = (SCOPE['repo_id'], SCOPE['company_id'], 'operation-1')


@pytest.fixture
def setup(tmp_path):
    repo = FactsGitRepository.initialize(tmp_path / 'facts.git', **SCOPE,
                                         installation_id='installation-test')
    db = sqlite3.connect(tmp_path / 'work.sqlite')
    store = OperationWorkStore(db)
    with db:
        db.execute('BEGIN')
        store.record_operation(*SCOPE_ARGS, 'task-1', 'key-1', TIME, {})
        store.mark_operation_prepared(*SCOPE_ARGS, 'prepared-1', TIME)
    coordination = BrokerCoordination(tmp_path / 'coordination', SCOPE['repo_id'], db)
    lease = coordination.claim_broker()
    gate = BrokerFinalGate(repo, coordination, store)
    yield repo, db, store, coordination, lease, gate
    lease.close()
    db.close()


def assembler(head, snapshot):
    tree = full_tree()
    tree[OP].update(base_commit=head, effect_digest=DIGEST)
    tree[EVENT]['base_commit'] = head
    records = {r.path: r.content for r in snapshot.records}
    records.update({p: git_format.dumps_record(v) for p, v in tree.items()})
    return AssembledAttempt(records, {'opaque': 'plan'}, EFFECT.copy(), {'version': head})


def run(setup, **kwargs):
    return setup[-1].accept(lease=setup[-2], operation_id='operation-1',
                           effect=kwargs.pop('effect', EFFECT),
                           assemble=kwargs.pop('assemble', assembler), recorded_at=TIME,
                           **kwargs)


def counts(store):
    return tuple(store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                 for table in ('fact_operation_attempts', 'fact_operation_work_events',
                               'fact_operation_attempt_events'))


def spy(monkeypatch, obj, method, log):
    real = getattr(obj, method)
    def wrapped(*args, **kwargs):
        log.append(method)
        return real(*args, **kwargs)
    monkeypatch.setattr(obj, method, wrapped)
    return real


def test_first_acceptance_real_validation_order_and_idempotency(setup, monkeypatch):
    repo, db, store, _, _, _ = setup
    log = []
    for obj, method in ((repo, 'build_tree'), (repo, 'create_commit'),
                        (store, 'admit_execution'), (broker_tree_gate, 'validate_broker_tree'),
                        (repo, 'accept'), (store, 'mark_accepted')):
        spy(monkeypatch, obj, method, log)
    real = repo.accept
    def durable(*args, **kwargs):
        assert not db.in_transaction
        # A separate connection observes the intent before any CAS.
        other = sqlite3.connect(db.execute('PRAGMA database_list').fetchone()[2])
        try:
            assert other.execute('SELECT state FROM fact_operation_work_events ORDER BY seq DESC LIMIT 1').fetchone()[0] == 'in_flight'
            assert other.execute('SELECT state FROM fact_operation_attempt_events ORDER BY seq DESC LIMIT 1').fetchone()[0] == 'accepting'
        finally:
            other.close()
        return real(*args, **kwargs)
    monkeypatch.setattr(repo, 'accept', durable)
    result = run(setup)
    assert result.status == 'accepted', result
    assert log == ['build_tree', 'create_commit', 'validate_broker_tree', 'admit_execution', 'validate_broker_tree', 'accept', 'mark_accepted']
    assert store.get_operation(*SCOPE_ARGS)['accepted_commit'] == result.accepted_commit == repo.head_oid()
    store.verify_integrity()
    before = counts(store)
    log.clear()
    replay = run(setup, assemble=lambda *_: pytest.fail('replay assembled'))
    assert replay.status == 'idempotent_replay'
    assert replay.accepted_commit == result.accepted_commit
    assert not log and counts(store) == before
    with pytest.raises(FrozenInstanceError):
        replay.status = 'accepted'
    assert run(setup, effect={'different': True}).status == 'rejected'
    assert counts(store) == before and not log


def competitor(repo):
    head = repo.head_oid()
    snap = repo.read_snapshot_at(head)
    records = {r.path: r.content for r in snap.records}
    tree = repo.build_tree(records.items(), expected_paths=records)
    commit = repo.create_commit(tree, head, message='competitor')
    assert repo.accept(head, commit, expected_paths=records).accepted
    return commit


@pytest.mark.parametrize('losses', [1, 2])
def test_competition_fresh_objects_two_round_budget(setup, monkeypatch, losses):
    repo, _, store, _, _, _ = setup
    original_accept = repo.accept
    attempts, assembled_heads, trees, commits = [], [], [], []
    original_build, original_commit = repo.build_tree, repo.create_commit
    def build(*args, **kwargs):
        result = original_build(*args, **kwargs)
        trees.append(result)
        return result
    def commit(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        commits.append((args[1], result))
        return result
    def racing(head, new, **kwargs):
        attempts.append((head, new))
        if len(attempts) <= losses:
            # Use original methods for the competing writer to keep spies exact.
            monkeypatch.setattr(repo, 'accept', original_accept)
            monkeypatch.setattr(repo, 'build_tree', original_build)
            monkeypatch.setattr(repo, 'create_commit', original_commit)
            competitor(repo)
            monkeypatch.setattr(repo, 'accept', racing)
            monkeypatch.setattr(repo, 'build_tree', build)
            monkeypatch.setattr(repo, 'create_commit', commit)
        return original_accept(head, new, **kwargs)
    def assemble(head, snap):
        assembled_heads.append(head)
        return assembler(head, snap)
    monkeypatch.setattr(repo, 'accept', racing)
    monkeypatch.setattr(repo, 'build_tree', build)
    monkeypatch.setattr(repo, 'create_commit', commit)
    result = run(setup, assemble=assemble)
    assert result.status == ('accepted' if losses == 1 else 'retry_wait'), result
    assert len(attempts) == len(assembled_heads) == len(trees) == len(commits) == 2
    assert len(set(trees)) == len({c for _, c in commits}) == 2
    assert [h for h, _ in commits] == assembled_heads
    assert len(set(assembled_heads)) == 2
    assert len(store.list_attempts(*SCOPE_ARGS)) == 2
    events = store.list_operation_events(*SCOPE_ARGS)
    assert [e['state'] for e in events] == ['prepared', 'in_flight', 'reserved', 'in_flight', result.status]
    if losses == 2:
        assert events[-1]['detail']['reason'] == 'lost_cas_exhausted'
        assert run(setup).status == 'rejected'
        assert len(attempts) == 2
    store.verify_integrity()


@pytest.mark.parametrize('failure', ['tree', 'stale_parent', 'intent', 'lease'])
def test_fail_closed_before_cas(setup, monkeypatch, failure):
    repo, db, store, _, lease, _ = setup
    head = repo.head_oid()
    called = []
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: called.append(1))
    assemble = assembler
    if failure == 'tree':
        def assemble(head, snap):
            result = assembler(head, snap)
            records = dict(result.records)
            payload = git_format.loads_record(records[OP])
            del payload['actor']
            records[OP] = git_format.dumps_record(payload)
            return replace(result, records=records)
    elif failure == 'stale_parent':
        def stale(*a, **k):
            from app.facts_tree.git_repository import StaleBaseError
            raise StaleBaseError('parent is stale')
        monkeypatch.setattr(repo, 'create_commit', stale)
    elif failure == 'intent':
        def fail(*a, **k):
            raise RuntimeError('intent insert failed')
        monkeypatch.setattr(store, 'admit_execution', fail)
    else:
        lease.close()
        assert run(setup).status == 'rejected'
        assert not called
        return
    result = run(setup, assemble=assemble)
    assert result.status == 'rejected'
    assert not called and repo.head_oid() == head and not db.in_transaction
    assert store.get_operation(*SCOPE_ARGS)['current_state'] != 'accepted'
    if failure == 'intent':
        assert counts(store) == (0, 1, 0)
    store.verify_integrity()


@pytest.mark.parametrize('failure', ['git_error', 'unknown_result', 'not_successor', 'corrupt_chain', 'accepted_elsewhere', 'effect_changed'])
def test_loss_evidence_failures_park_unknown_without_retry(setup, monkeypatch, failure):
    repo, _, store, _, _, _ = setup
    called = []
    assembled = []
    real_accept = repo.accept
    def assemble(head, snap):
        result = assembler(head, snap)
        # Isolate a mutable callback-owned object to test post-CAS effect drift.
        result = replace(result, effect={'opaque': ['effect', 1]})
        assembled.append(result)
        return result
    def cas(head, commit, **kwargs):
        called.append(1)
        if failure == 'git_error':
            raise GitRepositoryError('persistence unknown')
        if failure == 'unknown_result':
            return None
        if failure == 'accepted_elsewhere':
            assert real_accept(head, commit, **kwargs).accepted
            return CasResult(False, commit, head, commit)
        if failure == 'not_successor':
            return CasResult(False, head, head, commit)
        monkeypatch.setattr(repo, 'accept', real_accept)
        successor = competitor(repo)
        if failure == 'corrupt_chain':
            monkeypatch.setattr(repo, 'list_commit_chain', lambda *a: (_ for _ in ()).throw(GitRepositoryError('damaged chain')))
        if failure == 'effect_changed':
            assembled[-1].effect['opaque'].append('changed')
        return CasResult(False, successor, head, commit)
    monkeypatch.setattr(repo, 'accept', cas)
    result = run(setup, assemble=assemble)
    assert result.status == 'unknown', result
    assert len(called) == len(assembled) == 1
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    assert len(store.list_attempts(*SCOPE_ARGS)) == 1
    store.verify_integrity()


@pytest.mark.parametrize('failure', ['locator', 'identity', 'head_changed'])
def test_initial_git_evidence_quarantines_without_writes(setup, monkeypatch, failure):
    repo, _, store, _, _, _ = setup
    before = counts(store)
    if failure == 'locator':
        def fail(*a, **k):
            raise operation_acceptance.OperationAcceptanceError('chain anomaly')
        monkeypatch.setattr(operation_acceptance, 'locate_operation_acceptance', fail)
    elif failure == 'identity':
        repo._expected_meta = {**repo._expected_meta, 'company_id': 'foreign'}
    else:
        real = operation_acceptance.locate_operation_acceptance
        monkeypatch.setattr(operation_acceptance, 'locate_operation_acceptance',
                            lambda *a, **k: replace(real(*a, **k), head_oid='f' * 40))
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('evidence bypass'))
    assert run(setup).status == 'rejected'
    assert counts(store) == before


def test_second_broker_and_lock_order_rejected(setup, monkeypatch):
    repo, db, _, coord, _, _ = setup
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('CAS without gate'))
    with pytest.raises(BrokerOccupied):
        coord.claim_broker()
    db.execute('BEGIN')
    assert run(setup).status == 'rejected'
    db.rollback()
    # A refusal releases the admission for the next ordinary entrance.
    with coord.admit('candidate_revise'):
        pass


def test_concurrent_duplicate_serialized_before_git_evidence(setup, monkeypatch, tmp_path):
    repo, db, store, coord, lease, _ = setup
    # Each thread owns its SQLite connection/domain and obtains the same
    # process-bound lease through the host. Coordination binds leases to domain,
    # so serialize two calls on the host's own thread via the common gate using
    # independent cooperating domains and matching host-issued lease handles.
    from app.facts_tree.broker_coordination import BrokerLease
    calls = []
    real = FactsGitRepository.accept
    def cas(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(FactsGitRepository, 'accept', cas)
    def invoke(_):
        connection = sqlite3.connect(tmp_path / 'work.sqlite')
        try:
            local_store = OperationWorkStore(connection)
            local_coord = BrokerCoordination(tmp_path / 'coordination', SCOPE['repo_id'], connection)
            local_repo = FactsGitRepository.open(repo.path, **SCOPE, installation_id='installation-test')
            # The trusted host passes its existing process-bound descriptor;
            # neither thread claims or takes over another Broker.
            local_lease = BrokerLease(local_coord, lease.owner_id, lease.generation, lease._fd)
            return BrokerFinalGate(local_repo, local_coord, local_store).accept(
                lease=local_lease, operation_id='operation-1', effect=EFFECT,
                assemble=assembler, recorded_at=TIME)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sorted(r.status for r in results) == ['accepted', 'idempotent_replay']
    assert len(calls) == 1 and len(store.list_attempts(*SCOPE_ARGS)) == 1
    assert results[0].accepted_commit == results[1].accepted_commit


def test_silent_intent_rollback_cannot_reach_cas(setup, monkeypatch):
    from contextlib import contextmanager
    from app.facts_tree.broker_coordination import Admission
    repo, db, store, _, _, _ = setup
    @contextmanager
    def lost_transaction(self):
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.rollback()
    monkeypatch.setattr(Admission, 'work_transaction', lost_transaction)
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('undurable intent CAS'))
    result = run(setup)
    assert result.status == 'rejected'
    assert counts(store) == (0, 1, 0)


def test_old_patch_and_second_round_effect_change_rejected(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    first_head = repo.head_oid()
    calls = []
    real_accept = repo.accept
    def racing(head, commit, **kwargs):
        calls.append(1)
        monkeypatch.setattr(repo, 'accept', real_accept)
        competitor(repo)
        return real_accept(head, commit, **kwargs)
    monkeypatch.setattr(repo, 'accept', racing)
    def old_patch(head, snapshot):
        # A formerly legal complete tree must not be replayed at a new parent.
        return assembler(first_head, snapshot)
    result = run(setup, assemble=old_patch)
    assert result.status == 'rejected' and len(calls) == 1
    assert len(store.list_attempts(*SCOPE_ARGS)) == 1
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'reserved'
    assert run(setup).status == 'rejected'
    store.verify_integrity()


def test_post_intent_validator_refusal_stops_cas(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    real = broker_tree_gate.validate_broker_tree
    calls = []
    def validator(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise broker_tree_gate.BrokerTreeGateError('post-intent refusal')
        return real(*a, **k)
    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', validator)
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('B3 bypass'))
    result = run(setup)
    assert result.status == 'unknown' and len(calls) == 2
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    store.verify_integrity()
