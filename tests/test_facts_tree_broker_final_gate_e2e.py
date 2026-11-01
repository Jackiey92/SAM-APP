"""B4-3 real-material end-to-end ledger drills for the Broker final gate.

Every test here uses the real offline Git repository (both physical object
formats), the real SQLite work store and the real flock coordination ledger
inside tmp_path; nothing reaches the network and no CAS/tree gate/lock is
mocked away. Monkeypatching only injects competing writers or ambiguous CAS
points, exactly as the B4-1/B4-2 suites do.

Coverage:
- section 1.1: the whole final-gate chain (build -> commit -> CAS -> work
  mirror -> idempotent locate -> reconcile exits A/B) on real sha1 AND sha256
  repositories, with one parametrized assertion set per main path;
- section 1.2: one appended multi-operation ledger life on one repository and
  one work store, exercising first-acceptance idempotent location across later
  commits, the two-round lost-CAS budget with a real competitor, terminal
  integrity and mirror/Git first-acceptance agreement;
- section 1.3: the frozen-contract -> test matrix lives beside this module at
  ``tests/test_facts_tree_broker_final_gate_e2e_contract_matrix.md``.
"""
import re
import socket
import sqlite3
from copy import deepcopy
from hashlib import sha256

import pytest

from app.facts_tree import broker_tree_gate, git_format, operation_acceptance
from app.facts_tree.broker_coordination import BrokerCoordination
from app.facts_tree.broker_final_gate import (
    AssembledAttempt, BrokerFinalGate, VerifiedReentryFacts)
from app.facts_tree.git_repository import FactsGitRepository
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.record_schema import SCHEMA_BUILDERS
from tests.facts_tree_full_fixture import DIGEST as FIXTURE_DIGEST, SCOPE, full_tree


TIME = '2026-11-01T12:00:00+08:00'
OP1 = git_format.record_path('operation', operation_id='operation-1')
EVENT1 = git_format.record_path('event', event_id='event-1')
TEMPLATE = full_tree()
T_INPUT = TEMPLATE[git_format.record_path(
    'input_candidate', candidate_uid='candidate-一', candidate_revision=1)]
T_CONSUMED = TEMPLATE[git_format.record_path(
    'consumed_candidate', candidate_uid='candidate-一', candidate_revision=1)]
T_OPERATION = TEMPLATE[OP1]
T_EVENT = TEMPLATE[EVENT1]


def effect(n):
    return {'ledger': ['operation', n]}


def digest_of(value):
    return sha256(git_format.dumps_record(value)).hexdigest()


def op_id(n):
    return 'operation-%d' % n


def scope(n):
    return (SCOPE['repo_id'], SCOPE['company_id'], op_id(n))


class Environment:
    """One real repository + work store + coordination domain per test."""

    def __init__(self, directory, object_format):
        self.object_format = object_format
        self.oid_length = 40 if object_format == 'sha1' else 64
        self.repo = FactsGitRepository.initialize(
            directory / 'facts.git', **SCOPE,
            installation_id='installation-test', object_format=object_format)
        self.db = sqlite3.connect(directory / 'work.sqlite')
        self.store = OperationWorkStore(self.db)
        self.coordination = BrokerCoordination(
            directory / 'coordination', SCOPE['repo_id'], self.db)
        self.lease = self.coordination.claim_broker()
        self.gate = BrokerFinalGate(self.repo, self.coordination, self.store)

    def register(self, n):
        """Host steps 1-2: immutable request snapshot, then prepared."""
        identifier = op_id(n)
        with self.db:
            self.db.execute('BEGIN')
            self.store.record_operation(
                SCOPE['repo_id'], SCOPE['company_id'], identifier,
                'task-%d' % n, 'key-%d' % n, TIME, {})
            self.store.mark_operation_prepared(
                SCOPE['repo_id'], SCOPE['company_id'], identifier,
                'prepared-%d' % n, TIME)

    def accept(self, n, assemble):
        return self.gate.accept(lease=self.lease, operation_id=op_id(n),
                                effect=effect(n), assemble=assemble,
                                recorded_at=TIME)

    def close(self):
        self.lease.close()
        self.db.close()


@pytest.fixture(params=['sha1', 'sha256'])
def env(request, tmp_path):
    environment = Environment(tmp_path, request.param)
    yield environment
    environment.close()


def _assembled(records, head, eff):
    return AssembledAttempt(records, {'plan': head}, eff.copy(),
                            {'version': head})


def first_assembler(head, snapshot):
    """The B4-1 fixture assembler: complete fixture tree bound to this head."""
    tree = full_tree()
    tree[OP1].update(base_commit=head, effect_digest=digest_of(effect(1)))
    tree[EVENT1]['base_commit'] = head
    records = {r.path: r.content for r in snapshot.records}
    records.update({p: git_format.dumps_record(v) for p, v in tree.items()})
    return _assembled(records, head, effect(1))


def snapshot_map(snapshot):
    return {r.path: r.content for r in snapshot.records}


def competitor(repo):
    """A real competing writer: verbatim current snapshot, fresh CAS commit."""
    head = repo.head_oid()
    snap = repo.read_snapshot_at(head)
    records = snapshot_map(snap)
    tree = repo.build_tree(records.items(), expected_paths=records)
    commit = repo.create_commit(tree, head, message='competitor')
    assert repo.accept(head, commit, expected_paths=records).accepted
    return commit


def park_unknown(repo, monkeypatch, *, land):
    real_accept = repo.accept

    def ambiguous(head, commit, **kwargs):
        if land:
            assert real_accept(head, commit, **kwargs).accepted
        return None

    monkeypatch.setattr(repo, 'accept', ambiguous)


def is_oid(value, length):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{%d}' % length, value)


# -- section 1.1: main paths on both physical object formats ----------------

def test_e2e_first_acceptance_on_real_object_format(env, monkeypatch):
    env.register(1)
    seen = []
    real_validate = broker_tree_gate.validate_broker_tree

    def observe(content, **kwargs):
        seen.append(kwargs)
        return real_validate(content, **kwargs)

    monkeypatch.setattr(broker_tree_gate, 'validate_broker_tree', observe)
    genesis = env.repo.head_oid()

    result = env.accept(1, first_assembler)

    assert result.status == 'accepted', result
    assert is_oid(result.accepted_commit, env.oid_length)
    assert result.accepted_commit == env.repo.head_oid()
    assert env.store.get_operation(*scope(1))['accepted_commit'] == result.accepted_commit
    assert {kwargs['object_format'] for kwargs in seen} == {env.object_format}
    assert all(kwargs['head_oid'] == genesis for kwargs in seen)
    snap = env.repo.read_snapshot_at(result.accepted_commit)
    records = snapshot_map(snap)
    assert git_format.loads_record(records[OP1])['base_commit'] == genesis
    assert (git_format.loads_record(records[git_format.record_path(
        'fact_revision', fact_uid='fact-1', revision=1)])['fact_uid'] == 'fact-1')
    env.repo.verify_integrity()
    env.store.verify_integrity()


def test_e2e_replay_zero_cas_returns_first_accepting_commit(env, monkeypatch):
    env.register(1)
    first = env.accept(1, first_assembler)
    assert first.status == 'accepted'
    # A later commit still carries operation-1's frame; the locator must pin
    # the FIRST accepting commit, never the newer head.
    later = competitor(env.repo)
    assert later != first.accepted_commit
    attempts_before = len(env.store.list_attempts(*scope(1)))
    monkeypatch.setattr(
        env.repo, 'accept', lambda *a, **k: pytest.fail('CAS on replay'))

    replay = env.gate.accept(
        lease=env.lease, operation_id=op_id(1), effect=effect(1),
        assemble=lambda *a: pytest.fail('replay assembled'), recorded_at=TIME)

    assert replay.status == 'idempotent_replay'
    assert replay.accepted_commit == first.accepted_commit != later
    assert len(env.store.list_attempts(*scope(1))) == attempts_before
    origin = operation_acceptance.locate_operation_acceptance(
        env.repo, operation_id=op_id(1), commit_oid=env.repo.head_oid())
    assert origin.accepted_commit_oid == first.accepted_commit
    # A different claimed effect on the same on-chain operation is refused
    # before any CAS, not silently accepted.
    different = env.gate.accept(
        lease=env.lease, operation_id=op_id(1), effect={'ledger': ['other']},
        assemble=lambda *a: pytest.fail('replay assembled'), recorded_at=TIME)
    assert different.status == 'rejected'
    assert env.repo.head_oid() == later


def test_e2e_one_lost_cas_then_second_round_acceptance(env, monkeypatch):
    env.register(1)
    repo = env.repo
    real_accept = repo.accept
    cas_calls = []

    def racing(head, commit, **kwargs):
        if len(cas_calls) == 0:
            monkeypatch.setattr(repo, 'accept', real_accept)
            rival = competitor(repo)
            monkeypatch.setattr(repo, 'accept', racing)
            lost = real_accept(head, commit, **kwargs)
            cas_calls.append((head, commit, lost, rival))
            return lost
        won = real_accept(head, commit, **kwargs)
        cas_calls.append((head, commit, won, won.current_oid))
        return won

    monkeypatch.setattr(repo, 'accept', racing)

    result = env.accept(1, first_assembler)

    assert result.status == 'accepted', result
    rival = cas_calls[0][3]
    first_base, first_commit, lost_result, _ = cas_calls[0]
    second_base, second_commit, won_result, _ = cas_calls[1]
    assert lost_result.accepted is False and won_result.accepted is True
    assert (first_base, second_base) == (
        env.repo.list_commit_chain(env.repo.zero_oid, rival)[-2], rival)
    assert first_commit != second_commit == result.accepted_commit
    chain = repo.list_commit_chain(repo.zero_oid, result.accepted_commit)
    assert chain[-2:] == (rival, result.accepted_commit)
    attempts = env.store.list_attempts(*scope(1))
    assert len(attempts) == 2
    states = sorted(env.store.get_attempt(*scope(1), a['attempt_id'])['current_state']
                    for a in attempts)
    assert states == ['accepted', 'stale']
    events = [e['state'] for e in env.store.list_operation_events(*scope(1))]
    assert events == ['prepared', 'in_flight', 'reserved', 'in_flight', 'accepted']
    accepted_attempts = [a for a in attempts
                         if env.store.get_attempt(*scope(1), a['attempt_id'])
                         ['current_state'] == 'accepted']
    assert len(accepted_attempts) == 1
    repo.verify_integrity()
    env.store.verify_integrity()


def test_e2e_unknown_exit_a_converges_from_chain(env, monkeypatch):
    env.register(1)
    park_unknown(env.repo, monkeypatch, land=True)
    parked = env.accept(1, first_assembler)
    assert parked.status == 'unknown'
    landed = env.repo.head_oid()
    assert is_oid(landed, env.oid_length)
    attempt = next(
        a for a in env.store.list_attempts(*scope(1))
        if env.store.get_attempt(*scope(1), a['attempt_id'])['current_state']
        == 'unknown')
    monkeypatch.setattr(
        env.repo, 'accept', lambda *a, **k: pytest.fail('second CAS on exit A'))

    result = env.gate.reconcile_unknown(
        lease=env.lease, operation_id=op_id(1), effect=effect(1),
        assemble=lambda *a: pytest.fail('exit A assembled'), recorded_at=TIME)

    assert result.status == 'accepted', result
    assert result.accepted_commit == landed == env.repo.head_oid()
    assert result.attempt_id == attempt['attempt_id']
    assert [e['state'] for e in env.store.list_operation_events(*scope(1))] == [
        'prepared', 'in_flight', 'unknown', 'accepted']
    env.store.verify_integrity()


def test_e2e_unknown_exit_b_readmits_same_attempt(env, monkeypatch):
    env.register(1)
    park_unknown(env.repo, monkeypatch, land=False)
    parked = env.accept(1, first_assembler)
    assert parked.status == 'unknown'
    head = env.repo.head_oid()
    unknown_id = next(
        a['attempt_id'] for a in env.store.list_attempts(*scope(1))
        if env.store.get_attempt(*scope(1), a['attempt_id'])['current_state']
        == 'unknown')
    attempt = env.store.get_attempt(*scope(1), unknown_id)
    # park_unknown leaves the ambiguous patch on the instance; drop it so
    # attribute lookup returns the real bound method (mirrors the B4-2
    # helper's restore).
    monkeypatch.delattr(env.repo, 'accept', raising=False)
    seen = {}
    cas_log = []
    real_accept = env.repo.accept

    def cas(head_arg, commit, **kwargs):
        cas_log.append(1)
        return real_accept(head_arg, commit, **kwargs)

    def prove_isolated(evidence):
        seen['e'] = evidence
        return True

    monkeypatch.setattr(env.repo, 'accept', cas)

    result = env.gate.reconcile_unknown(
        lease=env.lease, operation_id=op_id(1), effect=effect(1),
        assemble=first_assembler, recorded_at=TIME,
        prove_executor_isolated=prove_isolated,
        reentry_facts=VerifiedReentryFacts(head, True, True))

    assert result.status == 'accepted', result
    assert result.attempt_id == attempt['attempt_id']
    assert len(cas_log) == 1 and is_oid(result.accepted_commit, env.oid_length)
    evidence = seen['e']
    assert evidence.operation_id == op_id(1)
    assert evidence.unknown_base_commit == attempt['base_commit'] == head
    states = [e['state'] for e in
              env.store.list_attempt_events(*scope(1), attempt['attempt_id'])]
    assert states == ['prepared', 'accepting', 'unknown', 'accepting', 'accepted']
    assert len(env.store.list_attempts(*scope(1))) == 1
    env.repo.verify_integrity()
    env.store.verify_integrity()


def test_e2e_tree_gate_failure_closes_before_cas(env, monkeypatch):
    env.register(1)
    head = env.repo.head_oid()
    monkeypatch.setattr(
        env.repo, 'accept', lambda *a, **k: pytest.fail('CAS past tree gate'))

    def broken(head_arg, snapshot):
        assembled = first_assembler(head_arg, snapshot)
        records = dict(assembled.records)
        payload = git_format.loads_record(records[OP1])
        del payload['actor']
        records[OP1] = git_format.dumps_record(payload)
        return AssembledAttempt(records, assembled.plan, assembled.effect,
                                assembled.dependencies)

    result = env.accept(1, broken)

    assert result.status == 'rejected'
    assert env.repo.head_oid() == head
    assert env.store.get_operation(*scope(1))['current_state'] == 'prepared'
    assert env.store.list_attempts(*scope(1)) == []


def test_e2e_final_gate_accepts_without_network_wait(tmp_path, monkeypatch):
    # The gate seam is a pure local assembler: a full acceptance must never
    # open a socket (Git runs local subprocesses; SQLite/flock are file IO).
    def refuse(*args, **kwargs):
        pytest.fail('final gate attempted network IO')

    monkeypatch.setattr(socket, 'socket', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    environment = Environment(tmp_path, 'sha1')
    try:
        environment.register(1)
        result = environment.accept(1, first_assembler)
        assert result.status == 'accepted', result
    finally:
        environment.close()


# -- section 1.2: one real appended multi-operation ledger life -------------

def issue_record(n):
    issue_uid = 'issue-%d' % n
    event_id = 'issue-event-%d' % n
    issue = SCHEMA_BUILDERS['issue_event'](issue_uid=issue_uid, event_id=event_id)
    issue.update(SCOPE)
    issue_ref = {'record_type': 'issue_event', 'stable_id': issue_uid,
                 'revision': event_id}
    issue.update(status='resolved',
                 disposition_result={'terminal': True,
                                     'result_refs': [deepcopy(issue_ref)]})
    return issue, issue_ref


def broker_frame(n, head, eff_digest):
    """Derive op-n's broker bookkeeping records from the frozen fixture tree.

    Only identity/base/digest bindings change; no business rule vocabulary is
    introduced here.
    """
    identifier = op_id(n)
    candidate_uid = 'candidate-%d' % n
    event_id = 'event-%d' % n
    issue, issue_ref = issue_record(n)
    candidate = {'candidate_uid': candidate_uid, 'candidate_revision': 1,
                 'payload_digest': FIXTURE_DIGEST}
    disposition = [dict(candidate)]
    disposition[0].update(terminal=True, result_refs=[deepcopy(issue_ref)])

    input_record = deepcopy(T_INPUT)
    input_record['candidate_uid'] = candidate_uid

    operation = deepcopy(T_OPERATION)
    operation.update(
        operation_id=identifier, request_id='request-%d' % n,
        base_commit=head, request_digest=eff_digest, effect_digest=eff_digest,
        candidate_refs=[deepcopy(candidate)], coverage_id='coverage-id-%d' % n,
        unit_id='unit-%d' % n, disposition_result=disposition,
        actor=deepcopy(issue['actor']), event_ids=[event_id],
        server_time=1793318400 + n)

    consumed = deepcopy(T_CONSUMED)
    consumed.update(candidate_uid=candidate_uid, operation_id=identifier,
                    disposition_result={'terminal': True,
                                        'result_refs': [deepcopy(issue_ref)]})

    event_record = deepcopy(T_EVENT)
    event_record.update(
        event_id=event_id, operation_id=identifier, base_commit=head,
        ordinal=0, changed_refs=[deepcopy(issue_ref)], before=[None],
        after=[digest_of(issue)], affected_refs=[deepcopy(issue_ref)])

    return {
        git_format.record_path('issue_event', issue_uid=issue['issue_uid'],
                               event_id=issue['event_id']): issue,
        git_format.record_path('input_candidate', candidate_uid=candidate_uid,
                               candidate_revision=1): input_record,
        git_format.record_path('operation', operation_id=identifier): operation,
        git_format.record_path('consumed_candidate', candidate_uid=candidate_uid,
                               candidate_revision=1): consumed,
        git_format.record_path('event', event_id=event_id): event_record,
    }


def append_business(records, n):
    """Append-only business change carried by operation-n (n >= 2)."""
    if n == 2:
        new_entity = SCHEMA_BUILDERS['entity_revision'](
            entity_uid='entity-2', revision=1)
        new_entity.update(SCOPE)
        records[git_format.record_path(
            'entity_revision', entity_uid='entity-2', revision=1)] = \
            git_format.dumps_record(new_entity)
        advanced = SCHEMA_BUILDERS['entity_revision'](
            entity_uid='entity-1', revision=3)
        advanced.update(SCOPE, predecessor_ref={
            'record_type': 'entity_revision', 'stable_id': 'entity-1',
            'revision': 2})
        records[git_format.record_path(
            'entity_revision', entity_uid='entity-1', revision=3)] = \
            git_format.dumps_record(advanced)
        current_path = git_format.record_path(
            'entity_current', entity_uid='entity-1')
        current = git_format.loads_record(records[current_path])
        current['revision'] = 3
        records[current_path] = git_format.dumps_record(current)
    else:
        new_entity = SCHEMA_BUILDERS['entity_revision'](
            entity_uid='entity-%d' % n, revision=1)
        new_entity.update(SCOPE)
        records[git_format.record_path(
            'entity_revision', entity_uid='entity-%d' % n, revision=1)] = \
            git_format.dumps_record(new_entity)


def ledger_assembler(n):
    def assemble(head, snapshot):
        records = snapshot_map(snapshot)
        additions = broker_frame(n, head, digest_of(effect(n)))
        records.update({p: git_format.dumps_record(v)
                        for p, v in additions.items()})
        append_business(records, n)
        return _assembled(records, head, effect(n))
    return assemble


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_e2e_multi_operation_ledger_full_life(tmp_path, monkeypatch,
                                              object_format):
    environment = Environment(tmp_path, object_format)
    repo = environment.repo
    store = environment.store
    try:
        genesis = repo.head_oid()

        # op-1: full fixture tree, fact-1 first revision lands at C1.
        environment.register(1)
        r1 = environment.accept(1, first_assembler)
        assert r1.status == 'accepted', r1
        c1 = r1.accepted_commit
        c1_records = snapshot_map(repo.read_snapshot_at(c1))
        assert git_format.record_path(
            'fact_revision', fact_uid='fact-1', revision=1) in c1_records
        assert git_format.loads_record(c1_records[git_format.record_path(
            'entity_current', entity_uid='entity-1')])['revision'] == 2

        # op-2: append entity-2 and advance entity-1 current to revision 3.
        environment.register(2)
        r2 = environment.accept(2, ledger_assembler(2))
        assert r2.status == 'accepted', r2
        c2 = r2.accepted_commit
        chain_c2 = repo.list_commit_chain(repo.zero_oid, c2)
        assert chain_c2 == (genesis, c1, c2)  # single parent each, C2 over C1
        c2_records = snapshot_map(repo.read_snapshot_at(c2))
        entity_path = lambda revision: git_format.record_path(
            'entity_revision', entity_uid='entity-1', revision=revision)
        assert entity_path(2) in c2_records and entity_path(3) in c2_records
        assert git_format.record_path(
            'entity_revision', entity_uid='entity-2', revision=1) in c2_records
        assert git_format.loads_record(c2_records[git_format.record_path(
            'entity_current', entity_uid='entity-1')])['revision'] == 3
        assert git_format.loads_record(c2_records[git_format.record_path(
            'operation', operation_id='operation-1')])['base_commit'] == genesis

        # Replays must pin the FIRST accepting commit even though later
        # commits also carry the operation; zero CAS / attempts / facts.
        monkeypatch.setattr(
            repo, 'accept', lambda *a, **k: pytest.fail('CAS on ledger replay'))
        attempts_before = {n: len(store.list_attempts(*scope(n))) for n in (1, 2)}
        replay1 = environment.gate.accept(
            lease=environment.lease, operation_id=op_id(1), effect=effect(1),
            assemble=lambda *a: pytest.fail('replay assembled'),
            recorded_at=TIME)
        assert (replay1.status, replay1.accepted_commit) == (
            'idempotent_replay', c1)
        replay2 = environment.gate.accept(
            lease=environment.lease, operation_id=op_id(2), effect=effect(2),
            assemble=lambda *a: pytest.fail('replay assembled'),
            recorded_at=TIME)
        assert (replay2.status, replay2.accepted_commit) == (
            'idempotent_replay', c2)
        assert {n: len(store.list_attempts(*scope(n))) for n in (1, 2)} \
            == attempts_before
        assert repo.head_oid() == c2
        monkeypatch.undo()

        # op-3: one real competitor wins round 1; round 2 rebuilds fresh over
        # the competitor head and wins. No rebase: exactly two attempts, one
        # acceptance, the new commit's parent is the competitor commit.
        environment.register(3)
        real_accept = repo.accept
        cas_log = []
        assembled_heads = []
        real_assemble = ledger_assembler(3)

        def observing_assemble(head, snapshot):
            assembled_heads.append(head)
            return real_assemble(head, snapshot)

        def racing(head, commit, **kwargs):
            if len(cas_log) == 0:
                monkeypatch.setattr(repo, 'accept', real_accept)
                rival = competitor(repo)
                monkeypatch.setattr(repo, 'accept', racing)
                lost = real_accept(head, commit, **kwargs)
                cas_log.append((head, commit, lost, rival))
                return lost
            won = real_accept(head, commit, **kwargs)
            cas_log.append((head, commit, won, won.current_oid))
            return won

        monkeypatch.setattr(repo, 'accept', racing)
        r3 = environment.gate.accept(
            lease=environment.lease, operation_id=op_id(3), effect=effect(3),
            assemble=observing_assemble, recorded_at=TIME)
        monkeypatch.undo()
        assert r3.status == 'accepted', r3
        c3 = r3.accepted_commit
        rival = cas_log[0][3]
        assert assembled_heads == [c2, rival]
        assert cas_log[0][2].accepted is False
        assert cas_log[1][2].accepted is True
        assert cas_log[0][1] != cas_log[1][1] == c3
        assert repo.list_commit_chain(repo.zero_oid, c3) == (
            genesis, c1, c2, rival, c3)
        attempts3 = store.list_attempts(*scope(3))
        assert len(attempts3) == 2
        attempt_states = sorted(
            store.get_attempt(*scope(3), a['attempt_id'])['current_state']
            for a in attempts3)
        assert attempt_states == ['accepted', 'stale']
        assert r3.attempt_id == next(
            a['attempt_id'] for a in attempts3
            if store.get_attempt(*scope(3), a['attempt_id'])['current_state']
            == 'accepted')
        assert [e['state'] for e in store.list_operation_events(*scope(3))] == [
            'prepared', 'in_flight', 'reserved', 'in_flight', 'accepted']
        c3_records = snapshot_map(repo.read_snapshot_at(c3))
        assert git_format.loads_record(c3_records[git_format.record_path(
            'operation', operation_id='operation-3')])['base_commit'] == rival
        assert git_format.record_path(
            'entity_revision', entity_uid='entity-3', revision=1) in c3_records

        # op-4 (optional natural step): ambiguous CAS that actually landed
        # parks unknown; exit A converges on the chain with zero second CAS.
        environment.register(4)
        ambiguous_calls = []
        real_accept2 = repo.accept

        def ambiguous(head, commit, **kwargs):
            ambiguous_calls.append(1)
            assert real_accept2(head, commit, **kwargs).accepted
            return None

        monkeypatch.setattr(repo, 'accept', ambiguous)
        r4 = environment.accept(4, ledger_assembler(4))
        monkeypatch.undo()
        assert r4.status == 'unknown'
        assert len(ambiguous_calls) == 1
        c4 = repo.head_oid()
        monkeypatch.setattr(
            repo, 'accept', lambda *a, **k: pytest.fail('CAS on convergence'))
        settled = environment.gate.reconcile_unknown(
            lease=environment.lease, operation_id=op_id(4), effect=effect(4),
            assemble=lambda *a: pytest.fail('exit A assembled'),
            recorded_at=TIME)
        monkeypatch.undo()
        assert settled.status == 'accepted'
        assert settled.accepted_commit == c4

        # Whole-ledger invariants.
        final_head = repo.head_oid()
        assert repo.list_commit_chain(repo.zero_oid, final_head) == (
            genesis, c1, c2, rival, c3, c4)
        repo.verify_integrity()
        store.verify_integrity()
        expected = {1: c1, 2: c2, 3: c3, 4: c4}
        for n, commit in expected.items():
            mirror = store.get_operation(*scope(n))
            assert mirror['current_state'] == 'accepted'
            assert mirror['accepted_commit'] == commit
            origin = operation_acceptance.locate_operation_acceptance(
                repo, operation_id=op_id(n), commit_oid=final_head)
            assert origin.accepted_commit_oid == commit
            for attempt in store.list_attempts(*scope(n)):
                assert store.get_attempt(
                    *scope(n), attempt['attempt_id'])['current_state'] in (
                    'accepted', 'stale')
        assert len(store.list_attempts(*scope(3))) == 2
        assert all(len(store.list_attempts(*scope(n))) == 1
                   for n in (1, 2, 4))
    finally:
        environment.close()
