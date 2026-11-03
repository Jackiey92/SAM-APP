"""Deliberately killed E2 admission worker; no simulated exceptions."""
import json
import os
from pathlib import Path
import signal
import sys

from tests.test_approval_use_e2 import make_scene, run


def main():
    def deny_network(event, args):
        if event.startswith('socket.'):
            raise AssertionError('E2 worker attempted network IO')
    sys.addaudithook(deny_network)
    directory = Path(sys.argv[1])
    phase = sys.argv[2]
    s = make_scene(directory)
    transfer = dict(worker_pid=os.getpid(), worker_group=os.getpgrp(), operation_id=s.prepared.operation_id, task_secret=s.cred.secret,
                    records={p: b.decode() for p, b in s.records.items()},
                    initial_head=s.initial_head)
    fd = os.open(directory / 'transfer.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(transfer, stream)
        stream.flush(); os.fsync(stream.fileno())

    def stop(*args, **kwargs):
        # All controlled Git children have been synchronously reaped. No
        # background descendants are started by this worker. Its own process
        # group is killed and reaped by the parent before takeover is allowed.
        print('READY ' + phase, flush=True)
        signal.pause()
        raise AssertionError('SIGKILL marker resumed')

    if phase == 'before_cas':
        s.repo.accept = stop
    else:
        s.store.mark_accepted = stop
    run(s)
    raise AssertionError('worker escaped its real death window')


if __name__ == '__main__':
    main()
