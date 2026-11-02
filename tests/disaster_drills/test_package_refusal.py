"""§8-4/5/10: real damaged/foreign/thin/hostile packages; valid ro media accepted."""
from dataclasses import asdict
import hashlib
import json
import shutil

import pytest

from app.facts_tree.git_repository import FactsGitRepository, REF_FACTS
from app.facts_tree.recovery_verifier import verify_recovery_package
from tests import test_facts_tree_backup_assembly as assembly_tests
from tests import test_facts_tree_recovery_verifier as recovery_tests
from tests.test_facts_tree_git_repository import _git, _tree_oid, _commit_tree


def _checked(package, world, *, reason=None, expected_identity=None):
    # Snapshot after deliberate injection, immediately before read-only checking.
    roots = [package, world['repo'].path, world['retention'], world['two_a'],
             world['ledger_root'], world['app_db']]
    before = [assembly_tests._fingerprint(root) for root in roots]
    verdict = verify_recovery_package(package, expected_identity=expected_identity)
    assert not verdict.recoverable, asdict(verdict)
    if reason:
        assert reason in verdict.blockers, asdict(verdict)
    assert [assembly_tests._fingerprint(root) for root in roots] == before
    return verdict


@pytest.mark.parametrize('damage', ['old-mapping', 'object-byte', 'unsupported-version', 'identity', 'empty'])
def test_damaged_or_wrong_identity_package_refused_without_runtime_writes(package, world, tmp_path, damage):
    reason = None
    expected = None
    if damage == 'old-mapping':
        old_ref = next(ref for ref in world['green_references']()
                       if ref.logical_id == 'map-src-contract-1-v1')
        core = recovery_tests._manifest(package)['core']
        entry = next(e for e in core['payload']['files']
                     if e.get('sha256_digest') == old_ref.sha256_digest)
        (package / entry['path']).unlink()
        reason = 'r2_backing_missing'
    elif damage == 'object-byte':
        core = recovery_tests._manifest(package)['core']
        entry = next(e for e in core['payload']['files'] if e['family'] == 'r')
        path = package / entry['path']
        body = bytearray(path.read_bytes())
        body[0] ^= 1
        path.write_bytes(body)
        reason = 'r_byte_mismatch'
    elif damage == 'unsupported-version':
        manifest = recovery_tests._manifest(package)
        manifest['version'] = 999
        (package / 'manifest.json').write_text(json.dumps(manifest))
        reason = 'unsupported_envelope_version'
    elif damage == 'identity':
        expected = dict(recovery_tests._manifest(package)['core']['identity'], company_id='acc-foreign')
        reason = 'identity_mismatch'
    else:
        package = tmp_path / 'empty-recovery-directory'
        package.mkdir()
        reason = 'missing_manifest_or_report'
    _checked(package, world, reason=reason, expected_identity=expected)


def test_same_digest_really_present_in_two_cabinets_refuses_publication(world):
    # Actually externalize the archive bytes into mapping_objects as well.
    assembly_tests.test_a2_same_digest_declared_in_two_cabinet_classes_fails_closed(world)


def _replace_bundle(package, core, bundle):
    shutil.copyfile(bundle, package / 'authority.bundle')
    body = bundle.read_bytes()
    core['bundle'].update(size_bytes=len(body), sha256_digest='sha256:' + hashlib.sha256(body).hexdigest())
    recovery_tests._seal(package, core)


def test_valid_bundle_from_foreign_repo_is_rejected(package, world, tmp_path):
    core = recovery_tests._manifest(package)['core']
    identity = dict(core['identity'], repo_id='d1-foreign-repo')
    repo = FactsGitRepository.initialize(tmp_path / 'foreign.git', object_format='sha1', **identity)
    h = repo.head_oid()
    repo.create_maintenance_pin(core['backup_id'], core['barrier_id'], h)
    bundle = tmp_path / 'foreign.bundle'
    repo.export_maintenance_pin_bundle(core['backup_id'], core['barrier_id'], bundle)
    core['authority'].update(commit_oid=h, genesis_oid=h, commit_count=1)
    _replace_bundle(package, core, bundle)
    _checked(package, world, reason='bundle_identity_mismatch')


def test_real_thin_bundle_requires_missing_prerequisite(package, world, tmp_path):
    core = recovery_tests._manifest(package)['core']
    repo = world['repo']
    base = repo.head_oid()
    h = _commit_tree(repo.path, _tree_oid(repo.path, base), [base], 'd1 thin descendant')
    _git(repo.path, 'update-ref', REF_FACTS, h, base)
    pin = core['authority']['pin_ref']
    _git(repo.path, 'update-ref', pin, h)
    bundle = tmp_path / 'thin.bundle'
    _git(repo.path, 'bundle', 'create', str(bundle), pin, '^' + base)
    assert b'\n-' + base.encode() + b' ' in bundle.read_bytes(), 'real prerequisite header required'
    empty = tmp_path / 'empty.git'
    _git(tmp_path, 'init', '--bare', '--object-format=sha1', str(empty))
    precondition = _git(empty, 'bundle', 'verify', str(bundle), check=False)
    assert precondition.returncode != 0 and b'prerequisite' in precondition.stderr
    core['authority'].update(commit_oid=h, commit_count=2)
    _replace_bundle(package, core, bundle)
    _checked(package, world, reason='bundle_invalid')


@pytest.mark.parametrize('attack', ['hook', 'remote-config', 'git-config'])
def test_hostile_payload_git_inputs_refused_and_execution_sentinel_absent(package, world, tmp_path, attack):
    sentinel = tmp_path / 'hostile-execution'
    evil = package / 'payload' / '.git'
    evil.mkdir()
    # Sentinel programs are real executable files. They never reach an executor
    # because this package contains an undeclared Git directory/configuration.
    script = tmp_path / 'hostile-helper'
    script.write_text('#!/bin/sh\nprintf executed > "' + str(sentinel) + '"\n')
    script.chmod(0o700)
    if attack == 'hook':
        hooks = evil / 'hooks'
        hooks.mkdir()
        shutil.copyfile(script, hooks / 'reference-transaction')
        (hooks / 'reference-transaction').chmod(0o700)
    elif attack == 'remote-config':
        (evil / 'config').write_text('[remote "origin"]\n url = ext::' + str(script) + '\n'
                                   '[protocol "ext"]\n allow = always\n')
    else:
        (evil / 'config').write_text('[core]\n hooksPath = ' + str(script.parent) + '\n'
                                   '[include]\n path = ' + str(script) + '\n')
    _checked(package, world, reason='payload_inventory_mismatch')
    assert not sentinel.exists()


def test_good_package_on_real_ro_bind_has_erofs_and_is_recoverable(package, world):
    roots = [world['retention'], world['two_a'], world['ledger_root'], world['repo'].path]
    before = [assembly_tests._fingerprint(root) for root in roots]
    # Reuses the actual EROFS=30 write probe, mode=ro SQLite audit, hostile
    # inherited config, and source fingerprint assertions from the R4 drill.
    recovery_tests.test_package_on_real_read_only_mount(package)
    assert [assembly_tests._fingerprint(root) for root in roots] == before


def test_closure_replay_is_explicitly_deferred_not_independent_omission_detection(package):
    recovery_tests.test_r_authoritative_closure_digest_is_forwarded_not_replayed(package)
