"""§8-1/2: real candidate/cut boundaries, killed processes and bounded busy probe."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from app.facts_tree import git_format
from app.facts_tree.backup_coordinator import read_freeze_record
from app.facts_tree.git_repository import FactsGitRepository
from tests import test_facts_tree_backup_coordinator_freeze as existing
from tests.disaster_drills.support import kill_at_gate, pause, PROJECT


def _cut_worker(config_path):
    from app.facts_tree import backup_coordinator as coordinator
    from app.facts_tree.broker_coordination import BrokerCoordination, FINAL_ACCEPT
    from tests.test_projection_fault_drills_d15 import scoped_tree
    from tests.test_projection_incremental import _append

    config = json.loads(Path(config_path).read_text())
    root, stage = Path(config['root']), config['stage']
    repo = existing._repo(root, 'sha1')
    spec, work, source = existing._work_database(root, 'w1')
    work.execute("UPDATE work_state SET payload='candidate-一'")
    work.commit()
    retention = root / 'retention'
    retention.mkdir(0o700)
    with closing(sqlite3.connect(root / 'broker.sqlite')) as broker:
        coordination = BrokerCoordination(root / 'domain', existing._IDS['repo_id'], broker)
        if stage in ('after-accept', 'after-cleanup'):
            lease = coordination.claim_broker()
            with coordination.admit(FINAL_ACCEPT, lease=lease):
                tree = scoped_tree(existing._IDS, repo.head_oid())
                meta = git_format.record_path('repository_meta')
                tree[meta] = git_format.loads_record(repo.read_snapshot_at(repo.head_oid()).records[0].content)
                _append(repo, tree)
                if stage == 'after-cleanup':
                    with work:
                        work.execute('DELETE FROM work_state')
            # Scope is physical company evidence; keep a separate company row
            # after candidate cleanup so the database still has that scope.
            if stage == 'after-cleanup':
                work.execute("INSERT INTO work_state VALUES (2, ?, 'other-candidate')",
                             (existing._IDS['company_id'],))
                work.commit()

        def gate(**details):
            pause(config['gate'], stage=stage, h=repo.head_oid(), source=str(source), **details)

        def trace(frame, event, value):
            # Observation only: neither the real backup nor the authority fix
            # is replaced. Kill pauses occur after the actual operation.
            if event == 'return' and stage == 'capture-H' and frame.f_code is coordinator._authority_fix.__code__:
                gate(frozen=False)
            if event == 'return' and stage == 'checkpoint-W' and frame.f_code is coordinator.create_sqlite_checkpoint.__code__:
                gate(frozen=False, checkpoint=str(value.path))
            return trace

        sys.settrace(trace)
        try:
            cut = coordinator.freeze_consistent_cut(existing._assembly(
                'd1-cut', coordination, repo, [spec], retention))
            assert cut.frozen, cut
            gate(frozen=True, checkpoint=str(cut.cut_dir / 'w1.sqlite'))
        finally:
            Path(config['finally']).touch()


@pytest.mark.parametrize('stage', ['capture-H', 'checkpoint-W', 'before-accept', 'after-accept', 'after-cleanup'])
def test_sigkill_same_candidate_never_leaves_old_h_and_cleaned_w(tmp_path, stage):
    config = dict(root=str(tmp_path), stage=stage, gate=str(tmp_path / 'gate'),
                  **{'finally': str(tmp_path / 'finally')})
    evidence = kill_at_gate(
        'from tests.disaster_drills.test_barrier_cut import _cut_worker; '
        'import sys; _cut_worker(sys.argv[1])', config, tmp_path / 'config.json')
    repo = FactsGitRepository.open(tmp_path / 'facts.git', **existing._IDS)
    records = {r.path for r in repo.read_snapshot_at(evidence['h']).records}
    consumed = git_format.record_path('consumed_candidate', candidate_uid='candidate-一', candidate_revision=1)
    w_path = evidence.get('checkpoint', evidence['source'])
    with closing(sqlite3.connect(Path(w_path).as_uri() + '?mode=ro', uri=True)) as db:
        pending = db.execute("SELECT count(*) FROM work_state WHERE payload='candidate-一'").fetchone()[0]
    assert consumed in records or pending == 1, 'H old and W cleaned would lose the candidate twice'
    assert (consumed in records) == (stage in ('after-accept', 'after-cleanup'))
    assert pending == (0 if stage == 'after-cleanup' else 1)
    record = read_freeze_record(tmp_path / 'retention', 'd1-cut')
    assert record.state == ('frozen' if evidence['frozen'] else 'preparing')
    if not evidence['frozen']:
        assert not list((tmp_path / 'retention' / 'cuts').glob('**/*.sqlite'))


def test_real_second_w_corruption_is_all_or_nothing(tmp_path, coordination, retention_root):
    existing.test_second_w_failure_is_all_or_nothing_and_old_cut_intact(tmp_path, coordination, retention_root)


def test_real_barrier_lock_timeout_has_distinct_evidence(tmp_path, coordination, retention_root):
    existing.test_preheld_lock_fails_within_budget_distinguishable(tmp_path, coordination, retention_root)


def test_formal_accept_waits_for_real_maintenance_barrier(tmp_path, coordination, retention_root):
    existing.test_barrier_blocks_real_broker_final_accept_until_release(tmp_path, coordination, retention_root)


def test_hold_budget_expiry_has_distinct_evidence(tmp_path, coordination, retention_root, monkeypatch):
    # The existing probe delays the real H read beyond its clock budget; it
    # neither fabricates evidence nor substitutes for a process-death drill.
    existing.test_hold_budget_expiry_aborts_before_pin_and_releases_barrier(
        tmp_path, coordination, retention_root, monkeypatch)


class BackupDeadlineExceeded(AssertionError):
    """Only the observed busy timeout may satisfy this production-gap xfail."""


@pytest.mark.xfail(strict=True, raises=BackupDeadlineExceeded,
    reason='app/facts_tree/backup_sqlite_checkpoint.py:440 + backup_coordinator.py:1127; §8-2: backup busy has no deadline; follow-up bounded checkpoint')
def test_real_sqlite_backup_busy_returns_within_hold_budget(tmp_path):
    source = tmp_path / 'busy.sqlite'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE candidate(id INTEGER)')
        db.commit()
    # This independent connection holds an actual EXCLUSIVE rollback-journal
    # write lock. A second reader proves SQLITE_BUSY before the child starts.
    with closing(sqlite3.connect(source)) as holder:
        holder.execute('BEGIN EXCLUSIVE')
        holder.execute('INSERT INTO candidate VALUES (1)')
        with closing(sqlite3.connect(source, timeout=0.01)) as reader:
            with pytest.raises(sqlite3.OperationalError) as busy:
                reader.execute('SELECT * FROM candidate').fetchall()
            assert busy.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        gate = tmp_path / 'backup-entered'
        code = '''
import sqlite3, sys
from pathlib import Path
from app.facts_tree.backup_sqlite_checkpoint import create_sqlite_checkpoint
source=sqlite3.connect(sys.argv[1], timeout=0.01)
def trace(frame,event,arg):
    if event=='c_call' and getattr(arg,'__name__','')=='backup':
        Path(sys.argv[3]).write_text(str(__import__('os').getpid()))
sys.setprofile(trace)
create_sqlite_checkpoint(source, sys.argv[2])
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(source),
                                  str(tmp_path / 'snapshot.sqlite'), str(gate)],
                                 cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not gate.exists():
                assert child.poll() is None, child.communicate(timeout=1)
                assert time.monotonic() < deadline, 'backup never entered'
                time.sleep(0.01)
            try:
                child.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                # Kill the interpreter, not a bwrap supervisor. Reap before
                # the expected failure so strict xfail cannot hide leaked work.
                import os
                import signal
                os.kill(int(gate.read_text()), signal.SIGKILL)
                child.communicate(timeout=10)
                raise BackupDeadlineExceeded('real backup remained busy beyond the 1s hard bound')
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
