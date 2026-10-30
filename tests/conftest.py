"""Isolate storage before collection, including children and production ports."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

from tests.isolation_guard import data_keys, install, self_check

_RUNTIME = tempfile.TemporaryDirectory(prefix='sam-pytest-')
_ROOT = Path(_RUNTIME.name)
_PROJECT = Path(__file__).resolve().parents[1]
_PROTECTED = [_PROJECT / 'data', Path('/tmp/sam-m5')]
_PROTECTED += [Path(value).resolve() for key, value in os.environ.items()
               if key.startswith('SAM_') and key.endswith('DB') and value]
# Kernel read-only open probes require real files, before audit hooks are active.
# Exclusive creation preserves existing contents and mtimes, including on races.
for sentinel in (_PROJECT / 'data/app.db', Path('/tmp/sam-m5/main.db')):
    if not sentinel.exists():
        if not sentinel.parent.exists():
            sentinel.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sentinel.open('xb'):
                pass
        except FileExistsError:
            pass
for key in data_keys(os.environ):
    os.environ[key] = str(_ROOT / key.lower())
# The content-addressed object store creates only its configured leaf and
# fsyncs the parent, refusing recursive mkdir; the isolated SAM_2A_ROOT must
# therefore exist before any test imports or constructs a store.
Path(os.environ['SAM_2A_ROOT']).mkdir()
os.environ['SAM_TEST_ROOT'] = str(_ROOT)
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['TMPDIR'] = str(_ROOT / 'tmp')
(_ROOT / 'tmp').mkdir()
tempfile.tempdir = str(_ROOT / 'tmp')
_CHILD_HOOK = _ROOT / 'child-hooks'
_CHILD_HOOK.mkdir()
shutil.copyfile(_PROJECT / 'tests/isolation_guard.py', _CHILD_HOOK / 'isolation_guard.py')
(_CHILD_HOOK / 'sitecustomize.py').write_text(
    'from isolation_guard import install\n'
    f'install({[str(p) for p in _PROTECTED]!r}, {str(_ROOT)!r})\n')
sys.dont_write_bytecode = True
self_check(os.environ, _ROOT)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
os.environ['SAM_OPENCLAW_GATEWAY_PORT'] = str(port)
os.environ['OPENCLAW_GATEWAY_PORT'] = str(port)

# Native subprocesses cannot bypass Python's audit hook: read-only host root,
# writable temporary storage, and read-only masks for protected paths in /tmp.
# No network/user namespace restriction is added here (QC owns its own stricter
# enclosure). If bwrap is unavailable, collection fails rather than falling back.
if not shutil.which('bwrap'):
    raise RuntimeError('pytest requires bwrap for subprocess write isolation')
_original_popen = subprocess.Popen


class IsolatedPopen(_original_popen):
    def __init__(self, args, *positional, **kwargs):
        if kwargs.get('shell'):
            args = ['/bin/sh', '-c', args]
            kwargs['shell'] = False
        if isinstance(args, (str, bytes, os.PathLike)):
            args = [args]
        wrapper = ['/usr/bin/bwrap', '--die-with-parent', '--ro-bind', '/', '/',
                   '--bind', str(_ROOT), str(_ROOT),
                   '--proc', '/proc', '--dev', '/dev']
        for path in _PROTECTED:
            if path.exists():
                wrapper += ['--ro-bind', str(path), str(path)]
        env = dict(kwargs.get('env') if kwargs.get('env') is not None else os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONPATH'] = str(_CHILD_HOOK) + os.pathsep + env.get('PYTHONPATH', '')
        kwargs['env'] = env
        super().__init__(wrapper + ['--'] + list(args), *positional, **kwargs)


subprocess.Popen = IsolatedPopen
install(_PROTECTED, _ROOT)

from app.db.migrate import main as migrate
migrate(['--db', os.environ['SAM_MAIN_DB']])


import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    self_check(os.environ, _ROOT)
    config._inicache['cache_dir'] = str(_ROOT / 'pytest-cache')
    config.option.basetemp = str(_ROOT / 'pytest')
