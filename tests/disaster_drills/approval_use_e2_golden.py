"""Executable default-off observation, shared verbatim by E1 and E2 packages."""
from collections.abc import Mapping
import dataclasses
from decimal import Decimal
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

from app.facts_tree import broker_final_gate as final, broker_tree_gate, git_format
from app.facts_tree import broker_coordination as coordination, git_repository
from app.facts_tree.git_repository import FactsGitRepository
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.projection_advance import build_standard_generation_for_commit
from app.facts_tree.projection_incremental import build_replayed_generation
from app.facts_tree.projection_decode import decode_generation_records, decode_rows
from app.facts_tree.record_tree import build_record_tree
from tests.facts_tree_full_fixture import full_tree, SCOPE
from tests.test_projection_generation import _read

ROOT = Path(sys.argv[1])
FORMAT = sys.argv[2]
EFFECT = {'opaque': ['effect', 1]}
TIME = '2026-11-01T12:00:00+08:00'
OP = git_format.record_path('operation', operation_id='operation-1')
EVENT = git_format.record_path('event', event_id='event-1')


def plain(value):
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [plain(v) for v in value]
    if isinstance(value, (set,frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, bytes):
        return {'bytes': value.hex()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return plain(str(value))
    if isinstance(value, str):
        return value.replace(str(ROOT), '@root').replace('.tmp-' + str(os.getpid()), '.tmp-@pid')
    return value


# Deterministic object times and gate-issued IDs, without changing Git format.
old_env = git_repository._git_env

def fixed_env():
    return {**old_env(), 'GIT_AUTHOR_DATE': '2026-11-01T00:00:00+00:00',
            'GIT_COMMITTER_DATE': '2026-11-01T00:00:00+00:00'}

git_repository._git_env = fixed_env
ids = itertools.count(1)
final.uuid4 = lambda: UUID(int=next(ids))
coordination.secrets.token_hex = lambda n: 'c' * (n*2)

# Imports/registration finish before IO observation. importlib/pkgutil remain
# available exactly as the original schema registry requires.
io = []

def audit(event, args):
    if event in {'open', 'sqlite3.connect', 'subprocess.Popen', 'os.system'} or event.startswith('socket.'):
        if event == 'subprocess.Popen':
            executable, argv, cwd, _env = args
            io.append([event, plain(executable), plain(argv), plain(cwd)])
        elif event == 'open' and isinstance(args[0], int):
            # Kernel-assigned FD numbers and anonymous pipe inode numbers are
            # process-local, not an IO behavior contract. Compare their actual
            # target kind/path plus unchanged open flags, count and order.
            target = os.readlink('/proc/self/fd/' + str(args[0]))
            if target.startswith('pipe:'):
                target = '@anonymous-pipe'
            io.append([event, [plain(target), *plain(args[1:])]])
        else:
            io.append([event, plain(args)])

sys.addaudithook(audit)
observations = {}
for scenario in ('accepted', 'racing', 'invalid'):
    root = ROOT/scenario
    root.mkdir(parents=True)
    repo = FactsGitRepository.initialize(root/'facts.git', **SCOPE,
        installation_id='install-test', object_format=FORMAT)
    db = sqlite3.connect(root/'work.sqlite')
    store = OperationWorkStore(db)
    scope = (*SCOPE.values(), 'operation-1')
    with db:
        db.execute('BEGIN')
        store.record_operation(*scope, 'task-1', 'key-1', TIME, {})
        store.mark_operation_prepared(*scope, 'prepared-1', TIME)
    domain = coordination.BrokerCoordination(root/'coordination', SCOPE['repo_id'], db)
    lease = domain.claim_broker()
    gate = final.BrokerFinalGate(repo, domain, store)
    events = []
    validate = broker_tree_gate.validate_broker_tree
    def validate_spy(*args, **kwargs):
        events.append(['validate', kwargs['head_oid'], kwargs['commit_oid']])
        return validate(*args, **kwargs)
    broker_tree_gate.validate_broker_tree = validate_spy
    accept = repo.accept
    attempts = []
    def cas(head, commit, **kwargs):
        events.append(['CAS', head, commit]); attempts.append(1)
        if scenario == 'racing' and len(attempts) == 1:
            records = {r.path:r.content for r in repo.read_snapshot_at(head).records}
            tree = repo.build_tree(records.items(), expected_paths=records)
            other = repo.create_commit(tree, head, message='competitor')
            assert accept(head, other, expected_paths=records).accepted
        return accept(head, commit, **kwargs)
    repo.accept = cas
    captured = []
    def assemble(head, snapshot):
        tree = full_tree()
        tree[OP].update(base_commit=head, effect_digest=final._digest(EFFECT))
        tree[EVENT]['base_commit'] = head
        if scenario == 'invalid':
            tree[OP]['approval_use_refs'] = [{}]
        records = {r.path:r.content for r in snapshot.records}
        records.update({p:git_format.dumps_record(v) for p,v in tree.items()})
        captured.append(plain(records))
        return final.AssembledAttempt(records, {'opaque':'plan'}, EFFECT, {'version':head})
    result = gate.accept(lease=lease, operation_id='operation-1', effect=EFFECT,
                          assemble=assemble, recorded_at=TIME)
    result2 = gate.accept(lease=lease, operation_id='operation-1', effect=EFFECT,
                           assemble=assemble, recorded_at=TIME)
    obs = dict(result=plain(result), replay=plain(result2), events=events,
               captured=captured, operation=plain(store.get_operation(*scope)),
               attempts=plain(store.list_attempts(*scope)))
    if result.status == 'accepted':
        snapshot = repo.read_snapshot_at(result.accepted_commit)
        obs['tree'] = plain(build_record_tree(snapshot.records, **SCOPE,
            commit_oid=result.accepted_commit, head_oid=result.accepted_commit,
            object_format=FORMAT))
        expected = dict(expected_repo_id=SCOPE['repo_id'], expected_company_id=SCOPE['company_id'])
        single = build_standard_generation_for_commit(repo, result.accepted_commit, root/'single', **expected)
        replay = build_replayed_generation(repo, repo.zero_oid, result.accepted_commit, root/'replay', **expected)
        obs['single'] = plain(single); obs['projection_replay'] = plain(replay)
        obs['rows'] = plain(_read(single.db_path))
        with sqlite3.connect(single.db_path) as conn:
            decoded = decode_generation_records(conn, authz_digest='authorized')
            rows = conn.execute('SELECT path,family,byte_length,sha256 FROM record_canonical ORDER BY path').fetchall()
            obs['decode'] = plain(decoded)
            obs['direct_decode'] = plain(decode_rows(conn, rows, authz_digest='authorized'))
    lease.close(); db.close()
    broker_tree_gate.validate_broker_tree = validate
    observations[scenario] = obs
print(json.dumps(dict(observations=observations, io=io), ensure_ascii=False, sort_keys=True))
