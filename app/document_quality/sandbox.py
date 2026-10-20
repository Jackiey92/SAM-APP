"""Host-controlled, role-specific Python verification in disposable bubblewrap.

No host shell interface, no credentials, no company-wide directory mount.
The only writable filesystem is a size-limited task-local tmpfs. Failure is
closed: no bubblewrap/venv means no execution, never an unsandboxed fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Mapping


class SandboxFailure(RuntimeError):
    pass


def conclusion(value):
    """Validate the delivery schema only; quality decisions belong to staff."""
    if not isinstance(value, dict) or set(value) - {'status', 'issues', 'defects'}:
        raise ValueError('invalid document quality conclusion')
    if value.get('status') not in {'pass', 'rework', 'needs_human'}:
        raise ValueError('invalid document quality status')
    issues = value.get('issues')
    if not isinstance(issues, list) or len(issues) > 128:
        raise ValueError('invalid quality issues')
    for issue in issues:
        if not isinstance(issue, dict) or set(issue) != {'source_loc', 'problem', 'expected'}:
            raise ValueError('invalid quality issue schema')
        if any(not isinstance(issue[k], str) or not issue[k].strip() or len(issue[k]) > 8000 for k in issue):
            raise ValueError('quality issues require original coordinates, problem and expectation')
    if value['status'] != 'pass' and not issues:
        raise ValueError('non-pass requires located issues')
    if value['status'] == 'pass' and issues:
        raise ValueError('pass may not carry unresolved conversion issues')
    defects = value.get('defects', [])
    if not isinstance(defects, list) or len(defects) > 32:
        raise ValueError('invalid defect list')
    for defect in defects:
        required = {'source_loc', 'reproduction_script', 'raw_fragment', 'snapshot'}
        if not isinstance(defect, dict) or set(defect) != required:
            raise ValueError('invalid defect schema')
        if any(not isinstance(defect[k], str) or len(defect[k]) > 32000 for k in required):
            raise ValueError('invalid defect evidence')
        if not defect['source_loc'] or not defect['reproduction_script'] or not defect['raw_fragment']:
            raise ValueError('defect requires reproduction evidence')
    if defects and value['status'] != 'needs_human':
        raise ValueError('engine defects require human review')
    return {'status': value['status'], 'issues': issues, 'defects': defects}


@dataclass(frozen=True)
class SandboxLimits:
    wall_seconds: float = 30
    cpu_seconds: int = 10
    disk_bytes: int = 16 * 1024 * 1024
    memory_bytes: int = 1024 * 1024 * 1024
    output_bytes: int = 128 * 1024
    input_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in self.__dict__.values()):
            raise ValueError('sandbox limits must be positive')


def _process_usage(pid):
    """Aggregate this enclosure's live/reaped children, not other user tasks."""
    ticks, count = 0, 0
    pending = [pid]
    seen = set()
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        try:
            fields = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()
            ticks += sum(int(fields[i]) for i in (11, 12, 13, 14))
            pending.extend(int(n) for n in Path(f'/proc/{child}/task/{child}/children').read_text().split())
            count += 1
        except (OSError, ValueError, IndexError):
            pass
    return ticks / os.sysconf('SC_CLK_TCK'), count


class QualitySandbox:
    def __init__(self, venv: Path, *, temporary_root: Path | None = None,
                 limits: SandboxLimits | None = None):
        self.venv = Path(venv).resolve()
        self.temporary_root = Path(temporary_root) if temporary_root else None
        self.limits = limits or SandboxLimits()

    def run(self, *, role: str, company_id: str, task_id: str, script: str,
            evidence: Mapping[str, bytes], cancel=None):
        if role != 'file_processor':
            raise PermissionError('verification scripts are restricted to the document processor')
        raw = self._execute(company_id=company_id, task_id=task_id, script=script, evidence=evidence, cancel=cancel)
        try:
            return conclusion(json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise SandboxFailure('verification did not return a structured quality conclusion') from exc

    def _execute(self, *, company_id, task_id, script, evidence, cancel=None):
        if os.getuid() == 0:
            raise PermissionError('document verification must run as a non-root user')
        if not company_id or not task_id:
            raise ValueError('host-owned company and task scope are required')
        if not isinstance(script, str) or len(script.encode()) > 64 * 1024:
            raise ValueError('invalid verification script')
        if not (self.venv / 'bin/python').is_file():
            raise SandboxFailure('isolated document-library venv is unavailable')
        size = 0
        for name, data in evidence.items():
            path = Path(name)
            if path.is_absolute() or '..' in path.parts or not path.parts or not isinstance(data, bytes):
                raise ValueError('invalid task evidence')
            size += len(data)
        if size > self.limits.input_bytes:
            raise SandboxFailure('task evidence exceeds input quota')
        if self.temporary_root:
            self.temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Names are opaque, never derived from user-supplied scope/path strings.
        with tempfile.TemporaryDirectory(prefix='qc-', dir=self.temporary_root) as directory:
            root = Path(directory)
            source = root / 'evidence'
            source.mkdir(mode=0o700)
            for name, data in evidence.items():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            script_path = root / 'verify.py'
            script_path.write_text(script, encoding='utf-8')
            args = ['/usr/bin/prlimit', f'--cpu={self.limits.cpu_seconds}:{self.limits.cpu_seconds + 1}',
                    f'--as={self.limits.memory_bytes}', f'--fsize={self.limits.disk_bytes}', '--nofile=64',
                    '/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--unshare-net', '--disable-userns', '--cap-drop', 'ALL',
                    '--die-with-parent', '--new-session', '--clearenv']
            for system in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
                if Path(system).exists():
                    args += ['--ro-bind', system, system]
            for public_config in ('/etc/fonts', '/etc/libreoffice'):
                if Path(public_config).is_dir():
                    args += ['--ro-bind', public_config, public_config]
            fonts = self.venv.parent / 'fonts'
            if fonts.is_dir():
                args += ['--ro-bind', str(fonts), '/opt/sam/fonts']
            args += ['--dev', '/dev', '--remount-ro', '/dev', '--proc', '/proc',
                     '--dir', '/opt', '--dir', '/opt/sam',
                     '--ro-bind', str(self.venv), '/opt/sam/venv',
                     '--ro-bind', str(source), '/evidence', '--ro-bind', str(script_path), '/verify.py',
                     '--size', str(self.limits.disk_bytes), '--tmpfs', '/work',
                     '--symlink', '/work', '/tmp', '--remount-ro', '/', '--chdir', '/work',
                     '--setenv', 'HOME', '/work', '--setenv', 'TMPDIR', '/work',
                     '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
                     '--setenv', 'PATH', '/opt/sam/venv/bin:/usr/bin:/bin',
                     '/opt/sam/venv/bin/python', '-I', '/verify.py']
            # File-backed output is bounded while the process is running; no
            # communicate() holding an unbounded employee stream in RAM.
            with (root / 'stdout').open('w+b') as out, (root / 'stderr').open('w+b') as err:
                try:
                    proc = subprocess.Popen(args, stdout=out, stderr=err, start_new_session=True, env={})
                except OSError as exc:
                    raise SandboxFailure('bubblewrap execution unavailable') from exc
                deadline = time.monotonic() + self.limits.wall_seconds
                failure = None
                try:
                    while proc.poll() is None:
                        if cancel:
                            cancel()
                        cpu, processes = _process_usage(proc.pid)
                        if cpu >= self.limits.cpu_seconds or processes > 64:
                            failure = 'verification process-tree CPU/process quota exceeded'
                            break
                        if time.monotonic() >= deadline:
                            failure = 'verification wall-clock quota exceeded'
                            break
                        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > self.limits.output_bytes:
                            failure = 'verification output quota exceeded'
                            break
                        time.sleep(0.01)
                finally:
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    proc.wait()
                if failure:
                    raise SandboxFailure(failure)
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > self.limits.output_bytes:
                    raise SandboxFailure('verification output quota exceeded')
                if proc.returncode:
                    err.seek(0)
                    detail = err.read(2000).decode('utf-8', errors='replace')
                    raise SandboxFailure(f'verification process failed ({proc.returncode}): {detail}')
                out.seek(0)
                raw = out.read(self.limits.output_bytes + 1)
                if len(raw) > self.limits.output_bytes:
                    raise SandboxFailure('verification output quota exceeded')
                return raw.decode('utf-8')
