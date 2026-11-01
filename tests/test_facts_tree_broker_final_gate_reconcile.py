"""B4-2 reconcile_unknown drills: Git convergence and isolated re-admission.

Every test runs against the real offline Git repository, the real SQLite work
store (with its append-only triggers and joint-history verification) and the
real flock coordination ledger. Host-only evidence is simulated by injected
predicates/facts; no subprocess fencing lives in this slice.
"""
import hashlib
import sqlite3
from dataclasses import replace

import pytest

from app.facts_tree import git_format, operation_acceptance
from app.facts_tree.broker_coordination import BrokerCoordination
from app.facts_tree.broker_final_gate import (
    AssembledAttempt, BrokerFinalGate, ReentryEvidence, VerifiedReentryFacts)
from app.facts_tree.git_repository import FactsGitRepository, GitRepositoryError
from app.facts_tree.operation_work_store import OperationWorkStore
from tests.facts_tree_full_fixture import full_tree, SCOPE


TIME = '2026-11-01T12:00:00+08:00'
EFFECT = {'opaque': ['effect', 1]}
DIGEST = hashlib.sha256(git_format.dumps_record(EFFECT)).hexdigest()
EFFECT2 = {'opaque': ['effect', 2]}
DIGEST2 = hashlib.sha256(git_format.dumps_record(EFFECT2)).hexdigest()
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


def make_assembler(effect=EFFECT, digest=DIGEST):
    def assemble(head, snapshot):
        tree = full_tree()
        tree[OP].update(base_commit=head, effect_digest=digest)
        tree[EVENT]['base_commit'] = head
        records = {r.path: r.content for r in snapshot.records}
        records.update({p: git_format.dumps_record(v) for p, v in tree.items()})
        return AssembledAttempt(records, {'opaque': 'plan'}, effect.copy(),
                                {'version': head})
    return assemble


def counts(store):
    return tuple(store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                 for table in ('fact_operation_attempts', 'fact_operation_work_events',
                               'fact_operation_attempt_events'))


def competitor(repo):
    head = repo.head_oid()
    snap = repo.read_snapshot_at(head)
    records = {r.path: r.content for r in snap.records}
    tree = repo.build_tree(records.items(), expected_paths=records)
    commit = repo.create_commit(tree, head, message='competitor')
    assert repo.accept(head, commit, expected_paths=records).accepted
    return commit


def park_unknown(setup, monkeypatch, *, land=False, effect=EFFECT, digest=DIGEST):
    """Drive the existing final gate into a real paired unknown mirror."""
    repo = setup[0]
    gate = setup[5]
    lease = setup[4]
    real_accept = repo.accept
    cas_calls = []

    def ambiguous(head, commit, **kwargs):
        cas_calls.append(1)
        if land:
            # The commit really reached the ref; the reporter still cannot
            # observe the outcome, so the gate must park unknown.
            assert real_accept(head, commit, **kwargs).accepted
        return None

    monkeypatch.setattr(repo, 'accept', ambiguous)
    result = gate.accept(lease=lease, operation_id='operation-1', effect=effect,
                         assemble=make_assembler(effect, digest), recorded_at=TIME)
    assert result.status == 'unknown', result
    monkeypatch.setattr(repo, 'accept', real_accept)
    return result, cas_calls


def unknown_attempt(setup):
    store = setup[2]
    attempts = [a for a in store.list_attempts(*SCOPE_ARGS)
                if store.get_attempt(*SCOPE_ARGS, a['attempt_id'])['current_state']
                == 'unknown']
    assert len(attempts) == 1
    return store.get_attempt(*SCOPE_ARGS, attempts[0]['attempt_id'])


def block_writes(monkeypatch, repo):
    def fail(name):
        def wrapped(*args, **kwargs):
            pytest.fail('unexpected ' + name)
        monkeypatch.setattr(repo, name, wrapped)
    for name in ('accept', 'build_tree', 'create_commit'):
        fail(name)


def reconcile(setup, *, predicate=None, facts='default', **kwargs):
    repo, _, _, _, lease, gate = setup
    if facts == 'default':
        facts = VerifiedReentryFacts(repo.head_oid(), True, True)
    kwargs.setdefault('effect', EFFECT)
    kwargs.setdefault('assemble', make_assembler())
    return gate.reconcile_unknown(
        lease=lease, operation_id='operation-1', recorded_at=TIME,
        prove_executor_isolated=predicate, reentry_facts=facts, **kwargs)


def op_states(store):
    return [e['state'] for e in store.list_operation_events(*SCOPE_ARGS)]


def att_states(store, attempt_id):
    return [e['state'] for e in
            store.list_attempt_events(*SCOPE_ARGS, attempt_id)]


# -- exit A: converge on Git-proven acceptance without a second CAS ----------

def test_exit_a_converges_on_chain_acceptance_with_zero_second_cas(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch, land=True)
    commit = repo.head_oid()
    attempt = unknown_attempt(setup)
    before = counts(store)
    block_writes(monkeypatch, repo)

    result = reconcile(setup)

    assert result.status == 'accepted', result
    assert result.accepted_commit == commit == repo.head_oid()
    assert result.attempt_id == attempt['attempt_id']
    assert op_states(store) == ['prepared', 'in_flight', 'unknown', 'accepted']
    events = store.list_operation_events(*SCOPE_ARGS)
    assert events[-1]['accepted_commit'] == commit
    assert events[-1]['detail'] == {'reason': 'unknown_reconciled_on_chain'}
    assert att_states(store, attempt['attempt_id']) == [
        'prepared', 'accepting', 'unknown', 'accepted']
    assert len(store.list_attempts(*SCOPE_ARGS)) == 1
    assert counts(store) == (before[0], before[1] + 1, before[2] + 1)
    assert store.get_operation(*SCOPE_ARGS)['accepted_commit'] == commit
    store.verify_integrity()


def test_exit_a_different_on_chain_effect_is_rejected(setup, monkeypatch):
    _, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch, land=True, effect=EFFECT2, digest=DIGEST2)
    before = counts(store)
    block_writes(monkeypatch, setup[0])

    result = reconcile(
        setup, effect=EFFECT,
        predicate=lambda evidence: pytest.fail('isolation proof not consultable'),
        facts=None)

    assert result.status == 'rejected'
    assert 'effect differs' in result.reason
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    assert counts(store) == before
    store.verify_integrity()


def test_exit_a_locator_anomaly_quarantines_without_writes(setup, monkeypatch):
    _, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    before = counts(store)

    def fail(*args, **kwargs):
        raise operation_acceptance.OperationAcceptanceError('chain anomaly')

    monkeypatch.setattr(operation_acceptance, 'locate_operation_acceptance', fail)
    block_writes(monkeypatch, setup[0])

    result = reconcile(setup)

    assert result.status == 'rejected'
    assert 'Git evidence refused' in result.reason
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    assert counts(store) == before
    store.verify_integrity()


# -- exit B: host isolation/authorization gates -----------------------------

@pytest.mark.parametrize('case', ['missing', 'false', 'raising'])
def test_exit_b_predicate_gate_failures_keep_unknown_without_cas(setup, monkeypatch, case):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    before = counts(store)
    cas_calls = []
    monkeypatch.setattr(repo, 'accept',
                        lambda *a, **k: cas_calls.append(1) or pytest.fail('CAS denied'))
    if case == 'missing':
        predicate = None
    elif case == 'false':
        predicate = lambda evidence: False
    else:
        def predicate(evidence):
            raise RuntimeError('host verifier unavailable')

    result = reconcile(setup, predicate=predicate)

    assert result.status == 'rejected', result
    assert not cas_calls
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    assert counts(store) == before
    store.verify_integrity()


def test_exit_b_full_readmission_runs_one_complete_gate(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    attempt = unknown_attempt(setup)
    head = repo.head_oid()
    seen = {}
    cas_log, build_log, commit_log = [], [], []
    real_accept = repo.accept
    real_build = repo.build_tree
    real_commit = repo.create_commit

    def predicate(evidence):
        seen['evidence'] = evidence
        return True

    def cas(head_arg, commit_arg, **kwargs):
        cas_log.append(1)
        return real_accept(head_arg, commit_arg, **kwargs)

    def build(*args, **kwargs):
        build_log.append(1)
        return real_build(*args, **kwargs)

    def commit_obj(*args, **kwargs):
        commit_log.append(1)
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(repo, 'accept', cas)
    monkeypatch.setattr(repo, 'build_tree', build)
    monkeypatch.setattr(repo, 'create_commit', commit_obj)

    result = reconcile(setup, predicate=predicate,
                       facts=VerifiedReentryFacts(head, True, True))

    assert result.status == 'accepted', result
    assert result.accepted_commit == repo.head_oid()
    assert result.attempt_id == attempt['attempt_id']
    assert (len(cas_log), len(build_log), len(commit_log)) == (1, 1, 1)
    evidence = seen['evidence']
    assert isinstance(evidence, ReentryEvidence)
    assert (evidence.operation_id == 'operation-1'
            and evidence.attempt_id == attempt['attempt_id']
            and evidence.unknown_base_commit == attempt['base_commit']
            and evidence.head_oid == head)
    op_events = store.list_operation_events(*SCOPE_ARGS)
    assert [e['state'] for e in op_events] == [
        'prepared', 'in_flight', 'unknown', 'in_flight', 'accepted']
    assert op_events[3]['detail'] == {
        'reason': 'unknown_re_admission', 'head_oid': head,
        'unknown_base_commit': attempt['base_commit']}
    assert att_states(store, attempt['attempt_id']) == [
        'prepared', 'accepting', 'unknown', 'accepting', 'accepted']
    attempts = store.list_attempts(*SCOPE_ARGS)
    assert len(attempts) == 1
    live = [a for a in attempts
            if store.get_attempt(*SCOPE_ARGS, a['attempt_id'])['current_state']
            not in ('stale', 'retry_wait')]
    assert len(live) == 1
    assert op_events[-1]['accepted_commit'] == result.accepted_commit
    store.verify_integrity()


@pytest.mark.parametrize('case', ['locator', 'range', 'different_effect'])
def test_exit_b_empty_chain_proof_breakage_refused(setup, monkeypatch, case):
    repo, _, store, _, _, _ = setup
    if case == 'different_effect':
        park_unknown(setup, monkeypatch, land=True, effect=EFFECT2, digest=DIGEST2)
    else:
        park_unknown(setup, monkeypatch)
    before = counts(store)
    proof = []

    def sentinel(evidence):
        proof.append(1)
        pytest.fail('isolation proof must not be consulted')

    if case == 'locator':
        def fail(*args, **kwargs):
            raise operation_acceptance.OperationAcceptanceError('chain anomaly')
        monkeypatch.setattr(operation_acceptance, 'locate_operation_acceptance', fail)
    elif case == 'range':
        def broken(*args, **kwargs):
            raise GitRepositoryError('range unreadable')
        monkeypatch.setattr(repo, 'list_commit_chain', broken)
    cas_calls = []
    monkeypatch.setattr(repo, 'accept',
                        lambda *a, **k: cas_calls.append(1) or pytest.fail('CAS denied'))

    result = reconcile(setup, predicate=sentinel, effect=EFFECT)

    assert result.status == 'rejected'
    assert not proof and not cas_calls
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
    assert counts(store) == before
    store.verify_integrity()


def test_exit_b_facts_must_be_head_bound_true_facts(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    before = counts(store)
    proof = []

    def sentinel(evidence):
        proof.append(1)
        pytest.fail('isolation proof must not be consulted')

    good = VerifiedReentryFacts(repo.head_oid(), True, True)
    cases = [
        None,
        object(),
        replace(good, head_oid='0' * 40),
        replace(good, authorization_valid='yes'),
        replace(good, dependencies_valid=False),
        replace(good, authorization_valid=False),
    ]
    cas_calls = []
    monkeypatch.setattr(repo, 'accept',
                        lambda *a, **k: cas_calls.append(1) or pytest.fail('CAS denied'))
    for facts in cases:
        proof.clear()
        result = reconcile(setup, predicate=sentinel, facts=facts)
        assert result.status == 'rejected', (facts, result)
        assert not proof and not cas_calls
        assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'
        assert counts(store) == before
    store.verify_integrity()


def test_readmission_two_definite_losses_park_terminal_without_third_round(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    original_accept = repo.accept
    losses = []

    def racing(head, commit, **kwargs):
        losses.append(1)
        monkeypatch.setattr(repo, 'accept', original_accept)
        competitor(repo)
        monkeypatch.setattr(repo, 'accept', racing)
        return original_accept(head, commit, **kwargs)

    monkeypatch.setattr(repo, 'accept', racing)
    proofs = []
    result = reconcile(setup, predicate=lambda evidence: proofs.append(1) or True)

    assert result.status == 'retry_wait', result
    assert result.reason == 'lost_cas_exhausted'
    assert len(losses) == 2 and len(proofs) == 1
    assert op_states(store) == [
        'prepared', 'in_flight', 'unknown', 'in_flight', 'reserved',
        'in_flight', 'retry_wait']
    attempts = store.list_attempts(*SCOPE_ARGS)
    assert len(attempts) == 2
    assert att_states(store, attempts[0]['attempt_id']) == [
        'prepared', 'accepting', 'unknown', 'accepting', 'stale']
    assert att_states(store, attempts[1]['attempt_id']) == [
        'prepared', 'accepting', 'retry_wait']
    store.verify_integrity()

    # A terminal pair can never buy a third round: exit B requires unknown.
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('no third CAS'))
    again = reconcile(setup, predicate=lambda evidence: True)
    assert again.status == 'rejected'
    assert len(losses) == 2


def test_readmission_ambiguous_cas_parks_unknown_again_without_retry(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    attempt = unknown_attempt(setup)
    cas_calls = []
    monkeypatch.setattr(
        repo, 'accept',
        lambda *a, **k: (cas_calls.append(1), None)[1])

    result = reconcile(setup, predicate=lambda evidence: True)

    assert result.status == 'unknown', result
    assert len(cas_calls) == 1
    assert op_states(store) == [
        'prepared', 'in_flight', 'unknown', 'in_flight', 'unknown']
    assert att_states(store, attempt['attempt_id']) == [
        'prepared', 'accepting', 'unknown', 'accepting', 'unknown']
    assert len(store.list_attempts(*SCOPE_ARGS)) == 1
    store.verify_integrity()

    # Failed isolation evidence still fails closed; no automatic retry CAS.
    again = reconcile(setup, predicate=lambda evidence: False)
    assert again.status == 'rejected'
    assert len(cas_calls) == 1
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'


# -- idempotency and the unchanged fresh contract ---------------------------

def test_reconcile_is_idempotent_after_convergence(setup, monkeypatch):
    repo, _, store, _, _, _ = setup
    park_unknown(setup, monkeypatch)
    first = reconcile(setup, predicate=lambda evidence: True)
    assert first.status == 'accepted'
    before = counts(store)
    block_writes(monkeypatch, repo)

    again = reconcile(
        setup, assemble=lambda *a: pytest.fail('no re-assembly'),
        predicate=lambda evidence: pytest.fail('no re-isolation'),
        facts=None)

    assert again.status == 'accepted'
    assert again.accepted_commit == first.accepted_commit
    assert again.attempt_id == first.attempt_id
    assert counts(store) == before
    store.verify_integrity()


def test_reconcile_is_idempotent_for_a_freshly_accepted_operation(setup, monkeypatch):
    repo, _, store, _, lease, gate = setup
    first = gate.accept(lease=lease, operation_id='operation-1', effect=EFFECT,
                        assemble=make_assembler(), recorded_at=TIME)
    assert first.status == 'accepted'
    before = counts(store)
    block_writes(monkeypatch, repo)

    again = gate.reconcile_unknown(
        lease=lease, operation_id='operation-1', effect=EFFECT,
        assemble=lambda *a: pytest.fail('no re-assembly'), recorded_at=TIME,
        prove_executor_isolated=lambda *a: pytest.fail('no re-isolation'),
        reentry_facts=None)

    assert again.status == 'accepted'
    assert again.accepted_commit == first.accepted_commit
    assert again.attempt_id == first.attempt_id
    assert counts(store) == before
    store.verify_integrity()


def test_fresh_entrance_still_refuses_an_unknown_operation(setup, monkeypatch):
    repo, _, store, _, lease, gate = setup
    park_unknown(setup, monkeypatch)
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('fresh CAS denied'))

    result = gate.accept(lease=lease, operation_id='operation-1', effect=EFFECT,
                         assemble=make_assembler(), recorded_at=TIME)

    assert result.status == 'rejected'
    assert 'fresh admission' in result.reason
    assert store.get_operation(*SCOPE_ARGS)['current_state'] == 'unknown'


# -- corrupted histories never pass the reconciliation reads ---------------

def _plant(setup, work):
    """Commit forged rows through a second connection, bypassing pairs."""
    db = setup[1]
    path = db.execute('PRAGMA database_list').fetchone()[2]
    connection = sqlite3.connect(path)
    try:
        raw = OperationWorkStore(connection)
        connection.execute('BEGIN IMMEDIATE')
        work(raw)
        connection.commit()
    finally:
        connection.close()


def _event_blob(detail=None):
    blob = git_format.dumps_record(detail or {})
    return blob, hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize('forge', ['op_only_accepted', 'commit_mismatch',
                                   'attempt_only_readmission'])
def test_corrupted_unknown_history_rejected_without_cas(setup, monkeypatch, forge):
    repo, _, store, coordination, _, _ = setup
    park_unknown(setup, monkeypatch)
    commit_a, commit_b = 'c' * 40, 'd' * 40

    def work(raw):
        if forge in ('op_only_accepted', 'commit_mismatch'):
            blob, sha = _event_blob()
            raw._insert_op_event(
                *SCOPE_ARGS, 4, 'forge-op-accepted', 'accepted', commit_a,
                TIME, blob, sha, replayed=False)
        if forge == 'commit_mismatch':
            blob, sha = _event_blob()
            raw._insert_att_event(
                *SCOPE_ARGS, store.list_attempts(*SCOPE_ARGS)[0]['attempt_id'],
                4, 'forge-att-accepted', 'accepted', commit_b,
                TIME, blob, sha, replayed=False)
        if forge == 'attempt_only_readmission':
            blob, sha = _event_blob()
            raw._insert_att_event(
                *SCOPE_ARGS, store.list_attempts(*SCOPE_ARGS)[0]['attempt_id'],
                4, 'forge-att-readmit', 'accepting', None,
                TIME, blob, sha, replayed=False)

    _plant(setup, work)
    monkeypatch.setattr(repo, 'accept', lambda *a, **k: pytest.fail('CAS on corruption'))

    with pytest.raises(ValueError, match='refusing to trust corrupted'):
        reconcile(setup, predicate=lambda evidence: True)
    # The propagated verification failure still releases the admission.
    with coordination.admit('candidate_revise'):
        pass
    with pytest.raises(ValueError, match='refusing to trust corrupted'):
        store.verify_integrity()
