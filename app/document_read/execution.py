"""Host-only bounded file workers and SQLite job-generation fencing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import time
import uuid

from ..db.database import connect
from ..facts_tree.tree_meta import require_enabled


class LeaseLost(RuntimeError):
    pass


class JobBusy(RuntimeError):
    pass


class JobLease:
    def __init__(self, db_path, job_id, company_id, *, seconds=360, cancel=None):
        require_enabled()
        if type(seconds) is not int or not 240 <= seconds <= 900:
            raise ValueError('lease seconds must be 240..900 (transport timeout is 180)')
        self.db_path, self.job_id, self.company_id = db_path, job_id, company_id
        self.seconds, self.owner = seconds, uuid.uuid4().hex
        self.cancel = cancel
        with connect(db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS md_job_leases ('
                'job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, owner TEXT NOT NULL, '
                'generation INTEGER NOT NULL, expires_at REAL NOT NULL)')
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT 1 FROM parse_jobs WHERE job_id=? AND company_id=?', (job_id, company_id)).fetchone()
            if not job: raise ValueError('job outside company scope')
            row = conn.execute('SELECT * FROM md_job_leases WHERE job_id=?', (job_id,)).fetchone()
            now = time.time()
            if row and row['expires_at'] > now: raise JobBusy('job already has an active file employee')
            self.generation = row['generation'] + 1 if row else 1
            conn.execute('INSERT INTO md_job_leases VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET '
                'company_id=excluded.company_id,owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at',
                (job_id, company_id, self.owner, self.generation, now + seconds))

    def check(self, conn, *, renew=True):
        if self.cancel is not None: self.cancel()
        # Check and subsequent write share the writer lock, never the model call.
        if not conn.in_transaction: conn.execute('BEGIN IMMEDIATE')
        now = time.time()
        row = conn.execute('SELECT * FROM md_job_leases WHERE job_id=?', (self.job_id,)).fetchone()
        if (not row or row['owner'] != self.owner or row['generation'] != self.generation
                or row['company_id'] != self.company_id or row['expires_at'] <= now):
            raise LeaseLost('file employee lease expired or generation replaced')
        if renew:
            conn.execute('UPDATE md_job_leases SET expires_at=? WHERE job_id=? AND owner=? AND generation=?',
                         (now + self.seconds, self.job_id, self.owner, self.generation))

    def release(self):
        with connect(self.db_path) as conn:
            conn.execute('UPDATE md_job_leases SET expires_at=0 WHERE job_id=? AND owner=? AND generation=?',
                         (self.job_id, self.owner, self.generation))


def run_files(tasks, *, workers=2):
    """Run finite file tasks concurrently. Each factory MUST return a fresh reader or an owning context manager.

    task = {reader_factory: callable, company_id, job_id, limits, optional
    renew_deadline_seconds/lease_seconds}. Upload ParseJobManager is NOT wired.
    Results preserve input order; exceptions are returned per job, not hidden.
    """
    require_enabled()
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError('workers must be 1..8')
    if not isinstance(tasks, (list, tuple)) or len(tasks) > 256:
        raise ValueError('provide at most 256 explicit file tasks')
    def execute(task):
        try:
            args = {k:v for k,v in task.items() if k != 'reader_factory'}
            with ExitStack() as stack:
                created = task['reader_factory']()
                reader = stack.enter_context(created) if hasattr(created, '__enter__') else created
                return reader.run(**args)
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mapped-md') as pool:
        return list(pool.map(execute, tasks))
