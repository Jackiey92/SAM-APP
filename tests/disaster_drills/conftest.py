"""Reuse S5 fixtures and enforce offline children in every D1 environment."""
import os
import socket
import subprocess

import pytest

from tests.test_facts_tree_backup_assembly import world
from tests.test_facts_tree_backup_coordinator_freeze import coordination, retention_root
from tests.test_facts_tree_recovery_verifier import package, host_package
from tests.test_projection_fault_drills_d15 import scene
from tests.test_projection_resources_d15 import resource_scene
from tests.test_projection_sigkill_d15 import drill


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    isolated = subprocess.Popen
    # Use the already-loaded conftest policy. Importing tests.conftest by a
    # second module name would initialize a second, conflicting audit root.
    assert isolated.__name__ == 'IsolatedPopen'
    policy = isolated.__init__.__globals__

    class OfflinePopen(isolated):
        def __init__(self, args, *positional, **kwargs):
            assert not kwargs.get('shell'), 'D1 never launches a shell command string'
            # Extend the existing IsolatedPopen enclosure with --unshare-net
            # in ONE wrapper. Nesting a second bwrap for every Git read can
            # exhaust the caller's user-namespace quota during full regression.
            # Preserve every root/protected mask and inherited Python hook.
            root = str(policy['_ROOT'])
            wrapper = ['/usr/bin/bwrap', '--die-with-parent', '--unshare-net',
                       '--ro-bind', '/', '/', '--bind', root, root,
                       '--proc', '/proc', '--dev', '/dev']
            for path in policy['_PROTECTED']:
                if path.exists():
                    wrapper += ['--ro-bind', str(path), str(path)]
            env = dict(kwargs.get('env') if kwargs.get('env') is not None else os.environ)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            env['PYTHONPATH'] = str(policy['_CHILD_HOOK']) + os.pathsep + env.get('PYTHONPATH', '')
            kwargs['env'] = env
            policy['_original_popen'].__init__(self, wrapper + ['--', *args], *positional, **kwargs)

    def denied(*args, **kwargs):
        raise AssertionError('D1 forbids network connections')

    monkeypatch.setattr(subprocess, 'Popen', OfflinePopen)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    # Fixture plumbing also ignores the user's Git templates/global config.
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_TEMPLATE_DIR', str(tmp_path / 'empty-template'))
