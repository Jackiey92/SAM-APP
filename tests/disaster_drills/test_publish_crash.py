"""§8-6/12: real SIGKILL at all five publication hooks, fresh-process convergence."""
from dataclasses import asdict
import json
from pathlib import Path
import pickle

import pytest

from app.facts_tree.backup_assembly import (
    assemble_backup, scan_assembly_state, read_current_backup,
)
from app.facts_tree.recovery_verifier import verify_recovery_package
from tests.test_facts_tree_backup_assembly import _publish_green_package, _IDENTITY, _fingerprint
from tests.disaster_drills.support import kill_at_gate, pause, run


def _publish_worker(config_path, *, restart=False):
    from app.facts_tree.backup_assembly import AssemblyPlan, AlreadyPublishedError
    from app.facts_tree.backup_object_closure import ClosureStores
    from app.facts_tree.git_repository import FactsGitRepository
    from app.storage.archive_store import ArchiveFileStore
    from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

    config = json.loads(Path(config_path).read_text())
    root = Path(config['root'])
    # Only test-generated immutable dataclasses are deserialized here.
    with (root / 'references.pickle').open('rb') as stream:
        references = pickle.load(stream)
    plan = AssemblyPlan(identity=_IDENTITY,
        repository=FactsGitRepository.open(root / 'facts.git', **asdict(_IDENTITY)),
        retention_root=root / 'retention', references=references,
        stores=ClosureStores(archive=ArchiveFileStore(root / '2a/bin', root / 'app.db'),
                            mapping_objects=VersionedMappingObjectStore(root / '2a/mapping-objects')),
        version_index_root=root / 'mapping-version-index')
    if restart:
        initial = scan_assembly_state(plan.retention_root, identity=_IDENTITY)
        expected = config['bid'] if config['stage'] == 'current_switched' else config['old_bid']
        assert initial.current.backup_id == expected
        assert all(item.status in ('partial', 'verified') for item in initial.staging)
        assert all(item.status == 'complete' for item in initial.packages)
        try:
            result = assemble_backup(plan, config['bid'])
            assert config['stage'] != 'current_switched'
            assert result.status == 'published', result
        except AlreadyPublishedError:
            assert config['stage'] == 'current_switched'
        state = scan_assembly_state(plan.retention_root, identity=_IDENTITY)
        assert state.current.backup_id == config['bid']
        packages = [item for item in state.packages if item.backup_id == config['bid']]
        assert len(packages) == 1 and packages[0].status == 'complete'
        pointer = read_current_backup(plan.retention_root, identity=_IDENTITY)
        assert pointer == state.current
        print(json.dumps(dict(initial_current=expected, final_current=pointer.backup_id,
                              packages=len(packages), staging=len(state.staging))))
        return

    def hook(stage):
        if stage == config['stage']:
            pause(config['gate'], stage=stage)

    try:
        assemble_backup(plan, config['bid'], crash_hook=hook)
        raise AssertionError('missed publication hook')
    finally:
        Path(config['finally']).touch()


@pytest.mark.parametrize('stage', ['bundle_exported', 'payload_staged', 'manifest_staged',
                                 'package_published', 'current_switched'])
def test_sigkill_publish_preserves_old_package_and_rerun_has_one_current(world, tmp_path, stage):
    old, old_package = _publish_green_package(world)
    old_bytes = _fingerprint(old_package)
    bid = 'd1-new-package'
    world['freeze'](bid)
    plan = world['make_plan'](bid=bid)
    with (tmp_path / 'references.pickle').open('wb') as stream:
        pickle.dump(plan.references, stream)
    config = dict(root=str(tmp_path), stage=stage, bid=bid, old_bid=world['green_bid'],
                  gate=str(tmp_path / 'gate'), **{'finally': str(tmp_path / 'finally')})
    path = tmp_path / 'config.json'
    kill_at_gate('from tests.disaster_drills.test_publish_crash import _publish_worker; '
                 'import sys; _publish_worker(sys.argv[1])', config, path)
    state = scan_assembly_state(world['retention'], identity=_IDENTITY)
    assert state.current.backup_id == (bid if stage == 'current_switched' else old.current.backup_id)
    assert verify_recovery_package(old_package).recoverable
    assert _fingerprint(old_package) == old_bytes
    if stage in ('bundle_exported', 'payload_staged', 'manifest_staged'):
        assert len(state.packages) == 1 and len(state.staging) == 1
    child = run('from tests.disaster_drills.test_publish_crash import _publish_worker; '
                'import sys; _publish_worker(sys.argv[1], restart=True)', path)
    assert child.returncode == 0, child.stdout + child.stderr
    assert json.loads(child.stdout)['final_current'] == bid
    final = scan_assembly_state(world['retention'], identity=_IDENTITY)
    assert len(final.packages) == 2  # old and new, exactly one current pointer
    assert _fingerprint(old_package) == old_bytes
