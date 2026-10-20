#!/usr/bin/env python3
"""Run only non-network tests with stricter isolation, without port allocation.

Not a replacement for full pytest/conftest: the real suite runs in normal WSL.
All filesystem writes are confined by the same G1 audit policy. This harness
additionally forbids ALL sockets and child processes, rather than weakening
conftest's sandbox or substituting a fixed production port.
"""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from tests.isolation_guard import data_keys, install, self_check


def main():
    with tempfile.TemporaryDirectory(prefix='sam-m5b-offline-') as directory:
        root = Path(directory)
        protected = [ROOT/'data', Path('/tmp/sam-m5')]
        protected.extend(Path(v).resolve() for k,v in os.environ.items()
                         if k.startswith('SAM_') and k.endswith('DB') and v)
        for key in data_keys(os.environ):
            os.environ[key] = str(root/key.lower())
        os.environ['SAM_TEST_ROOT'] = str(root)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        os.environ['TMPDIR'] = str(root)
        tempfile.tempdir = str(root)
        self_check(os.environ, root)
        install(protected, root)
        def no_network_or_children(event, args):
            if event.startswith('socket.') or event == 'subprocess.Popen' or event in ('os.system','os.fork','os.posix_spawn'):
                raise PermissionError('offline regression forbids sockets and child processes')
        sys.addaudithook(no_network_or_children)
        os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        import pytest
        return pytest.main(['--noconftest', '-q', '--basetemp', str(root/'pytest'),
            '-o', 'cache_dir='+str(root/'cache'),
            str(ROOT/'tests/test_phase1_catalog.py'), str(ROOT/'tests/test_document_read.py'),
            str(ROOT/'tests/test_m5b_real_runner.py')])


if __name__ == '__main__':
    raise SystemExit(main())
