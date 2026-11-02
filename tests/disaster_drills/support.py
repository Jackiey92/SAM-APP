"""File handshakes and fresh interpreters for sacrificial processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PROJECT = Path(__file__).resolve().parents[2]


def run(code, *args, **kwargs):
    return subprocess.run([sys.executable, '-c', code, *map(str, args)],
                          cwd=PROJECT, capture_output=True, text=True,
                          timeout=60, **kwargs)


def pause(path, **evidence):
    gate = Path(path)
    temporary = gate.with_suffix('.pending')
    temporary.write_text(json.dumps(dict(pid=os.getpid(), **evidence)))
    temporary.replace(gate)
    # Only the sacrificial process waits. The parent always has a hard deadline.
    time.sleep(45)
    raise AssertionError('parent failed to kill at the gate')


def kill_at_gate(code, config, path):
    path.write_text(json.dumps(config))
    gate = Path(config['gate'])
    log = path.with_suffix('.log')
    with log.open('w') as stream:
        child = subprocess.Popen([sys.executable, '-c', code, str(path)], cwd=PROJECT,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            deadline = time.monotonic() + 25
            while not gate.exists():
                assert child.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.01)
            evidence = json.loads(gate.read_text())
            assert evidence['pid'] != os.getpid() and child.poll() is None
            os.kill(evidence['pid'], signal.SIGKILL)
            status = child.wait(timeout=10)
            assert status in (-signal.SIGKILL, 128 + signal.SIGKILL), log.read_text()
            assert not Path(config['finally']).exists(), 'SIGKILL must bypass finally'
            print(json.dumps(dict(injection='SIGKILL', returncode=status, **evidence)))
            return evidence
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
