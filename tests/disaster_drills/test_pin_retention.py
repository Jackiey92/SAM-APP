"""§8-3: actual Git collection and conservative G1 planning, no R deletion/job claim."""
import json

import pytest

from app.facts_tree.git_repository import FactsGitRepository, REF_FACTS
from app.facts_tree.object_retention_plan import plan_mapping_retention
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore
from tests.test_facts_tree_backup_assembly import _fingerprint
from tests.test_facts_tree_git_repository import _init_repo, _git, _git_text, _tree_oid, _commit_tree, _IDS
from tests.disaster_drills.support import run


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_pin_survives_process_exit_reopen_and_real_gc(tmp_path, object_format):
    repo = _init_repo(tmp_path, object_format=object_format)
    h = repo.head_oid()
    code = '''
import json, sys
from app.facts_tree.git_repository import FactsGitRepository
repo = FactsGitRepository.open(sys.argv[1], **json.loads(sys.argv[2]))
assert repo.create_maintenance_pin('d1-pin', 'd1-barrier', repo.head_oid()).created
'''
    child = run(code, repo.path, json.dumps(_IDS))
    assert child.returncode == 0, child.stderr
    reopened = FactsGitRepository.open(repo.path, **_IDS)
    assert reopened.read_maintenance_pin('d1-pin', 'd1-barrier').commit_oid == h
    tree = _tree_oid(repo.path, h)
    other = _commit_tree(repo.path, tree, [], 'unrelated surviving authority')
    control = _commit_tree(repo.path, tree, [], 'unreachable unpinned control')
    assert _git_text(repo.path, 'cat-file', '-t', control).strip() == 'commit'
    _git(repo.path, 'update-ref', REF_FACTS, other, h)
    _git(repo.path, 'reflog', 'expire', '--expire=now', '--expire-unreachable=now', '--all')
    assert str(repo.path).startswith('/tmp/')
    _git(repo.path, 'gc', '--prune=now', '--quiet')
    assert _git_text(repo.path, 'cat-file', '-t', h).strip() == 'commit'
    assert _git(repo.path, 'cat-file', '-t', control, check=False).returncode != 0
    assert FactsGitRepository.open(repo.path, **_IDS).read_maintenance_pin(
        'd1-pin', 'd1-barrier').commit_oid == h


def test_missing_version_index_protects_all_bytes_without_deletion(tmp_path):
    store = VersionedMappingObjectStore(tmp_path / 'cabinet')
    digests = {store.put(b'historical mapping v1'), store.put(b'current mapping v2'),
               store.put(b'unindexed mapping')}
    before = _fingerprint(store.root)
    plan = plan_mapping_retention(store=store, ledger=None,
                                  installation_id='d1-install', company_id='acc-d1')
    assert plan.all_protected and not plan.reclaimable_candidates
    assert {item.digest for item in plan.protected} == digests
    assert plan.warnings == ('ledger_unavailable',)
    assert _fingerprint(store.root) == before
