"""Default-off byte/behavior/IO differential against the actual E1 commit."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'bc8158a929a7963e952c6ed69ec5e4ecf5ff766f'
CHANGED = (
    'app/facts_tree/broker_final_gate.py',
    'app/facts_tree/record_schema/__init__.py',
    'app/facts_tree/record_schema/_primitives.py',
    'app/facts_tree/record_schema/operation.py',
    'app/facts_tree/record_schema/event.py',
    'app/facts_tree/record_schema/link_placement.py',
    'app/facts_tree/record_schema/source_evidence.py',
)


@pytest.fixture(scope='module')
def e1_package(tmp_path_factory):
    path = tmp_path_factory.mktemp('e1-package')
    shutil.copytree(ROOT/'app', path/'app', ignore=shutil.ignore_patterns('__pycache__'))
    for name in CHANGED:
        original = subprocess.run(['git', '-C', str(ROOT), 'show', BASELINE+':'+name],
                                   check=True, stdout=subprocess.PIPE).stdout
        (path/name).write_bytes(original)
    (path/'app/facts_tree/record_schema/closure_07_approval_use.py').unlink()
    return path


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('seed', ['0', '1', '8675309'])
def test_default_off_golden_bytes_paths_digests_objects_errors_validate_and_io(
        tmp_path, e1_package, object_format, seed):
    results = []
    for package, name in [(e1_package, 'e1'), (ROOT, 'e2')]:
        env = dict(os.environ, PYTHONHASHSEED=seed,
                   PYTHONPATH=str(package)+os.pathsep+str(ROOT))
        output = subprocess.run([sys.executable, '-c',
            'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("tests.disaster_drills.approval_use_e2_golden",run_name="__main__")',
            str(package), str(tmp_path/name), object_format], cwd=ROOT, env=env,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        results.append(json.loads(output.stdout))
    assert results[0]['observations'] == results[1]['observations']
    assert results[0]['io'] == results[1]['io']
    for scenario in ('accepted', 'racing'):
        events = results[1]['observations'][scenario]['events']
        assert [e[0] for e in events] == ['validate','validate','CAS'] * (2 if scenario == 'racing' else 1)
    assert not any(e[0].startswith('socket.') for e in results[1]['io'])
